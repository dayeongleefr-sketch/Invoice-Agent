"""One warehouse entry report from saved calculations; no new billing decisions."""
from pathlib import Path
from collections import defaultdict, Counter
from decimal import Decimal
import csv
import hashlib
import json
import os
import sys

RESULTS = ('direct_wms_result.json', 'full_invoice_result.json', 'b2c_actual_claim_wms.json')


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def num(value):
    return '미기재' if value is None else format(Decimal(str(value)), ',f').rstrip('0').rstrip('.') if '.' in str(value) else format(int(value), ',')


def cell(value):
    return str(value).replace('|', '／').replace('\n', ' ')


def staff_review_items(d, b, review, investigation, base):
    """Presentation filter only: no new matching or billing decisions."""
    items = []

    def add(kind, category, number, date='', claim_qty=None, wms_qty=None,
            amount=None, tracking='', reason='', action='', status='확인 대상',
            claim_ref='', wms_ref='', review_group='', reference_dates='',
            order_number=None, receipt_number='', sku='', claim_go_to='',
            candidate_count=None, candidate_ref='', precision_status=''):
        items.append({'kind': kind, 'category': category, 'number': number,
                      'date': date, 'claim_quantity': claim_qty, 'wms_quantity': wms_qty,
                      'stated_amount': amount, 'tracking': tracking, 'reason': reason,
                      'action': action, 'status': status,
                      'claim_ref': claim_ref, 'wms_ref': wms_ref,
                      'review_group': review_group, 'reference_dates': reference_dates,
                      'order_number':number if order_number is None else order_number,
                      'receipt_number':receipt_number, 'sku':sku,
                      'claim_go_to':claim_go_to, 'candidate_count':candidate_count,
                      'candidate_ref':candidate_ref, 'precision_status':precision_status})

    def evidence(refs):
        return '; '.join(Path(d['sources'].get(x.get('source'), {}).get('path', x.get('source', 'WMS'))).name + ' / ' + x.get('sheet', 'CSV') + '!' + str(x.get('row', x.get('csv_record', ''))) for x in refs)

    accepted_annotation_keys = set()
    if review and investigation:
        remaining = set(review['remaining_orders']) | set(review.get('annotation_pending_orders', []))
        accepted = set(review['annotated_base_orders'])
        for row in investigation['claim_candidates']:
            if row['kind'] != 'b2c':
                continue
            if row['order'] in accepted:
                for sample in row.get('precision_investigation', {}).get('annotation_samples', []):
                    accepted_annotation_keys.update((sample['date'], order) for order in sample.get('orders', []))
            if row['order'] not in remaining:
                continue
            claim_rows = row.get('claim_rows', [row['row']])
            reason = '대응 원본 WMS 미연결'
            bridge = row.get('optional_reference_bridge', {})
            reference_dates = ', '.join(sorted({x['date'] for x in bridge.get('comparisons', []) if x.get('date')}))
            group = 'order_unlinked'
            action = '청구된 주문번호가 맞는지, 이 택배비가 어떤 실제 발송에 대한 것인지'
            status = '발송 내용 미확인'
            if bridge.get('status') == 'reference_found_but_not_fully_linked':
                group = 'raw_wms_evidence_unresolved'
                reason = '내부자료에 출고 기록은 있으나 원본 WMS의 해당 출고를 확인하지 못함'
                action = '담당자 개별 재검색 요청 없음; 현재 자료로 원본 출고 근거 미확인 유지'
                status = '원본 근거 미확인 유지'
            elif row['order'] == 'DEMO 실사 리스트':
                group = 'inventory_list_dispatch'
                action = '‘DEMO 실사 리스트’ 두 송장으로 무엇을 보냈는지; 상품 출고인지 다른 업무 발송인지'
            elif '샘플' in row['order']:
                group = 'sample_dispatch'
                action = '실제 샘플 발송이 맞는지, WMS 상품 출고와 별도로 관리하는 발송인지'
            precision = row.get('precision_investigation', {})
            precision_status = precision.get('status', '')
            candidate_count = precision.get('joint_rounding_groups')
            candidate_refs = []
            for candidate in precision.get('joint_rounding_samples', []):
                # The diagnostic samples the July CSV; these are not confirmed links.
                path = investigation['sources']['outbound_july']['path']
                candidate_refs.append(Path(path).name + ' / CSV!' + ','.join(map(str,candidate['csv_rows']))
                                      + ' / 출고번호 ' + candidate['outbound'] + ' / ' + candidate['date'])
            if group=='order_unlinked' and precision_status in ('rounding_joint_ambiguous','rounding_joint_unique_candidate'):
                reason = f'축약된 주문·송장번호에 맞는 WMS 출고 후보 {num(candidate_count)}건; 원형 번호 연결 미확정'
                action = '청구 이동 범위·WMS 후보 위치로 원문 조회; 번호 정밀도 문제로 담당자 전수 재검색 요청 없음'
            add('claim_only_b2c', 'B2C 출고', row['order'], amount=row.get('invoice_stated_freight'),
                tracking=row.get('tracking', ''), reason=reason,
                action=action, status=status,
                claim_ref=Path(b['sources']['actual_claim']['path']).name + ' / ' + row['sheet'] + '!' + ','.join(map(str, claim_rows)),
                review_group=group, reference_dates=reference_dates,
                claim_go_to='; '.join(f'{row["sheet"]}!A{n}:D{n}' for n in claim_rows),
                candidate_count=candidate_count, candidate_ref='; '.join(candidate_refs), precision_status=precision_status)
    else:
        # When source-bound decisions cannot be reused, expose saved unmatched
        # rows without pretending to know their original identifiers.
        path = base/'B2C_실제청구_WMS_차이.csv'
        if path.is_file():
            with path.open(encoding='utf-8-sig', newline='') as stream:
                for row in csv.DictReader(stream):
                    if row['결과'] == 'WMS에서 미연결':
                        add('claim_only_b2c', 'B2C 출고', '', reason='저장된 직접 비교의 미연결 행; 최신 연결 결정 미적용',
                            action='원본·결정 버전 확인 후 연결 목록 갱신', status='근거 재검토',
                            claim_ref=row['청구시트'] + '!' + row['청구행'])

    for row in d['rows']:
        if row['kind'] not in ('claim_only', 'wms_only'):
            continue
        if row.get('show_as_separate_issue') is False or row['judgment'] == 'retained_for_review_by_user':
            continue
        # Other cost sheets with an approved quantity comparison already cover
        # these rows; a missing B2B-table row alone is not a review item.
        if row['judgment'].startswith('excluded_from_b2b_omission'):
            continue
        key = row['key']; inbound = row['scope'].startswith('inbound')
        category = '입고' if inbound else 'B2B 출고'
        number = ' / '.join(key[1:])
        if row['kind'] == 'claim_only':
            action = '이 주문이 실제 출고됐는지, 청구에만 적힌 이유가 무엇인지'
            reason = '청구의 대응 WMS 미연결'
        else:
            action = '조립한 완성품의 입고여서 청구에서 빠진 것인지' if inbound else '이 출고가 물류비 청구 대상인지'
            reason = 'WMS의 대응 청구 미연결'
        add(row['kind'], category, number, key[0], row['claim_quantity'], row['wms_quantity'],
            reason=reason, action=action,
            status='기존 확정불가 유지' if row['judgment']=='not_determinable_by_user' else '확인 대상',
            claim_ref=evidence(row['claim_evidence']), wms_ref=evidence(row['wms_evidence']),
            order_number='' if inbound else key[1], receipt_number=key[1] if inbound and len(key)>2 else '',
            sku=key[-1] if inbound else '전체(주문 합계)' if row['kind']=='wms_only' else '')

    direct_rows = {tuple(row['key']): row for row in d['rows']}
    for row in d.get('review_items', []):
        if tuple(row['key']) in accepted_annotation_keys:
            continue
        original = direct_rows.get(tuple(row['key']), {})
        add('wms_only', '출고 · ' + ', '.join(row['types']), row['key'][1], row['key'][0],
            wms_qty=row['quantity'], reason='WMS의 대응 청구 미연결',
            action='요금이 발생하지 않는 재고 이동인지' if '이동요청' in row['types'] else '이 기타출고가 물류비 청구 대상 작업인지',
            status='내역 유지·청구 여부 미확정', wms_ref=evidence(original.get('wms_evidence', [])),
            sku='전체(주문 합계)')

    for row in d.get('missing_claim_quantity', []):
        add('missing_quantity', 'B2B 재출고', row['key'][1], row['key'][0], amount=row['freight'],
            reason=f'청구 {num(row["box"])}BOX · 상품 수량 빈칸', action=f'운임을 청구한 {num(row["box"])}박스에 상품 몇 개를 재출고했는지',
            status='기존 확정불가 유지' if row['judgment']=='not_determinable_by_user' else '확인 대상',
            claim_ref=evidence([{'source':row['source'], 'sheet':row['sheet'], 'row':row['row']}]))
    return items


def staff_lookup_filter(item):
    return {'raw_wms_evidence_unresolved':'내부자료 있음·원본 WMS 미연결',
            'order_unlinked':'발송내역 미연결',
            'inventory_list_dispatch':'실사 리스트 발송',
            'sample_dispatch':'샘플 발송'}.get(item.get('review_group'), item['category'])


def write_staff_review(path, items):
    columns = [('lookup_filter','조회구분'),('category','구분'),('date','일자'),('order_number','주문번호'),
               ('receipt_number','입고예정번호'),('sku','품번'),('tracking','송장번호'),
               ('claim_ref','청구 위치'),('wms_ref','WMS 위치'),
               ('claim_quantity','청구 수량'),('wms_quantity','WMS 수량'),
               ('stated_amount','청구 기재금액(원)'),
               ('reason','미연결·대사 불가 사유'),('action','담당자 확인 항목'),
               ('status','현재 처리'),
               ('reference_dates','내부자료 출고일(원본 WMS 일자 아님)'),
               ('claim_go_to','청구 이동 범위'),('candidate_count','축약번호 기준 WMS 출고 후보 수'),
               ('candidate_ref','WMS 후보 위치(대표·연결 미확정)')]

    def value(item, key):
        if key=='lookup_filter':
            return staff_lookup_filter(item)
        if key=='receipt_number':
            return item[key] or ('미기재' if item['category']=='입고' else '해당없음')
        if key=='order_number':
            return item[key] or ('해당없음' if item['category']=='입고' else '미기재')
        if key in ('date','sku','tracking'):
            return item[key] or '미기재'
        if key in ('claim_ref','wms_ref'):
            return item[key] or '대응 미확인'
        return item[key]

    with path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.writer(stream); writer.writerow([label for _,label in columns])
        writer.writerows([[value(item,key) for key,_ in columns] for item in items])


def lookup_condition(values, sources, ref):
    """Name each source field and link every grouped row to an exact CSV filter."""
    def common(key):
        unique = list(dict.fromkeys(str(item[key]) if item[key] else '' for item in values))
        return unique[0] if len(unique)==1 else None

    location_values = []
    for item in values:
        references = item['claim_ref'] or item['wms_ref']
        for original in references.split('; '):
            if not original:
                continue
            filename, separator, position = original.rpartition(' / ')
            source = sources.get(filename)
            if source and separator:
                label = '청구서' if item['claim_ref'] else '입고 WMS' if item['category']=='입고' else '출고 WMS'
                relative = (Path('..')/Path(source).relative_to('data/demo_company')).as_posix()
                location_values.append(ref(label,relative)+' '+position)
            else:
                location_values.append(original)
    locations = list(dict.fromkeys(location_values))
    if len(locations)>2:
        location = '대표 위치: ' + locations[0]
    else:
        location = ', '.join(locations) if locations else '대응 미확인'
    inbound = all(item['category']=='입고' for item in values)
    origin = '청구' if all(item['claim_ref'] for item in values) else 'WMS'
    date_label = '입고일' if inbound else '출고일'
    date = common('date')
    sku = common('sku')
    number_key, number_label = ('receipt_number','입고예정번호') if inbound else ('order_number','주문번호')
    number = common(number_key)
    fields = [f'**{common("category")}**',
              f'{origin} {date_label}: ' + (date or '기재 없음' if date is not None else '대상 목록에서 확인'),
              f'{origin} {number_label}: ' + (number or '기재 없음' if number is not None else '대상 목록에서 확인'),
              f'{origin} 품번: ' + ('주문 전체 합계' if sku=='전체(주문 합계)' else sku or '기재 없음' if sku is not None else '대상 목록에서 확인')]
    if not inbound and number is None:
        fields[2] = f'{origin} 주문번호·송장번호: 대상 목록에서 확인'
    filters = list(dict.fromkeys(staff_lookup_filter(item) for item in values))
    if len(values)>1 or len(locations)>2:
        location += '<br>' + ref(f'대상 {num(len(values))}줄 조회', '담당자_확인목록.csv')
        location += '<br>CSV 필터 **조회구분 = ' + ' / '.join(filters) + '**'
        if len(values)==1:
            location += f', **{number_label} = {number}**'
    return '<br>'.join(fields), location


def connected_special_cases(d, review, investigation, restoration):
    """Explain adopted nonstandard comparisons, without approving new rules."""
    if not review or not review.get('source_bound_decisions_valid') or not investigation:
        return []
    cases = []

    def add(case_id, target, condition, status, evidence):
        cases.append({'id':case_id, 'target':target, 'applied_condition':condition,
                      'basis_status':status, 'evidence':evidence})

    accepted = set(review['direct_orders'] + review['reference_bridge_orders'] + review['annotated_base_orders'])
    order_differences = []
    for row in investigation['claim_candidates']:
        if row['kind'] != 'b2c' or row['order'] not in accepted:
            continue
        comparisons = row.get('optional_reference_bridge', {}).get('comparisons', [])
        if any(c.get('claim_order_equals_reference') is False for c in comparisons):
            order_differences.append(row)
    if order_differences:
        count = len({row['order'] for row in order_differences})
        sample = order_differences[0]
        add('different_order_same_tracking', f'청구 ↔ 내부자료 · {num(count)}주문',
            '주문번호는 다르지만 송장번호가 같아 내부자료의 발송 기록을 연결함',
            '프로그램의 송장 기준 비교. 회사 내부 연결 기준은 미확인; 주문번호를 같게 고치지 않음',
            [{'path':'match_investigation.json', 'claim_sheet':sample['sheet'], 'claim_row':sample['row'],
              'claim_order':sample['order'], 'tracking':sample.get('tracking'),
              'reference_comparisons':sample['optional_reference_bridge']['comparisons']}])
    if restoration and restoration['comparison']['claim_orders_reference_recovered']:
        count = restoration['comparison']['claim_orders_reference_recovered']
        add('evidence_recovered_identifier', f'내부자료 경유 원본 WMS 연결 · {num(count)}주문',
            '같은 출고일·출고번호·품번별 수량의 내부자료에서 전체 번호를 복원하니 청구와 연결됨',
            '이번 자료의 연결 방식은 사용자 확인. 보조자료 경유이며 상품 수량·운임 검증과 별개',
            [{'path':'numeric_key_reconciliation.json'}, {'path':'WMS_번호복원_근거.csv'}])
    accepted_notes = set(review['annotated_base_orders'])
    notes = [row for row in investigation['claim_candidates'] if row['kind']=='b2c' and row['order'] in accepted_notes]
    if notes:
        claim_rows = sorted({n for row in notes for n in row.get('claim_rows', [row['row']])})
        add('annotation_separated', f'청구 ↔ WMS · 메모 주문 {num(len(accepted_notes))}개 ({num(len(claim_rows))}행)',
            '‘추가 1박스·추가 송장·반품’ 설명을 분리한 기본 주문번호로 연결됨',
            '해당 주문은 사용자 확인. 원문 메모 보존; 추가 발송·운임 적정성은 미확인',
            [{'path':'match_investigation.json', 'claim_sheet':'B2C출고_택배', 'claim_rows':claim_rows}])
    inbound = [row for row in d['rows'] if row['scope']=='inbound_june' and row['kind']=='quantity_match']
    if inbound:
        add('inbound_date_sku_aggregate', f'6월 입고 · 일자·품번 기준 {num(len(inbound))}건',
            'WMS 입고예정번호가 없어 날짜+품번별로 합산해 비교하니 수량이 일치함',
            '집계 수량 비교. 같은 입고예정번호·개별 입고 연결을 확인한 것은 아님',
            [{'path':'direct_wms_result.json', 'scope':'inbound_june'}])
    for work in d.get('work_quantity_checks', []):
        if work['status'] != 'match':
            continue
        if work['kind']=='relabel' and work.get('wms_materials'):
            add('work_materials_separated_' + work['orders'][0], '라벨 작업 · ' + work['orders'][0],
                f'WMS 전체 {num(work["wms_total"])}개에서 자재 {num(work["wms_materials"])}개를 분리해 상품 작업수량 {num(work["claim_quantity"])}개와 비교하니 일치함',
                '사용자 확인된 상품 수량 기준. 자재·아웃박스 작업은 별도',
                [{'path':'direct_wms_result.json', 'orders':work['orders']}])
        if work['kind']=='assembly' and len(work['orders']) > 1:
            expression = ' + '.join(num(q) for q in work['quantities'])
            add('assembly_orders_aggregated_' + work['output_sku'], '정합 · ' + work['output_sku'],
                f'같은 날짜·완성품 번호의 두 주문을 합산({expression} = {num(work["claim_quantity"])}세트)하니 완성품 입고와 일치함',
                '사용자 확인된 합계 비교. 개별 주문과 입고번호 연결은 미확인',
                [{'path':'direct_wms_result.json', 'orders':work['orders']}])
        if work['kind']=='assembly' and 'amount_divided_by_unit_price' in work.get('quantity_origins', []):
            source = next(row for row in work['claim_rows'] if row['quantity'] is None)
            add('quantity_backcalculated_' + work['output_sku'], '정합 · ' + work['output_sku'],
                f'청구 수량이 빈칸이라 {num(source["amount"])}원 ÷ {num(source["unit_price"])}원 = {num(work["claim_quantity"])}세트로 역산해 입고와 비교하니 일치함',
                '역산 비교는 사용자 확인. 원본에 기재된 수량으로 취급하지 않음',
                [{'path':'direct_wms_result.json', 'claim_sheet':'임가공-라벨', 'claim_row':source['row']}])
    return cases


def special_case_lookups(d, review, investigation, restoration, cases):
    """Make a view of adopted exceptional matches, keeping source roles explicit."""
    if not cases:
        return [], 0
    by_id = {case['id']: case for case in cases}
    rows = []
    claim_path = d['sources']['claim']['path']
    reference_path = investigation.get('optional_reference_diagnostic', {}).get('source', {}).get('path', '')

    def positions(evidence):
        groups = defaultdict(list)
        for item in evidence:
            source = d['sources'][item['source']]['path']
            groups[(source, item.get('sheet', 'CSV'))].append(item.get('row', item.get('csv_record')))
        return '; '.join(f'{source} / {sheet}!{",".join(str(n) for n in sorted(set(numbers)))}'
                         for (source, sheet), numbers in groups.items())

    def add(case_id, category, date='미기재', order='', sku='전체(주문 합계)',
            claim='', reference='', wms='', reference_order='', tracking='',
            wms_date='', outbound=''):
        case = by_id[case_id]
        lookup_filter = {'different_order_same_tracking':'주문번호 상이·송장 연결',
                         'evidence_recovered_identifier':'번호 복원 연결',
                         'annotation_separated':'메모 분리 연결',
                         'inbound_date_sku_aggregate':'6월 입고 합산 비교'}.get(case_id)
        if lookup_filter is None:
            lookup_filter = ('상품·자재 분리 · ' + order if case_id.startswith('work_materials_separated_') else
                             '정합 주문 합산 · ' + sku if case_id.startswith('assembly_orders_aggregated_') else '정합 수량 역산 · ' + sku)
        case['lookup_filter'] = lookup_filter
        row = {'case_id': case_id, '조회구분': lookup_filter, '특이사항': case['target'], '구분': category,
               '청구 일자': date, '주문번호': order, '입고예정번호': '', '품번': sku,
               '송장번호': tracking, '내부자료 주문번호': reference_order,
               'WMS 출고번호': outbound, 'WMS 일자': wms_date,
               '청구 원본 위치': claim, '내부자료 원본 위치': reference, 'WMS 원본 위치': wms,
               '적용 조건': case['applied_condition'], '기준 상태': case['basis_status']}
        rows.append(row)
        if 'lookup_hint' not in case:
            def short(location):
                parts = []
                for part in location.split('; '):
                    if part:
                        sheet, numbers = part.split(' / ', 1)[-1].rsplit('!', 1)
                        parts.append(sheet + '!' + numbers.split(',')[0] + (' 등' if ',' in numbers else ''))
                return '; '.join(parts)
            key = ('주문번호: ' + order if order else ('청구 입고일: ' if category=='입고' else '청구 일자: ') + date)
            if sku != '전체(주문 합계)':
                key += ' · 품번: ' + sku
            case['lookup_hint'] = (key + '<br>청구 ' + short(claim)
                                   + (' ↔ 내부 ' + short(reference) if reference else '')
                                   + (' ↔ WMS ' + short(wms) if wms else ''))

    accepted = set(review['direct_orders'] + review['reference_bridge_orders'] + review['annotated_base_orders'])
    candidates = [r for r in investigation['claim_candidates'] if r['kind'] == 'b2c' and r['order'] in accepted]
    different_orders = set()
    for r in candidates:
        comparisons = [c for c in r.get('optional_reference_bridge', {}).get('comparisons', [])
                       if c.get('claim_order_equals_reference') is False]
        if comparisons:
            different_orders.add(r['order'])
            references = sorted({n for c in comparisons for n in c.get('reference_rows', [])})
            add('different_order_same_tracking', 'B2C 출고', order=r['order'],
                claim=f'{claim_path} / {r["sheet"]}!{r["row"]}',
                reference=f'{reference_path} / 총 LAW!{",".join(map(str, references))}',
                reference_order='; '.join(sorted({o for c in comparisons for o in c.get('reference_orders', [])})),
                tracking=r.get('tracking', ''))
    recovered_orders = set(restoration['comparison']['reference_recovered_orders']) if restoration else set()
    for r in restoration.get('claim_matches', []) if restoration else []:
        if r['claim_order'] not in recovered_orders:
            continue
        paths = r['paths'].get('reference_recovered', [])
        evidence = [{'source': 'outbound_july', 'csv_record': n} for p in paths for n in p['wms_csv_rows']]
        add('evidence_recovered_identifier', 'B2C 출고', order=r['claim_order'],
            claim=f'{claim_path} / B2C출고_택배!{r["claim_row"]}', wms=positions(evidence),
            tracking=r['claim_tracking'], wms_date='; '.join(sorted({p['date'] for p in paths})),
            outbound='; '.join(sorted({p['shipment'] for p in paths})))
    for r in candidates:
        if r['order'] not in set(review['annotated_base_orders']):
            continue
        samples = r.get('precision_investigation', {}).get('annotation_samples', [])
        evidence = [{'source': 'outbound_july', 'csv_record': n} for s in samples for n in s['csv_rows']]
        add('annotation_separated', 'B2C 출고', order=r['order'],
            claim=f'{claim_path} / {r["sheet"]}!{r["row"]}', wms=positions(evidence),
            tracking=r.get('tracking', ''), wms_date='; '.join(sorted({s['date'] for s in samples})),
            outbound='; '.join(sorted({s['outbound'] for s in samples})))
    for r in d['rows']:
        if r['scope'] == 'inbound_june' and r['kind'] == 'quantity_match':
            add('inbound_date_sku_aggregate', '입고', date=r['key'][0], sku=r['key'][1],
                claim=positions(r['claim_evidence']), wms=positions(r['wms_evidence']), wms_date=r['key'][0])
    for work in d.get('work_quantity_checks', []):
        if work['status'] != 'match':
            continue
        ids = []
        if work['kind'] == 'relabel' and work.get('wms_materials'):
            ids.append('work_materials_separated_' + work['orders'][0])
        if work['kind'] == 'assembly' and len(work['orders']) > 1:
            ids.append('assembly_orders_aggregated_' + work['output_sku'])
        if work['kind'] == 'assembly' and 'amount_divided_by_unit_price' in work.get('quantity_origins', []):
            ids.append('quantity_backcalculated_' + work['output_sku'])
        for case_id in ids:
            add(case_id, '임가공', order='; '.join(work['orders']),
                sku=work.get('output_sku', work['claim_rows'][0]['output_code']),
                claim=positions([r['evidence'] for r in work['claim_rows']]),
                wms=positions(work['wms_evidence']), wms_date=work['date'])
    for case in cases:
        count = sum(row['case_id'] == case['id'] for row in rows)
        case['lookup_count'] = count
        if count > 1:
            # Keep the report short; the CSV retains all evidence rows in each group.
            case['lookup_hint'] += ' (대표)'
    return rows, len(different_orders & recovered_orders)


def write_special_lookups(path, rows):
    fields = ['조회구분', '특이사항', '구분', '청구 일자', '주문번호', '입고예정번호', '품번', '송장번호',
              '내부자료 주문번호', 'WMS 출고번호', 'WMS 일자', '청구 원본 위치', '내부자료 원본 위치',
              'WMS 원본 위치', '적용 조건', '기준 상태']
    tmp = path.with_suffix('.csv.tmp')
    with tmp.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)


def special_case_summary(cases, overlap, ref, link, sources, reference_path):
    if not cases:
        return []
    overlap_note = '**유형별 건수에는 같은 주문이 중복 포함됩니다.**'
    if overlap:
        overlap_note += f' 송장 연결·번호 복원에는 같은 {num(overlap)}주문이 있어 두 수치를 더하면 중복 집계됩니다.'
    lines = ['### 연결됐지만 적용 기준을 볼 특이사항', '', overlap_note, '',
             '| 대상 | 대표 조회 조건·전체 목록 | 적용한 비교 | 기준 상태 |',
             '|---|---|---|---|']
    for case in cases:
        lookup = case.get('lookup_hint', '') + '<br>' + ref(f'전체 {num(case.get("lookup_count", 0))}건 조회', '연결특이사항_조회목록.csv')
        lookup += '<br>CSV 필터 **조회구분 = ' + case.get('lookup_filter', '') + '**'
        lines.append('| ' + ' | '.join(cell(value) for value in (case['target'],lookup,case['applied_condition'],case['basis_status'])) + ' |')
    lines += ['', '전체 대상: ' + ref('연결 특이사항 조회목록', '연결특이사항_조회목록.csv')
              + '. **조회구분**을 표에 적힌 값으로 필터하면 모든 대상의 **청구 일자·주문번호·품번·청구/WMS 원본 위치**를 볼 수 있습니다.', '',
              '파일: ' + ' · '.join([link('청구', sources['claim']['path']), *([link('내부자료', reference_path)] if reference_path else []),
                                    link('6월 입고 WMS', sources['inbound_h1']['path']),
                                    link('7월 출고 WMS', sources['outbound_july']['path']),
                                    link('7월 입고 WMS', sources['inbound_july']['path'])])]
    return lines + ['']


def cost_summary(root, base, ref):
    path = base/'cost_verification.json'
    if not path.is_file():
        return []
    cost = json.loads(path.read_text('utf-8'))
    if cost.get('input_inventory_sha256', '').lower() != digest(base/'full_invoice_result.json'):
        return ['', '**비용 검사 결과의 자료 버전이 달라 다시 계산해야 합니다.**', '']
    for source in cost['sources'].values():
        if digest(root/source['path']) != source['sha256'].lower():
            return ['', '**비용 검사 원본의 버전이 달라 저장 결과만 남아 있습니다.**', '']
    decision_path = root/'ia/config/demo_company_cost_decisions.json'
    if (digest(decision_path) if decision_path.is_file() else '') != (cost.get('cost_decisions_sha256') or '').lower():
        return ['', '**비용 검사 결정이 변경되어 다시 계산해야 합니다.**', '']
    lines = ['### 비용검사에서 확인할 사항', '',
             '| 항목·청구 위치 | 확인된 차이 | 처리 |', '|---|---|---|']
    for check in cost['checks']:
        if check['status'] != 'difference':
            continue
        evidence = check['evidence'][-1]
        if check['id'].startswith('cost.contract_rate.'):
            value = f'계약서 {num(check["expected"])}원 / 청구 {num(check["actual"])}원'
            approved_skip = cost.get('decisions_applied', {}).get('U25', {}).get('state') == 'confirmed_unresolvable_by_user'
            handling = '해당 월 적용 ' + ('확정 불가' if approved_skip else '결정 대기') + '(U25); 과다청구액 미확정'
        else:
            value = f'계산 {num(check["expected"])}원 / 기재 {num(check["actual"])}원 · 차이 {num(check["difference"])}원'
            handling = '원본 계산 차이 유지'
        lines.append('| ' + ' | '.join(cell(v) for v in (check['label'] + ' · ' + evidence, value, handling)) + ' |')
    skipped = [{'U25':'7월 계약단가 적용','U26':'실비·별도협의 금액 적정성'}.get(key,key) for key, decision in cost.get('decisions_applied', {}).items()
               if decision.get('state') == 'confirmed_unresolvable_by_user']
    lines += ['', ('**판정 보류:** ' + '·'.join(skipped) + '는 확정 불가 답변에 따라 해당 적정성 판정만 제외합니다. ' if skipped else '')
              + '실제 피킹·팩킹 작업 발생과 미등록 계약 조건의 확정 검사는 남아 있습니다.',
              '', ref('항목별 비용검사 결과', '비용검사_결과.csv'), '']
    billing=cost.get('billing_quantity',{})
    causes=billing.get('cause_analysis',{})
    if causes:
        lines+=['**BOX·PCS 차이 원인** — 기존 비교 차이는 유지하며, 과다청구로 확정하지 않습니다.','',
                '| 구분 | 대상 | 확인된 내용·처리 | 대표 원본 |','|---|---:|---|---|']
        labels={
            'inbound_distribution':('입고','상품 총수량은 같음. 상세 행의 박스·낱개 배분과 합산 후 환산 방식이 다름.'),
            'inbound_pack_basis':('입고','상품 총수량은 같음. 청구서와 Master의 박스당 수량이 다름.'),
            'inbound_work_pcs_unit':('입고','낱개수량과 작업 PCS가 100:1로 기재됨. '),
            'outbound_ea_difference':('B2B 출고','상품 총수량 차이도 있음. 기존 EA 차이와 함께 검토.'),
            'outbound_box_field_difference':('B2B 출고','박스 수와 BOX 작업량이 다름. '),
            'outbound_partial_sku_boxes':('B2B 출고','PCS는 같고 BOX 차이가 잔량 있는 품번 수와 같음. 잔량을 별도 박스로 센 것인지는 미확정.'),
            'outbound_other_work_difference':('B2B 출고','상품수량 환산과 청구 작업량이 다름. 실제 작업 기준·횟수로 설명되는지는 미확정.'),
            'unexplained':('입출고','기재 차이 유지. 원인 미확정.')}
        for category,(label,explanation) in labels.items():
            selected=[e for e in billing['rows'] if e.get('cause_analysis',{}).get('category')==category]
            if not selected:continue
            example=selected[0];cause=example['cause_analysis']
            if category=='inbound_work_pcs_unit':
                explanation+=('작업 PCS 적정성만 판정 제외(U33).' if cause.get('work_pcs_appropriateness')=='not_determinable_by_user' else '작업 PCS 기준 결정 대기(U33).')
            if category=='outbound_box_field_difference':
                explanation+=('반복 작업 적정성만 판정 제외(U34).' if cause.get('repeated_work_appropriateness')=='not_determinable_by_user' else '반복 작업 사유 결정 대기(U34).')
            n=cause.get('affected_work_rows',example['claim_rows'])[0]
            sample=' / '.join(example['key'])+' · '+example['claim_sheet']+'!'+str(n)
            if category=='outbound_box_field_difference':sample=' / '.join(e['key'][1]+' · '+e['claim_sheet']+'!'+str(e['claim_rows'][0]) for e in selected)
            lines.append('| '+' | '.join([label,str(len(selected))+'묶음',cell(explanation),cell(sample)])+' |')
        excluded=sum(e['status']=='not_determinable_by_user' for e in billing['rows'])
        lines+=['',f'환산 근거가 없는 {excluded}묶음은 기존 결정에 따라 해당 비교만 제외합니다. 빈 작업량은 0으로 처리하지 않습니다.',
                '',ref('원본 수량·차이·원인별 전체 목록','과금수량_검사.csv'),'']
    elif billing:
        lines+=['**BOX·PCS 잠정 비교** — 같은 일자·번호·품번을 합산한 환산값이며, 과다청구 확정은 아닙니다.','',
                '| 구분 | 남은 차이·근거 부족 | 대표 대상 |','|---|---|---|']
        for inbound,label in [(True,'입고'),(False,'B2B 출고')]:
            entries=[x for x in billing['rows'] if x['scope'].startswith('inbound')==inbound]
            differences=[x for x in entries if x['status'].endswith('scenario_difference')]
            unavailable=[x for x in entries if x['pending_ids'] or x['status']=='not_determinable_by_user']
            example=differences[0] if differences else None
            sample='없음'
            if example:
                sample=' / '.join(example['key'])+' · '+example['claim_sheet']+'!'+str(example['claim_rows'][0])
                labels={'L':'입고BOX' if inbound else '픽킹BOX','M':'입고PCS' if inbound else '픽킹PCS','N':'팩킹BOX','O':'팩킹PCS'}
                sample+=' · 청구−환산 '+', '.join(labels[c]+' '+('+' if Decimal(str(q))>0 else '')+num(q) for c,q in example['differences'].items() if q not in (None,0))
            lines.append('| '+label+' | 차이 '+str(len(differences))+'묶음 · 근거 부족 '+str(len(unavailable))+'묶음 | '+cell(sample)+' |')
        pack_differences=billing['claim_master_pack_differences']
        if pack_differences:
            e=pack_differences[0]
            lines.append('| 박스당 상품수량 | 청구와 Master가 다른 '+str(len(pack_differences))+'행 | '+cell(e['sku']+' · 입고리스트!'+str(e['row'])+' · 청구 '+num(e['claim_inner_quantity'])+' / Master '+num(e['master_outer_quantity'])+'개')+' |')
        lines+=['','청구 내품수량을 보조로 쓴 대상과 원본 위치는 '+ref('과금 수량 검사목록', '과금수량_검사.csv')+'에 표시했습니다. 보조 환산은 독립적인 포장 실적 확인이 아닙니다. 차이와 근거 부족은 같은 주문에 함께 있을 수 있습니다.', '']
    if cost['questions']:
        lines += ['**사용자 결정 대기:** ' + ' / '.join(q['id'] + ' '+q['category'] for q in cost['questions']), '']
    return lines


def compact_summary(d, f, b, review, investigation, ref, items):
    lines = []; groups = defaultdict(list)
    sources = {Path(source['path']).name:source['path'] for source in list(d['sources'].values()) + list(b['sources'].values()) if isinstance(source,dict) and source.get('path')}
    for item in items:
        groups[item['kind']].append(item)
    rows = []
    b2c = groups['claim_only_b2c']
    if b2c:
        amounts = [x['stated_amount'] for x in b2c]
        total = sum((Decimal(str(v)) for v in amounts), Decimal(0)) if all(v is not None for v in amounts) else None
        lines += [f'**B2C에서 원본 WMS로 발송을 확인하지 못한 청구: 택배비 {num(len(b2c))}줄 · {num(total)}원.**', '']
        b2c_groups = defaultdict(list)
        for item in b2c:
            b2c_groups[item['review_group']].append(item)
        for group, values in b2c_groups.items():
            group_amounts = [x['stated_amount'] for x in values]
            group_total = sum((Decimal(str(v)) for v in group_amounts), Decimal(0)) if all(v is not None for v in group_amounts) else None
            if group == 'raw_wms_evidence_unresolved':
                label = lookup_condition(values,sources,ref)
                dates = ', '.join(sorted({date for x in values for date in x['reference_dates'].split(', ') if date}))
                reason = f'내부자료에는 {dates} 출고 기록이 있으나 원본 WMS 미연결'
                action = '담당자 추가 판단 요청 없음. 원본 출고 근거 미확인 유지'
            else:
                label = lookup_condition(values,sources,ref)
                reason = '같은 주문·송장의 출고 근거를 찾지 못함'
                action = values[0]['action']
                if group=='order_unlinked':
                    ambiguous = sum(x['precision_status']=='rounding_joint_ambiguous' for x in values)
                    unique = sum(x['precision_status']=='rounding_joint_unique_candidate' for x in values)
                    if ambiguous+unique==len(values):
                        reason = f'번호 축약으로 출고 특정 불가: 복수 후보 {num(ambiguous)}줄 · 단일 후보 {num(unique)}줄(원형 번호 미확인)'
            rows.append([*label, f'기재 운임 {num(group_total)}원', reason, action])
    for item in items:
        if item['kind']=='claim_only_b2c':
            continue
        value = (f'청구 {num(item["claim_quantity"])}개 · WMS 미연결' if item['kind']=='claim_only'
                 else f'WMS {num(item["wms_quantity"])}개 · 청구 미연결' if item['kind']=='wms_only'
                 else item['reason'] + f' · 운임 {num(item["stated_amount"])}원')
        action = item['action'] + (' (기존 확정불가)' if item['status']=='기존 확정불가 유지' else '')
        rows.append([*lookup_condition([item],sources,ref), value, item['reason'], action])
    lines += ['**조회 방법:** 대상 목록을 열어 **조회구분** 열을 표의 값으로 필터하세요. B2C는 청구서를 연 뒤 **Ctrl+G**에 목록의 **청구 이동 범위**(예: `B2C출고_택배!A3:D3`)를 입력하면 됩니다. A열 주문번호·C열 송장번호·D열 운임을 확인하세요. ‘기재 없음’은 검색값으로 입력하지 않습니다.', '',
              '| 구분·조회 조건 | 원본 위치·전체 대상 | 금액·수량 | 왜 남았나요? | 무엇을 확인하나요? |',
              '|---|---|---|---|---|']
    lines += ['| '+' | '.join(cell(value) for value in row)+' |' for row in rows]
    if not rows:
        lines += ['| 전체 | — | 미연결 내역 없음 | — | — |']
    lines += ['', ref('미연결 건만 담은 담당자 확인목록', '담당자_확인목록.csv'), '']
    return lines


def refresh(root, saved_presentation=False):
    root = Path(root).resolve()
    base = root / 'data/demo_company/derived'
    if not all((base / name).is_file() for name in RESULTS):
        return None
    paths = [base / name for name in RESULTS]
    d, f, b = [json.loads(p.read_text('utf-8-sig')) for p in paths]
    investigation_path=base/'match_investigation.json'
    investigation=json.loads(investigation_path.read_text('utf-8')) if investigation_path.exists() else None
    decision_path=root/'ia/config/matching_review_decisions.json'
    decisions=json.loads(decision_path.read_text('utf-8')) if decision_path.exists() else {}
    effective_decision_hash=digest(decision_path) if decision_path.exists() else None
    workflow_path=base/'workflow_result.json';context_path=base/'case_execution_context.json'
    if workflow_path.is_file() and context_path.is_file():
        workflow=json.loads(workflow_path.read_text('utf-8-sig'))
        if workflow.get('case_execution_context_sha256'):
            if digest(context_path).upper()!=workflow['case_execution_context_sha256'].upper():
                raise ValueError('revision_report_context_changed')
            for name in ('direct_wms_result.json','full_invoice_result.json','b2c_actual_claim_wms.json','match_investigation.json'):
                if digest(base/name).upper()!=workflow['result_hashes'][name].upper():
                    raise ValueError('revision_report_result_changed')
            context=json.loads(context_path.read_text('utf-8-sig'))
            decisions=context['matching_decisions']
            effective_decision_hash=context['matching_decisions_sha256']
    review=None
    if investigation:
        sys.path.insert(0,str(Path(__file__).resolve().parent))
        from match_investigation import review_summary
        review=review_summary(investigation,decisions)
    restoration_path = base/'numeric_key_reconciliation.json'
    restoration = None
    restoration_error = None
    binding = b.get('identifier_restoration', {})
    if binding:
        if restoration_path.is_file() and digest(restoration_path).lower() == binding.get('result_sha256', '').lower():
            restoration = json.loads(restoration_path.read_text('utf-8'))
            if (restoration.get('period') != [b['period']['start'], b['period']['end']]
                    or not effective_decision_hash
                    or restoration.get('decision_sha256', '').lower() != effective_decision_hash.lower()):
                restoration_error = '번호 복원 결과의 기간·사용자 결정이 현재 기준과 다름'
            evidence = restoration.get('evidence_csv', {})
            evidence_path = (root/evidence.get('path', '')).resolve()
            if (not evidence_path.is_relative_to(base) or not evidence_path.is_file()
                    or digest(evidence_path).lower() != evidence.get('sha256', '').lower()):
                restoration_error = '번호 복원 행별 근거가 없거나 계산 당시와 다름'
        else:
            restoration_error = '번호 복원 결과가 실제 청구 대사에 연결된 버전과 다름'
    destination = root / 'data/demo_company/runs/가상센터/report.md'

    def link(label, path):
        path = Path(path)
        if not path.is_absolute():
            path = root / path
        return f'[{label}](<{Path(os.path.relpath(path, destination.parent)).as_posix()}>)'

    def ref(label, name):
        return link(label, base / name)

    lines = ['# DEMO COMPANY 가상센터 · 물류비 청구 검증 보고서', '',
             f'비교기간: **{d["period"]["start"]} ~ {d["period"]["end"]}**', '']
    if not f.get('full_invoice_validated', False):
        lines += [
            '**실제 비교해 일치한 항목은 상세에서 생략합니다.** 차이·미연결·미검증은 아래 표와 전체 대상 목록에 남기며, 잠정 계산과 사용자 지정 정상 분류는 하단 참고 영역에서 별도로 설명합니다.', '',
            '**전체 청구금액 적정성은 미확정입니다.** 주문 연결·청구 산술 일치만으로 수량·단가 적용까지 정상인 것은 아닙니다.', '']
    warnings = [restoration_error] if restoration_error else []
    snapshot_compatible = restoration_error is None
    source_refs = {}
    for doc in (d, f, b, investigation or {}, restoration or {}):
        for source in doc.get('sources', {}).values():
            if not isinstance(source, dict) or not source.get('sha256'):
                continue
            source_refs.setdefault(source['path'], set()).add(source['sha256'].lower())
    if investigation:
        optional_source=investigation.get('optional_reference_diagnostic',{}).get('source')
        if optional_source:source_refs.setdefault(optional_source['path'],set()).add(optional_source['sha256'].lower())
    for path, hashes in source_refs.items():
        source_path = (root / path).resolve()
        if not source_path.is_relative_to(root / 'data/demo_company/source'):
            raise ValueError('Source outside active company scope')
        if len(hashes) != 1 or not source_path.is_file() or digest(source_path) not in hashes:
            warnings.append(Path(path).name + ': 결과의 원본 버전과 현재 파일이 다름')
        if len(hashes) != 1:
            snapshot_compatible = False
    if len({(x['period']['start'], x['period']['end']) for x in (d, f, b)}) != 1:
        warnings.append('비교기간이 다른 결과가 포함되어 있음')
        snapshot_compatible = False
    if investigation and investigation['period'] != [d['period']['start'],d['period']['end']]:
        warnings.append('추가 연결 조사와 기존 비교기간이 다름')
        snapshot_compatible = False
    # Display-only refresh can keep the source-bound, saved decisions. It must
    # never present the saved snapshot as verification of changed current files.
    saved_snapshot = saved_presentation and snapshot_compatible
    if warnings and not saved_snapshot:
        review=None
        restoration=None
    if warnings:
        if saved_snapshot:
            lines += [f'**저장된 이전 대사 결과 기준.** 현재 원본 {num(len(warnings))}개 파일의 버전이 달라 이번에는 표시만 수정했습니다.', '']
        else:
            lines += ['**주의: 아래는 저장 당시 결과입니다. 자료 버전·기간이 달라 현재 자료의 통합 검증 완료로 볼 수 없습니다.**', '',
                      '| 재계산이 필요한 근거 |', '|---|'] + ['| ' + cell(x) + ' |' for x in warnings] + ['']

    display_investigation = investigation if not warnings or saved_snapshot else None
    items = staff_review_items(d, b, review, display_investigation, base)
    staff_csv = base/'담당자_확인목록.csv'
    write_staff_review(staff_csv, items)
    if f.get('internal_reference_used') is False:
        lines += ['**원본자료 기준 실행:** 내부 검증파일을 사용하지 않았습니다. 이전 내부자료 경유 연결은 이번 연결 건수에 포함하지 않습니다.', '']
    from verification_report_scope import inventory, save_inventory, summary_table, exception_tables, tentative_conversion_note, b2c_charge_note, expanded_cost_note, provisional_note, CSV_NAME, TENTATIVE_ACCEPTANCE, REFERENCE_GROUPS, visible_rows
    cost_path=base/'cost_verification.json'
    cost=json.loads(cost_path.read_text('utf-8')) if cost_path.is_file() else None
    if cost:
        valid=(cost.get('input_inventory_sha256','').lower()==digest(base/'full_invoice_result.json'))
        valid=valid and all((root/s['path']).is_file() and digest(root/s['path'])==s['sha256'].lower() for s in cost['sources'].values())
        cost_decision=root/'ia/config/demo_company_cost_decisions.json'
        valid=valid and (digest(cost_decision) if cost_decision.is_file() else '')==(cost.get('cost_decisions_sha256') or '').lower()
        if not valid:
            cost=None
            lines+=['**비용 검사 근거 버전 불일치:** 이전 비용 결과를 정상으로 표시하지 않습니다. 재계산이 필요합니다.','']
    if cost:
        from supporting_evidence import refresh as refresh_supporting
        cost['supporting_evidence']=refresh_supporting(root,cost,f)
    inspection_rows=inventory(d,f,display_investigation,review,cost,items)
    exceptions=save_inventory(base/CSV_NAME,inspection_rows)
    if cost and not warnings and workflow_path.is_file():
        from financial_summary import markdown_observation
        financial=json.loads(workflow_path.read_text('utf-8-sig')).get('financial_summary')
        if financial:
            lines += markdown_observation(financial).splitlines() + ['']
    lines += summary_table(inspection_rows)
    if not workflow_path.is_file() or not json.loads(workflow_path.read_text('utf-8-sig')).get('financial_summary'):
        lines += provisional_note(cost,ref)
    lines += ['**청구서:** '+link(Path(d['sources']['claim']['path']).name,d['sources']['claim']['path'])+'. 아래 청구내역·입고리스트·B2B출고·임가공-라벨은 이 파일의 시트입니다.','']
    lines += expanded_cost_note(cost,ref)
    lines += ['### 1. WMS 미연결 및 실적 확인 필요', '']
    lines += compact_summary(d, f, b, review, display_investigation, ref, items)
    special_cases = connected_special_cases(d, review, investigation, restoration)
    lookup_rows, overlap = special_case_lookups(d, review, investigation, restoration, special_cases)
    lookup_csv = base/'연결특이사항_조회목록.csv'
    write_special_lookups(lookup_csv, lookup_rows)
    reference_path = (investigation or {}).get('optional_reference_diagnostic', {}).get('source', {}).get('path', '')
    lines += exception_tables([r for r in inspection_rows if r['검사'] not in REFERENCE_GROUPS],ref)
    lines += tentative_conversion_note(inspection_rows,ref)
    lines += b2c_charge_note(cost,ref)
    # Preserve traceability of conditional connections without repeating normal rows.
    lines += ['**연결 기준 유의사항:** '+ref('전체 적용 대상·원본 위치','연결특이사항_조회목록.csv')+
              '에서 메모 분리·날짜/품번 합산·역산 기준을 확인할 수 있습니다. 승인된 비교 기준은 유지하며 개별 입고번호·추가 운임 적정성까지 확정한 것은 아닙니다.', '',
              '파일: '+ ' · '.join([link('청구',d['sources']['claim']['path']),
                  link('6월 입고 WMS',d['sources']['inbound_h1']['path']),
                  link('7월 출고 WMS',d['sources']['outbound_july']['path']),
                  link('7월 입고 WMS',d['sources']['inbound_july']['path'])]), '']
    if workflow_path.is_file():
        comparison=json.loads(workflow_path.read_text('utf-8-sig')).get('revision_comparison',{})
        if comparison.get('sha256'):
            from issue_revision import COMPARISON
            if digest(base/COMPARISON).upper()!=comparison['sha256'].upper():
                raise ValueError('revision_report_comparison_changed')
            if comparison.get('status')!='baseline_created':
                labels={'resolved':'해소','remaining':'차이 유지','new_issue':'새 문제',
                        'unverified':'검사 미완료·판정 제외','not_comparable':'비교 불가','not_observed':'이번 결과에 없음(해소 아님)'}
                summary=' · '.join(labels[k]+' '+num(v) for k,v in comparison.get('counts',{}).items() if v)
                lines += [ref('수정 전후 값·근거','수정전후_문제비교.csv'),'']
    provenance_path = base/'warehouse_report_provenance.json'
    old_provenance = json.loads(provenance_path.read_text('utf-8')) if provenance_path.is_file() else {}
    previous_report_hash = digest(destination) if destination.is_file() else None
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix('.md.tmp')
    tmp.write_text('\n'.join(lines), encoding='utf-8'); tmp.replace(destination)
    if previous_report_hash == digest(destination):
        previous_report_hash = old_provenance.get('previous_report_sha256', previous_report_hash)
    provenance = {'presentation_format': 'provisional_evidence_v17', 'previous_report_sha256': previous_report_hash,
                  'presentation_change': '유효한 청구액 잠정 인정 범위의 요약 산술·상세 산출내역 부재를 잠정 확정으로 통일. 원래 계산·실제 차이·미연결 보존',
                  'exception_inventory_sha256':digest(base/CSV_NAME), 'exception_rows':len(exceptions),
                  'inspection_counts':dict(Counter(r['결과'] for r in visible_rows(inspection_rows))),
                  'covered_summary_arithmetic':[{ 'check':r['계산근거'], 'detail_check':r['_covered_by_detail'], 'claim':r['청구위치']} for r in inspection_rows if r.get('_covered_by_detail')],
                  'calculation_counts':dict(Counter(r['계산결과'] for r in inspection_rows)),
                  'normal_by_user_counts':dict(Counter(r['검사'] for r in inspection_rows if r['정상처리근거']==TENTATIVE_ACCEPTANCE)),
                  'connected_special_cases':special_cases,
                  'special_lookup_csv_sha256': digest(lookup_csv), 'special_lookup_rows': len(lookup_rows),
                  'different_order_and_restoration_overlap': overlap,
                  'staff_review_csv_sha256': digest(staff_csv), 'staff_review_rows': len(items),
                  'calculation_rerun': False, 'result_hashes': {p.name: digest(p) for p in paths},
                  'saved_result_presentation': saved_snapshot, 'current_sources_verified': not warnings,
                  'source_warnings': warnings, 'renderer_sha256': digest(Path(__file__)), 'report_sha256': digest(destination)}
    if investigation:provenance['investigation_sha256']=digest(investigation_path)
    if effective_decision_hash:provenance['review_decisions_sha256']=effective_decision_hash
    if restoration:
        provenance['numeric_restoration_sha256']=digest(restoration_path)
        provenance['numeric_restoration_evidence_sha256']=restoration['evidence_csv']['sha256']
    if (base/'cost_verification.json').is_file():
        provenance['cost_verification_sha256'] = digest(base/'cost_verification.json')
    (base / 'warehouse_report_provenance.json').write_text(json.dumps(provenance, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return destination


if __name__ == '__main__':
    args = [arg for arg in sys.argv[1:] if arg != '--saved-presentation']
    result = refresh(args[0] if args else Path(__file__).resolve().parents[2],
                     saved_presentation='--saved-presentation' in sys.argv[1:])
    if result is None:
        raise SystemExit('Saved reconciliation results are incomplete')
    print(result)
