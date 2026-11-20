"""Explicit same-invoice comparisons; never infer that a recorded job occurred.

Rules below are for the source-bound SF July layout. Numeric cells are summed
independently of saved subtotals/filters. Blank detail fields are not quantities.
"""
from decimal import Decimal
import re


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    v = Decimal(str(value))
    return v if v.is_finite() else None


def cell(sheets, sheet, row, col):
    c = sheets.get(sheet, {}).get(row, {}).get(col, {})
    return None if c.get('error') else number(c.get('value'))


def total(sheets, sheet, columns, first, last=None):
    if sheet not in sheets:
        return None, [sheet + ' (시트 없음)']
    end = last if last is not None else max(sheets[sheet], default=first)
    values = []
    for n, cells in sheets[sheet].items():
        if not first <= n <= end:
            continue
        for col in columns:
            c = cells.get(col, {})
            if c.get('error'):
                return None, [f'{sheet}!{col}{n}']
            v = c.get('value')
            if v is None:
                continue  # SUM semantics only; not a missing-work approval.
            q = number(v)
            if q is None:
                continue  # Excel SUM ignores text; never claims a missing job is zero.
            values.append(q)
    return sum(values, Decimal(0)), [f'{sheet}!{col}{first}:{col}{end}' for col in columns]


# Summary M cell's explicit detail references, verified in original XLSB tokens.
QUANTITIES = {
    22: [('입고 외 기타', 'G', 5, 18)],
    23: [('입고 외 기타', 'K', 5, 18)],
    24: [('입고 외 기타', 'JK', 5, 18)],
    25: [('입고 외 기타', 'H', 5, 18)],
    26: [('B2B출고', 'E', 4, None), ('B2C출고_용차', None, 5, 9)],
    39: [('B2C반품', 'F', 1, 1)],
    40: [('B2B 반품', 'K', 4, None)],
    41: [('B2B 반품', 'L', 4, None)],
    42: [('B2B 기타', 'BC', 25, 25)],
    43: [('B2B 기타', 'BC', 26, 26)],
    44: [('B2B 기타', 'BC', 27, 27)],
    45: [('B2B 기타', 'BC', 28, 28)],
    50: [('B2B 기타', 'C', 5, 16)],
    66: [('B2B 기타', 'E', 35, 40)],
    67: [('B2B 기타', 'C', 48, 51)],
    68: [('B2B 기타', 'D', 48, 51)],
    105: [('B2C출고_택배', 'I', 12, 12), ('반품리스트', 'Q', 11, 11)],
    106: [('B2C출고_택배', 'I', 13, 13)],
    107: [('B2C출고_택배', 'I', 14, 14)],
    108: [('B2C반품', 'J', 5, 11)],
}
NOTES = {
    23: '청구 라벨 교체 수량은 원본 수식이 참조하는 박스해체(K열) 합계와 비교. 작업명 대응 적정성은 미확정',
    24: '청구 PLT 재적치는 원본 수식의 파렛트교체(J열)+박스해체(K열)와 비교. 실제 재적치 작업 확정 아님',
    39: '반품 상세의 저장 집계 F1과 비교. 바른손 147박스와 개별 반품의 실제 BOX 근거까지 확인한 것은 아님',
}


def build(sheets, compare, decisions=None, charged_rows=None):
    checks, updates = [], {}
    daily = [chr(c) for c in range(ord('C'), ord('Z') + 1)] + ['AA','AB','AC','AD','AE','AF']
    for row, specs in QUANTITIES.items():
        if charged_rows is not None and row not in charged_rows:continue
        parts = [total(sheets, sh, cols or daily, start, end) for sh, cols, start, end in specs]
        expected = sum((p[0] for p in parts), Decimal(0)) if all(p[0] is not None for p in parts) else None
        refs = [ref for _, rr in parts for ref in rr] + [f'청구내역!M{row}']
        check = compare(f'cost.expanded_detail.{row}', expected, cell(sheets,'청구내역',row,'M'), refs, '청구 요약수량과 원본 상세 합계')
        check.update(unit=str(sheets['청구내역'][row].get('J',{}).get('value') or '건'),
                     basis=NOTES.get(row,'원본 상세의 명시된 수량 합계; 실제 작업·계약 적용 승인과 구분'),
                     report_group='요약·상세 집계', dimension='quantity', claim_row=row)
        checks.append(check)
        updates[row] = check
    for row, specs in {
        50:[('B2B 기타','D',5,16)],
        105:[('B2C출고_택배','J',12,12)],106:[('B2C출고_택배','J',13,13)],
        107:[('B2C출고_택배','J',14,14)],108:[('B2C반품','E',3,None)],
        110:[('B2B출고','T',4,4),('B2C출고_용차','E',14,None)],
        116:[('차감항목','B',3,3)],
    }.items():
        if charged_rows is not None and row not in charged_rows:continue
        parts=[total(sheets,*s) for s in specs]
        expected=sum((p[0] for p in parts),Decimal(0)) if all(p[0] is not None for p in parts) else None
        check=compare(f'cost.expanded_amount.{row}',expected,cell(sheets,'청구내역',row,'N'),
                      [r for _,rr in parts for r in rr]+[f'청구내역!N{row}'],'청구 요약금액과 원본 상세 합계')
        check.update(basis='청구 문서 내 상세금액 합산; 실비 발생·협의금액 적정성은 별도',
                     dimension='amount',claim_row=row)
        checks.append(check)
        if row not in updates: updates[row]=check
    # Source formula M54 contains constants as well as references. Retain them;
    # their unexplained work origin is not resolved by a correct formula result.
    if charged_rows is None or 54 in charged_rows:
        s=sheets.get('B2C출고',{});refs=[f'B2C출고!{c}11' for c in 'CDEFH']
        values=[cell(sheets,'B2C출고',11,c) for c in 'CDEFH']
        expected=sum(values,Decimal(0))+Decimal(21+13+2) if all(v is not None for v in values) else None
        check=compare('cost.expanded_detail.54',expected,cell(sheets,'청구내역',54,'M'),refs+['청구내역!M54'],'송장 추가부착 참조 합계')
        check.update(unit='BOX',dimension='quantity',claim_row=54,report_group='요약·상세 집계',
                     basis='원본 수식: C11:F11+H11+21+13+2. 상수 36건의 작업 근거는 미확정',
                     unexplained_constants=[21,13,2])
        checks.append(check);updates[54]=check
    # Shoring counts are explicitly the two container-size summary rows.
    if charged_rows is None or 63 in charged_rows:
        check=compare('cost.expanded_detail.63',sum((cell(sheets,'청구내역',r,'M') for r in (64,65)),Decimal(0))
                      if all(cell(sheets,'청구내역',r,'M') is not None for r in (64,65)) else None,
                      cell(sheets,'청구내역',63,'M'),['청구내역!M64:M65','청구내역!M63'],'쇼링·컨테이너 수량 합계')
        check.update(unit='컨테이너',dimension='quantity',claim_row=63,report_group='요약·상세 집계',
                     basis='원본 수식 M64+M65와 비교; 실제 컨테이너 작업 횟수 확정 아님')
        checks.append(check);updates[63]=check
    # Read explicitly named work in the invoice's remarks. This is a
    # same-document count, not an independent proof of physical work.
    selections={r:[] for r in (60,61,62,64,65)}
    olive=[]
    for n,c in sheets.get('B2B출고',{}).items():
        if n<4:continue
        memo=str(c.get('K',{}).get('value') or '')
        pallet=cell(sheets,'B2B출고',n,'E')
        if 'AJ PLT' in memo:selections[60].append((n,pallet))
        if '밴딩' in memo:selections[62 if '#' in memo else 61].append((n,pallet))
        for size,count in re.findall(r"(20|40)\s*'\s*[xX]\s*(\d+)",memo):
            selections[64 if size=='20' else 65].append((n,Decimal(count)))
        if '가상채널A' in str(c.get('C',{}).get('value') or ''):
            olive.append((n,cell(sheets,'B2B출고',n,'F')))
    if (decisions or {}).get('olive_extra_work',{}).get('mode')=='one_work_per_box':
        selections[52]=olive
    for row,values in selections.items():
        if charged_rows is not None and row not in charged_rows:continue
        # Blank pallet cells cannot be inferred from product quantity.
        expected=sum((v for _,v in values if v is not None),Decimal(0))
        refs=[f'B2B출고!C{n}:K{n}' for n,_ in values]+[f'청구내역!M{row}']
        check=compare(f'cost.expanded_detail.{row}',expected,cell(sheets,'청구내역',row,'M'),refs,
                      '원본 거래처·작업비고 수량 집계')
        check.update(unit='BOX(잠정 작업 1건)' if row==52 else 'PLT' if row<64 else '컨테이너',
                     dimension='quantity',claim_row=row,report_group='요약·상세 집계',
                     unvalued_rows=[n for n,v in values if v is None],
                     basis='U41: 가상채널A 출고 BOX당 작업 1건 잠정 비교' if row==52 else
                           '비고에 명시된 AJ PLT·일반/# 밴딩은 PLT수 합계, 20/40피트 컨테이너는 명시 횟수 합계; 실발생 승인 아님')
        checks.append(check);updates[row]=check
    return checks, updates


def inspection_orders(sheets, direct, compare):
    """Exact work-date/order presence only, never equate EA with inspection PLT."""
    from datetime import datetime,timedelta
    index={}
    for record in direct.get('rows',[]):
        if record.get('scope','').startswith('inbound') or not record.get('wms_present'):continue
        k=record['key'];index.setdefault(k[1],[]).extend(record.get('wms_evidence',[]))
    out=[]
    for n,c in sheets.get('B2B 기타',{}).items():
        if not 5<=n<=16 or not c.get('B',{}).get('value'):continue
        date=number(c.get('A',{}).get('value'));order=str(c['B']['value'])
        day=(datetime(1899,12,30)+timedelta(days=float(date))).date().isoformat() if date is not None else None
        evidence=index.get(order,[])
        out.append({'sheet':'B2B 기타','row':n,'date':day,'order':order,
                    'claimed_pallets':cell(sheets,'B2B 기타',n,'C'),
                    'status':'match' if evidence else 'not_found_in_existing_comparison',
                    'wms_evidence':evidence,'physical_work_verified':False,
                    'reason':'같은 주문번호의 출고 존재만 확인. 작업일·출고일은 별개이며 WMS EA를 검수 PLT로 변환하지 않음'})
    return out


# Inspected July original layout: these rows have no independent itemized
# monetary/area basis in its detail tabs. The original values remain untouched.
NO_DETAIL_ROWS={27,32,33,38,53,55,56,59,69,70,71,72,73,78,80,81,82,83,88,89,90,91,92,93,109,117}
STORAGE_ROWS=set(range(88,94))
WORK_ROWS={20,21,22,23,24,25,26,27,28,29,30,31,32,33,34,35,36,37,38,39,40,41,
           42,43,44,45,50,51,52,53,54,55,56,57,58,59,60,61,62,63,64,65,66,67,68,81,82}


def apply_evidence_gates(items, decisions, contract, actual_cost_rows):
    """Execute evidence prerequisites, retaining pending decisions per check.

    This does NOT turn a document match into proof of a physical job. The
    independent job-log and occupied-area roles have no files in the registered
    original inputs. Extra evidence would need explicit registration/adaptation.
    """
    work=decisions.get('independent_work_evidence',{}).get('state')=='confirmed_unresolvable_by_user'
    area=decisions.get('independent_storage_evidence',{}).get('state')=='confirmed_unresolvable_by_user'
    tariff=decisions.get('contract_rate_application',{}).get('state')=='confirmed_unresolvable_by_user'
    actual=decisions.get('actual_cost_application',{}).get('state')=='confirmed_unresolvable_by_user'
    questions=[]
    if not work:
        questions.append({'id':'U42','category':'독립 작업실적 부재','text':'검수·상하차·포장·임가공 등 독립 작업일지가 없어 실제 작업 횟수를 확정할 수 있나요?',
                          'choices':['확정할 수 없음'],'affected_rows':sorted(WORK_ROWS),'skip_scope':'independent_work_occurrence_only'})
    if not area:
        questions.append({'id':'U43','category':'보관면적·기간 근거','text':'보관비 88~93행의 실제 사용면적·기간 적정성을 확정할 수 있나요?',
                          'choices':['확정할 수 없음'],'affected_rows':sorted(STORAGE_ROWS),'skip_scope':'occupied_area_period_only'})
    for i in items:
        n=i['row']
        if n in NO_DETAIL_ROWS:
            decision='U43' if n in STORAGE_ROWS else 'U26' if n in actual_cost_rows else 'U42'
            approved=area if decision=='U43' else actual if decision=='U26' else work
            i['detail']={'status':'data_unavailable' if approved else 'awaiting_user_decision',
                         'result':None,'decision':decision,'rule_implemented':True,
                         'reason':'청구 요약 기재값만 있고 대응 세부 산출내역 없음'+
                           ('; 실제 사용면적·기간 근거 없음' if n in STORAGE_ROWS else ''),
                         'evidence':[f'청구내역!L{n}:N{n}'], 'required_evidence':
                           ['층별 실제 사용면적','적용기간'] if n in STORAGE_ROWS else ['항목별 상세 산출내역']}
        old=i['wms_cost_unit']
        # Preserve available original WMS conversion/packing observations.
        if n in WORK_ROWS:
            i['wms_cost_unit']={**old,'status':'data_unavailable' if work else 'awaiting_user_decision',
                               'rule_implemented':True,'available_comparison':old,
                               'reason':'청구 상세·승인 잠정 수량은 비교. 원본 WMS 상품 입출고에는 실제 작업 횟수·검수량 기록이 없음',
                               'required_evidence':['실제 작업유형·수량·단위 기록'],'decision':'U42'}
        elif n in STORAGE_ROWS:
            i['wms_cost_unit']={'status':'data_unavailable' if area else 'awaiting_user_decision',
                               'rule_implemented':True,'required_evidence':['층별 실제 사용면적','적용기간'],
                               'reason':'입출고 EA로 층별 실제 사용면적·기간을 확인할 수 없음','decision':'U43'}
            if decisions.get('independent_storage_evidence',{}).get('accept_as_invoiced_by_user'):
                i['user_acceptance']={'decision':'U43','status':'accepted_as_invoiced_by_user',
                                      'amount':i['stated_amount'],'independent_area_period_verified':False,
                                      'arithmetic_required':True}
        elif n in (79,111):
            i['wms_cost_unit']={'status':'data_unavailable','rule_implemented':True,
                               'reason':'수수료 산술은 비교했지만 기초비용의 실제 발생·실비 적정성은 U26 확정불가',
                               'check_id':f'cost.arithmetic.{n}'}
        elif n in actual_cost_rows or n in (72,98,99,100,101,102,103,104,106,107,109,117):
            i['wms_cost_unit']={**old,'status':'data_unavailable','rule_implemented':True,
                               'reason':old.get('reason') if old.get('status')=='data_unavailable' else
                                        '실비·협의금액 또는 운송 규격의 독립 근거 없음; 기재금액·계산 결과 유지',
                               'decision':'U26' if n in actual_cost_rows else 'U25'}
        # Every item is routed to a specific contract evidence type. The
        # signed tariff explicitly leaves unlisted services to agreement.
        if 'contract_rule' not in i:
            kind='at_cost' if n in (*range(98,111),78,81,82) else 'separate_agreement'
            i['contract_rule']={'status':'implemented_evidence_requirement','source':contract+'#page=6',
                                'type':kind,'required_evidence':['해당 월 실비 근거'] if kind=='at_cost' else ['해당 업무의 적용 단가·별도 합의'],
                                'exact_item_rate_found':False}
            i['contract_document']='not_fixed_rate_in_contract'
        i['contract_application']='not_determinable_by_user' if tariff else 'awaiting_user_decision'
        i['contract_application_reason']='U25: 7월 적용 단가 확정불가. '+(
            '계약 기재 단가 비교는 별도 수행' if i['contract_rule']['status']=='implemented_document_rate' else
            '보유 계약에는 해당 항목의 적용 가능한 확정 단가 대신 실비·별도합의 근거가 필요')
    return questions
