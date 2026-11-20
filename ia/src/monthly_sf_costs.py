"""Remaining SF monthly costs: current invoice detail, never July row constants."""
from decimal import Decimal
import re
from identifier_tools import stored_number as num, billable_amount
from monthly_detail_tools import value
from monthly_formula_tools import evaluate, UnsupportedFormula, MissingValue

GROUP = '청구 요약과 상세 합계 비교'


def compact(v):
    return re.sub(r'\s+', '', str(v or ''))


def parcel_amount(item, sheets, compare, emit):
    if '택배' not in item.get('category', '') or item.get('rate') is None:
        return False
    rate = item['rate']
    source_specs = [('B2B출고',{'A':'ERP','H':'운송 방법','J':'운임비'},'R','S','T'),
                    ('B2C출고_택배',{'A':'주문번호','B':'운송 방법','D':'운임비'},'G','H','I')]
    selected = []; missing = []
    for sheet, headers, method_col, rate_col, count_col in source_specs:
        rows=sheets.get(sheet)
        if rows is None:
            missing.append(sheet+' 시트 없음'); continue
        candidates=[n for n,c in rows.items() if all(value(c,col)==h for col,h in headers.items())]
        if len(candidates)!=1:
            emit(GROUP,'not_implemented',item['label']+' · '+sheet+'의 주문·운송방법·운임 열을 식별하지 못함',
                 청구위치=f"청구내역!N{item['row']}",단위='KRW')
            return True
        if not any(value(c,method_col)=='신용 택배' for c in rows.values()):
            missing.append(sheet+'의 단가별 택배 집계표 없음');continue
        matched=[(n,c) for n,c in rows.items() if n>candidates[0] and num(value(c,rate_col))==rate]
        if len(matched)!=1:
            missing.append(sheet+'에 유일한 동일 단가 집계가 없음');continue
        n,c=matched[0];q=num(value(c,count_col))
        if q is None: missing.append(f'{sheet}!{count_col}{n} 택배 건수 없음');continue
        selected.append((q*rate,f'{sheet}!{rate_col}{n}:{count_col}{n}'))
    ref=f"청구내역!L{item['row']}:N{item['row']}"
    expected=sum((v for v,_ in selected),Decimal(0)) if selected and not missing else None
    compare('택배 B2B·B2C 운임 합계',item['amount'],expected,[r for _,r in selected]+[ref],
            item['label']+' · B2B·B2C 단가별 집계의 건수×단가 합산. 한 주문의 여러 박스 운임을 단일 택배로 세지 않음. 실측 규격 확인은 별도.'+
            (' / '+', '.join(missing) if missing else ''),청구위치=ref,단위='KRW')
    return True


def parcel_totals(sheets,compare):
    """Independently compare rate-table totals to raw freight, including credits."""
    for sh,method,ratecol,qtycol,rawcol in [('B2B출고','H','S','T','J'),('B2C출고_택배','B','H','I','D')]:
        rows=sheets.get(sh,{})
        if not any(value(c,'R' if sh=='B2B출고' else 'G')=='신용 택배' for c in rows.values()):continue
        parts=[]; table=[]; error=False
        for n,c in rows.items():
            if n<3:continue
            amount=num(value(c,rawcol))
            if '택배' in str(value(c,method) or '') and value(c,'A') not in (None,'') and billable_amount(amount):
                parts.append((amount,f'{sh}!{rawcol}{n}'))
            rate,q=num(value(c,ratecol)),num(value(c,qtycol))
            if rate is not None:
                if q is None:error=True
                else:table.append((q*rate,f'{sh}!{ratecol}{n}:{qtycol}{n}'))
        if parts or table:
            compare('택배 집계와 발송 운임 합계',sum((a for a,_ in table),Decimal(0)) if table and not error else None,
                    sum((a for a,_ in parts),Decimal(0)) if parts else None,[r for _,r in table+parts],
                    sh+' · 단가별 택배 집계와 모든 택배 발송의 기재 운임 합계. 별도 도서산간·추가운임 표는 제외.',
                    청구위치=sh,단위='KRW')


def explicit_detail(item, sheets, compare, emit):
    """Strict named-column sums for rows whose stored totals may be filtered."""
    label=compact(item.get('item'));unit=compact(item.get('unit'))
    spec=None
    if label=='반출비':spec=('입고 외 기타',{'C':'상품 코드','G':'불량 반출PLT'},'C','G')
    elif label=='랩핑재작업':spec=('입고 외 기타',{'C':'상품 코드','H':'재작업 PLT'},'C','H')
    elif label=='라벨부착(B2B)':spec=('B2B출고',{'A':'ERP','P':'가공비'},'A','P')
    elif label=='반품(B2B)' and unit in ('BOX','PCS'):
        spec=('B2B 반품',{'A':'ASN 주문번호','D':'SKU 코드'},'A','K' if unit=='BOX' else 'L')
    if not spec: return False
    sheet,headers,keycol,quantitycol=spec; rows=sheets.get(sheet)
    ref=f"청구내역!M{item['row']}"
    if rows is None:
        emit(GROUP,'data_unavailable',item['label']+' · 청구서에 '+sheet+' 시트가 없음',청구위치=ref,단위=item['unit']);return True
    candidates=[n for n,c in rows.items() if all(value(c,k)==v for k,v in headers.items())]
    if len(candidates)!=1:
        emit(GROUP,'not_implemented',item['label']+' · '+sheet+' 상세 열 제목을 식별할 수 없는 양식',청구위치=ref,단위=item['unit']);return True
    parts=[]
    for n,c in sorted(rows.items()):
        if n<=candidates[0]:continue
        if compact(value(c,'A')) in ('합계','총합계','TOTAL'):break
        if value(c,keycol) not in (None,''):
            # Empty work cells remain unconfirmed, not invented zero work.
            parts.append((num(value(c,quantitycol)),f'{sheet}!{quantitycol}{n}'))
    expected=sum((q for q,_ in parts),Decimal(0)) if parts and all(q is not None for q,_ in parts) else None
    compare(GROUP,item['quantity'],expected,[r for _,r in parts]+[ref],
            item['label']+' · 해당 열의 전체 상세 합계; 필터·숨김 여부로 제외하지 않음',청구위치=ref,단위=item['unit'])
    return True


def formula_detail(item,sheets,formulas,compare,emit,observations):
    failures=[];found=False
    for col,field,unit in [('M','quantity',item['unit']),('N','amount','KRW')]:
        ref=col+str(item['row']);node=formulas.get(('청구내역',ref))
        if node is None:continue
        # A row's quantity*rate is arithmetic, already checked separately.
        if col=='N' and item['quantity'] is not None and item['rate'] is not None and '핸들링' not in item['label']:continue
        found=True
        try:
            if isinstance(node,str):raise UnsupportedFormula(node)
            result=evaluate(node,sheets,formulas,origin=('청구내역',ref))
            if not result['refs']:
                failures.append(('data_unavailable','청구서 수식이 상수만 포함하고 대응 상세 위치가 없음'));continue
            reason=item['label']+' · 원본 수식의 참조 셀을 다시 합산·계산. 실제 작업 발생 확인과 구분.'
            if result['constants'] and col=='M':
                reason+=' 수식에 직접 입력한 수량 '+', '.join(result['constants'])+' 포함; 별도 작업 근거 미확인.'
            if result['ignored_text_refs']:
                reason+=' SUM 범위의 문자 셀은 수량으로 세지 않음: '+', '.join(result['ignored_text_refs'][:2])
            if any(x in item['item'] for x in ('라벨 교체','PLT 재적치')):
                reason+=' 참조 열의 작업명과 요약 작업명이 다를 수 있어 업무 대응은 미확정.'
            compare(GROUP,item[field],result['value'],result['refs']+['청구내역!'+ref],reason,
                    청구위치='청구내역!'+ref,단위=unit)
            observations.append({'kind':'source_formula','claim_row':item['row'],'field':field,**result})
            return True
        except UnsupportedFormula as exc:failures.append(('not_implemented',str(exc)))
        except MissingValue as exc:failures.append(('data_unavailable',str(exc)))
    if found:
        status='not_implemented' if any(s=='not_implemented' for s,_ in failures) else 'data_unavailable'
        emit(GROUP,status,item['label']+' · '+' / '.join(dict.fromkeys(r for _,r in failures)),
             청구위치=f"청구내역!L{item['row']}:N{item['row']}",청구값=str(item['amount']),단위='KRW')
        return True
    return False


def evidence_requirement(item,emit):
    name=item['label']
    kinds=[(('보관비',),'층별 실제 사용면적·사용기간'),
           (('지원비','임대','폐기','부자재','무지 라벨','PLT'), '해당 비용의 작업·구매·사용 내역 또는 영수증'),
           (('추가 작업','선작업','분할출고','상품교체','상품해체','아웃박스','하차비','반품','쇼링'), '해당 비용의 작업 단위·작업 횟수 내역'),
           (('실비','상계처리','퀵','화물'), '운송·상계 내역 또는 영수증')]
    need=next((need for words,need in kinds if any(w in name for w in words)),None)
    if need is None:return False
    emit('실제 발생·적용 근거 확인','data_unavailable',
         name+' · 청구 기재값은 읽었으나 '+need+'이 연결되지 않음. 산술·상세 비교 결과와 구분.',
         청구위치=f"청구내역!L{item['row']}:N{item['row']}",청구값=str(item['amount']),단위='KRW')
    return True


def augment_item(item,sheets,formulas,compare,emit,observations):
    return (parcel_amount(item,sheets,compare,emit) or
            explicit_detail(item,sheets,compare,emit) or
            formula_detail(item,sheets,formulas,compare,emit,observations) or
            evidence_requirement(item,emit))
