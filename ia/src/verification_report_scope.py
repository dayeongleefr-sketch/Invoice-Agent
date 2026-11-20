"""Report matches and user-accepted tentative conversions; preserve raw checks."""
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
import csv

CSV_NAME = '검토필요_전체목록.csv'
TENTATIVE_GROUPS = ('입고 BOX·PCS 잠정 환산', 'B2B BOX·PCS 잠정 환산')
REFERENCE_GROUPS = TENTATIVE_GROUPS + ('B2C WMS 포장 수량', 'B2C WMS 포장 금액')
TENTATIVE_ACCEPTANCE = 'U35 사용자 지정: 잠정 환산 차이 정상 포함'

# Display labels do not change CSV filters, business keys or stored verdicts.
DISPLAY_LABELS = {
    '요약·상세 집계': '청구 요약과 상세 합계 비교',
    '실제 과금 작업량': '실제 작업·비용 발생 확인',
    '계약 적용·실비 적정성': '당월 계약 적용·실비 근거 확인',
    '입고 BOX·PCS 잠정 환산': '입고 박스·낱개 수량 환산(잠정)',
    'B2B BOX·PCS 잠정 환산': 'B2B 박스·낱개 수량 환산(잠정)',
    'B2C WMS 포장 수량': 'B2C 포장 수량(잠정)',
    'B2C WMS 포장 금액': 'B2C 포장 금액(잠정)',
}
COUNT_BASIS = {
    '물류비 금액 검증': '청구 비용 1항목',
    '입고 상품 수량': '일자·입고번호·품번별 1건¹',
    'B2B 상품 수량': '일자·주문번호별 출고 1건',
    'WMS에만 있는 실적': '대응 청구가 없는 실적 1건',
    'B2C 주문 연결': '택배 청구서 1줄',
    '임가공 작업상품 수량': '작업별 수량 비교 1항목',
    '청구·상세 금액 계산': '금액 비교 1항목',
    '계약서·청구 기재단가': '단가 1항목',
    'B2C 과금수량 역산': '과금수량 1항목',
    '택배 B2B·B2C 합산': '택배 단가 구간 1개',
    'B2C 택배 금액 집계': '금액 비교 1항목',
    'B2C WMS 포장 수량': '기본·추가 포장 각 1항목',
    'B2C WMS 포장 금액': '기본·추가 포장 각 1항목',
    '입고 BOX·PCS 잠정 환산': '박스 수량·낱개 수량을 각각 비교',
    'B2B BOX·PCS 잠정 환산': '피킹·포장별 박스 수량·낱개 수량을 각각 비교',
    '요약·상세 집계': '청구 요약서 1항목',
    '실제 과금 작업량': '청구 요약서 1항목',
    '계약 적용·실비 적정성': '계약 조건·실비 근거 각각 1개²',
}


def target_count(group, values):
    """Name what was counted; never replace comparisons with unique orders."""
    if group=='청구·상세 금액 계산':
        counts=Counter('임가공 상세' if r['청구위치'].startswith('임가공-라벨!') else '청구 요약' if r['청구위치'].startswith('청구내역!') else '금액 근거' for r in values)
        return ' · '.join(f'{name} {n:,}줄' for name,n in counts.items())
    n = len(values)
    if group == 'B2B 상품 수량':
        targets = {(r['일자'], r['주문번호']) for r in values}
        if len(targets) == n:
            if len({r['주문번호'] for r in values}) == n and all(r['주문번호'] for r in values):
                return ('수량 차이 주문' if all(r['결과']=='불일치' for r in values) else '출고') + f' {n:,}건'
            return f'출고 {n:,}건(일자·주문번호별)'
        return f'출고 {len(targets):,}건 · 수량 비교 {n:,}항목'
    if group in TENTATIVE_GROUPS:
        targets = {(r['일자'], r['주문번호'], r['입고예정번호'], r['품번']) for r in values}
        kind = '입고' if group == TENTATIVE_GROUPS[0] else '출고'
        return f'{kind} {len(targets):,}건 / 수량 비교 {n:,}개'
    noun = {
        '물류비 금액 검증': '청구 비용',
        '입고 상품 수량': '입고 품번', 'B2C 주문 연결': '택배 청구',
        '계약서·청구 기재단가': '단가', '요약·상세 집계': '청구',
        '실제 과금 작업량': '청구', '계약 적용·실비 적정성': '청구',
        'B2C WMS 포장 수량': '포장비', 'B2C WMS 포장 금액': '포장비',
    }.get(group, '비교')
    unit = '줄' if group == 'B2C 주문 연결' else '건' if group == '입고 상품 수량' else '항목'
    return f'{noun} {n:,}{unit}'


def limit_explanation(group, kind, values):
    if group=='요약·상세 집계' and kind=='자료 부족':
        return '청구서 청구내역에는 항목이 있으나 제공 청구파일에 대응하는 세부 산출내역이 없습니다. 요약값과 다시 합산해 비교할 상세 자료가 부족합니다.'
    if group in TENTATIVE_GROUPS:
        if all(r['청구값'] is None for r in values):
            return '청구서 해당 BOX·PCS 작업량 칸이 빈칸입니다. 0으로 계산하지 않고 U31에 따라 비교에서 제외했습니다.'
        if all('WMS 출고 미연결' in r['사유'] for r in values):
            return '원본 WMS에서 청구 주문의 출고를 찾지 못해 환산할 상품 개수가 없습니다.'
        return '제품 Master의 아웃박스수량·청구서 내품수량이 없거나 여러 값이라 한 박스당 개수를 정할 수 없습니다. BOX·PCS 계산만 제외했으며 품번별 사유는 전체 목록에 있습니다.'
    if group=='청구·상세 금액 계산' and kind=='자료 부족':
        if all(r['청구위치'].startswith('임가공-라벨!') for r in values):
            return '청구서 임가공-라벨 시트 E열 수량 또는 F열 단가가 숫자로 기재되지 않아 작업비 재계산 불가'
        return '청구서 청구내역 시트 L열 단가·M열 수량·N열 금액 중 계산에 필요한 숫자 없음(실비·별도협의·빈칸 등)'
    if kind == '기능 미구현':
        return {
            '요약·상세 집계': '반출비·라벨 교체비 등의 청구 요약항목과 대응 상세행을 연결해 합산·비교하는 기능 미완성',
            '실제 과금 작업량': '입고 검수·반출·라벨 교체 등 청구 작업을 실제 작업기록과 연결하는 기능 미완성; 수량 환산은 별도 수행',
            '계약 적용·실비 적정성': '청구 항목별 계약의 적용기간·대상업무·단가 조건을 판정하는 규칙 미등록',
        }.get(group, '해당 비교 규칙이 프로그램에 아직 연결되지 않음')
    reasons = list(dict.fromkeys(r['사유'] for r in values))
    if len(reasons) == 1:
        return reasons[0]
    return {
        '입고 BOX·PCS 잠정 환산': '품번별 박스당 수량 근거 부족',
        'B2B BOX·PCS 잠정 환산': '빈 작업량·박스당 수량 근거 부족·WMS 미연결',
        '실제 과금 작업량': '실제 작업 횟수·사용면적·실비·택배 규격 근거 부족. 기존 결정과 U42·U43에 따라 해당 적정성만 판정 제외',
    }.get(group, '항목별 사유는 전체 목록에서 확인')


def current_support_rows(cost, full):
    """Use dispositions evaluated for this invoice, never a stale saved result."""
    support = cost.get('supporting_evidence', {})
    expected = full.get('sources', {}).get('claim', {}).get('sha256', '')
    cost_hash = cost.get('sources', {}).get('claim', {}).get('sha256', expected)
    if (support.get('active') is not True or not expected
            or support.get('claim_sha256', '').upper() != expected.upper()
            or cost_hash.upper() != expected.upper()):
        return []
    counts = Counter((r.get('axis'), r.get('invoice_row')) for r in support.get('rows', []))
    return [r for r in support.get('rows', []) if counts[(r.get('axis'), r.get('invoice_row'))] == 1]


def accepted_summary_amounts(cost, full):
    """An accepted lump-sum expense must not become a duplicate missing-detail issue.

    Only the explicitly accepted amount axis covers this summary-level gap.
    Performed comparisons, item-level missing quantities and unsupported rules
    remain independent. Source calculations and approval records are untouched.
    """
    accepted = {}
    for resolution in current_support_rows(cost, full):
        row = resolution.get('invoice_row')
        if (resolution.get('axis') != 'actual_cost' or resolution.get('status') != 'provisional'
                or resolution.get('check_performed') is not False
                or resolution.get('unit') != 'KRW'
                or resolution.get('claim_location') != f'청구내역!N{row}'
                or resolution.get('expected') is not None or resolution.get('difference') is not None):
            continue
        claims = [i for i in full.get('claim_items', []) if i.get('row') == row and i.get('sheet') == '청구내역']
        items = [i for i in cost.get('items', []) if i.get('row') == row]
        if len(claims) != 1 or len(items) != 1:
            continue
        item = items[0]; detail = item.get('detail', {})
        if (detail.get('status') != 'data_unavailable' or detail.get('rule_implemented') is not True
                or detail.get('decision') != 'U26' or detail.get('result') in ('match', 'difference')
                or detail.get('expected') is not None or detail.get('difference') is not None
                or detail.get('required_evidence') != ['항목별 상세 산출내역']):
            continue
        try:
            values = [Decimal(str(v)) for v in (resolution.get('actual'), claims[0].get('amount'), item.get('stated_amount'))]
            if all(v.is_finite() and v == values[0] for v in values) and values[0] != 0:
                accepted[row] = resolution
        except (InvalidOperation, ValueError):
            continue
    return accepted


def accepted_storage_detail(item, cost):
    """U43/U44 area-period acceptance also covers the duplicate detail gap.

    Arithmetic and any numeric difference remain independent checks.
    """
    detail = item.get('detail', {})
    accepted = item.get('user_acceptance', {})
    support = cost.get('supporting_evidence', {})
    return (detail.get('status') == 'data_unavailable'
            and detail.get('decision') == 'U43'
            and detail.get('result') not in ('match', 'difference')
            and detail.get('required_evidence') == ['층별 실제 사용면적', '적용기간']
            and accepted.get('decision') == 'U43'
            and accepted.get('status') == 'accepted_as_invoiced_by_user'
            and support.get('active') is True
            and any(r.get('axis') == 'work' and r.get('invoice_row') == item['row']
                    and r.get('status') == 'provisional' for r in support.get('rows', [])))


def covered_summary_checks(cost, full):
    """Variable-rate summary multiplication is redundant after an amount match.

    Keep the raw skipped check and its position; do not turn it into a pass or
    use a quantity match to cover an amount check.
    """
    covered = {}
    for check in cost.get('checks', []):
        if not check['id'].startswith('cost.arithmetic.') or check.get('status') != 'data_unavailable' or check.get('check_performed'):
            continue
        suffix = check['id'].rsplit('.', 1)[-1]
        if not suffix.isdigit():continue
        row = int(suffix)
        claims = [i for i in full.get('claim_items', []) if i.get('row') == row and i.get('sheet') == '청구내역']
        items = [i for i in cost.get('items', []) if i.get('row') == row]
        if len(claims) != 1 or len(items) != 1:continue
        claim, item = claims[0], items[0]
        if str(claim.get('unit_price', '')).strip() not in ('별도 협의', '실비'):continue
        detail = item.get('detail', {})
        if detail.get('status') != 'completed' or detail.get('result') != 'match':continue
        matches = [c for c in cost['checks'] if c['id'] == detail.get('check_id')]
        if len(matches) != 1:continue
        match = matches[0]
        if (match.get('status') != 'match' or match.get('check_performed') is not True
                or match.get('unit', '원') not in ('원', 'KRW')
                or match.get('currency', 'KRW') != 'KRW'
                or f'청구내역!N{row}' not in match.get('evidence', [])
                or not any(not e.startswith('청구내역!') for e in match.get('evidence', []))):continue
        try:
            values = [Decimal(str(v)) for v in (claim.get('amount'), check.get('actual'), match.get('actual'), match.get('expected'))]
            if (all(v.is_finite() for v in values) and values[0] != 0
                    and all(v == values[0] for v in values)
                    and Decimal(str(match.get('difference'))) == 0):
                covered[check['id']] = match['id']
        except (InvalidOperation, ValueError):
            continue
    return covered


def visible_rows(rows):
    return [r for r in rows if not r.get('_covered_by_detail')]


def inventory(d, f, investigation, review, cost, staff, *, consolidate=True):
    rows = []
    sources = d.get('sources', {})

    def locations(evidence):
        return '; '.join(sources.get(e.get('source'), {}).get('path', '') + ' / ' +
                         e.get('sheet', 'CSV') + '!' + str(e.get('row', e.get('csv_record', '')))
                         for e in evidence)

    def add(group, status, *, performed=False, reason='', kind='', date='', order='', asn='', sku='',
            claim='', wms='', actual=None, expected=None, difference=None, unit='', note='', source='', tracking='', normal_by_user=False, raw_status=None):
        raw = raw_status or status
        raw_state = '정상' if performed and raw == 'match' else '불일치' if performed and raw == 'difference' else '미검증'
        accepted = normal_by_user and group in TENTATIVE_GROUPS and raw_state == '불일치'
        state = '잠정 확정' if status == 'provisional' else '정상' if accepted else raw_state
        rows.append(dict(검사=group, 결과=state, 계산결과=raw_state,
                         정상처리근거=TENTATIVE_ACCEPTANCE if accepted else '실제 비교 일치' if raw_state=='정상' else '',
                         사유구분=kind or ('차이 확인' if state == '불일치' else ''),
                         사유=reason, 일자=date or '미기재', 주문번호=order, 입고예정번호=asn, 품번=sku, 송장번호=tracking,
                         청구원본=sources.get('claim', {}).get('path', ''), 청구위치=claim, WMS위치=wms,
                         청구값=actual, 비교값=expected, 차이=difference, 단위=unit, 적용기준=note, 계산근거=source))

    for index, r in enumerate(d.get('rows', [])):
        if not r.get('claim_present'):
            continue  # Reverse unmatched records come from the approved current staff view below.
        inbound = r['scope'].startswith('inbound'); key = r['key']
        linked = r.get('wms_present', False)
        kind = r['kind']
        add('입고 상품 수량' if inbound else 'B2B 상품 수량',
            'match' if kind == 'quantity_match' else 'difference', performed=linked,
            reason=r.get('cause') or r.get('explanation', ''), kind='수량 차이' if linked else 'WMS 미연결',
            date=key[0], order='' if inbound else key[1], asn=key[1] if inbound and len(key)>2 else '',
            sku=key[-1] if inbound else '전체(주문 합계)', claim=locations(r.get('claim_evidence', [])),
            wms=locations(r.get('wms_evidence', [])), actual=r['claim_quantity'], expected=r['wms_quantity'],
            difference=r['difference_claim_minus_wms'], unit='EA',
            note='청구−WMS. 소모품 수량과 같은 차이도 그대로 유지', source=f'direct_wms_result.json#/rows/{index}')
    for index, r in enumerate(d.get('missing_claim_quantity', [])):
        add('B2B 상품 수량', 'unavailable', kind='자료 부족', reason='재출고 상품 수량 빈칸; 기존 확정불가 유지',
            date=r['key'][0], order=r['key'][1], claim=locations([r]), unit='EA',
            note=f"{r.get('box')}BOX / 운임 {r.get('freight')}원; 상품 개수 역산 불가",
            source=f'direct_wms_result.json#/missing_claim_quantity/{index}')
    for r in staff:
        if r['kind'] == 'wms_only':
            add('WMS에만 있는 실적', 'unavailable', kind='대응 청구 미연결', reason=r['reason'],
                date=r.get('date', ''), order=r.get('order_number') or r.get('number', ''),
                asn=r.get('receipt_number', ''), sku=r.get('sku', ''), wms=r.get('wms_ref', ''),
                expected=r.get('wms_quantity'), unit='EA', note=r['action'], source='담당자_확인목록.csv')

    connected = set().union(*(set((review or {}).get(k, [])) for k in
                              ('direct_orders', 'reference_bridge_orders', 'annotated_base_orders')))
    for index, r in enumerate((investigation or {}).get('claim_candidates', [])):
        if r['kind'] != 'b2c':
            continue
        for n in r.get('claim_rows', [r['row']]):
            common = dict(order=r['order'], tracking=r.get('tracking',''), claim=f"{r['sheet']}!A{n}:D{n}",
                          source=f'match_investigation.json#/claim_candidates/{index}')
            add('B2C 주문 연결', 'match' if r['order'] in connected else 'unavailable',
                performed=bool(review) and r['order'] in connected, kind='WMS 미연결',
                reason='원본 WMS의 대응 출고를 확정하지 못함', actual=r.get('invoice_stated_freight', r.get('freight')), unit='기재 운임 원', **common)
            if not (cost or {}).get('b2c_charge'):
                add('B2C 과금단위 검사', 'unavailable', kind='검사 결과 없음',
                    reason='포장비 역산·WMS 비교 및 택배 단가별 집계 결과 필요', unit='과금단위', **common)
    for index, r in enumerate(d.get('work_quantity_checks', [])):
        add('임가공 작업상품 수량', r['status'], performed=r['status'] in ('match', 'difference'),
            kind='수량 차이' if r['status']=='difference' else '자료 부족', date=r.get('date'),
            order='; '.join(r.get('orders', [])), sku=r.get('output_sku', ''),
            claim=locations([x['evidence'] for x in r.get('claim_rows', []) if x.get('evidence')]),
            wms=locations(r.get('wms_evidence', [])), actual=r.get('claim_quantity'), expected=r.get('wms_quantity'),
            difference=r.get('difference'), unit=r.get('unit', ''), source=f'direct_wms_result.json#/work_quantity_checks/{index}')
    if not cost:
        add('비용 검사', 'unavailable', kind='검사 결과 없음', reason='연결 가능한 비용 검사 결과가 없음')
        return rows
    support_rows = current_support_rows(cost, f)
    scoped_cost = dict(cost, supporting_evidence=dict(cost.get('supporting_evidence', {}),
                                                    active=bool(support_rows), rows=support_rows))
    selected_rates={x['invoice_row']:x for x in support_rows if x['axis']=='stated_rate' and x['status']=='provisional'}
    accepted_amounts = accepted_summary_amounts(cost, f)
    accepted_reason = '사용자가 이 비용의 청구액을 잠정 인정했습니다. 세부 산출내역·실제 발생 증빙은 아직 확인하지 못했습니다.'
    covered = covered_summary_checks(cost, f)
    for index, r in enumerate(cost['checks']):
        if r['id'].startswith('cost.expanded_detail.'):
            continue  # Exactly one summary/detail result per invoice item below.
        rate = r['id'].startswith('cost.contract_rate.')
        approved=rate and int(r['id'].rsplit('.',1)[1]) in selected_rates
        amount_accepted = False
        if (r['id'].startswith('cost.arithmetic.') and r['status']=='data_unavailable'
                and r.get('check_performed') is False and r.get('expected') is None and r.get('difference') is None
                and r.get('currency', 'KRW') == 'KRW'):
            resolution = accepted_amounts.get(int(r['id'].rsplit('.', 1)[1]))
            if resolution:
                try: amount_accepted = Decimal(str(r.get('actual'))) == Decimal(resolution['actual'])
                except (InvalidOperation, ValueError): pass
        add(r.get('report_group') or ('계약서·청구 기재단가' if rate else '청구·상세 금액 계산'), 'provisional' if approved or amount_accepted else r['status'], raw_status=r['status'],
            performed=r.get('check_performed', False), kind='사용자 잠정 확정' if amount_accepted else '단가 차이' if rate else '금액 차이' if r['status']=='difference' else '자료 부족',
            reason=accepted_reason if amount_accepted else (r['label']+' · '+r.get('basis','')) if r.get('scenario_only') else r['label'] if r['status']=='difference' else
                   '청구서 '+ '; '.join(r['evidence'])+'의 수량·단가·금액 중 계산에 필요한 숫자 없음',
            claim='; '.join(r['evidence']), actual=r.get('actual'), expected=r.get('expected'), difference=r.get('difference'),
            unit='원/과금단위' if rate else r.get('unit','원'), note='U45 사용자 채택 단가 잠정 확정' if approved else '당월 계약 적용은 별도 미확정' if rate else r.get('basis', ''),
            source=f'cost_verification.json#/checks/{index}')
        if r['id'] in covered:
            rows[-1]['_covered_by_detail'] = covered[r['id']]
    for index, r in enumerate(cost.get('billing_quantity', {}).get('rows', [])):
        inbound=r['scope'].startswith('inbound'); key=r['key']
        for col, actual in r['claimed_work'].items():
            expected=(r.get('expected_work') or {}).get(col); diff=(r.get('differences') or {}).get(col)
            performed = actual is not None and expected is not None and diff is not None
            label = ('입고 ' if inbound else 'B2B ') + ('BOX·PCS 잠정 환산')
            missing_parts=[p for p in r.get('wms_parts',[]) if p.get('outer_EA') is None]
            if actual is None:
                reason='청구서 '+r['claim_sheet']+' '+col+'열 작업량 빈칸(U31 비교 제외)'
            elif not r.get('wms_parts'):
                reason='청구 주문에 대응하는 원본 WMS 출고 미연결; 환산할 상품수량 없음'
            elif missing_parts:
                reason='제품 Master의 아웃박스수량과 청구서 내품수량으로 박스당 개수 확정 불가: '+ '; '.join(p['sku']+'('+p.get('reason','값 없음')+')' for p in missing_parts)
            else:
                reason=r.get('reason','승인된 BOX/PCS 비교 근거 없음')
            if performed:
                reason=r.get('cause_analysis', {}).get('label') or '기재 작업량과 잠정 환산값 차이'
            add(label, 'match' if performed and Decimal(str(diff))==0 else 'difference', performed=performed,
                kind='작업량 차이' if performed else '자료 부족', reason=reason, date=key[0],
                order='' if inbound else key[1], asn=key[1] if inbound and len(key)>2 else '',
                sku=key[-1] if inbound else '전체(주문 합계)',
                claim=r['claim_sheet']+'!'+','.join(col+str(n) for n in r['claim_rows']),
                wms=locations(r.get('wms_evidence', [])), actual=actual, expected=expected, difference=diff,
                unit='BOX' if col in ('L','N') else 'PCS', normal_by_user=True,
                note='청구−환산값. 잠정 환산 차이는 U35에 따라 보고서 정상에 포함; 실제 작업·과금 적정성 확정 아님',
                source=f'cost_verification.json#/billing_quantity/rows/{index}')
    for index, item in enumerate(cost['items']):
        detail=item['detail']; row=item['row']; location='; '.join(item['evidence'])
        common=dict(claim=location, unit='원', note=item['label']+' · '+str(item.get('unit') or '')+' · 청구 기재금액', source=f'cost_verification.json#/items/{index}')
        detail_common=dict(common)
        detail_common['unit']=detail.get('unit',common['unit'])
        if detail.get('evidence'):detail_common['claim']='; '.join(detail['evidence'])
        storage_accepted = accepted_storage_detail(item, scoped_cost)
        amount_accepted = row in accepted_amounts
        add('요약·상세 집계', 'provisional' if storage_accepted or amount_accepted else detail.get('result', ''), performed=detail.get('status')=='completed' and detail.get('result') in ('match','difference'),
            kind='사용자 잠정 확정' if storage_accepted or amount_accepted else '기능 미구현' if detail.get('status')=='not_implemented' else '사용자 결정 대기' if detail.get('status')=='awaiting_user_decision' else '자료 부족',
            reason=accepted_reason if amount_accepted else '사용자가 청구 보관면적·기간 기재값을 채택했습니다. 독립 면적·기간 증빙 부재는 확인 필요에 중복 포함하지 않습니다.' if storage_accepted else detail.get('reason') or '상세 합계 비교', actual=detail.get('actual',item.get('stated_amount')),
            expected=detail.get('expected'),difference=detail.get('difference'), **detail_common)
        work=item['wms_cost_unit']
        def followup(axis, group, status, **kwargs):
            resolution=next((x for x in support_rows if x['axis']==axis and x['invoice_row']==row),None)
            if resolution:
                kwargs.update(performed=resolution['check_performed'],reason=resolution['reason'],
                              kind='추가 증빙 차이' if resolution['status']=='difference' else '사용자 잠정 확정' if resolution['status']=='provisional' else '자료 부족',
                              actual=resolution['actual'],expected=resolution['expected'],difference=resolution['difference'],
                              claim=resolution['claim_location'],wms='; '.join(resolution['evidence']),unit=resolution['unit'],
                              source='supporting_evidence_result.json',note='U44 잠정 확정; 증빙 등록 시 같은 청구 행·단위로 비교')
                status=resolution['status']
            add(group,status,**kwargs)
        followup('work','실제 과금 작업량', work.get('result','unavailable'), performed=work['status']=='completed',
            kind='사용자 결정 대기' if work['status']=='awaiting_user_decision' else '자료 부족' if work['status'] in ('partial_scenario','data_unavailable') else '기능 미구현',
            reason='잠정 환산 후 실제 피킹·팩킹 발생 연결 미구현' if item['wms_cost_unit']['status']=='partial'
                   else item['wms_cost_unit'].get('reason', '작업량·과금 단위 검사 미구현'),
            actual=item.get('stated_amount'), **common)
        state=item['contract_application']
        followup('contract','계약 적용·실비 적정성', 'unavailable', kind='기능 미구현' if state=='not_implemented' else '계약 적용 미확정',
            reason='미등록 계약 조건 검사 미구현' if state=='not_implemented' else item.get('contract_application_reason','당월 적용 단가 확정불가; 사용자 판정 제외 유지'),
            actual=item.get('stated_amount'), **common)
        if item.get('actual_cost_application') or any(x['axis']=='actual_cost' and x['invoice_row']==row for x in support_rows):
            followup('actual_cost','계약 적용·실비 적정성', 'unavailable', kind='자료 부족',
                reason='실비·별도협의 금액 적정성 확정불가; 사용자 판정 제외 유지', actual=item.get('stated_amount'), **common)
    if not consolidate:
        return rows
    from cost_assessment import consolidate_costs
    return consolidate_costs(rows, cost, f)


def save_inventory(path, rows):
    rows=visible_rows(rows)
    selected=[r for r in rows if r['결과'] not in ('정상','잠정 확정') or r.get('_cost_policy')]
    fields=['조회구분', *dict.fromkeys(k for r in rows for k in r)] if rows else ['조회구분','검사','결과']
    with path.open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=fields);writer.writeheader()
        for r in selected:
            writer.writerow({'조회구분':r['검사']+' / '+r['결과'],**r})
    return selected


def summary_table(rows):
    rows=visible_rows(rows)
    groups=defaultdict(Counter)
    for r in rows:
        if r['검사'] not in REFERENCE_GROUPS:groups[r['검사']][r['결과']]+=1
    groups={g:c for g,c in groups.items() if c['불일치'] or c['미검증']}
    if not groups:
        return ['### 검사별 요약', '', '요약 대상에 불일치·미검증 항목이 없습니다. 정상 항목은 생략하고 잠정 계산은 별도 참고 항목으로 표시합니다.', '']
    lines=['### 검사별 요약', '', '| 확인 항목 | 세는 단위 | 정상 | 불일치 | 미검증 |', '|---|---|---:|---:|---:|']
    for group,counts in groups.items():
        lines.append('| '+DISPLAY_LABELS.get(group,group)+' | '+COUNT_BASIS.get(group,'비교 1항목')+' | '+' | '.join(f'{counts[s]:,}' for s in ('정상','불일치','미검증'))+' |')
    lines += ['', '불일치·미검증이 있는 항목만 표시합니다.',
              '각 행의 숫자는 위 단위로 센 결과입니다. BOX·PCS 환산과 B2C 포장비 잠정 비교는 보고서 하단의 별도 참고 항목에 표시하며 이 요약에 합산하지 않습니다.']
    if '입고 상품 수량' in groups:
        lines.append('¹ 입고번호가 없는 6월은 일자·품번별입니다.')
    if '계약 적용·실비 적정성' in groups:
        lines.append('² 같은 청구 항목에 계약 검사와 실비 검사가 모두 있으면 각각 셉니다. 예: 청구 78항목의 계약 조건 + 11항목의 실비 근거 = 89개 확인 조건.')
    return lines+['']


def provisional_note(cost, ref):
    rows=(cost or {}).get('supporting_evidence',{}).get('rows',[])
    if not any(r['status']=='provisional' for r in rows):return []
    lines=['### 잠정 확정', '',
            '**실제 작업량·계약 적용·실비 적정성은 현재 청구값으로 잠정 확정했습니다(U44).** 자료 부족 목록에서 제외하며, 독립 증빙으로 검증을 통과했다는 뜻은 아닙니다. 추가 증빙을 등록하면 청구값과 재대사하고 차이를 다시 표시합니다.',
            ref('잠정 확정 대상·추가 증빙 대사 결과','잠정확정_추가증빙대사.csv'), '']
    accepted=[r for r in rows if r['axis']=='stated_rate']
    if accepted:
        lines+=['**사용자 채택 단가로 재계산:** 계약서 원문과의 단가 차이는 잠정 확정으로 분리했습니다.', '',
                '| 청구 위치 | 적용 단가 | 청구 수량 | 재계산 금액 | 청구금액과 비교 |','|---|---:|---:|---:|---|']
        for r in accepted:
            v=r['applied_rate_comparison']
            fmt=lambda n:'미기재' if n is None else f'{Decimal(n):,.0f}'
            lines.append('| '+r['claim_location']+' | '+fmt(v['rate'])+'원 | '+fmt(v['quantity'])+' | '+fmt(v['calculated_amount'])+'원 | '+{'match':'일치','difference':'차이 '+fmt(v['difference'])+'원','data_unavailable':'계산값 부족'}[v['status']]+' |')
        lines+=['','청구 수량×채택 단가의 금액 비교입니다. WMS 수량·포장 건수 차이는 별도로 유지합니다.','']
    return lines


def tentative_conversion_note(rows, ref):
    selected=[r for r in rows if r['검사'] in TENTATIVE_GROUPS]
    if not selected:return []
    lines=['### 참고: 박스·낱개 수량 잠정 환산', '',
           '상품 개수(EA) 대사와 별개인 참고 계산입니다. 상품 개수를 박스·낱개로 나눈 예상값이며 실제 작업 횟수를 확정하지 않습니다.', '',
           '| 대상 | 비교 방식 | 수치 일치 | 사용자 지정 정상 | 비교하지 못함 |', '|---|---|---:|---:|---:|']
    for group in TENTATIVE_GROUPS:
        values=[r for r in selected if r['검사']==group]
        if not values:continue
        matched=sum(r['계산결과']=='정상' for r in values)
        accepted=sum(r['정상처리근거']==TENTATIVE_ACCEPTANCE for r in values)
        unavailable=sum(r['결과']=='미검증' for r in values)
        lines.append('| '+target_count(group,values)+' | '+COUNT_BASIS[group]+f' | {matched:,} | {accepted:,} | {unavailable:,} |')
    lines += ['', '위 숫자는 주문 수가 아닌 수량 비교 개수입니다. 예: 출고 1건의 피킹 BOX·PCS와 포장 BOX·PCS를 각각 비교하면 수량 비교 4개입니다.',
              '같은 일자·번호·품번의 WMS 수량 합산 → Master 박스당 수량(없으면 청구의 유일한 내품수량) 적용 → 완박스 몫·낱개 나머지 계산. 예: 650개 ÷ 100개 = 6BOX + 50PCS.',
              '6월 입고 번호가 없으면 일자·품번으로 합산하며, 6월 B2B 빈 단위는 승인된 EA 기준을 적용합니다. U35 사용자 지정 정상은 수치 일치·실제 작업 확인과 구분합니다.', '',
              '| 비교하지 못한 참고 항목 | 대상 | 이유·조회 조건 |','|---|---|---|']
    groups=defaultdict(list)
    for r in selected:
        if r['결과']=='미검증':
            reason='청구 작업량 빈칸' if r['청구값'] is None else 'WMS 미연결' if 'WMS 출고 미연결' in r['사유'] else '박스당 수량 미확정'
            groups[(r['검사'],reason)].append(r)
    for (group,reason),values in groups.items():
        filter_text='작업량 빈칸' if reason=='청구 작업량 빈칸' else 'WMS 출고 미연결' if reason=='WMS 미연결' else '박스당 개수 확정 불가'
        lines.append('| '+group+' · '+reason+' | '+target_count(group,values)+' | '+limit_explanation(group,'자료 부족',values)+'<br>'+ref('전체 대상',CSV_NAME)+' · **조회구분 = '+group+' / 미검증** · **사유: '+filter_text+' 포함** |')
    if not groups:lines.append('| 없음 | — | 모든 참고 수량 비교 수행 |')
    lines += ['', '원래 수량·차이는 '+ref('환산 근거·계산 내역','과금수량_검사.csv')+'에 보존합니다. 작업비의 청구 산술·상세 합계 검사는 본문에 유지합니다.', '']
    return lines


def b2c_charge_note(cost, ref):
    data=(cost or {}).get('b2c_charge')
    if not data or not any(x['claim_row'] in (28,31) for x in data.get('inversions',[])):return []
    def fmt(value):
        return '계산 대기' if value is None else f'{Decimal(str(value)):,.0f}'
    lines=['### 참고: B2C 포장비 잠정 비교', '',
           '| 항목 | 청구금액 ÷ 기재단가 | WMS 잠정수량 | WMS 수량 × 단가 | 금액 차이(청구−WMS) |', '|---|---:|---:|---:|---:|']
    for n,label in ((28,'기본포장'),(31,'추가포장')):
        inv=next((x for x in data['inversions'] if x['claim_row']==n),None)
        if inv is None:continue
        quantity=next(x for x in data['checks'] if x['id']==f'cost.b2c_wms_quantity.{n}')
        amount=next(x for x in data['checks'] if x['id']==f'cost.b2c_wms_amount.{n}')
        lines.append(f"| {label} | {fmt(inv['amount'])}원 ÷ {fmt(inv['rate'])}원 = **{fmt(inv['inverse_quantity'])}** | {fmt(quantity['expected'])} | {fmt(amount['expected'])}원 | **{fmt(amount['difference'])}원** |")
    unavailable=data['packing']['unavailable_groups']
    if data['packing'].get('prework_excluded'):
        lines += ['', '**비교 기준:** 원본 WMS의 주문번호·택배사·송장번호별 상품 수량 합산. 필요한 번호가 없으면 기존 승인 출고 기준을 사용합니다.',
                  f"**선포장 제외:** 품명에 `(선포장)`이 있는 {fmt(data['packing']['excluded_prepacked_EA'])}EA만 포장비에서 제외했습니다. 일반 상품과 섞이면 나머지 상품으로 계산하며 전체 출고수량은 유지합니다.",
                  f'**남은 한계:** 집계 번호가 불완전한 {unavailable:,}개 내역은 계산에서 분리했습니다. 위 금액 차이는 과다청구 확정액이 아닙니다. '+ref('포장 계산 전체 근거','B2C_과금수량_근거.csv'), '']
        return lines
    lines += ['', '**비교 기준:** 출고일·출고번호당 기본포장 1건, 상품 4개째부터 추가포장 개수. 6월은 빈 단위를 EA로 해석하고 출고번호가 없으면 출고일·온전한 주문번호로 집계.',
              '**범위 차이 포함:** 선작업을 빼기 전 WMS 수량으로 계산한 잠정값입니다. 차이를 과다청구액으로 확정하지 않습니다.'+
              (f' 출고번호·온전한 주문번호 등 계산 근거가 없는 {unavailable:,}개 자료 집계는 별도로 남겼습니다.' if unavailable else ''),
              '택배비는 상품 1개당 요금이 아니므로 상품 EA로 역산하지 않습니다. 택배 단가별 건수·금액·B2B/B2C 합계를 별도 검사했습니다. '+ref('포장 계산 전체 근거','B2C_과금수량_근거.csv'), '']
    return lines


def expanded_cost_note(cost, ref):
    if not (cost or {}).get('expanded_cost_checks'):return []
    return ['### 추가 비용 비교의 특이사항', '',
            '| 대상 | 적용 기준·남은 확인 |','|---|---|',
            '| 보관비 · 청구내역 88행 | 사용자 지정: 1,200을 평으로 해석(원본 표기: ㎡). 실제 면적·계약 적용을 확인한 것은 아님 |',
            '| 보관비 · 88~93행 | 사용자 지정: 청구 기재값 채택, 면적×단가 계산 확인. 실제 사용면적·기간 적정성은 판단 제외 |',
            '| 가상채널A · 청구내역 52행 | 사용자 지정: 출고 상세 1BOX당 추가작업 1건으로 잠정 비교 |',
            '| 라벨 교체·PLT 재적치 · 23~24행 | 원본 수식은 박스해체 수량을 참조. 합계 일치가 작업명 대응의 적정성을 뜻하지 않음 |',
            '| 송장 추가부착 · 54행 | 수식에 직접 더한 21+13+2건의 개별 작업 근거는 없음 |','',
            ref('청구 항목별 계산·근거 상태','비용검사_결과.csv'), '']


def exception_tables(rows, ref):
    rows=visible_rows(rows)
    def text(value):
        return str(value).replace('|','\\|').replace('\n',' ') if value is not None else '미기재'

    def number(value):
        if value is None:return '미기재'
        try:return f'{Decimal(str(value)):,.4f}'.rstrip('0').rstrip('.')
        except Exception:return text(value)

    def target(r):
        key=('주문번호: '+r['주문번호'] if r['주문번호'] else
             '입고예정번호: '+r['입고예정번호'] if r['입고예정번호'] else
             '품번: '+r['품번'] if r['품번'] else '')
        # The complete source/row list stays in the filtered CSV.
        positions=r['청구위치'].split('; ')
        position=next((p for p in positions if '!' in p),positions[0])
        if ' / ' in position:position=position.rsplit(' / ',1)[1]
        if ',' in position:
            parts=position.split(',');position=parts[0]+f' 외 {len(parts)-1}행'
        return text(('일자: '+r['일자']+' / '+key+' / ' if key else '')+position)

    lines=['### 2. 연결 후 수량·금액·기재 단가 차이', '',
           '| 확인 항목 | 차이가 있는 대상 | 대표 1건의 값: 청구 / 비교 / 차이 | 대표 조회·전체 목록 |', '|---|---|---|---|']
    groups=defaultdict(list)
    for r in rows:
        if r['결과']=='불일치':
            # Show the three distinct price differences separately.
            key=(r['검사'],r['사유'] if r['검사']=='계약서·청구 기재단가' else r['적용기준'] if r['검사']=='요약·상세 집계' else '')
            groups[key].append(r)
    for (group,label), values in groups.items():
        r=values[0];lookup=group+' / 불일치'
        difference=' / '.join(number(r[k]) for k in ('청구값','비교값','차이'))+' '+r['단위']
        lines.append('| '+text(DISPLAY_LABELS.get(group,group)+(' · '+label if label else ''))+' | '+target_count(group,values)+' | '+text(difference)+
                     ' | '+target(r)+'<br>'+ref('전체 대상',CSV_NAME)+' · **조회구분 = '+text(lookup)+'** |')
    if not groups:lines.append('| — | 0 | 확인된 차이 없음 | — |')
    lines += ['', '**대표 값은 전체 대상의 합계가 아닙니다.** 예를 들어 주문 46건 옆의 수량은 그중 1건입니다. 차이는 청구−비교값이며 과다청구액 확정과 구분합니다.', '',
              '### 3. 아직 확인하지 못한 항목', '']
    if any(r['사유구분']=='기능 미구현' for r in rows):
        lines += ['**개발 필요(기능 미구현)**는 비교 규칙이 아직 프로그램에 연결되지 않았다는 뜻입니다. 개발 지원 에이전트가 보완할 부분이며 담당자에게 전수 확인을 요청하는 목록이 아닙니다.', '']
    lines += ['| 확인 항목 | 대상 | 남은 이유 | 대표 위치·전체 목록 |', '|---|---|---|---|']
    groups=defaultdict(list)
    for r in rows:
        if r['결과']=='미검증' and r['사유구분'] not in ('WMS 미연결','대응 청구 미연결'):
            if r['검사']=='B2B 상품 수량':continue  # Missing re-shipment quantity is in section 1.
            source=r['청구위치'].split('!',1)[0] if r['검사']=='청구·상세 금액 계산' else ''
            if r['검사'] in TENTATIVE_GROUPS:
                source='청구 작업량 빈칸' if r['청구값'] is None else 'WMS 미연결' if 'WMS 출고 미연결' in r['사유'] else '박스당 수량 미확정'
            groups[(r['검사'],r['사유구분'],source)].append(r)
    for (group,kind,source),values in groups.items():
        r=values[0];lookup=group+' / 미검증'
        reason=limit_explanation(group,kind,values)
        label='청구 요약 금액 계산' if source=='청구내역' else '임가공 상세 작업비 계산' if source=='임가공-라벨' else DISPLAY_LABELS.get(group,group)+(' · '+source if group in TENTATIVE_GROUPS else '')
        lines.append('| '+text(label)+' | '+target_count(group,values)+' | **'+text('개발 필요' if kind=='기능 미구현' else kind)+'** · '+text(reason)+
                     ' | '+target(r)+'<br>'+ref('전체 대상',CSV_NAME)+' · **조회구분 = '+text(lookup)+'**'+
                     (' · **사유구분 = '+text(kind)+'**' if group in ('계약 적용·실비 적정성','실제 과금 작업량') else '')+
                      (' · **청구위치: '+text(source)+'!로 시작**' if source in ('청구내역','임가공-라벨') else
                       ' · **사유: '+('작업량 빈칸' if source=='청구 작업량 빈칸' else 'WMS 출고 미연결' if source=='WMS 미연결' else '박스당 개수 확정 불가')+' 포함**' if group in TENTATIVE_GROUPS else '')+' |')
    return lines+['', '기존 확정불가·비교 제외 결정은 유지합니다. 금액이 빈 청구 양식 행도 미검증으로 구분하며, 빈칸을 0원이나 정상으로 처리하지 않습니다.',
                  '전체 목록에는 정상과 U44 잠정 확정 항목을 제외했습니다. 잠정 확정 대상은 별도 증빙 대사 목록에 보존합니다. **조회구분**으로 필터하면 일자·주문/입고예정번호·품번·원본 위치·차이·사유를 확인할 수 있습니다.', '']
