"""Invoice arithmetic, explicit formula links and evidenced contract comparisons."""
from collections import Counter
from decimal import Decimal
import io
import json
from pathlib import Path
import struct
import csv
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from portable_workbook import open_workbook
from pyxlsb import biff12
from pyxlsb.reader import BIFF12Reader
from invoice_tools import digest, column
from xlsb_scan import scan_rows
from identifier_tools import stored_number, billable_amount

CONTRACT = 'data/demo_company/source/contract.pdf'
CONTRACT_HASH = 'EB4934984744523DA1E858027707562EA2CA5CAA537DE905CB8D1A63A9318761'
RATES = {20:7,21:3,26:13,28:11,29:5,31:2,34:7,35:3,36:7,37:3,39:7,40:7,50:23,51:4,60:23,61:31,63:47}
ACTUAL_COST_ROWS = (69, 70, 71, 73, 78, 80, 83, 105, 108, 110, 116)


def dec(value):
    return stored_number(value)


def charged_checks(checks, sheets):
    """Only nonzero numeric billed amounts are obligations; keep negative credits."""
    for check in checks:
        identifier = check['id']
        if identifier.startswith('cost.work.'):
            amount = sheets['임가공-라벨'].get(int(identifier.rsplit('.',1)[1]),{}).get('G',{}).get('value')
        elif identifier.startswith(('cost.arithmetic.', 'cost.contract_rate.', 'cost.detail.', 'cost.credit.', 'cost.expanded_detail.', 'cost.expanded_amount.', 'cost.b2c_inverse.', 'cost.b2c_wms_', 'cost.b2c_parcel_summary.')):
            amount = sheets['청구내역'].get(int(identifier.rsplit('.',1)[1]),{}).get('N',{}).get('value')
        elif identifier.startswith('cost.b2c_parcel_rate.'):
            amount = sheets['B2C출고_택배'].get(int(identifier.rsplit('.',1)[1]),{}).get('J',{}).get('value')
        else:
            yield check
            continue
        if billable_amount(amount):
            yield check


def comparison(check_id, expected, actual, refs, label):
    available = expected is not None and actual is not None
    return {'id': check_id, 'label': label, 'expected': expected, 'actual': actual,
            'difference': actual - expected if available else None,
            'status': ('match' if expected == actual else 'difference') if available else 'data_unavailable',
            'check_performed': available, 'evidence': refs, 'currency': 'KRW',
            'overcharge_confirmed': False}


def coverage_performance(item):
    """Current performed scope for the coverage view, not legacy reader defaults."""
    n=item['row'];work=item['wms_cost_unit']
    if n in (20,21,34,35,36,37):return '상품 EA 대사·BOX/PCS 부분 비교'
    if n in (28,31):return 'WMS 포장수량·금액 잠정 비교'
    if n in (57,58):return '일부 작업상품·완성품 수량 대사'
    if n==50:return '주문번호 연결 확인 / 실제 검수량 판정 제외'
    if n in (79,111):return '작업 횟수 대상 아님 / 금액×요율 검사'
    if n in range(88,94):return '청구값 채택 / 실제 면적·기간 판정 제외'
    if work.get('status')=='not_implemented':return '기능 미구현'
    if work.get('status')=='awaiting_user_decision':return '사용자 결정 대기'
    return '자료 부족 / '+work.get('reason','대응 실적 근거 없음')


def product_formula(tokens):
    # MS-XLSB 2.5.98.16: PtgRef, PtgNum, PtgMul only. No arbitrary evaluation.
    # https://learn.microsoft.com/en-us/openspecs/office_file_formats/ms-xlsb/5d7c0c3f-f75f-4306-804f-6f2ebc6bf811
    operands = []
    pos = 0
    try:
        while pos < len(tokens) - 1:
            token = tokens[pos]
            if token in (0x24, 0x44, 0x64):
                row, col = struct.unpack_from('<IH', tokens, pos + 1)
                if row >= 1048576:
                    return None
                operands.append({'cell': column((col & 0x3fff) + 1) + str(row + 1)})
                pos += 7
            elif token == 0x1f:
                value = struct.unpack_from('<d', tokens, pos + 1)[0]
                if dec(value) is None:
                    return None
                operands.append({'constant': str(value)})
                pos += 9
            else:
                return None
        return operands if len(operands) == 2 and pos == len(tokens) - 1 and tokens[pos] == 0x05 else None
    except (struct.error, IndexError):
        return None


def formula_links(book, sheet):
    if hasattr(book,"xlsx_path"):
        from portable_workbook import formula_links as read
        return read(book,sheet)

    target = next(t for n, t in book._sheets if n == sheet)
    reader = BIFF12Reader(io.BytesIO(book._zf.read('xl/' + target)))
    row = 0
    out = {}
    try:
        while True:
            kind = reader.read_id()
            if kind is None:
                break
            size = reader.read_len()
            raw = reader._fp.read(size)
            if len(raw) != size:
                raise ValueError('truncated_formula')
            if kind == biff12.ROW:
                row = struct.unpack_from('<I', raw)[0] + 1
            elif kind == biff12.FORMULA_FLOAT and row in (79, 111):
                col = struct.unpack_from('<I', raw)[0] + 1
                if col == 14:
                    length = struct.unpack_from('<I', raw, 18)[0]
                    out[row] = product_formula(raw[22:22 + length])
    finally:
        reader.close()
    return out


def run(root):
    root = Path(root).resolve()
    base = root / 'data/demo_company/derived'
    cfg = json.loads((root / 'ia/config/demo_company_direct_wms.json').read_text('utf-8-sig'))
    decisions = json.loads((root / cfg['decisions_path']).read_text('utf-8-sig'))
    cost_decisions = root / 'ia/config/demo_company_cost_decisions.json'
    if cost_decisions.is_file():
        decisions.update(json.loads(cost_decisions.read_text('utf-8-sig')))
    claim = root / cfg['sources']['claim']['path']
    if digest(claim) != cfg['sources']['claim']['sha256'].upper():
        raise ValueError('cost_source_changed')
    full = json.loads((base / 'full_invoice_result.json').read_text('utf-8-sig'))
    if full['sources']['claim']['sha256'].upper() != digest(claim):
        raise ValueError('cost_inventory_source_mismatch')
    if digest(root / CONTRACT) != CONTRACT_HASH:
        raise ValueError('contract_version_changed')
    with open_workbook(str(claim)) as book:
        sheets = {name: dict(scan_rows(book, name)) for name in book.sheets if name != 'B2C RAW'}
        links = formula_links(book, '청구내역')
    summary = sheets['청구내역']
    charged_rows={n for n,c in summary.items() if billable_amount(c.get('N',{}).get('value')) and not c.get('N',{}).get('error')}
    checks = []
    items = []
    questions = []
    rate_decision = decisions.get('contract_rate_application', {})
    actual_decision = decisions.get('actual_cost_application', {})
    for source in full['claim_items']:
        row = source['row']
        if row not in charged_rows:continue
        cells = summary[row]
        values = {k: dec(cells.get(k, {}).get('value')) for k in ('L', 'M', 'N')}
        q, price, amount = values['M'], values['L'], values['N']
        expected = q * price if q is not None and price is not None else None
        check = comparison(f'cost.arithmetic.{row}', expected, amount,
                           [f'청구내역!L{row}:N{row}'], source['item_context'])
        check['basis'] = '청구 기재 수량×기재 단가; 실제 작업량 또는 계약 적용 확정과 구분'
        if row in (79, 111) and links.get(row):
            operands = []
            evidence = []
            for term in links[row]:
                if 'constant' in term:
                    operands.append(Decimal(term['constant']))
                else:
                    ref = term['cell']
                    col = ''.join(c for c in ref if c.isalpha())
                    number = int(ref[len(col):])
                    operands.append(dec(summary.get(number, {}).get(col, {}).get('value')))
                    evidence.append('청구내역!' + ref)
            expected = operands[0] * operands[1] if all(v is not None for v in operands) else None
            check = comparison(f'cost.arithmetic.{row}', expected, amount,
                               evidence + [f'청구내역!N{row}'], source['item_context'])
            check['basis'] = '원본 수식의 참조 셀·상수·곱셈을 읽어 재계산'
            check['formula_operands'] = links[row]
        checks.append(check)
        item = {'row': row, 'label': source['item_context'], 'unit': source['unit'],
                'arithmetic': check['status'], 'stated_amount': amount,
                'detail': source['detail_comparison'], 'contract_document': 'not_implemented',
                'contract_application': 'not_implemented',
                'wms_cost_unit': source['wms_cost_unit'], 'evidence': [f'청구내역!L{row}:N{row}']}
        if row in RATES:
            rc = comparison(f'cost.contract_rate.{row}', Decimal(RATES[row]), price,
                            [CONTRACT + '#page=6', f'청구내역!L{row}'], source['item_context'] + ' 기재 단가')
            rc['unit'] = 'KRW per stated cost unit'
            rc['applicability_confirmed'] = False
            checks.append(rc)
            item['contract_document'] = rc['status']
            item['contract_application'] = ('not_determinable_by_user' if rate_decision.get('state') == 'confirmed_unresolvable_by_user'
                                            else 'awaiting_user_decision')
        if row in ACTUAL_COST_ROWS and amount not in (None, 0):
            item['actual_cost_application'] = ('not_determinable_by_user' if actual_decision.get('state') == 'confirmed_unresolvable_by_user'
                                               else 'awaiting_user_decision')
        items.append(item)
    # These labels and subtotals are explicit in the invoice detail sheet.
    for row, labels in ((57, {'바코드덧방', '상세라벨덧방', 'QR라벨'}), (58, {'정합 기타'})):
        if row not in charged_rows:continue
        details = [(n, c) for n, c in sheets['임가공-라벨'].items() if c.get('A', {}).get('value') in labels]
        amounts = [dec(c.get('G', {}).get('value')) for n, c in details]
        expected = sum(amounts, Decimal(0)) if amounts and all(a is not None for a in amounts) else None
        chk = comparison(f'cost.detail.{row}', expected, dec(summary[row].get('N', {}).get('value')),
                         [f'임가공-라벨!G{n}' for n, _ in details] + [f'청구내역!N{row}'], '임가공 상세 합계')
        chk['basis'] = '원본 작업구분별 상세 작업비 합계; 협의단가 적정성 확정 아님'
        checks.append(chk)
        next(i for i in items if i['row'] == row)['detail'] = {'status': 'completed', 'result': chk['status'], 'check_id': chk['id']}
        for n, c in details:
            if not billable_amount(c.get('G',{}).get('value')):continue
            quantity, rate = dec(c.get('E', {}).get('value')), dec(c.get('F', {}).get('value'))
            work_check=comparison(f'cost.work.{n}', quantity * rate if quantity is not None and rate is not None else None,
                                  dec(c.get('G', {}).get('value')), [f'임가공-라벨!E{n}:G{n}'], '임가공 상세 산술')
            work_check['business_target']=['임가공-라벨',*[c.get(col,{}).get('value') for col in ('A','B','C','D')]]
            checks.append(work_check)
    credit = dec(sheets['차감항목'].get(3, {}).get('B', {}).get('value'))
    if 116 in charged_rows:
        checks.append(comparison('cost.credit.116', credit, dec(summary[116].get('N', {}).get('value')),
                                 ['차감항목!B3', '청구내역!N116'], '차감항목 문서 비교'))
    if rate_decision.get('state') != 'confirmed_unresolvable_by_user':
        questions.append({'id': 'U25', 'category': '계약단가 적용', 'text': '보유 계약서의 단가를 이번 7월 청구에 적용하는지 확정할 수 있나요?',
                          'choices': ['확정할 수 없음'], 'affected_rows': list(RATES), 'skip_scope': 'contract_applicability_only'})
    if actual_decision.get('state') != 'confirmed_unresolvable_by_user':
        questions.append({'id': 'U26', 'category': '실비·별도협의 적정성', 'text': '실비·별도협의 금액의 적정성을 판단할 내부 기준이 있나요?',
                          'choices': ['확정할 수 없음'], 'affected_rows': list(ACTUAL_COST_ROWS), 'skip_scope': 'actual_cost_appropriateness_only'})
    from billing_quantity_tools import run as billing_quantities, MASTERS, SUMMARY
    billing=billing_quantities(root,decisions)
    billing['summary_checks']=[c for c in billing['summary_checks'] if c['claim_row'] in charged_rows]
    questions.extend(billing['questions'])
    for item in items:
        if item['row'] in SUMMARY:
            detail=next(x for x in billing['summary_checks'] if x['claim_row']==item['row'])
            item['detail']={'status':'completed' if detail['status'] in ('match','difference') else 'data_unavailable',
                            'result':detail['status'],'check_id':'billing.summary.'+str(item['row'])}
            scopes=[x for x in billing['rows'] if x['scope'].startswith('inbound')==(item['row'] in (20,21))]
            item['wms_cost_unit']={'status':'partial','mode':'approved_provisional_conversion',
                                  'counts':dict(Counter(x['status'] for x in scopes)),
                                  'reason':'동일 단위 상품수량 유지·Master 잠정환산. 실제 작업발생·과금 확정은 별도.'}
    from b2c_charge_tools import build as b2c_charges, write_evidence as write_b2c
    b2c_input=json.loads((base/'b2c_actual_claim_wms.json').read_text('utf-8-sig'))
    policy=decisions.get('b2c_charges',{})
    from demo_company_case_runtime import verified_reuse
    if policy.get('source_hashes')!={k:cfg['sources'][k]['sha256'] for k in ('claim','outbound_july','outbound_h1')} and not verified_reuse(root,'U36',policy):
        raise ValueError('b2c_charge_policy_source_changed')
    if 'cost_observations' not in b2c_input:
        raise ValueError('b2c_cost_observations_require_source_recalculation')
    b2c=b2c_charges(summary,sheets['B2C출고_택배'],sheets['B2B출고'],b2c_input['cost_observations'],policy)
    checks.extend(b2c['checks'])
    for item in items:
        if item['row'] in (28,31):
            item['wms_cost_unit']={'status':'partial_scenario','reason':'선작업 포함 WMS로 수량·금액 잠정 비교 완료. 실제 포장·선작업 제외 범위는 미확정'}
            if b2c['packing']['prework_excluded']:
                item['wms_cost_unit']['reason']='원본 WMS의 (선포장) 수량 제외 후 주문·택배사·송장별 포장비 잠정 비교. 미확인 포장 조건의 승인과는 구분'
        if item['row'] in range(98,105):
            c=next((x for x in b2c['checks'] if x['id']=='cost.b2c_parcel_summary.'+str(item['row'])),None)
            if c is None:continue
            item['detail']={'status':'completed' if c['check_performed'] else 'data_unavailable','result':c['status'],'check_id':c['id']}
            item['wms_cost_unit']={'status':'data_unavailable','reason':'단가별 건수·금액 계산 완료. WMS에 택배 포장별 실측 중량·크기가 없어 요금 구간 적정성은 확인되지 않음'}
    write_b2c(base/'B2C_과금수량_근거.csv',b2c,cfg['sources'])
    # A new, unapproved data condition must remain pending, not silently skipped.
    unexpected=[r for r in b2c['packing']['rows'] if r['reason'] and r['basis']!='unresolved']
    if unexpected:
        questions.append({'id':'B2C_COST_INPUT','category':'B2C 포장 환산 예외','text':'B2C 포장 환산에서 승인되지 않은 단위·수량·집계 조건이 발견됐습니다.',
                          'choices':['확정할 수 없음'],'evidence_ref':'B2C_과금수량_근거.csv','reasons':dict(Counter(r['reason'] for r in unexpected))})
    from cost_detail_tools import build as expanded_details, inspection_orders, apply_evidence_gates, NOTES
    expanded, detail_updates = expanded_details(sheets, comparison, decisions, charged_rows)
    checks.extend(expanded)
    # Formula-based fees have no separate job quantity. Their referenced cost
    # and multiplication are already actually evaluated above.
    for row in (79,111):
        if row not in charged_rows:continue
        detail_updates[row]=next(c for c in checks if c['id']==f'cost.arithmetic.{row}')
    for item in items:
        row=item['row']
        if row in detail_updates:
            c=detail_updates[row]
            item['detail']={'status':'completed' if c['check_performed'] else 'data_unavailable',
                            'result':c['status'],'check_id':c['id'], 'expected':c['expected'],
                            'actual':c['actual'],'difference':c['difference'],
                            'unit':c.get('unit','원'), 'evidence':c['evidence'], 'reason':c.get('basis','원본 수식 비교')}
        # These are document rate tests, not proof that the July tariff applies.
        if row in RATES:
            item['contract_rule']={'source':CONTRACT+'#page=6','status':'implemented_document_rate',
                                   'rate':RATES[row],'applicability_decision':'U25'}
    storage=decisions.get('storage_area_unit',{})
    for key in ('storage_area_unit','olive_extra_work'):
        approved=decisions.get(key,{})
        if approved and approved.get('claim_sha256','').upper()!=digest(claim) and not verified_reuse(root,approved['id'],approved):
            raise ValueError('cost_item_decision_source_changed:'+key)
    for key in ('independent_work_evidence','independent_storage_evidence'):
        approved=decisions.get(key,{})
        if approved and approved.get('source_hashes')!={k:v['sha256'] for k,v in cfg['sources'].items()} and not verified_reuse(root,approved['id'],approved):
            raise ValueError('cost_item_decision_source_changed:'+key)
    if storage.get('mode')=='claim_1200_as_pyeong' and 88 in charged_rows:
        sc=comparison('cost.contract_rate.88',Decimal(37000),dec(summary[88].get('L',{}).get('value')),
                      [CONTRACT+'#page=6','청구내역!J88:N88'],'1층 보관비 단가 (U40 평 해석)')
        sc.update(unit='원/평·월',basis='U40: 청구 1,200을 평으로 해석. 원본 ㎡ 표기는 보존. 면적 실측·당월 계약 적용 확정 아님',applicability_confirmed=False)
        checks.append(sc)
        si=next(i for i in items if i['row']==88)
        si.update(contract_document=sc['status'],contract_application='not_determinable_by_user' if rate_decision.get('state')=='confirmed_unresolvable_by_user' else 'awaiting_user_decision',
                  contract_rule={'status':'implemented_document_rate','source':CONTRACT+'#page=6','rate':37000,'unit':'평','decision':'U40','source_unit':'㎡ / 월'})
    direct=json.loads((base/'direct_wms_result.json').read_text('utf-8-sig'))
    work_links=inspection_orders(sheets,direct,comparison)
    inspection=next((i for i in items if i['row']==50),None)
    if inspection is not None:inspection['wms_cost_unit']={
        'status':'partial','implemented_checks':['inspection_order_presence'],
        'counts':dict(Counter(x['status'] for x in work_links)),
        'reason':'인스펙션 주문번호를 WMS 원본 대사 결과에서 조회. 작업일과 출고일·검수 PLT와 상품 EA를 구분하며 실제 검수량 확정은 별도'}
    questions.extend(apply_evidence_gates(items,decisions,CONTRACT,ACTUAL_COST_ROWS))
    # Keep unresolved implementations explicit. Do not mark source limitations
    # as resolved merely because a document subtotal has been implemented.
    items=[i for i in items if billable_amount(i['stated_amount'])]
    checks=list(charged_checks(checks,sheets))
    b2c['checks']=list(charged_checks(b2c['checks'],sheets))
    b2c['counts']=dict(Counter(c['status'] for c in b2c['checks']))
    remaining={axis:[i['row'] for i in items if (i[axis].get('status') if isinstance(i[axis],dict) else i[axis])=='not_implemented']
               for axis in ('detail','wms_cost_unit','contract_application')}
    result = {'schema_version': '1.1', 'company_id': 'demo_company', 'warehouse_id': 'demo_center',
              'sources': {**cfg['sources'], 'contract': {'path': CONTRACT, 'sha256': CONTRACT_HASH}, **MASTERS},
              'input_inventory_sha256': digest(base / 'full_invoice_result.json'),
              'decision_sha256': digest(root / cfg['decisions_path']), 'code_sha256': digest(Path(__file__)),
              'cost_decisions_sha256': digest(cost_decisions) if cost_decisions.is_file() else None,
              'checks': checks, 'items': items, 'counts': dict(Counter(c['status'] for c in checks)), 'billing_quantity':billing, 'b2c_charge':b2c,
              'b2c_input_sha256':digest(base/'b2c_actual_claim_wms.json'),
              'expanded_cost_checks':{'checks':len(expanded),'detail_rows':sorted(detail_updates),
                  'inspection_orders':work_links,'document_notes':NOTES,
                  'contract_rate_rows':sorted(RATES),'remaining_rows':remaining,
                  'module_sha256':digest(Path(__file__).with_name('cost_detail_tools.py'))},
              'questions': questions, 'waiting_for_input': bool(questions),
              'processing_status': 'awaiting_user_decision' if questions else 'completed_with_limitations',
              'full_invoice_validated': False, 'llm_used': False,
              'remaining_implementation': [f'{axis}: '+','.join(map(str,rows)) for axis,rows in remaining.items() if rows],
              'decisions_applied': {'U25': rate_decision, 'U26': actual_decision,
                                   **{decisions[k]['id']:decisions[k] for k in ('storage_area_unit','olive_extra_work','independent_work_evidence','independent_storage_evidence') if k in decisions}}}
    path = base / 'cost_verification.json'
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + '\n', encoding='utf-8')
    with (base / '비용검사_결과.csv').open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['청구행', '항목', '단위', '기재금액', '산술', '상세비교', '계약기재단가', '계약적용', '실비적정성','WMS과금단위','상세비교근거','작업량근거','계약근거'])
        for i in items:
            writer.writerow([i['row'], i['label'], i['unit'], i['stated_amount'], i['arithmetic'], i['detail']['status'],
                             i['contract_document'], i['contract_application'], i.get('actual_cost_application', ''),i['wms_cost_unit']['status'],
                             i['detail'].get('reason',''),i['wms_cost_unit'].get('reason',''),i.get('contract_application_reason','')])
    # The existing coverage view points to the newly added checks; its original
    # calculation JSON remains an immutable input to this separate cost result.
    coverage_path = base/'청구전체_검사범위.csv'
    if coverage_path.is_file():
        with coverage_path.open(encoding='utf-8-sig', newline='') as f:
            reader = csv.DictReader(f)
            fields, coverage = reader.fieldnames, list(reader)
        index = {str(i['row']): i for i in items}
        coverage=[row for row in coverage if row['청구행'] in index]
        for row in coverage:
            item = index[row['청구행']]
            row['산술'] = '검사 완료' if item['arithmetic'] in ('match', 'difference') else '자료 부족'
            row['산술결과'] = item['arithmetic']
            row['상세집계'] = '검사 완료' if item['detail']['status'] == 'completed' else row['상세집계']
            if item['row'] in RATES:
                row['계약'] = '기재단가 비교 완료 / 적용 ' + ('확정 불가' if item['contract_application'] == 'not_determinable_by_user' else '결정 대기')
                row['남은 이유'] = '계약 기재단가 비교 근거: cost_verification.json; 적용 조건·WMS 과금 작업량은 별도. 사용자 결정 U25.'
            if item['row'] in ACTUAL_COST_ROWS:
                row['남은 이유'] = '실비·협의금액 적정성: ' + item.get('actual_cost_application', '미확정') + '; 가능한 문서/산술은 별도 비교. U26.'
            if item['row'] in (79, 111):
                row['남은 이유'] = '원본 수식의 참조금액×수수료 재계산 완료. 수수료율의 계약 적정성까지 확정한 것은 아님.'
            if item['row'] in SUMMARY:
                row['과금단위']='부분검사 — Master 잠정환산'
                row['남은 이유']='상세 합계 비교·가능한 BOX/PCS 잠정환산 수행. 근거 부족은 개별 결정 적용; 실제 작업발생과 계약 적용은 별도. cost_verification.json 참조.'
            if item['row'] in (28,31):
                row['과금단위']='부분검사 — WMS 포장수량·금액 잠정 비교'
                row['남은 이유']='금액÷단가 역산·WMS 출고 집계×단가 비교 수행. 선작업 포함 범위 차이·실제 포장 발생은 별도. U36~U39.'
            if item['row'] in range(98,105):
                row['과금단위']='단가별 합계 검사 완료 / WMS 포장 규격 부족'
                row['남은 이유']=item['wms_cost_unit']['reason']
            row['상세집계']={'completed':'검사 완료','awaiting_user_decision':'사용자 결정 대기','data_unavailable':'자료 부족'}.get(item['detail']['status'],'기능 미구현')
            row['계약']='기재 단가/실비·별도합의 근거 검사 / 당월 적용 확정불가' if item['contract_application']=='not_determinable_by_user' else '당월 적용 결정 대기'
            row['과금단위']=item['wms_cost_unit']['reason']
            row['상품/작업 실적']=coverage_performance(item)
            row['남은 이유']=item['detail'].get('reason','')+' / '+item.get('contract_application_reason','')
        with coverage_path.open('w', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(coverage)
    return result


if __name__ == '__main__':
    import sys
    result = run(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parents[2])
    print(json.dumps({'counts': result['counts'], 'questions': result['questions']}, ensure_ascii=False))
