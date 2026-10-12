"""Reviewed batch planning: exact-copy grouping, source checks and audit report."""
from collections import defaultdict
from datetime import datetime
from pathlib import Path
import csv
import hashlib
import json
import sqlite3
from .database import Database
from .excel_import import load_mappings, preview_workbooks, catalog_identity


def catalog_records(mapping_paths):
    rows, issues = load_mappings(mapping_paths)
    if issues:
        raise ValueError('; '.join(issues))
    records=[]
    for row in rows:
        m=dict(row)
        code=m.pop('code')
        category,sample=catalog_identity(row)
        m.update(category_code=category,sample_code=sample,summary_code=code)
        m['model_name']=' '.join(filter(None,[m.get('manufacturer'),m.get('model_spec'),
            f"{m['nominal_capacity_ah']*1000:g}mAh" if m.get('nominal_capacity_ah') else '',m.get('condition')]))
        records.append(m)
    return records


def plan_candidates(candidates):
    groups=defaultdict(list)
    for c in candidates:
        groups[c['fingerprint'] if c.get('points') else (c['source_file'],c['source_sheet'])].append(c)
    plan=[]
    for group in groups.values():
        group.sort(key=lambda c:(Path(c['source_file']).suffix.lower()!='.csv',c['source_file']))
        first=group[0]
        conflicts=[]
        for other in group[1:]:
            for key in ('set_current_a','cutoff_voltage_v','protocol_class','test_date'):
                if first['metadata'].get(key)!=other['metadata'].get(key):
                    conflicts.append(f'相同原始曲线的不同文件存在 {key} 冲突')
        for c in group:
            conflicts.extend(c.get('issues',[]))
        first['issues']=list(dict.fromkeys(conflicts))
        first['metadata']['source_copies']=[{k:c[k] for k in ('source_file','source_sheet','source_hash')} for c in group]
        plan.append(first)
    return sorted(plan,key=lambda c:(c['metadata'].get('test_date') or '',c['source_file']))


def apply_plan(database_path, plan, catalog, report_dir):
    """Import only clean candidates under the caller's explicit authorization."""
    # Detect sources changed since review, including an Excel save during scanning.
    sources={}
    for c in plan:
        for source in c['metadata'].get('source_copies',[]):
            sources[source['source_file']]=source['source_hash']
    for m in catalog:
        sources[m['mapping_source']]=m['mapping_hash']
    for path,expected in sources.items():
        if hashlib.sha256(Path(path).read_bytes()).hexdigest()!=expected:
            raise ValueError(f'文件在扫描后发生变化，请重新审查：{path}')
    for c in plan:
        if c['metadata'].get('mapping_hash') and c['metadata']['mapping_hash']!=sources.get(c['metadata']['mapping_source']):
            raise ValueError('候选实验和汇总表不是同一版本')
    output=Path(report_dir);output.mkdir(parents=True,exist_ok=True)
    stamp=datetime.now().strftime('%Y%m%d-%H%M%S')
    database_path=Path(database_path)
    if database_path.exists():
        src=sqlite3.connect(database_path.resolve().as_uri()+'?mode=ro',uri=True)
        dst=sqlite3.connect(output/f'before-import-{stamp}.sqlite3')
        try:src.backup(dst)
        finally:src.close();dst.close()
        Database.migrate(database_path)
    else:
        Database.initialize(database_path)
    db=Database(database_path)
    report=[]
    try:
        db.import_catalog(catalog)
        existing={r['fingerprint']:r['id'] for r in db.tests()}
        clean=[]
        for c in plan:
            if not c['issues'] and c['summary'].get('integration_valid') and c['fingerprint'] not in existing:
                c['approved']=True
                clean.append(c)
        ids=db.import_candidates(clean)
        new_ids=dict(zip((c['fingerprint'] for c in clean),ids))
        for c in plan:
            tid=new_ids.get(c['fingerprint'],existing.get(c['fingerprint']))
            status='已导入' if c['fingerprint'] in new_ids else '已在库中' if tid else '暂缓'
            report.append(dict(status=status,test_id=tid,source_file=c['source_file'],source_sheet=c['source_sheet'],
                source_copies=c['metadata'].get('source_copies',[]),test_date=c['metadata'].get('test_date'),
                summary_code=c['metadata'].get('summary_code'),protocol_class=c['metadata'].get('protocol_class'),
                point_count=len(c['points']),issues=c['issues'],warnings=c.get('warnings',[]),summary=c['summary']))
        result=dict(created_at=stamp,counts=db.counts(),new_test_count=len(ids),results=report)
    finally:
        db.close()
    jsonpath=output/f'discharge-import-{stamp}.json'
    jsonpath.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    csvpath=output/f'discharge-import-{stamp}.csv'
    with csvpath.open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.writer(stream)
        writer.writerow(['状态','实验ID','汇总编号','实验时间','类型','采样点数','原始文件','相同曲线文件数','问题','说明'])
        for r in report:
            writer.writerow([r['status'],r['test_id'],r['summary_code'],r['test_date'],r['protocol_class'],
                r['point_count'],r['source_file'],len(r['source_copies']),'; '.join(r['issues']),'; '.join(r['warnings'])])
    return result,jsonpath,csvpath
