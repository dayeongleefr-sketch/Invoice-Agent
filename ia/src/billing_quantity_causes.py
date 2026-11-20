"""Explain recorded quantity differences without adopting alternative billing rules."""
from collections import Counter
from decimal import Decimal
import hashlib
import json


LABELS = {
    'inbound_distribution': '상세 행의 박스·낱개 배분과 합산 환산 차이',
    'inbound_pack_basis': '청구 내품수량과 Master 차이',
    'inbound_work_pcs_unit': '낱개수량과 작업 PCS의 단위 미확정',
    'outbound_ea_difference': '청구·WMS 상품 총수량 차이 동반',
    'outbound_box_field_difference': '청구 박스 수와 BOX 작업량 기재 차이',
    'outbound_partial_sku_boxes': 'BOX 차이가 잔량 있는 품번 수와 같음',
    'outbound_other_work_difference': '상품수량 환산만으로 작업량 차이를 설명하지 못함',
    'unexplained': '추가 원인 미확정',
}


def number(value):
    if value is None or isinstance(value, bool) or value == '':
        return None
    try:
        v = Decimal(str(value))
        return v if v.is_finite() else None
    except Exception:
        return None


def total(values):
    values = list(values)
    return sum(values, Decimal(0)) if values and all(v is not None for v in values) else None


def inbound_cause(entry, cells):
    expected = {k: number(v) for k, v in entry['expected_work'].items()}
    detail = [{k: number(c.get(k, {}).get('value')) for k in 'HIJKLM'} for c in cells]
    complete = all(all(v is not None for v in c.values()) for c in detail)
    valid = complete and all(c['H'] == c['I'] * c['J'] + c['K'] for c in detail)
    equal_ea = number(entry['claim_EA']) == number(entry['wms_EA'])
    same_pack = {c['J'] for c in detail} == {number(p['outer_EA']) for p in entry['wms_parts']}
    boxes = total(c['I'] for c in detail)
    pcs = total(c['K'] for c in detail)
    claimed = {k: number(v) for k, v in entry['claimed_work'].items()}
    result = {'category': 'unexplained', 'fact': '기재 수량과 환산 차이 유지',
              'detail_formula_holds': valid, 'same_EA': equal_ea,
              'recorded_detail': {'BOX': boxes, 'PCS': pcs},
              'source_cells': [dict(row=n, **c) for n, c in zip(entry['claim_rows'], detail)],
              'work_minus_detail': {k: claimed[k] - v if claimed[k] is not None and v is not None else None
                                    for k, v in [('L', boxes), ('M', pcs)]},
              'detail_minus_approved_conversion': {k: v - expected[k] if v is not None else None
                                                  for k, v in [('L', boxes), ('M', pcs)]}}
    if valid and equal_ea and all(c['L'] == c['I'] and c['M'] == c['K'] for c in detail):
        if same_pack:
            result.update(category='inbound_distribution',
                          fact='상품 총수량·박스당 수량은 같음. 청구 상세의 박스·낱개 합계와 합산 후 완박스·잔량 환산이 다름.')
        else:
            result.update(category='inbound_pack_basis',
                          fact='상품 총수량은 같으나 청구 내품수량과 Master 아웃박스수량이 다름. 상세 행 배분 영향이 함께 있을 수 있음.')
    elif valid and equal_ea and same_pack and boxes == expected['L'] and pcs == expected['M']:
        changed = [c for c in detail if c['M'] != c['K']]
        if changed and all(c['L'] == c['I'] for c in detail) and all(c['K'] > 0 and c['M'] * 100 == c['K'] for c in changed):
            result.update(category='inbound_work_pcs_unit',
                          fact='총 EA·BOX는 같음. 원문 낱개수량과 작업 PCS가 100:1로 기재됨. 해당 비율을 검증 규칙으로 적용하지 않음.',
                          affected_work_rows=[n for n, c in zip(entry['claim_rows'], detail) if c['M'] != c['K']])
    return result


def outbound_cause(entry, cells):
    detail = [dict(row=n, **{k: number(c.get(k, {}).get('value')) for k in 'FGLMNO'})
              for n, c in zip(entry['claim_rows'], cells)]
    claimed = {k: number(v) for k, v in entry['claimed_work'].items()}
    diff = {k: number(v) for k, v in entry['differences'].items()}
    boxes = total(c['F'] for c in detail)
    remainder_skus = sum(number(p.get('PCS')) not in (None, Decimal(0)) for p in entry['wms_parts'])
    equal_ea = number(entry['claim_EA']) == number(entry['wms_EA'])
    result = {'category': 'outbound_other_work_difference', 'same_EA': equal_ea,
              'source_cells': detail, 'recorded_boxes': boxes,
              'box_work_equals_recorded_boxes': boxes is not None and claimed['L'] is not None and boxes == claimed['L'],
              'remainder_sku_count': remainder_skus,
              'packing_repeats_picking': all(claimed.get(c) is not None for c in 'LMNO') and claimed['N'] == claimed['L'] and claimed['O'] == claimed['M'],
              'fact': '청구 작업량과 상품수량 기반 환산이 다름. 실제 작업 단위·횟수는 원본 수량만으로 확정하지 않음.'}
    if not equal_ea:
        result.update(category='outbound_ea_difference', fact='청구·WMS 상품 총수량도 다름. 소모품·수량 차이를 작업량 차이의 확정 원인으로 확대하지 않음.')
    elif boxes is not None and claimed['L'] is not None and claimed['L'] != boxes:
        result.update(category='outbound_box_field_difference', fact='청구서 안의 박스 수(F열)와 BOX 작업량(L열)이 다름. 작업 횟수나 기재 오류인지는 미확정.')
    elif remainder_skus > 0 and diff.get('L') == remainder_skus and diff.get('M') == 0 and all(diff.get(c) in (None, diff['L'] if c == 'N' else Decimal(0)) for c in 'NO'):
        result.update(category='outbound_partial_sku_boxes', fact='PCS 잔량은 같고 BOX 차이가 잔량 있는 품번 수와 같음. 잔량 박스 별도 계수는 후보이며 현재 완박스 기준을 바꾸지 않음.')
    return result


def annotate(rows, sheets, sources, masters, decisions, root=None):
    affected = []
    for entry in rows:
        if not entry['status'].endswith('scenario_difference'):
            continue
        cells = [sheets[entry['claim_sheet']][n] for n in entry['claim_rows']]
        cause = inbound_cause(entry, cells) if entry['scope'].startswith('inbound') else outbound_cause(entry, cells)
        cause.update(label=LABELS[cause['category']], comparison_changed=False, overcharge_confirmed=False)
        entry['cause_analysis'] = cause
        entry['cause_candidates'] = [cause['fact']]
        if cause['category'] == 'inbound_work_pcs_unit':
            affected.append({'scope':entry['scope'], 'key':entry['key'], 'rows':cause['affected_work_rows'],
                             'values':cause['source_cells']})
    fingerprint = hashlib.sha256(json.dumps({'sources':sources,'masters':masters,'affected':affected},
                                           ensure_ascii=False,sort_keys=True,default=str).encode()).hexdigest()
    decision = decisions.get('inbound_work_pcs_unit', {})
    decided = decision.get('state') == 'confirmed_unresolvable_by_user' and decision.get('scope_fingerprint') == fingerprint
    if not decided and root is not None and decision.get('state')=='confirmed_unresolvable_by_user':
        from demo_company_case_runtime import verified_reuse
        decided=verified_reuse(root,'U33',decision)
    questions = []
    if affected and not decided:
        questions.append({'id':'U33','category':'입고 작업 PCS 단위','skip_scope':'inbound_work_pcs_appropriateness_only',
                          'scope_fingerprint':fingerprint,'affected_rows':sorted(n for a in affected for n in a['rows']),
                          'text':'낱개수량과 작업 PCS가 100:1로 기재된 입고의 작업 PCS 기준은 무엇인가요?',
                          'choices':['확정할 수 없음'],'evidence_ref':'과금수량_검사.csv'})
    for entry in rows:
        cause = entry.get('cause_analysis', {})
        if cause.get('category') == 'inbound_work_pcs_unit':
            cause['work_pcs_appropriateness'] = 'not_determinable_by_user' if decided else 'awaiting_user_decision'
            cause['decision_id'] = 'U33'
            cause['decision_scope'] = '해당 입고 작업 PCS 적정성만 제외. EA·BOX·기재값과 원래 차이 유지. B2B에 확대하지 않음.' if decided else '입고 작업 PCS 기준 결정 대기; 새 환산 미적용'
    box_entries = [e for e in rows if e.get('cause_analysis',{}).get('category') == 'outbound_box_field_difference']
    box_fingerprint = hashlib.sha256(json.dumps({'sources':sources,'entries':[
        {'key':e['key'],'cells':e['cause_analysis']['source_cells']} for e in box_entries]},
        ensure_ascii=False,sort_keys=True,default=str).encode()).hexdigest()
    box_decision = decisions.get('outbound_repeated_box_work',{})
    box_decided = box_decision.get('state') == 'confirmed_unresolvable_by_user' and box_decision.get('scope_fingerprint') == box_fingerprint
    if not box_decided and root is not None and box_decision.get('state')=='confirmed_unresolvable_by_user':
        from demo_company_case_runtime import verified_reuse
        box_decided=verified_reuse(root,'U34',box_decision)
    if box_entries and not box_decided:
        questions.append({'id':'U34','category':'B2B 박스 수와 작업량 차이','skip_scope':'outbound_repeated_box_work_appropriateness_only',
                          'scope_fingerprint':box_fingerprint,'affected_rows':sorted(n for e in box_entries for n in e['claim_rows']),
                          'text':'청구 박스 수와 BOX 작업량이 다른 건에 별도 반복 작업 근거가 있나요?',
                          'choices':['확정할 수 없음'],'evidence_ref':'과금수량_검사.csv'})
    for entry in box_entries:
        entry['cause_analysis'].update(decision_id='U34',
            repeated_work_appropriateness='not_determinable_by_user' if box_decided else 'awaiting_user_decision',
            decision_scope='원본 작업량·차이 유지. 반복 작업 적정성만 제외.' if box_decided else '반복 작업 기준 결정 대기; 두 번 작업한 것으로 자동 인정하지 않음')
    counts = dict(Counter(e['cause_analysis']['category'] for e in rows if e.get('cause_analysis')))
    return {'counts':counts,'labels':LABELS,'questions':questions,'scope_fingerprint':fingerprint,
            'decision':decision,'box_scope_fingerprint':box_fingerprint,'box_decision':box_decision, 'comparison_changed':False,
            'interpretation':'원인 분류는 기재값·산술 관계의 설명이며 작업 발생·과금 적정성 판정이 아님.'}
