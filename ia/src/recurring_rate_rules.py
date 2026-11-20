"""Explicit, same-item/unit fixed rates for future monthly invoice comparisons.

Natural-language conditions are never executable predicates. Only the selector
shown in the approval preview is used; original arithmetic remains separate.
"""
from decimal import Decimal
import re

from identifier_tools import stored_number, billable_amount
from recurring_rules import snapshot,publication_guard,applicable,binding as scope_binding

KIND='recurring_rate'
GROUP='저장 기준 단가 비교'
FIELDS={'rate','unit','category','item','start_month'}


def clean(value):return ' '.join(str(value or '').split())


def selector(p):return tuple(clean(p[k]) for k in ('category','item','unit'))


def validate(p):
    if type(p) is not dict or set(p)!=FIELDS:raise ValueError('지속 단가의 항목·단위·시작월·단가를 확인해 주세요.')
    if any(type(p[k]) is not str or not p[k].strip() or len(p[k])>256 for k in FIELDS):raise ValueError('지속 단가의 모든 조건을 입력해 주세요.')
    if not re.fullmatch(r'20\d{2}-(0[1-9]|1[0-2])',p['start_month']):raise ValueError('적용 시작월은 YYYY-MM 형식이어야 합니다.')
    number=stored_number(p['rate'])
    if number is None or abs(number)>Decimal('1e15'):raise ValueError('단가는 유한한 숫자로 입력해 주세요.')


def binding(record,p):
    validate(p);scope_binding(record,p['start_month'])


def options(root,record):
    from rate_rules import selected
    v=record.get('payload',record)
    if v.get('scope')!='future':return None
    rows,_=selected(root,record)
    if len(rows)!=1:raise ValueError('지속 단가는 비용 항목 한 개씩 등록해 주세요.')
    item=rows[0][1]
    category=clean(item.get('category_context'));label=clean(item.get('item_context') or category)
    p={'category':category,'item':label,'unit':clean(item['unit']),'start_month':v['effective_from_month'],'rate':'0'}
    binding(record,p)
    fields=[{'name':'rate','label':'계속 적용할 단가 (원)','type':'text','required':True}]
    for key,title in [('category','같아야 할 비용 구분'),('item','같아야 할 비용 항목'),('unit','같아야 할 과금 단위'),('start_month','적용 시작 청구월')]:
        fields.append({'name':key,'label':title,'type':'select','options':{p[key]:p[key]},'value':p[key],'required':True})
    return {'kind':KIND,'label':'같은 항목·단위에 지정 단가 계속 적용','parameters':fields,
        'description':'위 항목명·단위가 유일하게 일치하는 신규 월 청구에 적용합니다. 원래 청구값은 유지합니다.',
        'limitations':['표시한 항목·단위 조건만 적용합니다. 자유 문장 속 다른 조건이나 작업량 변경은 포함하지 않습니다.',
            '미리보기는 현재 청구 수량을 사용한 계산 예시입니다. 미래 월은 그 월의 실제 청구 수량으로 다시 계산합니다.',
            '신규 월을 검증할 때 실행합니다. 이미 완료된 월은 재검증해야 바뀝니다.']}


def calculate(root,record,p):
    from rate_rules import selected,calculate as calculate_rate
    binding(record,p);rows,_=selected(root,record)
    if len(rows)!=1:raise ValueError('지속 단가는 한 항목씩 확인해 주세요.')
    item=rows[0][1];actual={'category':item.get('category_context'),'item':item.get('item_context') or item.get('category_context'),'unit':item.get('unit')}
    if selector(p)!=selector(actual):raise ValueError('표시된 지속 조건과 선택한 원본 항목·단위가 다릅니다.')
    result=calculate_rate(root,record,{'rate':p['rate'],'unit':str(item['unit']).strip()},custom=True)
    result['adapter_version']='recurring-fixed-rate-v1'
    result['limitations']=options(root,record)['limitations']
    return result


def apply(root,case,items,state,emit):
    from rule_commands import code_fingerprint
    current_code=code_fingerprint(root) if state['events'] else None
    groups={};evidence=[]
    for event in applicable(case,state,KIND):
        payload=event['payload'];p=payload['spec']['parameters'];validate(p)
        groups.setdefault(selector(p),[]).append(event)
    for key,events in groups.items():
        matches=[i for i in items if selector(i)==key and billable_amount(i['amount'])]
        if not matches:continue
        unique=len(events)==len(matches)==1
        for item in matches:
            event=events[0];payload=event['payload'];p=payload['spec']['parameters']
            valid=unique and not event['invalidated'] and event['code_fingerprint']==current_code
            q=stored_number(item['quantity']);amount=stored_number(item['amount'])
            expected=q*stored_number(p['rate']) if valid and q is not None else None
            status='data_unavailable' if expected is None else 'match' if amount==expected else 'difference'
            reason=(f"{key[0]} / {key[1]} / {key[2]} · {p['start_month']}부터 · 단가 {p['rate']}원 × 이 월의 청구 수량")
            if not unique:reason+=' · 동일 조건의 기준 또는 청구 항목이 여러 개여서 적용하지 않음'
            elif not valid:reason+=' · 저장된 기준의 코드·유효 상태 재확인 필요'
            elif q is None:reason+=' · 이 월 청구서의 수량 값이 없어 계산하지 못함'
            r=emit(GROUP,status,reason,청구위치=f"청구내역!L{item['row']}:N{item['row']}",
                청구값=str(amount),비교값=str(expected) if expected is not None else '',차이=str(amount-expected) if expected is not None else '',단위='KRW',
                계산근거=f"지속 기준 버전 {event['rule_version']} / {event['rule_id']}")
            evidence.append({'row':item['row'],'rule_id':event['rule_id'],'rule_version':event['rule_version'],
                'parameters':p,'status':status,'applied':expected is not None,'base_arithmetic_unchanged':True})
    return evidence
