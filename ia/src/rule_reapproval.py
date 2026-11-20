"""Rebind a saved rule to uniquely identified targets in the same invoice scope."""
import base64
import copy
import hashlib
import json
import re

from business_memory import BindingError, VersionConflict, digest, _validate_payload

POLICY = 'ia/config/demo_company_supporting_evidence.json'
KEY_FIELDS = ('check', 'source', 'date', 'order_id', 'inbound_id', 'item_id',
              'tracking_id', 'unit', 'applied_criterion')


def target_key(target):
    # Row positions may shift, but the sheet, business key, unit and check may not.
    sheet = re.search(r'([^/;!]+)!', target.get('locator', ''))
    return digest({**{k:target.get(k, '') for k in KEY_FIELDS},
                   'sheet':sheet[1].strip() if sheet else target.get('locator', '')})


def validate(original, reapproval, fingerprint):
    if type(reapproval) is not dict or set(reapproval) != {'previous_rule_id','source_snapshot','targets'}:
        raise BindingError('현재 자료 재확인 정보가 올바르지 않습니다.')
    snapshot = reapproval['source_snapshot']
    if type(snapshot) is not dict or digest(snapshot) != fingerprint:
        raise BindingError('재확인 대상 자료의 버전이 다릅니다.')
    try:
        saved = snapshot['baseline_configs'][POLICY]
        raw = base64.b64decode(saved['content_base64'], validate=True)
        if hashlib.sha256(raw).hexdigest() != saved['sha256']:
            raise ValueError('policy hash')
        if snapshot['files'][POLICY]['sha256'] != saved['sha256']:
            raise ValueError('manifest hash')
        policy = json.loads(raw.decode('utf-8-sig'))
        month = policy['invoice_month']
        period = {k:policy['period'][k] for k in ('start','end')}
    except (ValueError, TypeError, KeyError) as exc:
        raise BindingError('현재 청구월·기간의 원본 설정을 확인할 수 없습니다.') from exc
    if month != original['month'] or period != original['period']:
        raise VersionConflict('청구월 또는 기간이 달라졌습니다. 새 범위로 요청을 등록해 주세요.')
    before = original['targets'];after = reapproval['targets']
    if not isinstance(after, list) or len(before) != len(after):
        raise BindingError('이전 대상 전체를 현재 자료와 연결해야 합니다.')
    if any(type(target) is not dict for target in after):
        raise BindingError('현재 대상의 식별 정보가 올바르지 않습니다.')
    if len({target_key(t) for t in before}) != len(before):
        raise BindingError('이전 대상에 구분할 수 없는 중복 항목이 있습니다.')
    if any(target_key(a) != target_key(b) for a,b in zip(before, after)):
        raise BindingError('원래 기준과 다른 주문·항목·단위에는 적용할 수 없습니다.')
    renewed = copy.deepcopy(original)
    renewed.update(targets=after, source_snapshot=snapshot)
    return _validate_payload(renewed)


def build(root, record, previous, sources):
    import chat_intake
    original = copy.deepcopy(record['payload'])
    if previous:
        # Use the latest approved target identities, not obsolete row numbers.
        original['targets'] = previous['payload']['targets']
    current = chat_intake._current(root)
    lookup = {}
    for index,row in enumerate(current['rows']):
        target = chat_intake._target(row,index,current['version'])
        lookup.setdefault(target_key(target),[]).append(target)
    targets=[]
    for old in original['targets']:
        matches=lookup.get(target_key(old),[])
        if len(matches)!=1:
            detail='대응 항목이 없습니다' if not matches else f'후보가 {len(matches)}개입니다'
            raise BindingError(f"{old['label']}: 현재 목록에 {detail}. 대상을 다시 선택해 새 요청을 등록해 주세요.")
        targets.append(matches[0])
    context={'previous_rule_id':previous['rule_id'] if previous else None,
             'source_snapshot':sources,'targets':targets}
    renewed=validate(record['payload'],context,digest(sources))
    return dict(record,payload=renewed),context


def changes(previous_snapshot, current_snapshot):
    before=previous_snapshot.get('files',{});after=current_snapshot.get('files',{})
    return [{'path':p,'change':'추가' if p not in before else '삭제' if p not in after else '내용 변경'}
            for p in sorted(before.keys() | after.keys()) if before.get(p)!=after.get(p)]
