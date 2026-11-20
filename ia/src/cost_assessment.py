"""One comparison result per billed cost; arithmetic is not occurrence evidence.

The original checks and scoped approvals remain unchanged. This shared policy
is used by the workflow, reports and web; component checks are kept in detail.
"""
import json
import re
from collections import defaultdict
from decimal import Decimal, InvalidOperation

GROUP = '물류비 금액 검증'
POLICY = 'cost-comparison-evidence-v3'


def matched_invoice_amount(details, claim):
    """Require an actually compared, complete monetary amount, never a rate/EA match."""
    try:
        billed = Decimal(str(claim['amount']))
    except (InvalidOperation, ValueError, KeyError):
        return []
    matches = []
    for check in details:
        if (check.get('결과') != '정상' or check.get('_covered_by_detail')
                or check.get('확인범위') not in ('청구서 계산', '청구서 내부 합계', '청구 요약행 간 계산',
                                              'WMS 대사', '실제 작업 증빙', '계약·실비 증빙')
                or check.get('단위') not in ('원', 'KRW')):
            continue
        try:
            actual, expected, difference = (Decimal(str(check.get(k))) for k in ('청구값', '비교값', '차이'))
        except (InvalidOperation, ValueError):
            continue
        if all(v.is_finite() for v in (billed, actual, expected, difference)) and actual == expected == billed and difference == 0:
            matches.append(check)
    return matches


def has_comparison_source(check):
    """Summary arithmetic is not a comparison to detail/WMS/expense evidence."""
    if check.get('결과') != '정상':
        return False
    try:
        actual, expected, delta = (Decimal(str(check.get(k))) for k in ('청구값', '비교값', '차이'))
    except (InvalidOperation, ValueError):
        return False
    if not all(v.is_finite() for v in (actual, expected, delta)) or actual != expected or delta != 0:
        return False
    scope = check.get('확인범위')
    if scope in ('실제 작업 증빙', '계약·실비 증빙'):
        return bool(check.get('WMS위치'))  # Resolved supporting source, not approval.
    if scope == 'WMS 대사':
        return bool(check.get('WMS위치') or check.get('계산근거'))
    if scope not in ('청구서 계산', '청구서 내부 합계'):
        return False
    return any('!' in part and not part.strip().startswith('청구내역!') and '.json' not in part
               for part in check.get('청구위치', '').split(';'))


def target(row, cost):
    ref = row.get('계산근거', '')
    m = re.fullmatch(r'cost_verification\.json#/(checks|items)/(\d+)', ref)
    if m:
        obj = cost[m[1]][int(m[2])]
        if m[1] == 'items': return obj['row']
        if obj.get('claim_row'): return obj['claim_row']
        ident = obj.get('id', '')
        if ident.startswith(('cost.arithmetic.', 'cost.contract_rate.', 'cost.b2c_',
                             'cost.expanded_', 'cost.detail.')):
            end = ident.rsplit('.', 1)[-1]
            if end.isdigit(): return int(end)
    refs = {int(n) for n in re.findall(r'청구내역![$A-Z]*(\d+)', row.get('청구위치', ''))}
    return next(iter(refs)) if len(refs) == 1 else None


def component(row, cost, full):
    r = dict(row)
    group = r['검사']
    if group == '청구·상세 금액 계산': scope = '청구서 계산'
    elif group == '요약·상세 집계': scope = '청구서 내부 합계'
    elif group.startswith('B2C WMS'): scope = 'WMS 대사'
    elif group == '실제 과금 작업량': scope = '실제 작업 증빙'
    elif group == '계약 적용·실비 적정성': scope = '계약·실비 증빙'
    elif group == '계약서·청구 기재단가': scope = '문서 기재 단가'
    else: scope = group
    r['확인범위'] = scope
    if r['결과'] == '정상':
        r['표시결과'] = '계산 일치' if scope == '청구서 계산' else '청구서 내부 일치' if scope == '청구서 내부 합계' else '비교 일치'
        r['사유'] = r.get('적용기준') or scope + '의 비교값이 일치합니다.'
    else: r['표시결과'] = r['결과']
    # Detail objects sometimes contain a check ID rather than its numeric values.
    m = re.fullmatch(r'cost_verification\.json#/items/(\d+)', r.get('계산근거', ''))
    if m and group == '요약·상세 집계':
        detail = cost['items'][int(m[1])].get('detail', {})
        if detail.get('basis'): r['사유'] = detail['basis']
        ident = detail.get('check_id')
        checks = cost.get('checks', []) + full.get('checks', []) + cost.get('billing_quantity', {}).get('summary_checks', [])
        check = next((c for c in checks if ident and c.get('id') == ident), None)
        if ident and ident.startswith('billing.summary.'):
            summary = next((c for c in cost.get('billing_quantity', {}).get('summary_checks', [])
                            if c.get('claim_row') == int(ident.rsplit('.',1)[1])), None)
            if summary:
                check = dict(summary, actual=summary.get('claim_quantity'), expected=summary.get('detail_quantity'),
                             check_performed=summary['status'] in ('match','difference'),
                             evidence=[f"청구내역!M{summary['claim_row']}", f"{summary['detail_sheet']}!{summary['detail_column']}열 합계"])
        if check and check.get('check_performed') and check.get('status') in ('match', 'difference'):
            for output, field in [('청구값','actual'), ('비교값','expected'), ('차이','difference')]:
                r[output] = check.get(field, check.get('observed') if field=='actual' else None)
            r['단위'] = check.get('unit') or check.get('currency') or r.get('단위', '')
            if ident and ident.startswith('b2c.summary.'):
                r['단위'] = cost['items'][int(m[1])].get('unit') or '과금 수량'
            r['청구위치'] = '; '.join(check.get('evidence', []))
            r['사유'] = check.get('basis') or check.get('label') or r['사유']
    if scope == '청구서 내부 합계' and '.json' not in r.get('청구위치',''):
        sheets = re.findall(r'([^; ]+)!', r.get('청구위치',''))
        if sheets and all(s == '청구내역' for s in sheets):
            r['확인범위'] = '청구 요약행 간 계산'
            if r['결과'] == '정상': r['표시결과'] = '계산 일치'
    return r


def consolidate_costs(rows, cost, full):
    claims = {}
    for item in full.get('claim_items', []):
        try: value = Decimal(str(item.get('amount')))
        except (ValueError, InvalidOperation): continue
        if value.is_finite() and value != 0: claims[item['row']] = item
    if not claims: return rows
    buckets = defaultdict(list)
    other = []
    for row in rows:
        n = target(row, cost)
        if n in claims: buckets[n].append(row)
        else: other.append(row)
    source = full.get('sources', {}).get('claim', {}).get('path', '')
    items = {i['row']: i for i in cost.get('items', [])}
    from verification_report_scope import current_support_rows
    support_rows = current_support_rows(cost, full)
    expense_support = {r['invoice_row']: r for r in support_rows
                       if r.get('axis') == 'actual_cost'}
    agreement_support = {r['invoice_row']: r for r in support_rows if r.get('axis') == 'contract'}
    for n, claim in claims.items():
        details = [component(r, cost, full) for r in buckets[n]]
        amount_matches = matched_invoice_amount(details, claim)
        # A performed full-amount comparison can cover unavailable multiplication
        # of a variable-rate summary. Preserve the skipped check in the detail.
        if amount_matches:
            for check in details:
                if (check['결과'] == '미검증' and check['확인범위'] == '청구서 계산'
                        and check.get('비교값') in (None, '') and check.get('차이') in (None, '')
                        and check.get('단위') in ('원', 'KRW')):
                    check['_covered_by_detail'] = '동일 청구금액의 계산·상세 합계 일치'
        relevant = [r for r in details if not r.get('_covered_by_detail')]
        differences = [r for r in relevant if r['결과'] == '불일치']
        missing = [r for r in relevant if r['결과'] == '미검증']
        accepted = [r for r in relevant if r['결과'] == '잠정 확정']
        comparisons = [r for r in relevant if has_comparison_source(r)]
        # A detailed quantity plus full monetary arithmetic, or a full detail
        # amount, can establish the performed comparison. Summary-only formulas
        # and rates never suffice. Expense reimbursement requires its own proof.
        comparison_match = bool(amount_matches and comparisons)
        expense = expense_support.get(n, {})
        agreement_required = '협의' in str(claim.get('unit_price', ''))
        if agreement_required and not expense:
            expense = agreement_support.get(n, {})
        expense_required = ('실비' in str(claim.get('unit_price', ''))
                            or agreement_required or bool(items.get(n, {}).get('actual_cost_application')) or bool(expense))
        expense_matched = (expense.get('check_performed') is True and expense.get('status') == 'match'
                           and bool(expense.get('evidence'))
                           and any(r['확인범위'] == '계약·실비 증빙' and has_comparison_source(r)
                                   for r in amount_matches))
        # No approval is invented. A remaining difference outranks acceptance.
        if differences: status, reason = '불일치', ' · '.join(dict.fromkeys(r['확인범위'] + ' 차이' for r in differences))
        elif missing: status, reason = '미검증', ' · '.join(dict.fromkeys(r['확인범위'] + ' 근거 부족' for r in missing))
        elif expense_required and not expense_matched:
            status = '잠정 확정' if expense.get('status') == 'provisional' else '미검증'
            reason = '실비·협의금액 증빙 미확인' + (' · 기존 결정으로 청구액 임시 인정' if status == '잠정 확정' else '')
        elif comparison_match: status, reason = '정상', 'WMS·청구 상세 또는 추가 증빙과 대사 일치'
        elif accepted: status, reason = '잠정 확정', '청구값 임시 인정 · 실제 작업·계약 또는 실비 증빙 미확인'
        else: status, reason = '미검증', '청구서 내부 계산·합계만 확인됨 · 실제 발생 근거 없음'
        first = next(iter(buckets[n]), {})
        record = {k: v for k,v in first.items() if not k.startswith('_')}
        amount = Decimal(str(claim['amount']))
        if '차감' in claim.get('section', ''): amount = -amount
        money = [r for r in (differences or relevant) if r.get('단위') in ('원','KRW')
                 and r.get('비교값') not in (None, '') and r.get('차이') not in (None, '')
                 and r['확인범위'] != '청구서 계산']
        # A quantity discrepancy does not yield an invented monetary delta.
        compare = money[0] if len(money) == 1 and (not differences or money[0] in differences) else None
        record.update(검사=GROUP, 결과=status, 계산결과=status, 정상처리근거=reason if status == '정상' else '',
            사유구분='차이 확인' if differences else '사용자 잠정 확정' if status=='잠정 확정' else '자료 부족' if status=='미검증' else '',
            사유=reason, 일자='미기재', 주문번호='', 입고예정번호='', 품번='', 송장번호='',
            청구원본=source, 청구위치=f'청구내역!L{n}:N{n}', WMS위치='',
            청구값=str(amount), 비교값=str(amount) if status == '정상' and amount_matches else compare['비교값'] if compare else None,
            차이='0' if status == '정상' and amount_matches else compare['차이'] if compare else None, 단위='원', 적용기준=claim.get('item_context') or items.get(n,{}).get('label','청구 비용'),
            계산근거='cost_assessment', _cost_row=n, _cost_policy=POLICY,
            _cost_result_scope='WMS·청구 상세·증빙 대사 결과; 요약행 곱셈만으로 일치 처리하지 않음. 실비는 지출 증빙 별도 확인',
            _cost_occurrence_pending=any(r['결과'] in ('잠정 확정','미검증') and r['확인범위'] in ('실제 작업 증빙','계약·실비 증빙') for r in details),
            _cost_checks=json.dumps(details, ensure_ascii=False, default=str),
            _cost_quantity=str(claim.get('quantity') or ''), _cost_rate=str(claim.get('unit_price') or ''),
            _cost_unit=str(claim.get('unit') or ''), _cost_category=claim.get('category_context') or '',
            _cost_label=claim.get('item_context') or items.get(n,{}).get('label','청구 비용'))
        evidence = [p.strip() for r in details for p in r.get('청구위치','').split(';')
                    if '!' in p and not p.strip().startswith('청구내역!') and '.json' not in p and re.search(r'![$A-Z]*\d',p)]
        record['_cost_evidence'] = '; '.join(dict.fromkeys(evidence))
        record['WMS위치'] = '; '.join(dict.fromkeys(r['WMS위치'] for r in details if r.get('WMS위치') and '.json' not in r['WMS위치']))
        other.append(record)
    return other


def assess(full, cost, supporting=None):
    if not full.get('claim_items') or not cost.get('items'):
        return {}  # Partial legacy artifacts cannot establish a whole-cost verdict.
    from verification_report_scope import inventory
    scoped = dict(cost, supporting_evidence=supporting or cost.get('supporting_evidence', {}))
    raw = inventory({'sources': full.get('sources', {})}, full, {}, {}, scoped, [], consolidate=False)
    return {r['_cost_row']: r for r in consolidate_costs(raw, scoped, full) if r.get('_cost_policy')}
