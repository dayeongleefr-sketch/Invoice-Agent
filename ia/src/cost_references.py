"""Explicit invoice-amount references for formulas; never derived-result chaining."""
import json
from decimal import Decimal, localcontext

from identifier_tools import stored_number, billable_amount

MAX_REFERENCES = 20
FIELDS = {'row', 'category', 'item', 'billing_unit'}


def selector(item, *, original=False):
    if original:
        category = str(item.get('category_context') or '')
        return category, str(item.get('item_context') or category), str(item.get('unit') or '')
    return str(item.get('category') or ''), str(item.get('item') or ''), str(item.get('unit') or '')


def token(item):
    category, name, unit = selector(item, original=True)
    return json.dumps(dict(row=item['row'], category=category, item=name, billing_unit=unit), ensure_ascii=False, sort_keys=True)


def decode(references):
    if type(references) is not list or len(references) > MAX_REFERENCES:
        raise ValueError('참조 비용은 최대 20개까지 선택해 주세요.')
    result = []
    for value in references:
        if type(value) is not str or len(value) > 1500:
            raise ValueError('참조 비용의 선택 정보가 올바르지 않습니다.')
        try:
            item = json.loads(value)
        except (ValueError, TypeError) as exc:
            raise ValueError('참조 비용을 목록에서 다시 선택해 주세요.') from exc
        if (type(item) is not dict or set(item) != FIELDS or type(item['row']) is not int or item['row'] < 1
                or any(type(item[k]) is not str or len(item[k]) > 300 for k in FIELDS - {'row'})):
            raise ValueError('참조 비용의 항목·단위를 확인해 주세요.')
        result.append(item)
    if len({i['row'] for i in result}) != len(result):
        raise ValueError('같은 청구 금액을 중복 참조할 수 없습니다.')
    return result


def names(references):
    return ({f'참조금액{i + 1}' for i in range(len(references))} | {'참조합계'}) if references else set()


def label(item):
    return ' · '.join(dict.fromkeys(v for v in (item['category'], item['item'], item['billing_unit']) if v))


def labels(references):
    return [f'참조금액{i + 1}: {label(item)} · 청구내역 {item["row"]}행' for i, item in enumerate(decode(references))]


def choices(root, record):
    import rate_rules as r
    targets, _ = r.selected(root, record, formula=True)
    exclude = {item['row'] for _, item in targets}
    items, _, _ = r.inventory(root)
    return {token(item): f'{label(json.loads(token(item)))} · {stored_number(item["amount"]):,}원 · {item["row"]}행'
            for item in sorted(items, key=lambda i: i['row']) if item['row'] not in exclude and billable_amount(item.get('amount'))}


def facts(values):
    result = {f'참조금액{i + 1}': value for i, value in enumerate(values)}
    with localcontext() as ctx:
        ctx.prec = 100
        result['참조합계'] = sum(values, Decimal(0)) if all(v is not None for v in values) else None
    return result


def original(root, record, references):
    """Reread the chosen source N cells, checking catalogue identity and hash."""
    import rate_rules as r
    specs = decode(references)
    if not specs:
        return {}, [], []
    available = choices(root, record)
    # Match structured metadata, not client-provided paths or unchecked row numbers.
    allowed = {tuple(sorted(json.loads(k).items())) for k in available}
    if any(tuple(sorted(s.items())) not in allowed for s in specs):
        raise ValueError('참조 비용이 변경됐거나 비교 대상 자체를 참조했습니다. 항목을 다시 선택해 주세요.')
    items, source, _ = r.inventory(root)
    path = r._safe(root, source['path'])
    if not path.is_relative_to(r._safe(root, r.BASE / 'source')) or r._hash(path).lower() != source['sha256'].lower():
        raise ValueError('참조 청구 원본이 변경되었습니다. 등록 자료를 다시 검증해 주세요.')
    by_row = {i['row']: i for i in items}
    raw = r.read_claim(path, {s['row'] for s in specs})
    values, refs, notes = [], [], []
    for index, spec in enumerate(specs, 1):
        n = spec['row']; item = by_row[n]; cells = raw.get(n, {})
        amount = stored_number(cells.get('N', {}).get('value'))
        if (not billable_amount(amount) or amount != stored_number(item.get('amount'))
                or any(cells.get(k, {}).get('value') != v for k, v in item.get('raw_labels', {}).items())
                or str(cells.get('J', {}).get('value') or '').strip() != str(item.get('unit') or '').strip()):
            raise ValueError(f'참조 비용 {n}행의 원본 금액·항목·단위가 완료 결과와 다릅니다. 다시 검증해 주세요.')
        values.append(amount)
        refs.append(source['path'] + f' / 청구내역!N{n}')
        notes.append(f'참조금액{index}: {label(spec)} = {amount:,}원 (청구내역!N{n})')
    if r._hash(path).lower() != source['sha256'].lower():
        raise ValueError('계산 중 참조 청구 원본이 변경되었습니다.')
    return facts(values), refs, notes


def monthly(references, items, target):
    """Resolve each semantic selector uniquely across this month's registered bills."""
    specs = decode(references)
    if not specs:
        return {}, [], []
    values, refs, problems = [], [], []
    for index, spec in enumerate(specs, 1):
        key = tuple(spec[k] for k in ('category', 'item', 'billing_unit'))
        matches = [i for i in items if selector(i) == key and billable_amount(i.get('amount'))]
        if len(matches) != 1:
            values.append(None)
            problems.append(f'참조금액{index} ({label(spec)}): ' + ('해당 월에 금액 있는 청구행 없음' if not matches else '같은 항목·단위의 청구행이 여러 개여서 참조 금액 미확정'))
            continue
        item = matches[0]
        if (item['claim_path'], item['row']) == (target['claim_path'], target['row']):
            values.append(None); problems.append(f'참조금액{index}: 계산 대상의 자기 참조는 허용하지 않음'); continue
        values.append(stored_number(item['amount']))
        sheet=item.get('source_sheet','청구내역');col=item.get('source_columns',{}).get('N','N')
        refs.append(item['claim_path'] + f' / {sheet}!{col}{item["row"]}')
    if len(refs) != len(set(refs)):
        problems.append('같은 청구 금액이 여러 참조에 중복 연결됨')
        values = [None] * len(specs)
    return facts(values), refs, problems
