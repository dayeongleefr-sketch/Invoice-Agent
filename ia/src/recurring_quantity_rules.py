"""Explicit EA consumable policy by operation; leaves base checks and fees intact."""
from decimal import Decimal
import re
from recurring_rules import binding as scope_binding,applicable

KIND='recurring_quantity'
FIELDS={'consumables','operation','target_scope','start_month'}
OPERATIONS={'inbound':'입고','b2b_outbound':'B2B 출고'}

def validate(p):
    if type(p) is not dict or set(p) not in (FIELDS,FIELDS|{'match_value'}) or any(type(v) is not str for v in p.values()):
        raise ValueError('소모품 처리·업무 구분·미래 대상 범위·시작월을 확인해 주세요.')
    if p['consumables'] not in ('include','exclude') or p['operation'] not in OPERATIONS or p['target_scope'] not in ('all_claims_of_operation','matching_claims'):
        raise ValueError('미래 적용 대상 전체 범위를 직접 선택해 주세요. 임의 조건은 실행하지 않습니다.')
    value=p.get('match_value','')
    if p['target_scope']=='matching_claims':
        if not value.strip() or value!=value.strip() or len(value)>200 or any(ord(c)<32 for c in value):
            raise ValueError('청구서의 품번 또는 채널명을 앞뒤 공백 없이 정확하게 입력해 주세요.')
    elif value:
        raise ValueError('전체에 적용하려면 품번·채널명 조건값을 비워 주세요.')
    if not re.fullmatch(r'20\d{2}-(0[1-9]|1[0-2])',p['start_month']):raise ValueError('시작월은 YYYY-MM 형식으로 지정하세요.')

def condition_label(p):
    return '청구 품번' if p['operation']=='inbound' else '청구 채널명(거래처)'

def overlap(a,b):
    """No priority or last-write wins. Distinct exact selectors are disjoint."""
    return a['operation']==b['operation'] and (a['target_scope']=='all_claims_of_operation' or
        b['target_scope']=='all_claims_of_operation' or a['match_value']==b['match_value'])

def matches(p,key,g):
    """True/False/None: a missing/mixed selector must never silently pass."""
    if p['target_scope']=='all_claims_of_operation':return True
    if p['operation']=='inbound':return key[2]==p['match_value'] if key[2] else None
    values=set(g.get('counterparties',[]))
    if not values or '' in values:return None
    if p['match_value'] not in values:return False
    return True if len(values)==1 else None

def binding(record,p):
    validate(p);scope_binding(record,p['start_month'])
    targets=record.get('payload',record).get('targets',[])
    check='입고 상품 수량' if p['operation']=='inbound' else 'B2B 상품 수량'
    if not targets or any(t.get('check')!=check or t.get('unit')!='EA' for t in targets):
        raise ValueError('선택한 수량 대상과 미래 업무 구분이 다릅니다. 입고와 B2B는 따로 등록하세요.')

def description(p):
    scope='청구 전체' if p['target_scope']=='all_claims_of_operation' else f"{condition_label(p)} = {p['match_value']}인 청구만"
    return f"{p['start_month']}부터 · 가상센터 {OPERATIONS[p['operation']]} {scope} · WMS (소모품) 명시 수량 {'제외' if p['consumables']=='exclude' else '포함'}"

def options(root,record):
    from rule_adapters import _targets
    v=record.get('payload',record)
    if v.get('scope')!='future':return None
    selected,_=_targets(root,record)
    operations={'b2b_outbound' if c['scope']=='outbound' else 'inbound' for _,c in selected}
    if len(operations)!=1:raise ValueError('입고와 B2B 출고의 미래 기준은 따로 등록하세요.')
    operation=operations.pop();p={'operation':operation,'consumables':'include','target_scope':'all_claims_of_operation','start_month':v['effective_from_month']}
    binding(record,p)
    return {'kind':KIND,'label':'소모품 수량 기준을 다음 달에도 적용','parameters':[
        {'name':'consumables','label':'소모품 수량을 포함할까요?','type':'select','options':{'include':'포함 — 상품과 소모품 수량을 함께 비교','exclude':'제외 — ‘(소모품)’ 표시 수량을 빼고 비교'},'value':None,'required':True},
        {'name':'operation','label':'업무 구분','type':'select','options':{operation:OPERATIONS[operation]},'value':operation,'required':True},
        {'name':'target_scope','label':'앞으로 적용할 대상','type':'select','options':{'all_claims_of_operation':f"가상센터 {OPERATIONS[operation]} 청구 전체에 적용",'matching_claims':('특정 품번의 입고에만 적용' if operation=='inbound' else '특정 채널명(거래처)의 B2B 출고에만 적용')},'value':None,'required':True},
        {'name':'match_value','label':condition_label(p)+' (전체 적용이면 비워 두세요)','type':'text','value':'','required':False},
        {'name':'start_month','label':'적용 시작 청구월','type':'select','options':{p['start_month']:p['start_month']},'value':p['start_month'],'required':True}],
        'description':'청구서에 적힌 품번 또는 채널명이 정확히 같은 대상에 적용할 수 있습니다. 현재 선택 건으로 먼저 비교한 뒤 저장합니다.',
        'limitations':['청구 수량·기본 비교·금액 검사는 유지하고 수량 비교만 별도로 추가합니다.',
            '품명에 (소모품)이 명시된 WMS 수량만 분리합니다. 품명이 없으면 제외 여부를 확정하지 않습니다.',
            '기간·회사·창고·입고 유형 제외·EA 단위 기준은 해당 월 등록을 따릅니다. B2C 포장비·금액에는 적용하지 않습니다.',
            '앞으로만 적용한 현재 계산은 예시입니다. 이미 완료한 월은 재검증해야 바뀝니다.']}

def calculate(root,record,p):
    from rule_adapters import calculate as quantity_calculate
    binding(record,p)
    if p['target_scope']=='matching_claims':
        # Read the original claim selector as well as recomputing EA below.
        # Reject out-of-scope selected targets, rather than broadening the rule.
        import rule_adapters as a
        from collections import defaultdict
        from copy import deepcopy
        from pathlib import Path
        root=Path(root).resolve();selected,doc=a._targets(root,record)
        cfg=deepcopy(a._json(root,a.CONFIG));source=cfg['sources']['claim']
        path=a.chat_intake._safe_file(root,source['path'])
        if a.digest(path).upper()!=source['sha256'].upper() or doc['sources']['claim']!=source:
            raise ValueError('등록한 청구 원본이 바뀌었습니다. 전체 검증 후 다시 설정해 주세요.')
        role='inbound' if p['operation']=='inbound' else 'outbound'
        mapping=cfg['claim_mappings'][role];sheet=mapping['claim_sheet']
        field='D' if role=='inbound' else 'C'
        cfg['claim_mappings'][role]['claim_headers'][field+'2']='품번' if role=='inbound' else '채널명'
        requested=defaultdict(set)
        for _,calc in selected:
            for ref in calc['claim_evidence']:requested[ref['sheet']].add(ref['row'])
        original=a._read_claim(path,requested,cfg)
        for _,calc in selected:
            values={a.key_text(original[a._evidence_key(ref)].get(field)) for ref in calc['claim_evidence']}
            if values!={p['match_value']}:
                raise ValueError('선택한 청구의 '+condition_label(p)+'가 조건값과 모두 같아야 합니다. 해당 대상만 선택해 주세요.')
        if a.digest(path).upper()!=source['sha256'].upper():raise ValueError('조건 확인 중 청구 원본이 바뀌었습니다.')
    result=quantity_calculate(root,record,'quantity_components',{'consumables':p['consumables']})
    result['adapter_version']='recurring-quantity-v2'
    result['limitations']=options(root,record)['limitations']
    for change in result['changes']:change['basis']=description(p)+' · '+change['basis']
    return result

def apply(root,case,claim_groups,context,state,emit):
    from rule_commands import code_fingerprint
    events=applicable(case,state,KIND)
    if not events:return []
    current_code=code_fingerprint(root);by_role={};evidence=[]
    for event in events:
        p=event['payload']['spec']['parameters'];validate(p)
        role='inbound' if p['operation']=='inbound' else 'outbound'
        by_role.setdefault(role,[]).append(event)
    for role,rules in by_role.items():
        for key,g in claim_groups[role].items():
            candidates=[(e,matches(e['payload']['spec']['parameters'],key,g)) for e in rules]
            candidates=[(e,m) for e,m in candidates if m is not False]
            if not candidates:continue
            event,condition_match=candidates[0];p=event['payload']['spec']['parameters']
            rr=context['rows'][role].get(key,[])
            bad=context['invalid'][role]
            related_bad=[r for r in bad if r['date']==key[0] and (not r['order'] or r['order']==key[1]) and
                         (role=='outbound' or not r['sku'] or r['sku']==key[2])]
            incomplete=bool(context['failed'][role] or related_bad)
            problem=''
            if len(candidates)!=1:problem='이 청구에 적용할 기준이 여러 개여서 자동 선택하지 않음'
            elif condition_match is None:problem='청구 채널명이 비어 있거나 같은 주문에 여러 채널명이 있어 조건 적용 대상을 확정할 수 없음'
            elif event['invalidated'] or event['code_fingerprint']!=current_code:problem='저장 기준의 코드·유효 상태 재확인 필요'
            elif incomplete:problem='대응 WMS 수량·단위·번호·품번 또는 파일 읽기가 불완전하여 부분 합계를 채택하지 않음'
            elif not rr:problem='동일 일자·번호'+('·품번' if role=='inbound' else '')+'의 WMS 실적이 없음'
            elif p['consumables']=='exclude' and any(not r['name'].strip() for r in rr):problem='대응 WMS 품명이 비어 소모품 제외 여부를 확인할 수 없음'
            elif g['missing']:problem='청구 상세의 상품 수량이 비어 있음'
            total=sum((r['quantity'] for r in rr),Decimal(0)) if rr else None
            consumables=sum((r['quantity'] for r in rr if '(소모품)' in r['name']),Decimal(0)) if rr else None
            expected=None if problem else total-consumables if p['consumables']=='exclude' else total
            claimed=None if g['missing'] else g['qty']
            status='data_unavailable' if expected is None else 'match' if claimed==expected else 'difference'
            reason=description(p)+' · '+(problem if problem else f'WMS 전체 {total}EA / 명시 소모품 {consumables}EA; 청구 수량 유지')
            refs=[r['ref'] for r in rr]+[r['ref'] for r in related_bad]+context['failed'][role]
            emit('저장 기준 '+OPERATIONS[p['operation']]+' 수량',status,reason,
                일자=key[0],주문번호=key[1] if role=='outbound' else '',입고예정번호=key[1] if role=='inbound' else '',
                품번=key[2] if role=='inbound' else '전체(주문 합계)',청구위치='; '.join(g['refs']),WMS위치='; '.join(refs),
                청구값=str(claimed) if claimed is not None else '',비교값=str(expected) if expected is not None else '',
                차이=str(claimed-expected) if expected is not None else '',단위='EA',
                계산근거=f"저장 기준 버전 {event['rule_version']} / {event['rule_id']}",
                recurring_rule_id=event['rule_id'],recurring_target_scope=p['target_scope'])
            evidence.append({'kind':KIND,'operation':p['operation'],'key':list(key),'rule_id':event['rule_id'],'rule_version':event['rule_version'],
                'parameters':p,'total_quantity':str(total) if total is not None else None,'consumable_quantity':str(consumables) if consumables is not None else None,
                'comparison':str(expected) if expected is not None else None,'status':status,'applied':expected is not None,'base_calculation_unchanged':True})
    return evidence
