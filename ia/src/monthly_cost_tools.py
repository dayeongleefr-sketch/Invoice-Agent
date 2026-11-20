"""Monthly SF detail checks and explicitly selected WMS packing/carton scenarios.

Uses the new month's stored values. No July approvals, constants, quantities or
manual verification workbooks are loaded here.
"""
from collections import defaultdict
from decimal import Decimal
import re
from identifier_tools import stored_number as num, expand_identifier as ident, billable_amount, scientific_info
from billing_quantity_tools import split_units


def value(c,col):return None if c.get(col,{}).get('error') else c.get(col,{}).get('value')


def total_detail(sheets,sheet,col,keycol,first=4):
    if sheet not in sheets:return None,[]
    rows=[(n,c) for n,c in sheets[sheet].items() if n>=first and value(c,keycol) is not None]
    values=[num(value(c,col)) for _,c in rows]
    return (sum((v for v in values if v is not None),Decimal(0)) if rows and all(v is not None for v in values) else None,
            [f'{sheet}!{col}{n}' for n,_ in rows])


def augment(root,case,sheets,items,wms,compare,emit,tabular,claim_sheets=None,formulas=None):
    checked=[];rules=case['rules'];packing=defaultdict(list);unresolved=[]
    from monthly_sf_costs import parcel_totals
    parcel_totals(sheets,compare)
    grouping=rules.get('packing_group','unconfirmed');prepacked=rules.get('prepacked','unconfirmed')
    for (d,order),rows in wms['outbound'].items():
        for r in rows:
            if r['type'] not in ('B2C','B2C주문','비연동(B2C)'):continue
            if grouping=='shipment':key=(d,r['outbound']) if r['outbound'] else None
            elif grouping=='order_carrier_tracking':
                fields=(order,r.get('carrier',''),r['tracking'])
                raw_ids=(r.get('raw_order',order),r.get('raw_tracking',r['tracking']))
                lossy=any((p:=scientific_info(v)) and p['last_stored_digit_scale']>0 for v in raw_ids)
                key=fields if all(fields) and not lossy else None
            else:key=None
            if key is None:unresolved.append(r['ref'])
            else:packing[key].append(r)
    amounts=[]
    for n,c in sheets.get('B2C출고_택배',{}).items():
        if n>=3 and value(c,'A') is not None and billable_amount(value(c,'D')):amounts.append((n,num(value(c,'D'))))
    # Monetary row checks are executed only for actual nonzero charged amounts.
    for n,c in sheets.get('B2C출고_택배',{}).items():
        amount=num(value(c,'J'))
        if n<5 or not billable_amount(amount):continue
        q,rate=num(value(c,'I')),num(value(c,'H'))
        compare('B2C 택배 금액 계산',amount,q*rate if q is not None and rate is not None else None,
                [f'B2C출고_택배!H{n}:J{n}'],'택배 기재 건수×단가; 상품 EA 단가로 사용하지 않음',청구위치=f'B2C출고_택배!H{n}:J{n}',단위='KRW')
    for item in items:
        n=item['row'];name=item['label'];label=item.get('item',name);unit=item['unit'];amount=item['amount'];qty=item['quantity']
        ref=f'청구내역!L{n}:N{n}';matched=False
        detail=None
        if label=='입고비' and unit in ('BOX','PCS'):detail=('입고리스트','L' if unit=='BOX' else 'M','B')
        elif label in ('B2B 작업비','B2B 작업비 (수출)') and unit in ('BOX','PCS'):
            detail=('B2B출고',('N' if unit=='BOX' else 'O') if '(수출)' in label else ('L' if unit=='BOX' else 'M'),'A')
        if detail:
            expected,refs=total_detail(sheets,*detail)
            compare('청구 요약과 상세 합계 비교',qty,expected,refs+[ref],name+' · 원본 상세 작업량 합계; 독립 작업 횟수 확인과 구분',청구위치=ref,단위=unit)
            matched=True
        if label in ('상품 라벨덧방','정합'):
            labels={'바코드덧방','상세라벨덧방','QR라벨'} if label=='상품 라벨덧방' else {'정합 기타'}
            parts=[(i,c) for i,c in sheets.get('임가공-라벨',{}).items() if value(c,'A') in labels and billable_amount(value(c,'G'))]
            expected=sum((num(value(c,'G')) for _,c in parts),Decimal(0)) if parts else None
            compare('청구 요약과 상세 합계 비교',amount,expected,[f'임가공-라벨!G{i}' for i,_ in parts]+[ref],name+' · 해당 작업구분의 실제 기재금액 합계',청구위치=ref,단위='KRW')
            for i,c in parts:
                q,rate=num(value(c,'E')),num(value(c,'F'))
                compare('임가공 금액 계산',num(value(c,'G')),q*rate if q is not None and rate is not None else None,[f'임가공-라벨!E{i}:G{i}'],'청구 기재 수량×단가; 수량 빈칸은 임의 역산 확정하지 않음',청구위치=f'임가공-라벨!E{i}:G{i}',단위='KRW')
            matched=True
        if label in ('기본포장','추가포장') and '샤쉐' not in unit:
            threshold=rules.get('packing_threshold')
            ready=bool(packing) and grouping!='unconfirmed' and prepacked!='unconfirmed' and type(threshold) is int
            expected=Decimal(0);total_ea=Decimal(0);excluded=Decimal(0)
            for parts in packing.values():
                q=sum((r['quantity'] for r in parts),Decimal(0));total_ea+=q
                take=sum((r['quantity'] for r in parts if '(선포장)' in r.get('name','')),Decimal(0)) if prepacked=='exclude_explicit' else Decimal(0)
                excluded+=take;q-=take
                if q<0 or q%1:ready=False
                if ready:expected+=int(q>0) if label=='기본포장' else max(q-threshold,Decimal(0))
            reason=f'{name} · 등록한 포장 기준으로 계산. 전체 출고 EA는 유지하며 선포장 제외는 포장비에만 적용.'
            if unresolved:reason+=f' 집계 키 없는 WMS {len(unresolved):,}행은 미확정.'
            if not ready:reason+=' 포장 집계·선포장·기본포장 포함 수량 기준 또는 비교 실적이 없음.'
            if ready and not unresolved:
                compare('B2C WMS 포장비 잠정 비교',amount,expected*item['rate'] if item['rate'] is not None else None,[ref],reason,청구위치=ref,단위='KRW')
            else:
                emit('B2C WMS 포장비 잠정 비교','data_unavailable',reason,청구위치=ref,청구값=str(amount),비교값=str(expected*item['rate']) if ready and item['rate'] is not None else '',단위='KRW')
            checked.append({'claim_row':n,'grouping':grouping,'packing_count':expected if ready else None,'unresolved_rows':len(unresolved),'total_EA':total_ea,'excluded_prepacked_EA':excluded})
            matched=True
        if not matched:
            from monthly_detail_tools import augment_item
            matched=augment_item(item,sheets,compare,emit)
        if not matched:
            from monthly_sf_costs import augment_item as sf_item
            matched=sf_item(item,sheets,formulas or {},compare,emit,checked)
        if not matched:
            independent=any(word in name for word in ('보관비','지원비','폐기','임대','실비'))
            emit('비용별 상세·과금 연결','data_unavailable' if independent else 'not_implemented',name+' · '+('기재 수량×단가는 별도 비교. 독립 작업량·실비·사용면적·기간 자료가 없어 적정성 미확정.' if independent else '월별 계산의 이 비용 항목 상세 연결은 아직 지원하지 않습니다. 기재 수량×단가 산술은 별도 수행.'),청구위치=ref,청구값=str(amount),단위='KRW')
    masters=[m for m in case['materials'] if m['role']=='master']
    if masters:
        rates=defaultdict(set);refs=defaultdict(list);failed=[]
        for master in masters:
            try:
                count=0
                for sh,n,r in tabular(root/master['path'],{'품번','아웃박스수량(EA)'}):
                    count+=1;v=num(r['아웃박스수량(EA)']);sku=ident(r['품번'])
                    if v is not None and v>0 and v%1==0:rates[sku].add(v);refs[sku].append(f"{master['path']} / {sh}!{n}")
                checked.append({'kind':'master_file','path':master['path'],'rows_read':count})
            except (ValueError,UnicodeError) as exc:
                failed.append(master['path'])
                emit('Master 양식','not_implemented',str(exc),WMS위치=master['path'])
        if rules.get('carton_basis')!='master_then_claim':
            emit('박스·낱개 참고 환산','data_unavailable','등록 Master를 읽었지만 해당 월의 잠정 환산 적용 기준을 선택하지 않았습니다.',WMS위치='; '.join(m['path'] for m in masters))
            return checked
        fallback=defaultdict(set)
        for invoice in claim_sheets or [sheets]:
            for n,c in invoice.get('입고리스트',{}).items():
                v=num(value(c,'J'));sku=ident(value(c,'D'))
                if n>=4 and sku and v is not None and v>0 and v%1==0:fallback[sku].add(v)
        for sku,choices in rates.items():
            if len(choices)>1:
                emit('Master 환산 기준 충돌','data_unavailable','등록 Master의 박스당 수량이 다름: '+', '.join(sorted(str(v) for v in choices)),품번=sku,단위='EA/BOX',WMS위치='; '.join(refs[sku]))
        for role,groups in wms.items():
            for key,parts in groups.items():
                sku_values=defaultdict(Decimal)
                for r in parts:sku_values[r['sku']]+=r['quantity']
                boxes=Decimal(0);pieces=Decimal(0);missing=[]
                for sku,q in sku_values.items():
                    choices=rates.get(sku) or fallback.get(sku) or set()
                    if failed or len(choices)!=1 or q<0 or q%1:missing.append(sku);continue
                    b,p=split_units(q,next(iter(choices)));boxes+=b;pieces+=p
                checked.append({'role':role,'key':list(key),'reference_boxes':boxes,'reference_pieces':pieces,'unconverted_skus':missing,'actual_work_confirmed':False})
    return checked
