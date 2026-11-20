"""Human-readable saved evidence; no new matching or business decisions."""
import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path
from dashboard_view import format_number, display_explanation

METHODS={
 '입고 상품 수량':'같은 입고 일자·입고예정번호·품번의 수량 합계 비교. 번호가 없는 자료는 승인된 일자·품번 기준 사용.',
 'B2B 상품 수량':'같은 출고일·주문번호에 속한 상품·소모품 수량을 합산해 청구 수량과 비교.',
 'B2C 주문 연결':'택배 청구에 대응하는 원본 WMS 출고 내역이 있는지 확인. 상품 수량·운임 적정성과는 별도 검사.',
 'B2C WMS 포장 수량':'WMS 출고를 승인된 발송 기준으로 합산하고 선포장 수량을 제외해 기본포장 건수·3개 초과 추가포장 수량을 계산한 뒤 청구 수량과 비교.',
 'B2C WMS 포장 금액':'WMS 잠정 포장 수량×채택한 청구 기재 단가를 계산하여 청구 금액과 비교. 포장 범위의 차이일 수 있어 과다청구 확정과 구분.',
 '요약·상세 집계':'청구 요약에 적힌 값과 해당 비용의 상세 내역 합계를 비교.',
 '청구·상세 금액 계산':'청구에 기재된 수량·단가 등 산출값으로 계산한 금액과 기재 청구금액을 비교.',
 'WMS에만 있는 실적':'WMS에 있는 실적에 대응하는 청구 내역을 확인.',
 '입고 BOX·PCS 잠정 환산':'WMS 수량을 합산한 뒤 제품 Master의 박스당 수량으로 환산. Master가 없으면 승인된 청구 내품수량을 보조 기준으로 사용.',
 'B2B BOX·PCS 잠정 환산':'WMS 수량을 일자·주문·품번별 합산한 뒤 박스당 수량으로 나눠 완박스와 낱개를 비교. 실제 피킹·포장 횟수 확정과는 별도.',
}

def comparison_label(row):
    """Name the actual comparator, never label a claim-only check as WMS."""
    group = row.get('검사', '')
    if group in ('입고 상품 수량', 'B2B 상품 수량', '임가공 작업상품 수량', 'WMS에만 있는 실적'):
        return 'WMS 수량'
    if group == 'B2C WMS 포장 수량':
        return 'WMS 포장 수량'
    if group == 'B2C WMS 포장 금액':
        return 'WMS 산출 금액'
    if group == 'B2C 주문 연결':
        return 'WMS 연결값'
    if 'BOX·PCS' in group:
        return 'WMS 환산 수량'
    if group in ('청구·상세 금액 계산', 'B2C 과금수량 역산'):
        return '재계산 금액' if group == '청구·상세 금액 계산' else '역산 수량'
    if group in ('요약·상세 집계', 'B2C 택배 금액 집계', '택배 B2B·B2C 합산'):
        return '청구 상세 합계'
    if group == '계약서·청구 기재단가':
        return '계약 기재 단가'
    if row.get('_cost_checks'):
        checks = json.loads(row['_cost_checks'])
        compared = [c for c in checks if c.get('비교값') not in (None, '') and c.get('단위') in ('원', 'KRW')]
        scopes = {c.get('확인범위', '') for c in compared}
        if scopes == {'WMS 대사'}:
            return 'WMS 산출 금액'
        if scopes == {'청구서 내부 합계'}:
            return '청구 상세 합계'
        if scopes == {'청구서 계산'}:
            return '재계산 금액'
        return '비용 비교 금액'
    return '비교값'


def brief(row):
    from dashboard_view import display_rows
    row = display_rows([row])[0]
    if row.get('_cost_checks'):
        parts = [('청구 금액', format_number(row['청구값']) + '원'), ('판정 이유', row.get('사유', ''))]
        if row.get('_cost_occurrence_pending') in (True, 'True', 'true'):
            parts.append(('별도 확인', '실제 작업 발생·계약 또는 실비 증빙은 아직 확인하지 못했습니다. 기존 청구값 임시 인정 결정은 유지합니다.'))
        for check in json.loads(row['_cost_checks']):
            scope = check.get('확인범위', check['검사'])
            status = check.get('표시결과', check['결과'])
            a, b, d = (check.get(k) for k in ('청구값','비교값','차이'))
            if check.get('_covered_by_detail'):
                continue
            value = status
            if a not in (None, ''):
                value += f" · 청구 {format_number(a)} {check.get('단위') or ''}"
            if b not in (None, ''):
                value += f" / 비교 {format_number(b)} / 차이 {format_number(d)}"
            if scope == '청구서 계산':
                value += f" · {format_number(row.get('_cost_quantity'))} × {format_number(row.get('_cost_rate'))}"
            if scope in ('청구서 내부 합계','청구 요약행 간 계산','WMS 대사'):
                value += '\n' + (check.get('사유') or '')
            parts.append((scope, value))
        return [(k, display_explanation(v)) for k,v in parts]
    if row.get('_storage_calculation'):
        parts = [('보관비 계산', row['_storage_calculation'])]
        names = {'실제 과금 작업량': '면적 근거', '계약 적용·실비 적정성': '단가 적용 근거',
                 '계약서·청구 기재단가': '계약 기재 단가', '청구·상세 금액 계산': '금액 계산', '요약·상세 집계': '금액 상세 근거'}
        for check in json.loads(row.get('_storage_checks') or '[]'):
            status = check['결과']
            value = f"{status} · 청구 {format_number(check.get('청구값'))} {check.get('단위') or ''}"
            if check.get('비교값') is not None:
                value += f" / 비교 {format_number(check['비교값'])} / 차이 {format_number(check.get('차이'))}"
            parts.append((names.get(check['검사'], check['검사']), value))
        return parts
    if row.get('_evidence_review'):
        return [('확인할 내용', '작업·사용면적·비용이 실제 발생했는지 확인'),
                ('미검증 이유', row['_display_reason']),
                ('다음 대사', '영수증·작업기록 등을 등록하면 청구 항목·기간·금액 또는 작업량과 비교합니다.')]
    group=row.get('검사','');parts=[('비교 방법',METHODS.get(group) or row.get('_display_basis') or '저장된 청구값과 근거의 비교값을 대조했습니다.')]
    a,b,d=(row.get(k) for k in ('청구값','비교값','차이'));unit=row.get('단위','')
    if group=='B2C 주문 연결':
        parts.append(('청구 기재 운임',format_number(a)+'원 · 상품 개수가 아닙니다.'))
        parts.append(('연결 판정','WMS 주문 연결 확인' if row.get('결과')=='정상' else '대응하는 WMS 출고를 확정하지 못했습니다.'))
    elif all(x not in (None,'') for x in (a,b,d)):
        parts.append(('저장된 차이 계산',f'청구 {format_number(a)} − {comparison_label(row)} {format_number(b)} = {format_number(d)} {unit}'))
    else:
        missing=[name for name,value in [('청구값',a),('비교값',b),('차이',d)] if value in (None,'')]
        parts.append(('계산하지 못한 부분',', '.join(missing)+' 없음. 빈칸을 0이나 정상으로 처리하지 않았습니다.'))
    reason=row.get('_display_reason')
    if reason:parts.append(('판단 이유',reason))
    if row.get('_display_basis'):parts.append(('적용한 기준',row['_display_basis']))
    if row.get('_normal_basis'):parts.append(('정상 표시 근거',row['_normal_basis']))
    return parts


def packing_sources(root):
    """File identities from the hash-verified saved calculation, never JSON as source."""
    from agent_adapter import safe_file
    base=Path(root)/'data/demo_company/derived'
    workflow=json.loads((base/'workflow_result.json').read_text('utf-8-sig'))
    path=safe_file(Path(root),'data/demo_company/derived/b2c_actual_claim_wms.json')
    doc=_document(str(path),(path.stat().st_mtime_ns,path.stat().st_size),workflow['result_hashes'][path.name])
    return [{**doc['sources'][key],'label':label} for key,label in
            (('raw_wms_july','7월 출고 WMS'),('raw_wms_june','6월 출고 WMS')) if key in doc['sources']]

@lru_cache(maxsize=2)
def _document(path,stamp,expected):
    raw=Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest()!=expected.lower():raise ValueError('저장된 계산 근거가 변경되어 상세 설명을 표시할 수 없습니다.')
    return json.loads(raw.decode('utf-8-sig'))

def explain(root,row):
    parts=brief(row);ref=row.get('계산근거','');match=re.fullmatch(r'match_investigation\.json#/claim_candidates/(\d+)',ref)
    if not match:return parts
    from agent_adapter import safe_file
    base=Path(root)/'data/demo_company/derived'
    workflow=json.loads((base/'workflow_result.json').read_text('utf-8-sig'))
    path=safe_file(Path(root),'data/demo_company/derived/match_investigation.json')
    doc=_document(str(path),(path.stat().st_mtime_ns,path.stat().st_size),workflow['result_hashes'][path.name])
    r=doc['claim_candidates'][int(match[1])]
    if r['order']!=row.get('주문번호') or str(r.get('tracking',''))!=row.get('송장번호',''):raise ValueError('선택한 주문과 저장 근거가 다릅니다. 목록을 다시 조회하세요.')
    if doc.get('period'):parts.append(('조회 기간',' ~ '.join(doc['period'])))
    precision=r.get('precision_investigation') or {};count=precision.get('joint_rounding_groups')
    if count and row.get('결과')!='정상':
        parts.append(('연결이 남은 이유',f'축약된 주문번호·송장번호의 숫자 범위를 함께 대조해도 후보 출고가 {count:,}개입니다. 어느 출고인지 하나로 특정하지 못했습니다.'))
    elif r.get('status')=='unconnected':
        parts.append(('번호 조회 결과','청구의 주문번호·송장번호에 대응하는 확정 출고를 찾지 못했습니다. 금액 차이가 0이라는 뜻은 아닙니다.'))
    parts.append(('청구서에서 확인',f"{r['sheet']} 시트 · {', '.join(str(n) for n in r.get('claim_rows',[r['row']]))}행 · 주문번호 {r['order']} · 송장번호 {r.get('tracking','기재 없음')}"))
    return parts

def render_brief(row):
    from nicegui import ui
    with ui.expansion('비교 방법·판단 근거',value=True).classes('w-full').props('data-testid=calculation-explanation'):
        for label,value in brief(row):
            ui.label(label).classes('muted mt-2')
            ui.label(value).classes('text-sm break-words leading-6')
