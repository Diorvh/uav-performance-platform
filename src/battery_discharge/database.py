"""Explicit schema initialization, transactional writes, and read-only access."""
from pathlib import Path
import sqlite3
import json
import hashlib
from datetime import datetime

SCHEMA = '''
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS categories (
 id INTEGER PRIMARY KEY, form_factor TEXT NOT NULL, category_code TEXT NOT NULL,
 UNIQUE(form_factor,category_code));
CREATE TABLE IF NOT EXISTS samples (
 id INTEGER PRIMARY KEY, category_id INTEGER NOT NULL REFERENCES categories(id),
 sample_code TEXT NOT NULL, metadata_json TEXT NOT NULL,
 model_name TEXT GENERATED ALWAYS AS (json_extract(metadata_json,'$.model_name')) VIRTUAL,
 manufacturer TEXT GENERATED ALWAYS AS (json_extract(metadata_json,'$.manufacturer')) VIRTUAL,
 supplier TEXT GENERATED ALWAYS AS (json_extract(metadata_json,'$.supplier')) VIRTUAL,
 condition TEXT GENERATED ALWAYS AS (json_extract(metadata_json,'$.condition')) VIRTUAL,
 nominal_capacity_ah REAL GENERATED ALWAYS AS (json_extract(metadata_json,'$.nominal_capacity_ah')) VIRTUAL,
 UNIQUE(category_id,sample_code));
CREATE TABLE IF NOT EXISTS discharge_tests (
 id INTEGER PRIMARY KEY, sample_id INTEGER NOT NULL REFERENCES samples(id),
 test_date TEXT NOT NULL, metadata_json TEXT NOT NULL, summary_json TEXT NOT NULL,
 source_file TEXT NOT NULL, source_sheet TEXT NOT NULL, source_hash TEXT NOT NULL,
 fingerprint TEXT NOT NULL UNIQUE, repeat_key TEXT,
 set_current_a REAL GENERATED ALWAYS AS (json_extract(metadata_json,'$.set_current_a')) VIRTUAL,
 cutoff_voltage_v REAL GENERATED ALWAYS AS (json_extract(metadata_json,'$.cutoff_voltage_v')) VIRTUAL,
 initial_soc_pct REAL GENERATED ALWAYS AS (json_extract(metadata_json,'$.initial_soc_pct')) VIRTUAL,
 reference_capacity_ah REAL GENERATED ALWAYS AS (json_extract(metadata_json,'$.reference_capacity_ah')) VIRTUAL,
 soc_basis TEXT GENERATED ALWAYS AS (json_extract(metadata_json,'$.soc_basis')) VIRTUAL,
 total_capacity_ah REAL GENERATED ALWAYS AS (json_extract(summary_json,'$.total_capacity_ah')) VIRTUAL,
 total_energy_wh REAL GENERATED ALWAYS AS (json_extract(summary_json,'$.total_energy_wh')) VIRTUAL,
 duration_s REAL GENERATED ALWAYS AS (json_extract(summary_json,'$.duration_s')) VIRTUAL,
 formula_version TEXT GENERATED ALWAYS AS (json_extract(summary_json,'$.formula_version')) VIRTUAL,
 review_status TEXT NOT NULL DEFAULT 'reviewed' CHECK(review_status IN ('reviewed','excluded')),
 preferred INTEGER NOT NULL DEFAULT 0 CHECK(preferred IN (0,1)));
CREATE UNIQUE INDEX IF NOT EXISTS one_preferred_per_repeat ON discharge_tests(repeat_key)
 WHERE preferred=1 AND repeat_key IS NOT NULL;
CREATE TABLE IF NOT EXISTS discharge_points (
 id INTEGER PRIMARY KEY, test_id INTEGER NOT NULL REFERENCES discharge_tests(id),
 point_order INTEGER NOT NULL, source_row INTEGER, raw_time TEXT,
 elapsed_s REAL NOT NULL, voltage_v REAL NOT NULL, current_a REAL NOT NULL,
 temperature_c REAL, power_w REAL, capacity_ah REAL, energy_wh REAL, remaining_pct REAL, soc_pct REAL,
 UNIQUE(test_id,point_order));
CREATE TABLE IF NOT EXISTS audit_log (
 id INTEGER PRIMARY KEY, changed_at TEXT NOT NULL, action TEXT NOT NULL,
 test_id INTEGER REFERENCES discharge_tests(id), before_json TEXT, after_json TEXT, actor TEXT NOT NULL);
CREATE VIEW IF NOT EXISTS adopted_tests AS SELECT * FROM discharge_tests WHERE preferred=1 AND review_status='reviewed';
CREATE VIEW IF NOT EXISTS adopted_points AS SELECT p.* FROM discharge_points p JOIN adopted_tests t ON p.test_id=t.id;
PRAGMA user_version=1;
'''
POINT_KEYS = ('source_row','raw_time','elapsed_s','voltage_v','current_a','temperature_c',
              'power_w','capacity_ah','energy_wh','remaining_pct','soc_pct')
SAMPLE_FIELDS = ('manufacturer','supplier','condition','nominal_capacity_ah','model_name',
    'appearance','purchase_date','rated_c_rate','ac_resistance_mohm','ac_resistance_voltage_v',
    'ac_resistance_note','usage','mapping_source','mapping_sheet','mapping_row','mapping_hash',
    'summary_reference','summary_reference_start_column','summary_code',
    'model_spec','dimensions','price','nominal_voltage_v','weight_g',
    'catalog_fields','catalog_reference_tests','catalog_continuation_rows')
EXTRA_SAMPLE_COLUMNS = {'appearance':'TEXT','purchase_date':'TEXT','rated_c_rate':'REAL',
    'ac_resistance_mohm':'REAL','ac_resistance_voltage_v':'REAL','usage':'TEXT'}


class Database:
    @staticmethod
    def initialize(path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as con:
            con.executescript(SCHEMA)
        Database.migrate(path)

    @staticmethod
    def migrate(path):
        con = sqlite3.connect(Path(path).resolve().as_uri()+'?mode=rw',uri=True)
        try:
            version = con.execute('PRAGMA user_version').fetchone()[0]
            if version not in (1,2):
                raise ValueError('不支持的数据库版本')
            with con:
                columns = {r[1] for r in con.execute('PRAGMA table_xinfo(samples)')}
                for name,kind in EXTRA_SAMPLE_COLUMNS.items():
                    if name not in columns:
                        con.execute(f"ALTER TABLE samples ADD COLUMN {name} {kind} GENERATED ALWAYS AS (json_extract(metadata_json,'$.{name}')) VIRTUAL")
                con.execute('PRAGMA user_version=2')
        finally:
            con.close()

    def __init__(self, path, readonly=False):
        self.path = Path(path).resolve()
        self.readonly = readonly
        self.connection = sqlite3.connect(self.path.as_uri() + ('?mode=ro' if readonly else '?mode=rw'), uri=True)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute('PRAGMA foreign_keys=ON')
        self.version = self.connection.execute('PRAGMA user_version').fetchone()[0]
        if self.version not in (1,2):
            self.connection.close()
            raise ValueError('不支持的数据库版本')

    def save_sample(self, metadata):
        """Called inside the caller's transaction; preserves the complete catalog row."""
        m = metadata
        self.connection.execute('INSERT OR IGNORE INTO categories(form_factor,category_code) VALUES (?,?)', (m['form_factor'],m['category_code']))
        category = self.connection.execute('SELECT id FROM categories WHERE form_factor=? AND category_code=?', (m['form_factor'],m['category_code'])).fetchone()[0]
        identity = {k:m.get(k) for k in SAMPLE_FIELDS}
        old = self.connection.execute('SELECT id,metadata_json FROM samples WHERE category_id=? AND sample_code=?',(category,m['sample_code'])).fetchone()
        if old:
            stored = json.loads(old['metadata_json'])
            if any(stored.get(k) != identity.get(k) for k in stored):
                raise ValueError(f"样品 {m['category_code']}/{m['sample_code']} 汇总信息发生变化，需审查更新")
            if stored != identity:
                self.connection.execute('UPDATE samples SET metadata_json=? WHERE id=?',(json.dumps(identity,ensure_ascii=False),old['id']))
            return old['id']
        return self.connection.execute('INSERT INTO samples(category_id,sample_code,metadata_json) VALUES (?,?,?)',
            (category,m['sample_code'],json.dumps(identity,ensure_ascii=False))).lastrowid

    def import_catalog(self, metadata_rows):
        if self.readonly or self.version != 2:
            raise PermissionError('请使用已迁移的可写数据库')
        with self.connection:
            ids = [self.save_sample(m) for m in metadata_rows]
            self._audit('catalog',None,None,{'sample_ids':ids})
        return ids

    def sync_catalog(self, metadata_rows):
        """Explicit catalog refresh; preserve sample IDs and all curve records.

        Only supplied catalog fields are refreshed. Missing samples are added;
        samples absent from these workbooks (e.g. manually registered cells) stay.
        The caller backs up the database and authorizes this write separately.
        """
        if self.readonly or self.version != 2:
            raise PermissionError('请使用已迁移的可写数据库')
        rows = list(metadata_rows)
        identities = [(m.get('form_factor'), m.get('category_code'), m.get('sample_code')) for m in rows]
        if any(not all(key) for key in identities) or len(set(identities)) != len(identities):
            raise ValueError('汇总表电池身份空缺或重复，请先处理')
        result = {'inserted': [], 'updated': [], 'unchanged': []}
        with self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            for m in rows:
                old = self.connection.execute('''SELECT s.id,s.metadata_json FROM samples s
                    JOIN categories c ON c.id=s.category_id
                    WHERE c.form_factor=? AND c.category_code=? AND s.sample_code=?''',
                    (m['form_factor'], m['category_code'], m['sample_code'])).fetchone()
                if old is None:
                    sid = self.save_sample(m)
                    result['inserted'].append(sid)
                    self._audit('catalog_sample_added', None, None,
                        {'sample_id': sid, 'metadata': {k:m[k] for k in SAMPLE_FIELDS if k in m}})
                    continue
                sid = old['id']
                before = json.loads(old['metadata_json'])
                after = dict(before)
                after.update({k:m[k] for k in SAMPLE_FIELDS if k in m})
                if before == after:
                    result['unchanged'].append(sid)
                    continue
                self.connection.execute('UPDATE samples SET metadata_json=? WHERE id=?',
                    (json.dumps(after, ensure_ascii=False), sid))
                self._audit('catalog_sample_updated', None,
                    {'sample_id': sid, 'metadata': before}, {'sample_id': sid, 'metadata': after})
                result['updated'].append(sid)
        return result

    def close(self):
        self.connection.close()

    def counts(self):
        return {t: self.connection.execute(f'SELECT count(*) FROM {t}').fetchone()[0]
                for t in ('categories','samples','discharge_tests','discharge_points')}

    def tests(self, filters=None):
        result = []
        for row in self.connection.execute('SELECT * FROM discharge_tests ORDER BY test_date,id'):
            item = dict(row)
            item.update(json.loads(item.pop('metadata_json')))
            item.update(json.loads(item.pop('summary_json')))
            result.append(item)
        for key, value in (filters or {}).items():
            if value in ('', None, '全部'):
                continue
            if key == 'search':
                result = [r for r in result if str(value).casefold() in json.dumps(r, ensure_ascii=False).casefold()]
            elif key == 'test_date':
                result = [r for r in result if str(r.get(key, '')).startswith(str(value))]
            else:
                result = [r for r in result if str(r.get(key, '')) == str(value)]
        return result

    def points(self, test_id):
        return [dict(r) for r in self.connection.execute('SELECT * FROM discharge_points WHERE test_id=? ORDER BY point_order', (test_id,))]

    def samples(self):
        result=[]
        for row in self.connection.execute('SELECT s.*,c.form_factor,c.category_code FROM samples s JOIN categories c ON s.category_id=c.id ORDER BY c.form_factor,c.category_code,s.id'):
            item=dict(row)
            item.update(json.loads(item.pop('metadata_json')))
            result.append(item)
        return result

    def import_candidates(self, candidates):
        if self.readonly or self.version != 2:
            raise PermissionError('只读连接不能导入')
        candidates = list(candidates)
        for c in candidates:
            if c.get('approved') is not True or c.get('issues') or not c.get('summary', {}).get('integration_valid'):
                raise ValueError('只允许导入明确批准且已解决异常的实验')
            m = c['metadata']
            if not all(m.get(k) for k in ('form_factor','category_code','sample_code','test_date')):
                raise ValueError('样品身份或实验时间不完整')
            if not c.get('points') or not c.get('fingerprint'):
                raise ValueError('缺少采样或指纹')
        inserted = []
        with self.connection:
            for c in candidates:
                m = c['metadata']
                sample_id = self.save_sample(m)
                rk = [sample_id,m.get('set_current_a'),m.get('cutoff_voltage_v'),m.get('mode'),m.get('protocol_class')]
                repeat = hashlib.sha256(json.dumps(rk).encode()).hexdigest() if all(v is not None for v in rk) else None
                tid = self.connection.execute('''INSERT INTO discharge_tests(sample_id,test_date,metadata_json,summary_json,source_file,source_sheet,source_hash,fingerprint,repeat_key)
                    VALUES (?,?,?,?,?,?,?,?,?)''', (sample_id,m['test_date'],json.dumps(m,ensure_ascii=False),json.dumps(c['summary'],ensure_ascii=False),c['source_file'],c['source_sheet'],c['source_hash'],c['fingerprint'],repeat)).lastrowid
                self.connection.executemany(f"INSERT INTO discharge_points(test_id,point_order,{','.join(POINT_KEYS)}) VALUES ({','.join(['?']*(len(POINT_KEYS)+2))})",
                    [(tid,n,*[p.get(k) for k in POINT_KEYS]) for n,p in enumerate(c['points'],1)])
                self._audit('import',tid,None,{'approved':True,'preferred':False})
                inserted.append(tid)
        return inserted

    def _audit(self, action, tid, before, after):
        self.connection.execute('INSERT INTO audit_log(changed_at,action,test_id,before_json,after_json,actor) VALUES (?,?,?,?,?,?)',
            (datetime.now().isoformat(), action, tid, json.dumps(before), json.dumps(after),'user'))

    def set_preferred(self, test_ids, preferred=True):
        if self.readonly:
            raise PermissionError('只读连接不能修改标记')
        ids = list(dict.fromkeys(int(i) for i in test_ids))
        with self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            selected = []
            for tid in ids:
                row = self.connection.execute('SELECT * FROM discharge_tests WHERE id=?',(tid,)).fetchone()
                if row is None:
                    raise ValueError('实验不存在')
                selected.append(row)
            keys = [r['repeat_key'] for r in selected if r['repeat_key'] is not None]
            if preferred and len(keys) != len(set(keys)):
                raise ValueError('同一重复集合只能选择一个代表实验')
            for row in selected:
                if preferred and row['repeat_key']:
                    previous = self.connection.execute('SELECT id FROM discharge_tests WHERE repeat_key=? AND preferred=1 AND id<>?',(row['repeat_key'],row['id'])).fetchall()
                    for old in previous:
                        self.connection.execute('UPDATE discharge_tests SET preferred=0 WHERE id=?',(old['id'],))
                        self._audit('preferred',old['id'],True,False)
                self.connection.execute('UPDATE discharge_tests SET preferred=? WHERE id=?',(int(preferred),row['id']))
                self._audit('preferred',row['id'],bool(row['preferred']),bool(preferred))
