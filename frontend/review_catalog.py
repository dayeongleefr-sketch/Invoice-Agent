"""Read saved checks (including normal checks); never run or publish calculations."""
import csv
import hashlib
import io
import json
import re
from functools import lru_cache
from pathlib import Path

BASE=Path('data/demo_company/derived')
NAMES=('direct_wms_result.json','full_invoice_result.json','match_investigation.json','cost_verification.json','supporting_evidence_result.json')

def _digest(raw):return hashlib.sha256(raw).hexdigest()

def signature(row):
    return _digest(json.dumps({k:v for k,v in row.items() if not k.startswith('_')},ensure_ascii=False,sort_keys=True,default=str).encode())


@lru_cache(maxsize=2)
def _claim_labels(path, stamp, expected_hash):
    raw=Path(path).read_bytes()
    if _digest(raw)!=expected_hash.lower():
        raise ValueError('청구 항목명의 근거 버전이 완료 결과와 다릅니다.')
    doc=json.loads(raw.decode('utf-8-sig'))
    return doc['sources']['claim']['path'], {int(item['row']):item['item_context']
        for item in doc.get('claim_items',[]) if item.get('item_context')}


def label_cost_rows(root, rows):
    """Join invoice names for display only, scoped to the saved invoice and row."""
    base=Path(root)/BASE
    flow_path=base/'workflow_result.json';path=base/'full_invoice_result.json'
    if not flow_path.is_file() or not path.is_file():return rows
    expected=json.loads(flow_path.read_text('utf-8-sig')).get('result_hashes',{}).get(path.name)
    if not expected:return rows
    source,labels=_claim_labels(str(path),(path.stat().st_mtime_ns,path.stat().st_size),expected)
    result=[]
    for row in rows:
        entry=dict(row)
        refs={int(n) for n in re.findall(r'청구내역![$A-Z]*(\d+)',row.get('청구위치',''))}
        if row.get('청구원본','').replace('\\','/')==source.replace('\\','/') and len(refs)==1:
            label=labels.get(next(iter(refs)))
            if label:entry['_ui_cost_label']=label
        result.append(entry)
    return result

def identity(row):
    fields=('검사','청구원본','청구위치','WMS원본','WMS위치','일자','주문번호','입고예정번호','품번','송장번호','단위','적용기준')
    return _digest(json.dumps({k:row.get(k,'') for k in fields},ensure_ascii=False,sort_keys=True).encode())

@lru_cache(maxsize=2)
def _extra(root, stamps):
    from agent_adapter import safe_file,intake_module
    intake_module()
    from verification_report_scope import inventory
    from match_investigation import review_summary
    root=Path(root);flow=json.loads((root/BASE/'workflow_result.json').read_text('utf-8-sig'))
    docs={}
    for name in NAMES:
        raw=safe_file(root,(BASE/name).as_posix()).read_bytes()
        if _digest(raw)!=flow['result_hashes'][name].lower():raise ValueError('완료 결과의 근거 버전이 다릅니다. 정상 내역을 임의로 표시하지 않습니다.')
        docs[name]=json.loads(raw.decode('utf-8-sig'))
    decision=json.loads((root/'ia/config/matching_review_decisions.json').read_text('utf-8-sig'))
    context=root/BASE/'case_execution_context.json'
    if flow.get('case_execution_context_sha256'):
        raw=context.read_bytes()
        if _digest(raw)!=flow['case_execution_context_sha256'].lower():raise ValueError('수정본의 기준 버전이 다릅니다.')
        decision=json.loads(raw.decode('utf-8-sig'))['matching_decisions']
    m=docs['match_investigation.json'];cost=docs['cost_verification.json'];cost['supporting_evidence']=docs['supporting_evidence_result.json']
    checks=inventory(docs['direct_wms_result.json'],docs['full_invoice_result.json'],m,review_summary(m,decision),cost,[])
    from cost_coverage import occurrence_pending_items
    import re
    occurrence = occurrence_pending_items(docs['full_invoice_result.json'], cost, docs['supporting_evidence_result.json'])
    cost_labels = {int(item['row']): item.get('item_context') for item in docs['full_invoice_result.json'].get('claim_items', [])}
    rows=[]
    for index,r in enumerate(checks):
        if r.get('_cost_policy'):continue  # One persisted cost row, including provisional states.
        if r['결과'] not in ('정상','잠정 확정'):continue
        r={k:'' if v is None else str(v) for k,v in r.items()}
        if r['결과']=='정상':
            r['사유']='주문 연결 확인. 상품 수량·운임 적정성은 별도 검사입니다.' if r['검사']=='B2C 주문 연결' else r['정상처리근거'] or '저장된 비교 결과가 일치합니다.'
            r['사유구분']='정상 판정 근거'
        r={'조회구분':r['검사']+' / '+r['결과'],**r}
        entry = dict(r,_id='normal:'+str(index),_saved_normal=True)
        target = re.search(r'청구내역![$A-Z]*(\d+)', r.get('청구위치', ''))
        if target and r.get('계산근거', '').startswith('supporting_evidence_result.json'):
            entry['_ui_cost_label'] = cost_labels.get(int(target[1])) or '청구 비용 항목'
        if r['검사'] == '실제 과금 작업량' and target and int(target[1]) in occurrence:
            entry.update(_evidence_review=True,
                         _ui_cost_label=occurrence[int(target[1])].get('item_context') or '청구 비용 항목')
        rows.append(entry)
    return rows

def additional_rows(root):
    root=Path(root)
    if not (root/BASE/NAMES[0]).is_file():return []
    flow=json.loads((root/BASE/'workflow_result.json').read_text('utf-8-sig'))
    if not all(n in flow.get('result_hashes',{}) for n in NAMES):return []
    paths=[root/BASE/n for n in NAMES]+[root/BASE/'workflow_result.json',root/'ia/config/matching_review_decisions.json']
    context=root/BASE/'case_execution_context.json'
    if context.exists():paths.append(context)
    stamps=tuple((str(p),p.stat().st_mtime_ns,p.stat().st_size) for p in paths)
    return [dict(r) for r in _extra(str(root),stamps)]

def original_rows(root):
    path=Path(root)/BASE/'검토필요_전체목록.csv';raw=path.read_bytes();version=_digest(raw)
    return [dict(r,_id=str(i),_result_version=version) for i,r in enumerate(csv.DictReader(io.StringIO(raw.decode('utf-8-sig'))))]+additional_rows(root)

def effective_status(row):
    handler=row.get('_review_status')
    if handler in ('reviewed','exception_accepted'):return '정상'
    if handler=='review_required' and row['결과'] in ('정상','잠정 확정'):return '미검증'
    return row['결과']

def annotate(row):
    row=dict(row);row['_effective_status']=effective_status(row)
    row['_normal_basis']='담당자 처리로 정상 전환' if row.get('_review_status') in ('reviewed','exception_accepted') else row.get('정상처리근거','')
    return row


def display_group(row):
    return '잠정 확정 · 증빙 확인 필요' if row.get('_evidence_review') else row['검사']

def filter_values(value):
    """Accept legacy scalar shortcuts and multi-select values; empty means all."""
    values = [value] if isinstance(value, str) else list(value or [])
    return list(dict.fromkeys(v for v in values if v)) or ['전체']


def filter_selection(value, previous=None):
    """Selecting all replaces specifics; selecting a specific replaces all."""
    values = filter_values(value)
    if '전체' in values and len(values) > 1:
        return [v for v in values if v != '전체'] if '전체' in filter_values(previous) else ['전체']
    return values


def filtered(rows,group='전체',status='확인 필요',query=''):
    term=(query or '').strip().casefold()
    groups, statuses = filter_values(group), filter_values(status)
    def matches_status(row):
        return any(s == '전체'
                   or s == '확인 필요' and row['_effective_status'] in ('불일치', '미검증')
                   or s in ('직접 완료', '사용자 처리완료') and row.get('_review_status') in ('reviewed', 'exception_accepted')
                   or row['_effective_status'] == s for s in statuses)
    return [r for r in rows if ('전체' in groups or display_group(r) in groups)
        and matches_status(r)
        and (not term or term in ' '.join(str(v) for v in r.values()).casefold())]

def summary(rows):
    from agent_adapter import intake_module
    intake_module()
    from verification_report_scope import DISPLAY_LABELS,COUNT_BASIS,REFERENCE_GROUPS
    groups={}
    for row in rows:
        group=display_group(row);status=row['_effective_status']
        if group in REFERENCE_GROUPS or status=='잠정 확정':continue
        item=groups.setdefault(group,dict(label=DISPLAY_LABELS.get(group,group),group=group,unit='청구 비용 1항목' if group=='실제 발생 미검증' else COUNT_BASIS.get(group,'비교 항목'),정상=0,불일치=0,미검증=0,manual_normal=0))
        item[status]+=1
        if row.get('_review_status') in ('reviewed','exception_accepted'):item['manual_normal']+=1
    return list(groups.values())

def coverage(root,rows):
    path=Path(root)/BASE/'full_invoice_result.json'
    if not path.exists():return []
    doc=json.loads(path.read_text('utf-8-sig'))
    sheets={x['sheet']:x['nonempty_rows'] for x in doc.get('sheet_inventory',[]) if x['source']=='claim'}
    result=[]
    for group,sheet,label,unit in [('입고 상품 수량','입고리스트','입고','일자·입고번호·품번별 비교'),
        ('B2B 상품 수량','B2B출고','B2B 출고','일자·주문번호별 출고'),
        ('B2C 주문 연결','B2C출고_택배','B2C 택배','택배 청구행'),('물류비 금액 검증','청구내역','비용 요약','청구 비용 항목')]:
        count=sum(r['검사']==group for r in rows)
        result.append(dict(group=group,label=label,sheet=sheet,raw_rows=sheets.get(sheet),targets=count,unit=unit))
    return result
