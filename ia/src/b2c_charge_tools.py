"""B2C invoice inversions and explicitly authorized WMS packaging scenarios.

Freight, packing events and product EA are separate dimensions. Inverting a
price proves invoice arithmetic only; independent WMS quantities are separate.
"""
from collections import Counter
from decimal import Decimal, InvalidOperation
import csv

from identifier_tools import expand_identifier, scientific_info

INVERSE_ROWS = (28, 29, 30, 31, *range(98, 105))


def decimal(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value).replace(',', ''))
        return result if result.is_finite() else None
    except InvalidOperation:
        return None


def inverse_quantity(amount, rate):
    amount, rate = decimal(amount), decimal(rate)
    return amount / rate if amount is not None and rate is not None and rate > 0 else None


def observe_group(groups, source, row, date, shipment, order, unit, quantity, name='', carrier='', tracking='', parcel_basis=False):
    """Retain source facts, including missing units/IDs; no charge decision here."""
    ship = expand_identifier(shipment)
    order_id = expand_identifier(order)
    precision = scientific_info(order)
    complete_order = bool(order_id) and not (precision and precision['last_stored_digit_scale'] > 0)
    basis = 'shipment' if ship else 'order' if complete_order else 'unresolved'
    identity = ship or (order_id if complete_order else 'row:' + str(row))
    key = (source, date, basis, identity, unit)
    carrier_id, tracking_id = expand_identifier(carrier), expand_identifier(tracking)
    tracking_precision = scientific_info(tracking)
    complete_tracking = bool(tracking_id) and not (tracking_precision and tracking_precision['last_stored_digit_scale'] > 0)
    if parcel_basis and complete_order and complete_tracking and carrier_id:
        basis = 'order_carrier_tracking'
        key = (source, basis, order_id, carrier_id, tracking_id, unit)
    group = groups.setdefault(key, dict(source=source, date=date, shipment=ship,
        basis=basis, unit=unit, orders=set(), quantity=Decimal(0), missing_quantity_rows=[], rows=[],
        prepacked_quantity=Decimal(0), prepacked_rows=[], carrier=carrier_id, tracking=tracking_id,
        dates=set(), shipments=set(), prepacked_observed=True))
    group['dates'].add(date)
    group['shipments'].add(ship)
    group['orders'].add(order_id)
    group['rows'].append(row)
    qty = decimal(quantity)
    if qty is None:
        group['missing_quantity_rows'].append(row)
    else:
        group['quantity'] += qty
        if '(선포장)' in str(name):
            group['prepacked_quantity'] += qty
            group['prepacked_rows'].append(row)


def observed_groups(groups):
    return [{k: sorted(v) if isinstance(v, set) else v for k,v in g.items()} for g in groups.values()]


def packing_scenario(groups, policy):
    """The scope is prework-inclusive, never silently substituted for billing."""
    ready = all(policy.get(k, {}).get('state') == 'confirmed_by_user' for k in ('grouping', 'prework'))
    rows = []
    manual = policy.get('manual_reconciliation', {})
    exclude = manual.get('state') == 'confirmed_by_user' and manual.get('prepacked') == 'exclude_explicit_quantity'
    for g in groups:
        reason = ''
        if not ready:
            reason = '포장 집계·선작업 적용 기준 결정 대기'
        elif g['basis'] == 'order' and (g['source'] != 'outbound_h1' or policy.get('june_fallback', {}).get('state') != 'confirmed_by_user'):
            reason = '출고번호 공란의 보조 집계 기준 미확정'
        elif g['basis'] == 'unresolved':
            reason = '출고번호 없고 주문번호도 공란 또는 자릿수 손실'
        elif g['unit'] != 'EA' and not (g['source'] == 'outbound_h1' and not g['unit'] and
                                       policy.get('june_unit', {}).get('state') == 'confirmed_by_user'):
            reason = '상품 수량 단위 미확정'
        qty = decimal(g['quantity'])
        if g['missing_quantity_rows'] or qty is None or qty <= 0 or qty != qty.to_integral_value():
            reason = '누락·0 이하·소수 상품 수량의 포장 기준 미확정'
        excluded = decimal(g.get('prepacked_quantity', 0)) if exclude else Decimal(0)
        if exclude and not g.get('prepacked_observed'):
            reason = '원본 WMS 선포장 품명 재읽기 필요'
        if excluded is None or qty is not None and not 0 <= excluded <= qty:
            reason = '선포장 수량의 원본 합계 확인 필요'
        billable = qty - excluded if qty is not None and excluded is not None else None
        rows.append({**g, 'quantity': qty, 'excluded_prepacked_quantity': excluded, 'packing_quantity': billable,
                     'basic': None if reason else Decimal(int(billable > 0)),
                     'extra': None if reason else max(billable - 3, Decimal(0)),
                     'status': 'unavailable' if reason else 'calculated_scenario', 'reason': reason})
    usable = [r for r in rows if r['status'] == 'calculated_scenario']
    return {'rows': rows, 'basic': sum((r['basic'] for r in usable), Decimal(0)),
            'extra': sum((r['extra'] for r in usable), Decimal(0)),
            'EA': sum((r['quantity'] for r in usable), Decimal(0)),
            'eligible_groups': len(usable), 'unavailable_groups': len(rows)-len(usable),
            'excluded_prepacked_EA': sum((r['excluded_prepacked_quantity'] for r in usable), Decimal(0)),
            'prepacked_only_groups': sum(r['packing_quantity'] == 0 for r in usable),
            'mixed_prepacked_groups': sum(0 < r['excluded_prepacked_quantity'] < r['quantity'] for r in usable),
            'grouping_counts': dict(Counter(r['basis'] for r in usable)),
            'scenario_only': True, 'prework_excluded': exclude}


def check(identifier, label, actual, expected, evidence, unit, group, reason='', **extra):
    actual, expected = decimal(actual), decimal(expected)
    performed = actual is not None and expected is not None
    return dict(id=identifier, label=label, actual=actual, expected=expected,
        difference=actual-expected if performed else None,
        status=('match' if actual == expected else 'difference') if performed else 'data_unavailable',
        check_performed=performed, evidence=evidence, unit=unit, report_group=group,
        basis=reason, business_target=[label, *evidence], overcharge_confirmed=False, **extra)


def build(summary, parcel_sheet, b2b_sheet, wms, policy):
    from identifier_tools import billable_amount
    def v(cells, col):
        return cells.get(col, {}).get('value')
    checks = []
    inversions = []
    for n in INVERSE_ROWS:
        cells = summary[n]
        if not billable_amount(v(cells, 'N')):continue
        amount, rate, quantity = (decimal(v(cells, col)) for col in ('N', 'L', 'M'))
        unit = '기본포장 건' if n == 28 else '추가포장 EA' if n == 31 else '샤쉐 작업 건' if n in (29, 30) else '택배 과금 건'
        inverse = inverse_quantity(amount, rate)
        label = 'B2C 기본포장' if n == 28 else 'B2C 추가포장' if n == 31 else f'청구 {n}행 역산'
        record = dict(claim_row=n, amount=amount, rate=rate, stated_quantity=quantity,
                      inverse_quantity=inverse, unit=unit, independent_wms_check=False)
        inversions.append(record)
        checks.append(check(f'cost.b2c_inverse.{n}', label, quantity, inverse,
            [f'청구내역!L{n}:N{n}'], unit, 'B2C 과금수량 역산',
            '청구금액÷기재단가와 기재수량 비교. 상품 EA·실제 작업량 검증과 구분'))
    # The invoice explicitly sums B2B and B2C parcel-rate tables, not B2C alone.
    for n in range(98, 105):
        if not billable_amount(v(summary[n], 'N')):continue
        detail_row = n - 93
        b2c = parcel_sheet.get(detail_row, {})
        b2b = b2b_sheet.get(detail_row, {})
        rate = decimal(v(summary[n], 'L'))
        q1, q2 = decimal(v(b2c, 'I')), decimal(v(b2b, 'T'))
        rates_match = decimal(v(b2c, 'H')) == rate == decimal(v(b2b, 'S'))
        qty = q1 + q2 if rates_match and q1 is not None and q2 is not None else None
        checks.append(check(f'cost.b2c_parcel_summary.{n}', f'택배 {rate}원 구간 수량',
            inverse_quantity(v(summary[n], 'N'), rate), qty,
            [f'청구내역!L{n}:N{n}', f'B2C출고_택배!H{detail_row}:J{detail_row}', f'B2B출고!S{detail_row}:U{detail_row}'],
            '택배 과금 건', '택배 B2B·B2C 합산', '청구 역산수량 ↔ B2C 기재건수+B2B 기재건수'))
    parcels = [(n, c) for n, c in parcel_sheet.items() if n >= 3 and v(c, 'A') is not None and billable_amount(v(c, 'D'))]
    amounts = [decimal(v(c, 'D')) for _, c in parcels]
    detail_total = sum(amounts, Decimal(0)) if all(x is not None for x in amounts) else None
    totals = [decimal(v(parcel_sheet.get(n, {}), 'J')) for n in range(5, 15)]
    rate_total = sum(totals, Decimal(0)) if all(x is not None for x in totals) else None
    checks.append(check('cost.b2c_parcel_total', 'B2C 택배 상세 운임 ↔ 단가별 금액+추가운임',
        detail_total, rate_total, ['B2C출고_택배!D3:D마지막행', 'B2C출고_택배!H5:J14'],
        '원', 'B2C 택배 금액 집계', '전 청구행 운임을 합산; 반복 주문의 다른 송장을 제거하지 않음'))
    for n in range(5, 12):
        c = parcel_sheet.get(n, {})
        if not billable_amount(v(c, 'J')):continue
        rate, qty, amount = (decimal(v(c, col)) for col in ('H', 'I', 'J'))
        checks.append(check(f'cost.b2c_parcel_rate.{n}', 'B2C 택배 기재단가×건수', amount,
            rate*qty if rate is not None and qty is not None else None,
            [f'B2C출고_택배!H{n}:J{n}'], '원', 'B2C 택배 금액 집계',
            '택배비는 상품 EA당 요금이 아님; 중량·규격별 실제 적용은 별도'))
    manual = policy.get('manual_reconciliation', {})
    use_parcels = manual.get('state') == 'confirmed_by_user' and manual.get('grouping') == 'order_carrier_tracking_with_existing_fallback'
    if use_parcels and 'parcel_groups' not in wms:
        raise ValueError('parcel_groups_require_raw_wms_recalculation')
    scenario = packing_scenario(wms.get('parcel_groups' if use_parcels else 'groups', []), policy)
    previous = packing_scenario(wms.get('groups', []), {k:v for k,v in policy.items() if k != 'manual_reconciliation'})
    ready = bool(scenario['eligible_groups'])
    for n, metric in ((28, 'basic'), (31, 'extra')):
        if not billable_amount(v(summary[n], 'N')):continue
        inv = next(r for r in inversions if r['claim_row'] == n)
        expected_q = scenario[metric] if ready else None
        basis = '사용자 승인: 출고일·출고번호 기준, 선작업 제외 전 잠정 비교. 청구의 제외 적용 범위와 달라 과다청구 판정 아님'
        if use_parcels:
            basis = '원본 WMS 주문번호·택배사·송장별 합산, 키 부족 시 기존 승인 출고 기준. (선포장) 수량만 제외한 포장비 잠정 비교; 과다청구 확정 아님'
        for suffix, actual, expected, unit in (
            ('quantity', inv['inverse_quantity'], expected_q, inv['unit']),
            ('amount', inv['amount'], expected_q*inv['rate'] if expected_q is not None and inv['rate'] is not None else None, '원')):
            checks.append(check(f'cost.b2c_wms_{suffix}.{n}', '기본포장' if n==28 else '추가포장', actual, expected,
                [f'청구내역!L{n}:N{n}', 'b2c_actual_claim_wms.json#/cost_observations'], unit,
                'B2C WMS 포장 '+('수량' if suffix=='quantity' else '금액'), basis,
                scenario_only=True, scope_complete=not scenario['unavailable_groups']))
    return {'checks': checks, 'inversions': inversions, 'packing': scenario,
            'previous_packing_summary': {k:v for k,v in previous.items() if k != 'rows'},
            'manual_workbook_used': False,
            'parcel_rows': len(parcels), 'parcel_total': detail_total, 'parcel_rate_total': rate_total,
            'product_EA_from_freight': False, 'full_cost_approved': False,
            'policy': policy, 'counts': dict(Counter(c['status'] for c in checks))}


def write_evidence(path, data, sources):
    fields = ['원본 파일', '시트', 'WMS 일자', '출고번호', '주문번호', '집계 기준', 'WMS 행', '상품 수량', '기본포장 건', '추가포장 EA', '계산 상태', '사유', '택배사', '송장번호', '제외 선포장 EA', '포장 계산 EA']
    with path.open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.writer(f); writer.writerow(fields)
        for r in data['packing']['rows']:
            writer.writerow([sources[r['source']]['path'], 'CSV' if r['source']=='outbound_july' else '출고 Data(04월~06월)',
                ';'.join(r.get('dates', [r['date']])), ';'.join(r.get('shipments', [r['shipment']])), ';'.join(r['orders']), {'shipment':'일자·출고번호','order':'일자·주문번호','order_carrier_tracking':'주문번호·택배사·송장번호','unresolved':'집계 근거 부족'}[r['basis']],
                ','.join(str(n) for n in r['rows']), r['quantity'], r['basic'], r['extra'],
                '잠정 계산' if r['status']=='calculated_scenario' else '자료 부족', r['reason'], r.get('carrier',''), r.get('tracking',''), r.get('excluded_prepacked_quantity',0), r.get('packing_quantity')])
