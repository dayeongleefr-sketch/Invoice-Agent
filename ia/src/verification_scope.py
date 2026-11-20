"""Cost presentation scope; stored area/rate checks remain untouched."""
import json
import re
from decimal import Decimal, InvalidOperation


def storage_area(item):
    unit = str(item.get('unit') or '').replace(' ', '').lower()
    name = ' '.join(str(item.get(k) or '') for k in ('label', 'item', 'item_context', 'category'))
    return any(u in unit for u in ('㎡', 'm²', 'm2', '평')) and ('보관' in name or 'storage' in name.lower())


def storage_money_rows(rows, cost, full):
    """Only amount rows are listed; area/rate outcomes are nested in their detail."""
    items = {i['row']: i for i in cost.get('items', []) if storage_area(i)}
    claims = {i['row']: i for i in full.get('claim_items', [])}
    hidden = {'실제 과금 작업량', '계약 적용·실비 적정성', '계약서·청구 기재단가'}
    def target(r):
        refs = {int(n) for n in re.findall(r'청구내역![$A-Z]*(\d+)', r.get('청구위치', ''))}
        return next(iter(refs)) if len(refs) == 1 else None
    def number(v):
        try:
            n = Decimal(str(v))
            if not n.is_finite(): return '미기재'
            s = f'{n:,f}'
            return s.rstrip('0').rstrip('.') if '.' in s else s
        except (InvalidOperation, ValueError): return '미기재'
    result = []
    for original in rows:
        r = dict(original, _storage_calculation='', _storage_checks='')
        n = target(r)
        if n not in items:
            result.append(r); continue
        if r['검사'] in hidden:
            continue
        item = items[n]; claim = claims.get(n, {})
        unit = str(item.get('unit') or '')
        decision = cost.get('decisions_applied', {}).get('U40', {})
        if n == 88 and decision.get('state') == 'confirmed_by_user':
            unit = '평/월 (청구서 표기는 ㎡/월)'
        quantity, rate = claim.get('quantity'), claim.get('unit_price')
        try: calculated = Decimal(str(quantity)) * Decimal(str(rate))
        except (InvalidOperation, ValueError): calculated = None
        r['_storage_calculation'] = f"면적 {number(quantity)} [{unit}] × 단가 {number(rate)}원 = {number(calculated)}원 · 청구금액 {number(item.get('stated_amount'))}원"
        details = [x for x in rows if target(x) == n]
        r['_storage_checks'] = json.dumps([{'검사': x['검사'], '결과': x['결과'],
            '청구값': x.get('청구값'), '비교값': x.get('비교값'), '차이': x.get('차이'),
            '단위': x.get('단위'), '청구위치': x.get('청구위치'), '사유': x.get('사유')}
            for x in details], ensure_ascii=False, default=str)
        if r['검사'] == '요약·상세 집계':
            dependent = [x for x in details if x['검사'] in hidden]
            # Never conceal a newly observed area/rate discrepancy behind acceptance.
            differences = [x for x in dependent if x['결과'] == '불일치']
            if differences:
                r.update(결과='불일치', 계산결과='불일치', 사유구분='산출 근거 차이',
                         사유='면적 또는 단가 비교에 차이가 있습니다. 금액 산출 상세에서 확인하세요.',
                         비교값=None, 차이=None, 정상처리근거='')
            elif any(x['결과'] == '미검증' for x in dependent) and r['결과'] == '정상':
                r.update(결과='미검증', 사유='면적 또는 단가의 적용 근거가 미확정입니다. 금액 산출 상세에서 확인하세요.', 정상처리근거='')
            r.update(청구값=item.get('stated_amount'), 단위='원')
        result.append(r)
    return result
