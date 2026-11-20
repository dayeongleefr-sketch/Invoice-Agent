"""Compare relabel/assembly work in its own units, never sum boxes with pieces."""
from collections import defaultdict
from decimal import Decimal
import io,re,struct
from portable_workbook import open_workbook
from wms_tools import xlsb_rows
from invoice_tools import number
from identifier_tools import expand_identifier

def read_varint(stream):
    value=0
    for shift in range(0,28,7):
        data=stream.read(1)
        if not data:
            if shift:raise ValueError('truncated_record_header')
            return None
        b=data[0];value|=(b&127)<<shift
        if not b&128:return value
    raise ValueError('invalid_record_header')

def merged_ranges(book,sheet_name):
    if hasattr(book,"xlsx_path"):
        from portable_workbook import merged_ranges as read
        return read(book,sheet_name)

    # MS-XLSB BrtMergeCell (176): rwFirst,rwLast,colFirst,colLast, zero based.
    # https://learn.microsoft.com/en-us/openspecs/office_file_formats/ms-xlsb/582eecc9-6c0c-4006-8fdf-d355dada29b1
    target=next(t for n,t in book._sheets if n==sheet_name)
    stream=io.BytesIO(book._zf.read('xl/'+target));ranges=[]
    while True:
        record=read_varint(stream)
        if record is None:break
        size=read_varint(stream)
        if size is None:raise ValueError('missing_record_length')
        raw=stream.read(size)
        if len(raw)!=size:raise ValueError('truncated_record')
        if record==176:
            if size!=16:raise ValueError('invalid_merge_record')
            a,b,c,d=struct.unpack('<IIII',raw)
            if not(0<=a<=b<1048576 and 0<=c<=d<16384):raise ValueError('invalid_merge_range')
            ranges.append((a+1,b+1,c+1,d+1))
    return ranges

def read_work_specs(path,orders):
    sheet_name='임가공-라벨';out={}
    with open_workbook(str(path)) as book:
        ranges=merged_ranges(book,sheet_name)
        with book.get_sheet(sheet_name) as sheet:rows=dict(xlsb_rows(sheet))
        for n,c in rows.items():
            order=expand_identifier(c.get('B',{}).get('value'))
            if order not in orders:continue
            output=c.get('D',{}).get('value');anchor=n
            if output is None:
                merge=next((x for x in ranges if x[0]<=n<=x[1] and x[2]<=4<=x[3]),None)
                if merge:anchor=merge[0];output=rows[anchor].get('D',{}).get('value')
            entry={'row':n,'work':str(c.get('A',{}).get('value','')),
                'input_code':expand_identifier(c.get('C',{}).get('value')),'output_code':expand_identifier(output),
                'output_anchor_row':anchor,'quantity':c.get('E',{}).get('value'),
                'unit_price':c.get('F',{}).get('value'),'amount':c.get('G',{}).get('value'),
                'evidence':{'source':'claim','sheet':sheet_name,'row':n}}
            out.setdefault(order,[]).append(entry)
    return out

def work_quantity(entry):
    if entry['quantity'] is not None:
        return Decimal(str(entry['quantity'])),'stated'
    if entry['unit_price'] is not None and entry['amount'] is not None:
        price=Decimal(str(entry['unit_price']));amount=Decimal(str(entry['amount']))
        if price>0:return amount/price,'amount_divided_by_unit_price'
    return None,'missing'

def compare_work_quantities(orders,work_rows,inbound,decision):
    checks=[];groups={};pending=[]
    for key,wms in orders.items():
        date,order=key;rows=work_rows.get(order,[])
        primary=[r for r in rows if r['output_anchor_row']==r['row'] and re.fullmatch(r'[A-Z]{2}SK[A-Z]\d{3}',str(r['output_code']))]
        label=[r for r in primary if r['work']=='상세라벨덧방']
        sets=[r for r in primary if r['work']=='정합 기타']
        kind='relabel' if label else 'assembly' if sets else 'unsupported'
        state=decision.get(kind,{}).get('state')
        if state not in ['confirmed_by_user','confirmed_unresolvable_by_user']:
            pending.append(kind)
            checks.append({'kind':kind,'orders':[order],'status':'awaiting_user_decision','claim_rows':rows,'wms_quantity':wms['quantity']})
            continue
        if state=='confirmed_unresolvable_by_user':
            checks.append({'kind':kind,'orders':[order],'status':'not_determinable_by_user','claim_rows':rows,'wms_quantity':wms['quantity']})
            continue
        if kind=='relabel':
            quantities=[work_quantity(r)[0] for r in label]
            q=sum(quantities,Decimal(0)) if all(x is not None for x in quantities) else None
            expected=wms['quantity']-wms['consumables']
            checks.append({'kind':kind,'orders':[order],'date':date,'unit':'작업상품 EA',
                'claim_quantity':q,'wms_quantity':expected,'wms_total':wms['quantity'],'wms_materials':wms['consumables'],
                'difference':None if q is None else q-expected,'status':'missing' if q is None else 'match' if q==expected else 'difference',
                'claim_rows':label,'separate_box_work':[r for r in rows if r['output_code']=='아웃박스 라벨'],
                'wms_evidence':wms['rows'],'sku_equivalence_confirmed':False})
        elif kind=='assembly':
            for entry in sets:
                g=groups.setdefault((date,entry['output_code']),{'orders':[],'claim_rows':[],'quantities':[],
                    'quantity_origins':[],'component_quantity':Decimal(0),'component_evidence':[]})
                qty,origin=work_quantity(entry)
                g['orders'].append(order);g['claim_rows'].append(entry);g['quantities'].append(qty);g['quantity_origins'].append(origin)
                g['component_quantity']+=wms['quantity'];g['component_evidence']+=wms['rows']
    for (date,sku),g in groups.items():
        candidates=[(k,v) for k,v in inbound.items() if k[0]==date and k[-1]==sku]
        q=sum(g['quantities'],Decimal(0)) if all(v is not None for v in g['quantities']) else None
        w=sum((v['quantity'] for k,v in candidates),Decimal(0)) if candidates else None
        checks.append({'kind':'assembly','date':date,'output_sku':sku,'unit':'완성품 EA(세트)',**g,
            'claim_quantity':q,'wms_quantity':w,'difference':None if q is None or w is None else q-w,
            'status':'missing' if q is None or w is None else 'match' if q==w else 'difference',
            'link_basis':'사용자 승인한 동일일자+결과품번 집계 비교; 개별주문-입고번호 연결 아님',
            'wms_evidence':[ev for k,v in candidates for ev in v['rows']],
            'observed_component_ratio':g['component_quantity']/q if q else None,'component_recipe_confirmed':False})
    return checks,sorted(set(pending))
