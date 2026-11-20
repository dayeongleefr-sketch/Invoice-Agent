"""Read-only, money-aware presentation of the latest completed July result.

Amounts are affected *billed* amounts, never approved totals or overcharges.
Summary/detail overlaps are removed using recorded invoice evidence. WMS EA
differences are not converted to money. No ledger or calculation is changed.
"""
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re

BASE = Path('data/demo_company/derived')
REFERENCE_GROUPS = {'입고 BOX·PCS 잠정 환산', 'B2B BOX·PCS 잠정 환산'}
REVIEW_CATEGORIES = ('불일치', '미검증')


def number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        value = Decimal(str(value).replace(',', ''))
        return value if value.is_finite() else None
    except InvalidOperation:
        return None


def refs(text):
    """Only explicit spreadsheet row references; never extract JSON indices."""
    return {(m[1], int(m[2])) for m in re.finditer(
        r'(청구내역|B2B출고|B2C출고_택배|임가공-라벨)![$A-Z]*(\d+)', text or '')}


def needs_review(row):
    status = row.get('_effective_status', row.get('결과'))
    # Provisional adoption stays visible separately; it must not cover an
    # independently unresolved detail when deduplicating pending money.
    return status in REVIEW_CATEGORIES


def review_cost_sections(sections, amount_atoms, detail_parents, amount_categories):
    """Assign the dashboard's deduplicated money to its original cost section.

    B2B components retain their individual work/freight parents. Never assign
    an entire shipment to a section by its name, or add raw checklist totals.
    """
    buckets = [dict(label=s['label'], amount=Decimal(0), components={}, atoms={},
                    breakdown={key: Decimal(0) for key in REVIEW_CATEGORIES})
               for s in sections]
    by_parent = {item['row']: bucket for section, bucket in zip(sections, buckets)
                 for item in section['items']}
    summary_labels = {}
    for section in sections:
        packing = []
        for item in section['items']:
            if ('청구내역', item['row']) not in amount_atoms:
                continue
            label = item.get('label') or item.get('category') or '청구 비용'
            summary_labels[item['row']] = label
            if 'B2C' in item.get('category', '') and label in ('기본포장', '추가포장'):
                packing.append(item)
                summary_labels[item['row']] = f'B2C {label}비'
        if {item['label'] for item in packing} == {'기본포장', '추가포장'}:
            for item in packing:
                summary_labels[item['row']] = 'B2C 기본·추가 포장비'
    for target, amount in amount_atoms.items():
        if target[0] == '청구내역':
            parent = target[1]
        elif len(target) == 3:
            parent = target[2]
        else:
            candidates = detail_parents.get(target, set())
            if len(candidates) != 1:
                raise ValueError('확인 필요 금액의 비용 항목 연결을 확인할 수 없습니다.')
            parent = next(iter(candidates))
        if parent not in by_parent:
            raise ValueError('확인 필요 금액에 연결된 청구 비용 항목이 없습니다.')
        bucket = by_parent[parent]
        label = (summary_labels[parent] if target[0] == '청구내역' else
                 {'B2B출고': 'B2B 출고', 'B2C출고_택배': 'B2C 택배',
                  '임가공-라벨': '임가공 상세'}.get(target[0], target[0]))
        bucket['amount'] += amount
        bucket['components'][label] = bucket['components'].get(label, Decimal(0)) + amount
        bucket['atoms'][target] = amount
        bucket['breakdown'][amount_categories[target]] += amount
    assert sum(b['amount'] for b in buckets) == sum(amount_atoms.values(), Decimal(0))
    assert all(sum(b['breakdown'].values()) == b['amount'] for b in buckets)
    return buckets


def completion_card_amounts(rows, sheets, b2b_parts, signed, summary_targets,
                            amount_atoms, detail_parents, pending):
    """Card-specific billed money; do not price EA differences or sum cards.

    Shipment cards use their original detail rows, deduplicated by source row
    and cost component. Cost cards include unresolved detail amounts even
    when their parent summary has no pending check. Reuse the overview's
    monetary atoms so parent/detail amounts are never counted twice.
    Missing links remain explicit instead of being interpreted as zero costs.
    """
    result = {}
    for key, group, sheet in (('b2b', 'B2B 상품 수량', 'B2B출고'),
                              ('b2c', 'B2C 주문 연결', 'B2C출고_택배')):
        bucket = {}
        for field in ('total', 'pending'):
            selected = [r for r in rows if r['검사'] == group
                        and (field == 'total' or needs_review(r))]
            targets, missing = set(), set()
            for row in selected:
                found = {n for s, n in refs(row.get('청구위치', '')) if s == sheet}
                targets.update(found)
                if not found:
                    missing.add(('check', row['_id']))
            amounts = {}
            for n in targets:
                if key == 'b2b':
                    components = b2b_parts.get(n, {})
                    if not components:
                        missing.add(('row', n))
                    amounts.update({(n, parent): amount for parent, amount in components.items()})
                else:
                    amount = number(sheets.get(sheet, {}).get(n, {}).get('D'))
                    if amount is None:
                        missing.add(('row', n))
                    else:
                        amounts[n] = amount
            bucket[field] = sum(amounts.values(), Decimal(0)) if amounts or not selected else None
            bucket[field + '_unlinked'] = len(missing)
        bucket['basis'] = ('해당 B2B 출고 상세에 연결된 작업비·운임·핸들링 청구액'
                           if key == 'b2b' else '해당 B2C 택배 상세의 청구 운임')
        result[key] = bucket
    cost_groups = {'청구·상세 금액 계산', '요약·상세 집계', '물류비 금액 검증',
                   'B2C WMS 포장 수량', 'B2C WMS 포장 금액'}
    cost_rows = [r for r in pending if r['검사'] in cost_groups or r.get('_evidence_review')]
    cost_targets, differences, provisional = set(summary_targets), set(), set()
    for row in cost_rows:
        targets = set()
        for sheet, n in refs(row.get('청구위치', '')):
            if sheet == '청구내역' and n in signed:
                targets.add(n)
            elif len(detail_parents.get((sheet, n), set())) == 1:
                targets.update(detail_parents[(sheet, n)])
        cost_targets.update(targets)
        if row.get('_effective_status', row.get('결과')) == '불일치':
            differences.update(targets)
        elif row.get('_effective_status', row.get('결과')) == '잠정 확정':
            provisional.update(targets)
    cost_atoms = {key: value for key, value in amount_atoms.items()
                  if key[0] == '청구내역' or
                  (key[0] not in ('B2B출고', 'B2C출고_택배') and
                   len(detail_parents.get(key[:2], set())) == 1)}
    result['cost'] = {'total': sum(signed.values(), Decimal(0)),
                      'pending': sum(cost_atoms.values(), Decimal(0)),
                      'count': len(cost_targets), 'difference_count': len(differences),
                      'provisional_count': len(provisional - differences),
                      'unverified_count': len(cost_targets - differences - provisional),
                      'ids': list(dict.fromkeys(r['_id'] for r in cost_rows)),
                      'total_unlinked': 0, 'pending_unlinked': 0,
                      'basis': '청구 비용의 미완료 요약·상세 금액 · 요약과 상세 중복 제외 · 차감 후 공급가액'}
    return result


@lru_cache(maxsize=4)
def _invoice(path, size, modified, expected_hash):
    path = Path(path)
    if hashlib.sha256(path.read_bytes()).hexdigest().lower() != expected_hash.lower():
        raise ValueError('청구서가 마지막 검증 이후 변경되었습니다. 재검증 후 금액 요약을 확인해 주세요.')
    from portable_workbook import open_workbook
    from xlsb_scan import scan_rows
    with open_workbook(str(path)) as book:
        sheets = {name: {n: {k: v['value'] for k, v in cells.items()}
                         for n, cells in scan_rows(book, name)}
                  for name in ('청구내역', 'B2B출고', 'B2C출고_택배')}
    labels = {str(c.get('M', '')).replace(' ', ''): (n, number(c.get('N')))
              for n, c in sheets['청구내역'].items() if number(c.get('N')) is not None}
    required = ('청구금액', '차감비용', '합계', 'VAT(부가세)', '최종금액(VAT포함)')
    if any(k not in labels for k in required):
        raise ValueError('청구서의 공급가액·차감·부가세 합계 위치를 확인할 수 없습니다.')
    totals = dict(zip(('gross', 'credit', 'net', 'vat', 'payable'),
                      [labels[k][1] for k in required]))
    if totals['gross'] - totals['credit'] != totals['net'] or totals['net'] + totals['vat'] != totals['payable']:
        raise ValueError('청구서의 차감·부가세 합계가 맞지 않아 비중 계산을 보류합니다.')
    totals['locations'] = {k: f'청구내역!N{labels[k][0]}' for k in required}
    return sheets, totals


def b2b_billed_parts(sheets, items, full, cost):
    """Attribute the invoice's own billed work and freight to its detail rows.

    Never value an EA discrepancy. Only use already reconciled detail columns
    and the invoice's recorded rates; each component retains its summary parent.
    """
    details = sheets.get('B2B출고', {})
    parts, basis = defaultdict(dict), {}
    for parent, column in ((34, 'L'), (35, 'M'), (36, 'N'), (37, 'O'), (51, 'P')):
        item = items.get(parent, {})
        rate, quantity, amount = (number(item.get(k)) for k in ('unit_price', 'quantity', 'amount'))
        matches = [c for c in full.get('checks', []) if c.get('id') == f'detail.{parent}' and c.get('source') == 'claim']
        if len(matches) != 1 or rate is None or quantity is None or amount is None:
            continue
        check = matches[0]
        evidence = check.get('evidence', [])
        values = {n: number(c.get(column)) for n, c in details.items() if n >= 4}
        if not (check.get('check_performed') and check.get('status') == 'match'
                and f'B2B출고!{column}4:마지막행' in evidence and f'청구내역!M{parent}' in evidence
                and number(check.get('expected')) == number(check.get('observed')) == quantity
                and sum((v for v in values.values() if v is not None), Decimal(0)) == quantity
                and quantity * rate == amount):
            continue
        for n, value in values.items():
            if value in (None, Decimal(0)):
                continue
            parts[n][parent] = value * rate
            basis[('B2B출고', n, parent)] = f'B2B출고!{column}{n} {value:,} × 청구내역!L{parent} {rate:,}원'

    # J is the row's actual freight, not a product unit price. Its carrier
    # subtotal T4 is explicitly included in the recorded summary amount check.
    freight_checks = [c for c in cost.get('checks', []) if c.get('id') == 'cost.expanded_amount.110']
    freight = {n: number(c.get('J')) for n, c in details.items()
               if n >= 4 and str(c.get('H') or '').startswith('용달') and number(c.get('J')) not in (None, Decimal(0))}
    freight_total = sum(freight.values(), Decimal(0))
    freight_check = freight_checks[0] if len(freight_checks) == 1 else {}
    freight_ok = (110 in items and freight_check.get('check_performed') and freight_check.get('status') == 'match'
                  and freight_check.get('currency') == 'KRW'
                  and {'B2B출고!T4:T4', '청구내역!N110'} <= set(freight_check.get('evidence', []))
                  and number(freight_check.get('expected')) == number(freight_check.get('actual')) == number(items[110]['amount'])
                  and freight_total == number(details.get(4, {}).get('T')))
    if freight_ok:
        for n, value in freight.items():
            parts[n][110] = value
            basis[('B2B출고', n, 110)] = f'B2B출고!J{n} 기재 운임 → 청구내역!N110'
        fees = [c for c in cost.get('checks', []) if c.get('id') == 'cost.arithmetic.111']
        fee = fees[0] if len(fees) == 1 else {}
        operands = fee.get('formula_operands', [])
        constants = [number(v.get('constant')) for v in operands if 'constant' in v]
        rate = constants[0] if len(constants) == 1 else None
        if (111 in items and fee.get('check_performed') and fee.get('status') == 'match'
                and fee.get('currency') == 'KRW' and len(operands) == 2
                and {'cell': 'N110'} in operands and rate is not None
                and number(items[110]['amount']) * rate == number(items[111]['amount'])
                and number(fee.get('expected')) == number(fee.get('actual')) == number(items[111]['amount'])):
            for n, value in freight.items():
                parts[n][111] = value * rate
                basis[('B2B출고', n, 111)] = f'B2B출고!J{n} 기재 운임 × 청구내역!N111 수식의 {rate}'
    return parts, basis


def load_overview(root, rows):
    root = Path(root)
    workflow = json.loads((root / BASE / 'workflow_result.json').read_text('utf-8'))
    payloads = {}
    for name in ('full_invoice_result.json', 'cost_verification.json', 'supporting_evidence_result.json'):
        raw = (root / BASE / name).read_bytes()
        expected = workflow.get('result_hashes', {}).get(name, '')
        if not expected or hashlib.sha256(raw).hexdigest().lower() != expected.lower():
            raise ValueError('완료 결과와 계산 근거가 달라 금액 요약을 불러올 수 없습니다.')
        payloads[name] = json.loads(raw)
    full, cost, supporting = payloads.values()
    # A revised invoice deliberately deactivates the old invoice's U44 policy.
    # Its empty result has no claim hash and must not block unrelated comparisons.
    # Active decisions still require an exact version match; inactive decisions
    # may never contribute adoption/evidence rows.
    if supporting.get('active') is False and not supporting.get('rows'):
        pass
    elif (supporting.get('active') is not True
          or supporting.get('claim_sha256', '').lower() != full['sources']['claim']['sha256'].lower()):
        raise ValueError('잠정 채택 기록과 청구서 버전이 달라 연결 현황을 불러올 수 없습니다.')
    source = full['sources']['claim']
    path = (root / source['path']).resolve()
    if not path.is_relative_to((root / 'data/demo_company/source').resolve()):
        raise ValueError('현재 범위의 청구서가 아닙니다.')
    stat = path.stat()
    sheets, totals = _invoice(str(path), stat.st_size, stat.st_mtime_ns, source['sha256'])
    return build_overview(rows, full, cost, sheets, totals, workflow, supporting)


def build_overview(rows, full, cost, sheets, totals, workflow, supporting=None):
    items = {i['row']: i for i in full['claim_items'] if number(i.get('amount')) not in (None, Decimal(0))}
    # The SF invoice explicitly subtracts the credit section before VAT.
    signed = {n: (-abs(number(i['amount'])) if '차감' in i.get('section', '') else number(i['amount']))
              for n, i in items.items()}
    if sum(signed.values(), Decimal(0)) != totals['net']:
        raise ValueError('비용 항목 합계와 차감 후 공급가액이 달라 금액 비중을 계산하지 않았습니다.')
    b2b_parts, amount_basis = b2b_billed_parts(sheets, items, full, cost)
    # Keep the invoice's own cost sections, including the signed deduction.
    # This is a breakdown of billed amounts, not a new validation verdict.
    billed_sections = {}
    item_checks = defaultdict(list)
    for row in rows:
        for sheet, n in refs(row.get('청구위치', '')):
            if sheet == '청구내역' and n in items:
                item_checks[n].append(row['_id'])
    for n, item in items.items():
        section = item.get('section') or '기타 비용'
        bucket = billed_sections.setdefault(section, {
            'label': re.sub(r'^\d+(?:-\d+)*\.\s*', '', section),
            'amount': Decimal(0), 'rows': [], 'items': []})
        bucket['amount'] += signed[n]
        bucket['rows'].append(n)
        bucket['items'].append({
            'row': n, 'category': item.get('category_context') or '',
            'label': item.get('item_context') or '청구 항목',
            'unit': item.get('unit'), 'quantity': item.get('quantity'),
            'unit_price': item.get('unit_price'), 'amount': signed[n],
            'check_ids': list(dict.fromkeys(item_checks[n])),
            'source_cells': dict(sheets.get('청구내역', {}).get(n, {})),
        })
    checks = cost.get('checks', [])
    parents = defaultdict(set)
    parcel_rates, parcel_extras, extra_labels = {}, {}, {}
    for check in checks:
        evidence = '; '.join(check.get('evidence', []))
        summary = {n for sheet, n in refs(evidence) if sheet == '청구내역' and n in items}
        if len(summary) != 1:
            continue
        parent = next(iter(summary))
        if check['id'].startswith('cost.detail.') and check.get('currency') == 'KRW':
            for sheet, n in refs(evidence):
                if sheet == '임가공-라벨':
                    parents[(sheet, n)].add(parent)
        if check['id'].startswith('cost.b2c_parcel_summary.'):
            rate = number(items[parent].get('unit_price'))
            if rate is not None:
                parcel_rates.setdefault(rate, set()).add(parent)
        if check['id'].startswith('cost.expanded_amount.') and check.get('currency') == 'KRW':
            for sheet, n in refs(evidence):
                if sheet == 'B2C출고_택배':
                    block = sheets[sheet].get(n, {})
                    rate = number(block.get('H'))
                    # Some invoice rate blocks say "도서산간" instead of a
                    # rate. Use the block's own total / count only to avoid
                    # counting its amount twice, not to approve a tariff.
                    count, amount = number(block.get('I')), number(block.get('J'))
                    if rate is None and count and amount is not None:
                        rate = amount / count
                    if rate is not None and rate:
                        parcel_extras.setdefault(rate, set()).add(parent)
                        if number(block.get('H')) is None and block.get('H'):
                            extra_labels[parent] = str(block['H']).strip()

    pending = [r for r in rows if needs_review(r)]
    reference = [r for r in pending if r['검사'] in REFERENCE_GROUPS]
    pending = [r for r in pending if r['검사'] not in REFERENCE_GROUPS]
    summary_targets = set()
    summary_differences = set()
    summary_provisional = set()
    occurrence_targets = set()
    for row in pending:
        targets = {n for s, n in refs(row.get('청구위치', '')) if s == '청구내역' and n in items}
        summary_targets.update(targets)
        if row.get('_evidence_review'):
            occurrence_targets.update(targets)
        if row.get('_effective_status', row.get('결과')) == '불일치':
            summary_differences.update(targets)
        elif row.get('_effective_status', row.get('결과')) == '잠정 확정':
            summary_provisional.update(targets)

    # Monetary atoms: a parent summary amount takes precedence over its detail.
    amounts = {('청구내역', n): signed[n] for n in summary_targets}
    unpriced = set()
    detail_targets = set()
    for row in pending:
        locations = refs(row.get('청구위치', ''))
        if any(s == '청구내역' and n in items for s, n in locations):
            continue
        if row['검사'] == 'B2B 상품 수량':
            targets = {n for s, n in locations if s == 'B2B출고'}
            linked = False
            for n in targets:
                for parent, part in b2b_parts.get(n, {}).items():
                    linked = True
                    if parent not in summary_targets:
                        amounts[('B2B출고', n, parent)] = part
                if b2b_parts.get(n):
                    detail_targets.add(('B2B출고', n))
            if not linked or any(not b2b_parts.get(n) for n in targets):
                unpriced.add(row['_id'])
            continue
        value = number(row.get('청구값'))
        if value in (None, Decimal(0)) or '원' not in row.get('단위', ''):
            unpriced.add(row['_id'])
            continue
        detail = {(s, n) for s, n in locations if s != '청구내역'}
        if len(detail) != 1:
            unpriced.add(row['_id']); continue
        target = next(iter(detail))
        parent_set = parents.get(target, set())
        if len(parent_set) == 1:
            if not (parent_set & summary_targets):
                amounts[target] = value
            detail_targets.add(target)
        elif target[0] == 'B2C출고_택배':
            raw_value = number(sheets[target[0]].get(target[1], {}).get('D'))
            if raw_value != value:
                unpriced.add(row['_id']); continue
            # Split only a unique rate + optional additional fee combination
            # supported by the stored summary/detail checks. This is solely
            # an amount-overlap calculation, not a shipment/rate approval.
            options = []
            for rate, pset in parcel_rates.items():
                if len(pset) != 1: continue
                base = [(next(iter(pset)), rate)]
                if rate == value: options.append(base)
                for extra, eset in parcel_extras.items():
                    extra_parent = next(iter(eset)) if len(eset) == 1 else None
                    memo = str(sheets[target[0]].get(target[1], {}).get('E') or '')
                    named_fee = extra_labels.get(extra_parent)
                    if named_fee and named_fee in memo and rate + extra == value:
                        options.append(base + [(next(iter(eset)), extra)])
            if len(options) != 1:
                unpriced.add(row['_id']); continue
            for parent, part in options[0]:
                if parent not in summary_targets:
                    amounts[(target[0], target[1], parent)] = part
            detail_targets.add(target)
        else:
            unpriced.add(row['_id'])

    review_amount = sum(amounts.values(), Decimal(0))
    ratio = review_amount / totals['net'] * 100 if totals['net'] > 0 else None
    # Partition the same monetary ledger, rather than adding overlapping totals.
    # A whole cost awaiting occurrence evidence covers its subordinate detail.
    # Original differences remain in the checklist even when money is counted here.
    different_locations = {loc for r in pending
                           if r.get('_effective_status', r.get('결과')) == '불일치'
                           for loc in refs(r.get('청구위치', ''))}
    amount_categories = {}
    for target in amounts:
        if target[:2] in different_locations:
            category = '불일치'
        else:
            category = '미검증'
        amount_categories[target] = category
    money_breakdown = []
    for category in REVIEW_CATEGORIES:
        amount = sum((value for key, value in amounts.items() if amount_categories[key] == category), Decimal(0))
        ids = [r['_id'] for r in pending
               if r.get('_effective_status', r.get('결과')) == category]
        money_breakdown.append({'label': category, 'amount': amount, 'ids': ids,
                                'ratio': amount / totals['net'] * 100 if totals['net'] > 0 else None})
    assert sum(r['amount'] for r in money_breakdown) == review_amount
    groups = defaultdict(list)
    for row in pending:
        name = '실제 발생 미검증' if row.get('_evidence_review') else row['검사']
        if name in ('청구·상세 금액 계산', '요약·상세 집계', '물류비 금액 검증'):
            name = '비용 산식·상세 합계'
        if name in ('B2C WMS 포장 수량', 'B2C WMS 포장 금액'):
            name = 'B2C 포장비'
        groups[name].append(row)
    lines = []
    for group, group_rows in groups.items():
        keys = set()
        line_amounts = {}
        for row in group_rows:
            locs = refs(row.get('청구위치', ''))
            summary_locs = {('청구내역', n) for s, n in locs if s == '청구내역' and n in items}
            if summary_locs:
                keys.update(summary_locs)
                line_amounts.update({k: signed[k[1]] for k in summary_locs})
            elif group == 'B2C 주문 연결':
                keys.update(locs or {('row', row['_id'])})
                for target in locs:
                    value = number(row.get('청구값'))
                    if value is not None: line_amounts[target] = value
            elif group == '비용 산식·상세 합계':
                for target in locs:
                    par = parents.get(target, set())
                    keys.update({('청구내역', n) for n in par} or {target})
                    if not par & summary_targets:
                        value = number(row.get('청구값'))
                        if value is not None: line_amounts[target] = value
            else:
                keys.add((row.get('일자'), row.get('주문번호'), row.get('입고예정번호'),
                          row.get('품번'), row.get('청구위치') or row.get('WMS위치') or row['_id']))
                if group == 'B2B 상품 수량':
                    for s, n in locs:
                        if s == 'B2B출고':
                            line_amounts.update({(s, n, parent): part for parent, part in b2b_parts.get(n, {}).items()})
        unit = '청구 줄' if group == 'B2C 주문 연결' else '출고 건' if group == 'B2B 상품 수량' else '입출고 건' if group == 'WMS에만 있는 실적' else '비용 항목'
        reasons = {'B2C 주문 연결': '청구된 발송의 WMS 연결 근거 확인',
                   'B2B 상품 수량': '청구·WMS 상품 수량 차이 또는 출고 근거 부족',
                   'WMS에만 있는 실적': 'WMS 실적의 청구 포함 여부 확인',
                   'B2C 포장비': '선포장 제외 후 WMS 기준 잠정 계산과 차이',
                   '비용 산식·상세 합계': '청구서 수량·단가 빈칸, 세부 근거 부족 또는 합계 차이',
                   '실제 발생 미검증': '작업·사용면적·비용이 실제 발생했는지 확인할 증빙 없음'}
        reason = reasons.get(group, group_rows[0].get('사유', ''))
        if group == 'B2C 포장비':
            differences = []
            for row in group_rows:
                if row['검사'] != 'B2C WMS 포장 금액': continue
                for s, n in refs(row.get('청구위치', '')):
                    diff = number(row.get('차이'))
                    if s == '청구내역' and n in items and diff is not None:
                        differences.append(f"{items[n]['item_context']} {diff:+,.0f}원")
            if differences: reason = ' · '.join(differences) + ' (청구−WMS 잠정액)'
        if group == 'B2B 상품 수량':
            different = sum(r.get('_effective_status', r.get('결과')) == '불일치' for r in group_rows)
            reason = f"수량 차이 {different}건 · 출고 근거/수량 확인 {len(group_rows) - different}건"
        lines.append({'group': group, 'count': len(keys), 'unit': unit,
                      'amount': sum(line_amounts.values(), Decimal(0)) if line_amounts else None,
                      'reason': reason,
                      'ids': [r['_id'] for r in group_rows]})
    overlap = sum((r['amount'] or Decimal(0) for r in lines), Decimal(0)) - review_amount
    from financial_coverage import build_coverage
    coverage = build_coverage(list(billed_sections.values()), full, cost, supporting)
    return {'totals': totals, 'billed_sections': list(billed_sections.values()), 'coverage': coverage,
            'review_sections': review_cost_sections(list(billed_sections.values()), amounts, parents, amount_categories),
            'completion_amounts': completion_card_amounts(
                rows, sheets, b2b_parts, signed, summary_targets, amounts, parents, pending),
            'claim_source': full.get('sources', {}).get('claim', {}).get('path', ''),
            'review_amount': review_amount, 'ratio': ratio,
            'money_breakdown': money_breakdown, 'amount_categories': amount_categories,
            'overlap_amount': overlap,
            'lines': lines, 'reference_count': len(reference),
            'summary_count': len(summary_targets), 'detail_count': len(detail_targets),
            'summary_difference_count': len(summary_differences),
            'summary_provisional_count': len(summary_provisional - summary_differences),
            'summary_unverified_count': len(summary_targets - summary_differences - summary_provisional),
            'occurrence_count': len(occurrence_targets),
            'other_unverified_count': len(summary_targets - summary_differences - summary_provisional - occurrence_targets),
            'unpriced_count': len(unpriced), 'amount_atoms': amounts, 'amount_basis': amount_basis,
            'scope_counts': {name: sum(r['검사'] == name for r in rows) for name in
                             ('입고 상품 수량', 'B2B 상품 수량', 'B2C 주문 연결')},
            'period': workflow.get('period', {}), 'month': workflow.get('invoice_month', ''),
            'claim_item_count': len(items)}
