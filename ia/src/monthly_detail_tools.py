"""Named SF work in the current month's invoice; no prior-month constants.

These checks compare the invoice's summary against explicit detail records.
They do not establish that work physically occurred or approve a tariff.
"""
from decimal import Decimal
import re
from identifier_tools import stored_number, billable_amount


def value(cells, col):
    c = cells.get(col, {})
    return None if c.get('error') else c.get('value')


def named_work(item, sheets, compare, emit):
    label = re.sub(r'\s+', '', item.get('item', ''))
    unit = re.sub(r'\s+', '', item.get('unit', ''))
    kind = ('aj' if label == 'AJPLT(B2B)' else
            'band_hash' if label == '#밴딩(B2B)' else
            'band' if label == '밴딩(B2B)' else
            'container' if label == '컨테이너추가작업(B2B)' and re.match(r"^(20|40)['′’]컨테이너$", unit) else None)
    if kind is None:
        return False
    ref = f"청구내역!M{item['row']}"
    detail = sheets.get('B2B출고', {})
    header = detail.get(2, {})
    if not (value(header, 'A') == 'ERP' and value(header, 'K') == '비고'
            and (kind == 'container' or value(header, 'E') == '파렛트 수')):
        emit('청구 요약과 상세 합계 비교', 'not_implemented',
             item['label']+' · B2B출고의 주문번호·비고·파렛트 수 열을 확인할 수 없는 양식입니다.', 청구위치=ref, 단위=unit)
        return True
    parts = []
    size = unit[:2]
    for n, cells in detail.items():
        if n < 4 or value(cells, 'A') in (None, ''):
            continue
        memo = str(value(cells, 'K') or '')
        if kind == 'container':
            for selected, count in re.findall(r"(20|40)\s*['′’]\s*[xX×]\s*(\d+)", memo):
                if selected == size:
                    parts.append((Decimal(count), f'B2B출고!K{n}'))
        elif (kind == 'aj' and 'AJ PLT' in memo or
              kind == 'band_hash' and '밴딩' in memo and '#' in memo or
              kind == 'band' and '밴딩' in memo and '#' not in memo):
            parts.append((stored_number(value(cells, 'E')), f'B2B출고!E{n}:K{n}'))
    # Absence of explicit records is not proof of zero work. A blank pallet
    # count in any selected record also prevents a full aggregate match.
    expected = (sum((q for q, _ in parts), Decimal(0))
                if parts and all(q is not None for q, _ in parts) else None)
    compare('청구 요약과 상세 합계 비교', item['quantity'], expected,
            [r for _, r in parts]+[ref], item['label']+' · 해당 월 비고에 명시된 작업과 수량 합계. 실제 작업 발생 여부는 별도.',
            청구위치=ref, 단위=item['unit'])
    return True


def return_freight(item, sheets, compare, emit):
    if item.get('item') != '반품 및 회수' or '택배' not in item.get('category', ''):
        return False
    ref = f"청구내역!N{item['row']}"
    detail = sheets.get('B2C반품', {})
    header = detail.get(2, {})
    if value(header, 'E') != '운임비' or value(header, 'C') != '송장번호':
        emit('청구 요약과 상세 합계 비교', 'not_implemented',
             'B2C반품의 운임비·송장번호 열을 확인할 수 없는 양식입니다.', 청구위치=ref, 단위='KRW')
        return True
    parts = [(n, stored_number(value(c, 'E'))) for n, c in detail.items()
             if n >= 3 and billable_amount(value(c, 'E'))]
    expected = sum((q for _, q in parts), Decimal(0)) if parts else None
    compare('청구 요약과 상세 합계 비교', item['amount'], expected,
            [f'B2C반품!E{n}' for n, _ in parts]+[ref],
            '반품 상세에 기재된 양수·음수 운임 합계. 빈칸·0·비숫자 제외.', 청구위치=ref, 단위='KRW')
    return True


def augment_item(item, sheets, compare, emit):
    return named_work(item, sheets, compare, emit) or return_freight(item, sheets, compare, emit)
