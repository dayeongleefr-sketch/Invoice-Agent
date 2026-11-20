"""Bounded current-invoice quantity scenarios, calculated from original cells.

Invoice selection conditions never delete source rows. A coarser join must have
one invoice comparison owner; ambiguous many-to-one joins are not allocated or
declared matched. No manual verification workbook is an input.
"""
from collections import defaultdict
from datetime import date
from decimal import Decimal
from functools import lru_cache
import csv
import json
from pathlib import Path

from identifier_tools import expand_identifier
from invoice_tools import digest, number
from wms_tools import day, key_text, validated_rows, xlsb_rows, xlsx_rows
from claim_wms_rules import july_date
from portable_workbook import open_workbook

VERSION = 'quantity-rematch-v1'
FIELDS = {'start', 'end', 'join', 'filter_field', 'filter_value', 'filter_mode', 'consumables'}
JOINS = {'baseline': '현재 연결키 유지', 'date_number': '일자 + 주문번호',
         'number': '주문번호별 기간 합계', 'date_number_sku': '일자 + 입고예정번호 + 품번',
         'number_sku': '입고예정번호 + 품번별 기간 합계', 'date_sku': '일자 + 품번 합계'}
LIMITATIONS = [
    '현재 7월 청구의 선택한 입고·B2B 상품 EA만 비교합니다. 청구액·기존 판정은 유지합니다.',
    '거래처 조건은 청구 B2B출고의 채널명, 품번 조건은 청구 입고리스트의 품번에 적용합니다.',
    '같은 새 연결키에 여러 청구 건이 겹치면 WMS 수량을 임의 배분하지 않고 판단 불가로 남깁니다.',
    '등록 WMS에서 찾은 실적의 비교입니다. 기간 내 모든 실적이 빠짐없이 제공됐다는 보증은 아닙니다.',
    '이번 청구에만 적용합니다. 다른 월 자동 적용·B2C·임의 수식·단위 변경은 지원하지 않습니다.',
]


def validate(p):
    if type(p) is not dict or set(p) != FIELDS:
        raise ValueError('기간·연결키·대상 조건·소모품 기준을 확인해 주세요.')
    if any(type(v) is not str for v in p.values()):
        raise ValueError('비교 기준 값의 형식이 잘못되었습니다.')
    try:
        start, end = date.fromisoformat(p['start']), date.fromisoformat(p['end'])
    except ValueError as exc:
        raise ValueError('기간은 YYYY-MM-DD로 입력해 주세요.') from exc
    if (start.isoformat(), end.isoformat()) != (p['start'], p['end']):
        raise ValueError('기간은 YYYY-MM-DD로 입력해 주세요.')
    if not date(2029, 6, 1) <= start <= end <= date(2029, 7, 31):
        raise ValueError('현재 등록 원본의 2029-06-01~2029-07-31 안에서 비교기간을 지정해 주세요.')
    if p['join'] not in JOINS or p['filter_field'] not in ('none', 'sku', 'counterparty') or p['filter_mode'] not in ('include', 'exclude') or p['consumables'] not in ('include', 'exclude'):
        raise ValueError('지원하는 연결키와 조건을 선택해 주세요.')
    if len(p['filter_value']) > 200 or (p['filter_field'] != 'none' and not p['filter_value'].strip()):
        raise ValueError('조건에 사용할 청구 품번 또는 채널명을 정확히 입력해 주세요.')
    if p['filter_field'] == 'none' and p['filter_value'].strip():
        raise ValueError('조건값을 적용하려면 품번 또는 거래처 조건을 선택해 주세요.')
    return p


def choice(root, record):
    import rule_adapters as a
    try:
        payload = a._record(record)
        if payload.get('scope') != 'once' or payload.get('application') != 'current_case':
            return None
        selected, doc = a._targets(root, record, allow_unlinked=True)
        directions = {direction(c['scope']) for _, c in selected}
        if len(directions) != 1: return None
        outbound = next(iter(directions)) == 'outbound'
        keys = ['baseline', 'date_number', 'number'] if outbound else ['baseline', 'date_number_sku', 'number_sku', 'date_sku']
        return {'kind': 'quantity_rematch', 'label': '기간·연결키·대상 조건으로 수량 다시 비교',
                'description': '같은 연결키의 WMS 수량을 합산합니다. 변경 전후를 보고 적용하거나 취소할 수 있습니다.',
                'parameters': [
                    {'name': 'start', 'label': '비교 시작일 (YYYY-MM-DD)', 'type': 'text', 'value': doc['period']['start'], 'required': True},
                    {'name': 'end', 'label': '비교 종료일 (YYYY-MM-DD)', 'type': 'text', 'value': doc['period']['end'], 'required': True},
                    {'name': 'join', 'label': '연결·집계 기준', 'type': 'select', 'options': {k: JOINS[k] for k in keys}, 'value': None, 'required': True},
                    {'name': 'filter_field', 'label': '선택 청구의 대상 조건', 'type': 'select', 'options': {'none': '조건 없음', **({'counterparty': '청구 채널명 (거래처)'} if outbound else {'sku': '청구 품번'})}, 'value': 'none', 'required': True},
                    {'name': 'filter_value', 'label': '청구서에 적힌 조건값 (정확히 일치)', 'type': 'text'},
                    {'name': 'filter_mode', 'label': '조건에 맞는 청구', 'type': 'select', 'options': {'include': '해당 대상만 비교', 'exclude': '해당 대상 비교 제외'}, 'value': 'include', 'required': True},
                    {'name': 'consumables', 'label': 'WMS 수량의 소모품', 'type': 'select', 'options': {'include': '포함', 'exclude': '품명에 (소모품) 명시된 수량 제외'}, 'value': None, 'required': True},
                ], 'limitations': LIMITATIONS}
    except (ValueError, OSError, KeyError, TypeError):
        return None


def direction(scope):
    return 'outbound' if scope == 'outbound' else 'inbound'


def baseline_key(r):
    if r['direction'] == 'outbound': return (r['date'], r['number'])
    if r['date'] < '2029-07-01': return (r['date'], r['sku'])
    return (r['date'], r['number'], r['sku'])


def join_key(r, mode):
    if mode == 'baseline': return baseline_key(r)
    fields = {'date_number': ('date','number'), 'number': ('number',),
              'date_number_sku': ('date','number','sku'), 'number_sku': ('number','sku'),
              'date_sku': ('date','sku')}[mode]
    values = tuple(r[k] for k in fields)
    return values if all(values) else None


def read_claim(path, cfg, operation):
    mapping = cfg['claim_mappings'][operation]; m = mapping['claim']; result = []
    with open_workbook(str(path)) as book:
        with book.get_sheet(mapping['claim_sheet']) as sheet:
            for n, c in validated_rows(xlsb_rows(sheet), mapping['claim_headers']):
                if n < mapping.get('claim_start', 4): continue
                if not any(c.get(k,{}).get('value') not in (None,'') for k in m['key']): continue
                d = day(c.get(m['date']))
                if not '2029-06-01' <= d <= '2029-07-31': continue
                try: qty = number(c.get(m['quantity']))
                except ValueError: qty = None
                result.append(dict(direction=operation, date=d, number=key_text(c.get(m['key'][0])),
                    sku=key_text(c.get(m.get('sku','D'))) if operation=='inbound' else '',
                    counterparty=key_text(c.get('C')) if operation=='outbound' else '',
                    quantity=qty, name=str(c.get(m.get('name'),{}).get('value','')),
                    role='claim', sheet=mapping['claim_sheet'], row=n))
    return result


def read_wms(paths, operation, wanted, baseline, params, decisions, cost_decisions):
    """Scan raw files; retain only candidate or baseline rows. Never deduplicate."""
    import rule_adapters as a
    roles = ('outbound_h1','outbound_july') if operation=='outbound' else ('inbound_h1','inbound_july')
    result = []
    def retain(r, cells):
        new_key = join_key(r, params['join']); old_key = baseline_key(r)
        if new_key not in wanted and old_key not in baseline: return
        issue = None
        try:
            _, qty, name = a._wms_value(r['role'], cells, decisions, cost_decisions)
        except (ValueError, KeyError, TypeError) as exc:
            qty, name, issue = None, '', str(exc)
        r.update(quantity=qty, name=name, issue=issue)
        result.append(r)
    for role in roles:
        if role not in paths: continue
        if role == 'outbound_july':
            with paths[role].open(encoding='utf-8-sig',newline='') as stream:
                reader = csv.DictReader(stream)
                required = {'회사구분','출고창고','단위','출고일','주문번호(연동)','품번','품명','수량','구분','출고번호'}
                if not required.issubset(reader.fieldnames or []): raise ValueError('출고 WMS 열 제목이 바뀌었습니다.')
                for n,c in enumerate(reader,2):
                    if None in c or None in c.values(): raise ValueError('출고 CSV 행 형식을 읽을 수 없습니다.')
                    r = dict(direction=operation, date=july_date(c['출고일'],decisions['yearless_date']),
                             number=expand_identifier(c['주문번호(연동)']), sku=expand_identifier(c['품번']),
                             role=role,sheet='CSV',row=n)
                    retain(r,c)
        else:
            sheet = a.SHEETS[role]; cols = a.XLSX_COLUMNS[role]
            for n,c in validated_rows(xlsx_rows(paths[role],sheet,columns=cols),a.XLSX_HEADERS[role]):
                if n == 1: continue
                dcol, ncol, scol = ('B','J','P') if role=='outbound_h1' else ('B',None,'F') if role=='inbound_h1' else ('D','F','Q')
                if not any(c.get(k,{}).get('value') not in (None,'') for k in (ncol,scol) if k): continue
                r = dict(direction=operation,date=day(c.get(dcol)),number=key_text(c.get(ncol)) if ncol else '',
                         sku=key_text(c.get(scol)),role=role,sheet=sheet,row=n)
                retain(r,c)
    return result


def _num(v):
    if v is None: return None
    d = Decimal(str(v))
    if not d.is_finite(): raise ValueError('유한한 수량만 비교할 수 있습니다.')
    text = format(d, 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def _values(claim, comparison, status=None):
    diff = None if comparison is None or claim is None else Decimal(claim)-Decimal(comparison)
    return dict(claim=_num(claim),comparison=_num(comparison),difference=_num(diff),
                status=status or ('not_determinable' if diff is None else 'match' if diff==0 else 'difference'),unit='EA')


def compare(selected, claims, wms, p, cfg, period):
    """Deterministic scenario core with explicit non-allocation of shared keys."""
    claim_groups=defaultdict(list)
    for r in claims: claim_groups[baseline_key(r)].append(r)
    owners=defaultdict(set)
    for r in claims:
        if p['start'] <= r['date'] <= p['end']:
            owners[join_key(r,p['join'])].add(baseline_key(r))
    changes=[]
    for target, calc in selected:
        original_key=tuple(calc['key']); cr=claim_groups.get(original_key,[])
        if not cr or any(r['quantity'] is None for r in cr): raise ValueError('원본 청구 수량을 다시 확인할 수 없습니다.')
        if {r['row'] for r in cr} != {e['row'] for e in calc['claim_evidence']}:
            raise ValueError('원본 청구 대상이 저장 결과와 다릅니다. 전체 검증을 먼저 실행해 주세요.')
        claim=sum((Decimal(r['quantity']) for r in cr),Decimal(0))
        old=[r for r in wms if baseline_key(r)==original_key and period['start']<=r['date']<=period['end']]
        if any(r.get('issue') for r in old): raise ValueError('기존 연결 WMS 수량·단위를 재확인할 수 없습니다.')
        old_qty=sum((Decimal(r['quantity']) for r in old),Decimal(0)) if old else None
        before=_values(claim,old_qty)
        for field,key in (('claim_value','claim'),('comparison_value','comparison'),('difference','difference')):
            if _num(target.get(field) if target.get(field) not in ('',None) else None) != before[key]:
                raise ValueError('원본 재집계와 저장된 기존 값이 다릅니다. 전체 검증을 먼저 실행해 주세요.')
        candidates=[]; reason=''; excluded=False; new_key=join_key(cr[0],p['join'])
        if not all(p['start']<=r['date']<=p['end'] for r in cr):
            reason='선택한 청구일이 새 비교기간 밖입니다. 이번 기준의 비교에서만 제외합니다.'; excluded=True
        elif p['filter_field']!='none':
            values={r[p['filter_field']] for r in cr}
            if len(values)!=1 or '' in values:
                reason='청구 조건값이 없거나 여러 값으로 섞여 있어 적용 대상을 확정할 수 없습니다.'
            else:
                same=next(iter(values))==p['filter_value'].strip()
                if same != (p['filter_mode']=='include'):
                    reason='지정한 청구 조건의 비교 대상이 아닙니다. 이번 기준의 비교에서만 제외합니다.'; excluded=True
        if not reason:
            if new_key is None: reason='새 연결키에 필요한 번호가 청구서에 없습니다.'
            else:
                candidates=[r for r in wms if p['start']<=r['date']<=p['end'] and join_key(r,p['join'])==new_key]
                if len(owners[new_key])!=1:
                    reason=f'새 연결키에 청구 {len(owners[new_key])}건이 겹칩니다. WMS 수량을 개별 청구에 배분할 근거가 없습니다.'
                elif not candidates: reason='새 기간·연결키에 대응하는 원본 WMS 실적을 찾지 못했습니다.'
                elif any(r.get('issue') for r in candidates): reason='연결 후보 WMS의 회사·창고·단위 또는 수량이 확인되지 않습니다.'
        compared=None
        if not reason:
            compared=sum((Decimal(r['quantity']) for r in candidates if p['consumables']=='include' or '(소모품)' not in r['name']),Decimal(0))
        after=_values(claim,compared,'excluded' if excluded else 'not_determinable' if reason else None)
        refs=[]
        for r in cr+old+candidates:
            ref=cfg['sources'][r['role']]['path']+' / '+r['sheet']+'!'+str(r['row'])
            if ref not in refs: refs.append(ref)
        condition='조건 없음' if p['filter_field']=='none' else f"청구 {'채널명' if p['filter_field']=='counterparty' else '품번'} = {p['filter_value']} ({'포함' if p['filter_mode']=='include' else '제외'})"
        basis=f"{p['start']}~{p['end']} · {JOINS[p['join']]} · {condition} · 소모품 {'포함' if p['consumables']=='include' else '제외'}. WMS 원본 {len(candidates)}행. "
        basis+=reason or '같은 연결키의 수량을 합산하여 선택한 청구 수량과 비교했습니다.'
        changes.append(dict(target_id=target['target_id'],label=target['label'],check=target['check'],before=before,after=after,basis=basis,evidence_refs=refs))
    return dict(adapter_version=VERSION,changes=changes,limitations=LIMITATIONS,calculation_executed=True)


@lru_cache(maxsize=3)
def _load(root_text, config_json, operation, wanted_tuple, baseline_tuple, params_json, decisions_json, cost_json, source_hashes):
    # Caller rehashes originals before every call and again before returning.
    cfg=json.loads(config_json); root=Path(root_text); p=json.loads(params_json)
    paths={role:root/cfg['sources'][role]['path'] for role,_ in source_hashes}
    claims=read_claim(paths['claim'],cfg,operation)
    wms=read_wms(paths,operation,set(wanted_tuple),set(baseline_tuple),p,json.loads(decisions_json),json.loads(cost_json))
    return claims,wms


def calculate(root,record,params):
    import rule_adapters as a
    p=validate(params); payload=a._record(record)
    if payload.get('scope')!='once' or payload.get('application')!='current_case':
        raise ValueError('기간·연결키·대상 조건 변경은 이번 청구만 지원합니다. 적용 범위를 이번 청구로 확인해 주세요.')
    selected,doc=a._targets(root,record,allow_unlinked=True)
    operations={direction(c['scope']) for _,c in selected}
    if len(operations)!=1: raise ValueError('입고와 B2B 출고는 각각 선택해 주세요.')
    operation=next(iter(operations))
    allowed={'baseline','date_number','number'} if operation=='outbound' else {'baseline','date_number_sku','number_sku','date_sku'}
    if p['join'] not in allowed or p['filter_field'] not in ('none','counterparty' if operation=='outbound' else 'sku'):
        raise ValueError('이 청구 시트에 없는 필드로 비교할 수 없습니다. B2B는 채널명·주문번호, 입고는 품번·입고예정번호를 사용합니다.')
    cfg=a._json(root,a.CONFIG); decisions=a._json(root,Path(cfg['decisions_path'])); cost=a._json(root,Path('ia/config/demo_company_cost_decisions.json'))
    period=decisions['comparison_period']
    if period.get('state')!='confirmed_by_user' or any(period.get(k)!=doc['period'].get(k) for k in ('start','end')):
        raise ValueError('기존 비교기간의 결정과 결과가 다릅니다.')
    roles=['claim']+(['outbound_h1','outbound_july'] if operation=='outbound' else ['inbound_h1','inbound_july'])
    source_hashes=[]; paths={}
    for role in roles:
        if role not in cfg['sources']: continue
        source=cfg['sources'][role]
        if source!=doc['sources'].get(role): raise ValueError('등록 원본과 저장 결과의 자료 버전이 다릅니다.')
        path=a.chat_intake._safe_file(root,Path(source['path']))
        if not path.resolve().is_relative_to((root/a.BASE/'source').resolve()): raise ValueError('원본 자료 경계를 벗어났습니다.')
        if digest(path).upper()!=source['sha256'].upper(): raise ValueError('등록된 원본이 바뀌었습니다. 전체 검증을 먼저 실행해 주세요.')
        paths[role]=path; source_hashes.append((role,source['sha256']))
    if p['join'] in ('number','number_sku'):
        needed={operation+('_h1' if p['start']<'2029-07-01' else '_july')}
        if p['end']>='2029-07-01': needed.add(operation+'_july')
    else:
        needed={operation+('_h1' if c['key'][0]<'2029-07-01' else '_july') for _,c in selected}
    if not needed.issubset(paths): raise ValueError('새 비교기간에 필요한 WMS 파일이 등록되지 않았습니다.')
    wanted=set(); baseline=set()
    for _,c in selected:
        k=c['key']; baseline.add(tuple(k))
        r=dict(direction=operation,date=k[0],number=k[1] if c['scope']!='inbound_june' else '',sku=k[-1] if operation=='inbound' else '')
        new_key=join_key(r,p['join'])
        if new_key is not None: wanted.add(new_key)
    dump=lambda v:json.dumps(v,ensure_ascii=False,sort_keys=True)
    claims,wms=_load(str(root),dump(cfg),operation,tuple(sorted(wanted)),tuple(sorted(baseline)),dump({'join':p['join']}),dump(decisions),dump(cost),tuple(source_hashes))
    result=compare(selected,claims,wms,p,cfg,period)
    for role,path in paths.items():
        if digest(path).upper()!=cfg['sources'][role]['sha256'].upper(): raise ValueError('재계산 중 원본이 변경되었습니다.')
    a._targets(root,record,allow_unlinked=True)
    return result


def baseline_parameters(root):
    import rule_adapters as a
    cfg=a._json(root,a.CONFIG); period=a._json(root,Path(cfg['decisions_path']))['comparison_period']
    return dict(start=period['start'],end=period['end'],join='baseline',filter_field='none',filter_value='',filter_mode='include',consumables='include')
