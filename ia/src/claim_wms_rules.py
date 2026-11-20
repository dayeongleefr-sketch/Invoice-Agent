"""Deterministic cause candidates; no workbook or prior-result dependencies."""
from decimal import Decimal
import re

def july_date(raw, decision):
    from datetime import date
    text = raw.strip()
    if re.fullmatch(r'2029-07-\d{2}', text):
        return date.fromisoformat(text).isoformat()
    match = re.fullmatch(r'(\d{1,2})-Jul', text, flags=re.I)
    if match and decision.get('state') == 'confirmed_by_user' and decision.get('year') == 2029 and decision.get('month') == 7:
        return date(2029, 7, int(match[1])).isoformat()
    raise ValueError('unconfirmed_or_invalid_date:' + text)


def explain(claim, wms):
    def number(group, field):
        if group is None or group.get(field) is None:
            return None
        value = Decimal(str(group[field]))
        if not value.is_finite():
            raise ValueError('non_finite_quantity')
        return value
    c, w, s = number(claim, 'quantity'), number(wms, 'quantity'), number(wms, 'consumables')
    result = dict(claim_present=claim is not None, wms_present=wms is not None,
                  claim_quantity=c, wms_quantity=w, wms_consumables=s,
                  difference_claim_minus_wms=None, candidate=None,
                  residual_after_candidate=None, amount_difference=None,
                  billing_decision='미확정', candidate_is_confirmed_cause=False,
                  cause='현재 근거로 발생 원인 미확정', cause_status='미확정',
                  cause_evidence='대응 내역이나 수량 차이만으로 발생 원인을 확정할 수 없음')
    if claim is None:
        result.update(kind='wms_only', explanation='WMS 내역에 대응하는 청구 없음. 미청구 후보이나 청구 대상 여부는 별도 판단.')
    elif wms is None:
        result.update(kind='claim_only', explanation='청구 내역의 WMS 근거 미연결. 키·기간·실적 누락 또는 청구 추가 후보이며 과다청구 확정 아님.')
    elif c is None or w is None:
        result.update(kind='quantity_unknown', explanation='연결된 자료의 수량 빈칸. 누락 수량을 0으로 대체하지 않음.')
    else:
        delta = c - w
        result['difference_claim_minus_wms'] = delta
        if s is not None and (s < 0 or s > w):
            raise ValueError('invalid_consumable_quantity')
        if delta == 0:
            result.update(kind='quantity_match', explanation='청구 수량과 WMS 전체 수량 일치. 금액 적정성 확정 아님.')
            result.update(cause='수량 차이 없음', cause_status='해당 없음', cause_evidence='청구−WMS=0')
        elif s is not None and s > 0 and delta == -s:
            result.update(kind='consumables_exclusion_candidate',
                          candidate='WMS 소모품 수량 제외',
                          residual_after_candidate=c - (w - s),
                          explanation='청구가 WMS보다 적은 수량이 WMS 소모품 수량과 같음. 소모품 제외 집계 후보; 청구금액의 소모품 제외 여부는 미확정.')
            result.update(cause='청구와 WMS의 소모품 집계 범위 차이 추정',
                          cause_status='근거 있는 추정',
                          cause_evidence=f'WMS 전체 {w:,}개 중 소모품 {s:,}개를 빼면 청구 {c:,}개와 일치. 청구 작성 시 실제 제외했는지와 금액 반영은 미확인.')
        else:
            result.update(kind='unexplained_quantity_difference',
                          explanation='청구가 WMS보다 많음.' if delta > 0 else '청구가 WMS보다 적음.')
            if s is not None and s > 0:
                result.update(candidate='WMS 소모품 수량 제외 시나리오(차이 해소 안 됨)',
                              residual_after_candidate=c - (w - s))
                result['cause_evidence'] = f'소모품 제외 후에도 청구 대비 차이 {c-(w-s):,}개가 남아 포함/제외만으로 설명되지 않음.'
    return result

