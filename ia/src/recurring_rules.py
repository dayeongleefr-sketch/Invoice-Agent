"""Shared version/scope boundary for explicitly confirmed recurring comparisons."""
from contextlib import contextmanager
from business_memory import BusinessMemory,digest

KINDS=frozenset({'recurring_rate','recurring_quantity','formula'})

def binding(record,start_month):
    v=record.get('payload',record)
    if v.get('scope')!='future' or v.get('application') not in ('future_only','current_and_future'):
        raise ValueError('먼저 채팅에서 앞으로 적용할 범위와 시작월을 지정해 주세요.')
    if start_month!=v.get('effective_from_month'):raise ValueError('저장 요청의 적용 시작월과 다릅니다.')
    if start_month<v['month'] or (v['application']=='future_only' and start_month<=v['month']):
        raise ValueError('앞으로만 적용할 때는 현재 청구월 다음 달부터 지정해 주세요.')

def snapshot(root):
    from rule_memory import RuleMemory
    events=[r for r in RuleMemory(root).list_rules() if r['payload']['spec']['kind'] in KINDS]
    return {'events':events,'fingerprint':digest(events)}

def applicable(case,state,kind):
    return [e for e in state['events'] if e['payload']['spec']['kind']==kind and not e['stopped']
        and e['payload'].get('scope')=='future'
        and e['payload']['company_id']==case.get('company_id')
        and e['payload']['warehouse_id']==case.get('warehouse')=='가상센터'
        and case['month']>=e['payload']['spec']['parameters']['start_month']
        and case['month']>e['payload']['month']]

@contextmanager
def publication_guard(root,expected):
    with BusinessMemory(root)._connect(write=True):
        if snapshot(root)['fingerprint']!=expected['fingerprint']:
            raise ValueError('계산 중 지속 적용 기준이 바뀌었습니다. 마지막 완료 결과를 유지하므로 다시 검증해 주세요.')
        yield
