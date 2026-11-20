"""Recompute explicitly selected upstream cost formulas; never trust saved numbers."""
from decimal import Decimal, localcontext

MAX_DEPTH = 20


def decode(references):
    if type(references) is not list or len(references) > 20 or any(
            type(v) is not str or not v or len(v) > 256 for v in references):
        raise ValueError('참조할 계산 기준은 최대 20개까지 선택해 주세요.')
    if len(references) != len(set(references)):
        raise ValueError('같은 계산 결과를 중복 참조할 수 없습니다.')
    return references


def names(references):
    return ({f'계산결과{i+1}' for i in range(len(references))} | {'계산결과합계'}) if references else set()


def _context(event, target):
    p = event['payload']; t = target.get('payload', target)
    return all(p.get(k) == t.get(k) for k in ('company_id', 'warehouse_id', 'month', 'period'))


def _parameters(event):
    spec = event['payload']['spec']
    if spec['kind'] != 'formula' or spec['parameters']['operation'] != 'cost':
        raise ValueError('청구 비용 계산식의 결과만 참조할 수 있습니다.')
    return spec['parameters']


def choices(root, record):
    from rule_memory import RuleMemory
    from rule_commands import code_fingerprint
    code = code_fingerprint(root); target = record.get('record_id')
    result = {}
    for event in RuleMemory(root).list_rules():
        if event['record_id'] == target or event['stopped'] or event['invalidated'] or event['code_fingerprint'] != code:
            continue
        if not _context(event, record):
            continue
        try:
            p = _parameters(event)
        except ValueError:
            continue
        if len(event['payload']['targets']) != 1:
            continue
        result[event['record_id']] = f"{p['category']} · {p['item']} / {p['billing_unit']} · {p['expression']}"
    return result


def facts(values):
    with localcontext() as ctx:
        ctx.prec = 100
        return {**{f'계산결과{i+1}': value for i, value in enumerate(values)},
                '계산결과합계': sum(values, Decimal(0)) if all(v is not None for v in values) else None}


def _enter(record_id, stack):
    if record_id in stack:
        raise ValueError('계산 기준이 서로를 참조하는 순환 참조입니다.')
    if len(stack) >= MAX_DEPTH:
        raise ValueError('계산 결과 참조는 20단계 이내로 지정해 주세요.')
    return (*stack, record_id)


def original(root, record, references, *, stack=(), events=None):
    from rule_commands import RuleCommands, code_fingerprint
    from rule_memory import RuleMemory
    from formula_rules import calculate
    references = decode(references)
    if not references:
        return {}, [], []
    events = RuleMemory(root).list_rules() if events is None else events
    lookup = {e['record_id']: e for e in events}
    stack = _enter(record.get('record_id', '<preview>'), stack)
    code = code_fingerprint(root); values = []; positions = []; notes = []
    target = record.get('payload', record)
    for i, rid in enumerate(references, 1):
        try:
            _enter(rid, stack)
            event = lookup.get(rid)
            if not event or event['stopped'] or event['invalidated'] or event['code_fingerprint'] != code:
                raise ValueError('참조한 계산 기준이 중지·변경되었거나 없습니다.')
            if not _context(event, record):
                raise ValueError('다른 회사·센터·기간의 계산을 참조할 수 없습니다.')
            p = _parameters(event)
            if len(event['payload']['targets']) != 1:
                raise ValueError('한 비용에 대한 계산 결과만 참조할 수 있습니다.')
            if target.get('scope') == 'future' and (
                    event['payload']['scope'] != 'future' or p['start_month'] > target['effective_from_month']):
                raise ValueError('향후 적용에는 같은 시작월 또는 이전부터 적용하는 지속 기준이 필요합니다.')
            if target.get('application') != 'future_only' and event['payload']['application'] == 'future_only':
                raise ValueError('앞으로만 적용한 계산 결과는 이번 청구에 적용할 수 없습니다.')
            upstream = event.get('_replay_record')
            if upstream is None:
                upstream, _ = RuleCommands(root, session_id='dependency-read')._record(rid)
            result = calculate(root, upstream, p, _stack=stack, _events=events)
            change = result['changes'][0]; after = change['after']
            if after['comparison'] is None or after['status'] not in ('match', 'difference') or after['unit'] != 'KRW':
                raise ValueError('앞 계산을 완료하지 못했습니다: ' + change['basis'])
            value = Decimal(after['comparison']); values.append(value)
            positions += change['evidence_refs']
            notes.append(f"계산결과{i}: {p['item']} = {value:,}원 (기준 버전 {event['rule_version']}, 원본 재계산)")
        except ValueError as exc:
            values.append(None); notes.append(f'계산결과{i}: ' + str(exc)[:700])
    return facts(values), list(dict.fromkeys(positions)), notes


def monthly(references, items, case, state, target_event, code, *, stack=()):
    """Resolve the current month's unique invoice, following active criteria versions."""
    import cost_references
    from formula_rules import validate, compare
    from identifier_tools import stored_number
    from recurring_rules import applicable
    references = decode(references)
    if not references:
        return {}, [], []
    stack = _enter(target_event['record_id'], stack)
    lookup = {e['record_id']: e for e in applicable(case, state, 'formula')}
    values = []; positions = []; problems = []
    for i, rid in enumerate(references, 1):
        try:
            _enter(rid, stack)
            event = lookup.get(rid)
            if not event or event['invalidated'] or event['code_fingerprint'] != code:
                raise ValueError('해당 월의 유효한 계산 기준이 없습니다.')
            p = _parameters(event); validate(p)
            key = (p['category'], p['item'], p['billing_unit'])
            candidates = [v for v in items if cost_references.selector(v) == key]
            competing = [e for e in lookup.values() if e['payload']['spec']['parameters']['operation'] == 'cost'
                         and tuple(e['payload']['spec']['parameters'][k] for k in ('category','item','billing_unit')) == key]
            if len(candidates) != 1 or len(competing) != 1:
                raise ValueError('참조할 비용 또는 적용 기준이 없거나 여러 개입니다.')
            item = candidates[0]
            base = {'청구수량': stored_number(item['quantity']), '청구단가': stored_number(item['rate']),
                    '비용구분': item['category'], '항목명': item['item'], '과금단위': item['unit']}
            source, refs, errors = cost_references.monthly(p.get('references', []), items, item)
            derived, linked, missing = monthly(p.get('result_references', []), items, case, state, event, code, stack=stack)
            if errors or missing:
                raise ValueError(' / '.join(errors + missing))
            base.update(source); base.update(derived)
            after, reason = compare(p, base, str(item['amount']), 'KRW')
            if after['comparison'] is None or after['status'] not in ('match','difference'):
                raise ValueError(reason or '앞 계산이 적용 조건에 해당하지 않습니다.')
            values.append(Decimal(after['comparison']))
            from document_layouts import fee_location
            positions += [item['claim_path'] + ' / ' + fee_location(item)] + refs + linked
        except ValueError as exc:
            values.append(None); problems.append(f'계산결과{i}: ' + str(exc)[:700])
    return facts(values), list(dict.fromkeys(positions)), problems
