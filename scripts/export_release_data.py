"""导出新的公开数据库和 Releases 附件；源库只读，私人模型和原始文件不打包。"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PureWindowsPath
import re
import sqlite3
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from platform_config import project_path, settings

# 原始单元格镜像和购买/个人用途不进入公开元数据；实验条件与质量说明保留。
PRIVATE_KEYS = {'supplier', 'purchase_date', 'price', 'usage', 'catalog_fields',
                'catalog_continuation_rows', 'summary_reference', 'summary_reference_start_column'}
PUBLIC_SAMPLE_KEYS = {
    'manufacturer', 'condition', 'nominal_capacity_ah', 'model_name', 'appearance',
    'rated_c_rate', 'ac_resistance_mohm', 'ac_resistance_voltage_v', 'ac_resistance_note',
    'mapping_source', 'mapping_sheet', 'mapping_row', 'mapping_hash', 'summary_code',
    'model_spec', 'dimensions', 'nominal_voltage_v', 'weight_g', 'catalog_reference_tests',
}
PUBLIC_TEST_KEYS = PUBLIC_SAMPLE_KEYS | {
    'form_factor', 'category_code', 'sample_code', 'test_date', 'filename_time', 'recorded_start',
    'set_current_a', 'cutoff_voltage_v', 'mode', 'initial_soc_pct', 'reference_capacity_ah',
    'soc_basis', 'protocol_class', 'condition_source', 'test_c_rate', 'max_gap_s',
    'measured_current_a', 'last_voltage_v', 'import_warnings', 'form_factor_override',
    'source_copies', 'cutoff_correction', 'cutoff_inference', 'cutoff_voltage_source',
    'duplicate_rows_removed', 'isolated_voltage_rows_removed', 'manual_rows_removed',
    'voltage_window_basis', 'voltage_window_max_v', 'voltage_window_min_v',
    'voltage_window_removed_source_rows', 'voltage_window_rows_removed_count',
}
TABLES = {'force': ('test_groups', 'measurements'),
          'battery': ('categories', 'samples', 'discharge_tests', 'discharge_points', 'audit_log')}


def relative_source(value, kind):
    path = PureWindowsPath(value)
    if not path.is_absolute():
        if '..' in path.parts:
            return path.name
        return path.as_posix()
    mappings = settings().get('source_roots', []) + [
        {'original': str(project_path(kind + '_raw')), 'current': settings()['paths'][kind + '_raw']}]
    for mapping in mappings:
        if (ROOT / mapping['current']).resolve() != project_path(kind + '_raw'):
            continue
        try:
            return path.relative_to(PureWindowsPath(mapping['original'])).as_posix()
        except ValueError:
            pass
    # 未知绝对来源只保留文件名；从不发布设备盘符、用户名或共享服务器名。
    return path.name


def public_value(value, kind, key=''):
    if isinstance(value, dict):
        return {k: public_value(v, kind, k) for k, v in value.items() if k not in PRIVATE_KEYS}
    if isinstance(value, list):
        return [public_value(v, kind, key) for v in value]
    if isinstance(value, str):
        if key in {'source_file', 'mapping_source', 'path', 'database_path'}:
            return relative_source(value, kind)
        # 历史说明中的设备路径也不公开，数值、截止依据和异常标记不重算。
        return re.sub(r'(?:[A-Za-z]:[\\/]|\\\\)[^\r\n\"<>；;]*', '[本地路径已移除]', value)
    return value


def export_database(kind, destination):
    source = project_path(kind + '_database')
    if not source.is_file():
        raise FileNotFoundError(source)
    if destination.exists():
        raise FileExistsError(f'公开文件已存在，不覆盖：{destination}')
    counts = {}
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as src, \
            closing(sqlite3.connect(destination)) as dst:
        src.execute('PRAGMA query_only=ON')
        src.execute('BEGIN')
        schema = src.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE sql IS NOT NULL").fetchall()
        actual = {name for typ, name, _, _ in schema if typ == 'table'}
        if actual != set(TABLES[kind]):
            raise ValueError(f'{kind} 数据表变化，请先审查新的公开范围。')
        with dst:
            for table in TABLES[kind]:
                sql = next(sql for typ, name, _, sql in schema if typ == 'table' and name == table)
                dst.execute(sql)
                # table_info 排除生成列，避免把派生字段作为原始字段写入。
                columns = [r[1] for r in src.execute(f'PRAGMA table_info("{table}")')]
                names = ','.join('"' + c + '"' for c in columns)
                insert = f'INSERT INTO "{table}" ({names}) VALUES ({",".join("?" for _ in columns)})'
                count = 0
                if table != 'audit_log':
                    cursor = src.execute(f'SELECT {names} FROM "{table}" ORDER BY id')
                    while batch := cursor.fetchmany(2000):
                        converted = []
                        for row in batch:
                            record = dict(zip(columns, row))
                            if 'metadata_json' in record:
                                allowed = PUBLIC_SAMPLE_KEYS if table == 'samples' else PUBLIC_TEST_KEYS
                                metadata = json.loads(record['metadata_json'])
                                record['metadata_json'] = json.dumps(public_value(
                                    {k: v for k, v in metadata.items() if k in allowed}, kind), ensure_ascii=False)
                            for key in ('source_file', 'notes'):
                                if key in record:
                                    record[key] = public_value(record[key], kind, key)
                            converted.append(tuple(record[c] for c in columns))
                        dst.executemany(insert, converted)
                        count += len(converted)
                counts[table] = count
            for typ, _, table, sql in schema:
                if typ == 'index' and table in TABLES[kind]:
                    dst.execute(sql)
            version = src.execute('PRAGMA user_version').fetchone()[0]
            dst.execute(f'PRAGMA user_version={version}')
        # 新库从空文件构建，不携带删除记录、旧审计内容或未使用页面。
    return {'file': 'data/' + destination.name, 'schema_version': version, 'rows': counts,
            'sha256': hashlib.sha256(destination.read_bytes()).hexdigest(), 'bytes': destination.stat().st_size}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', args.version):
        parser.error('版本只能含字母、数字、点、下划线、连字符。')
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    data = output / 'data'
    data.mkdir()
    files = [export_database(kind, data / (kind_name + '.sqlite3')) for kind, kind_name in
             (('force', 'force_efficiency'), ('battery', 'battery_discharge'))]
    manifest = {
        'version': args.version, 'created_at_utc': datetime.now(timezone.utc).isoformat(),
        'license': 'CC-BY-4.0', 'attribution': '无人机性能平台数据贡献者',
        'database_ids': settings()['database_ids'], 'databases': files,
        'publication_policy': {'private_metadata_removed': sorted(PRIVATE_KEYS),
                               'metadata_allowlist': {'samples': sorted(PUBLIC_SAMPLE_KEYS),
                                                     'discharge_tests': sorted(PUBLIC_TEST_KEYS)},
                               'audit_history_included': False, 'raw_files_included': False,
                               'personal_pack_library_included': False,
                               'source_paths': 'relative to raw/force or raw/battery; unknown roots reduced to filename',
                               'measurements': 'original numerical values and IDs; no refitting or recalculation'},
    }
    manifest_path = output / 'database-manifest.json'
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    archive = output / f'uav-databases-{args.version}.zip'
    with zipfile.ZipFile(archive, 'x', zipfile.ZIP_DEFLATED) as bundle:
        for entry in files:
            bundle.write(output / entry['file'], entry['file'])
        bundle.write(manifest_path, manifest_path.name)
        for name in ('DATA_LICENSE', 'DATA_LICENSE_NOTICE'):
            bundle.write(ROOT / name, name)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (output / 'SHA256SUMS.txt').write_text(f'{digest}  {archive.name}\n', encoding='utf-8')
    print(json.dumps({'archive': str(archive), 'databases': files}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
