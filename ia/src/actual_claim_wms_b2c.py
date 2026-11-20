"""Compare actual B2C invoice detail to raw WMS by exact order number."""
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
import csv, json, sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from invoice_tools import digest, number
from xlsb_scan import scan_rows
from portable_workbook import open_workbook
from wms_tools import xlsx_rows
from identifier_tools import expand_identifier, scientific_info, billable_amount
from b2c_charge_tools import observe_group, observed_groups

CLAIM='data/demo_company/source/legacy_example_4586925ff6d2.xlsb'
WMS_JULY='data/demo_company/source/05. 물류실적 Data/02. demo_company_출고_WMS_7월.csv'
WMS_JUNE='data/demo_company/source/legacy_example_f159d3f7ed15.xlsx'
PERIOD=('2029-06-26','2029-07-25')
B2C_TYPES={'B2C주문','비연동(B2C)'}

def ident(v):
    return expand_identifier(v)

def normalize_date(value):
    text=str(value or '').strip()
    return _normalize_date_text(text)

@lru_cache(maxsize=8192)
def _normalize_date_text(text):
    for fmt in ('%Y-%m-%d','%Y.%m.%d','%Y%m%d'):
        try: return datetime.strptime(text,fmt).date().isoformat()
        except ValueError: pass
    # The user confirmed yearless July dates in this WMS export mean 2029.
    try: return datetime.strptime(text,'%d-%b').replace(year=2029).date().isoformat()
    except ValueError: raise ValueError('unsupported_wms_outbound_date')

def run(root, include_reference=False, defer_report=False):
    root=Path(root).resolve()
    cfg=json.loads((root/'ia/config/demo_company_direct_wms.json').read_text('utf-8-sig'))
    CLAIM=cfg['sources']['claim']['path']; WMS_JULY=cfg['sources']['outbound_july']['path']; WMS_JUNE=cfg['sources']['outbound_h1']['path']
    claim=root/CLAIM; july=root/WMS_JULY; june=root/WMS_JUNE
    deps=[{'path':p,'sha256':digest(root/p)} for p in (CLAIM,WMS_JULY,WMS_JUNE)]
    bill={}; parcel=[]; cost_groups={}; parcel_groups={}
    with open_workbook(str(claim)) as book:
        for n,c in scan_rows(book,'청구내역'):
            if n in (28,29,30,31,98,99,100,101,102,103,104,105,106,107,108,109,110,111):
                bill[n]={'category':ident(c.get('B',{}).get('value')),'item':ident(c.get('C',{}).get('value')) or ident(c.get('G',{}).get('value')),
                    'unit':ident(c.get('J',{}).get('value')),'price':c.get('L',{}).get('value'),
                    'quantity':c.get('M',{}).get('value'),'amount':c.get('N',{}).get('value')}
        for n,c in scan_rows(book,'B2C출고_택배'):
            if n<3: continue
            order=ident(c.get('A',{}).get('value'))
            if order and billable_amount(c.get('D',{}).get('value')): parcel.append({'row':n,'order':order,'method':ident(c.get('B',{}).get('value')),
                                     'tracking':ident(c.get('C',{}).get('value')),'freight':c.get('D',{}).get('value')})

    # July CSV: compare invoice order number (A) to WMS linked order number.
    july_by_order=defaultdict(lambda:{'rows':0,'qty':Decimal(0),'dates':set(),'outbound_ids':set()})
    july_dates=Counter(); july_rows=0; july_qty=Decimal(0); july_blank_order_rows=0
    required={'회사구분','출고창고','출고일','출고번호','주문번호(연동)','구분','단위','수량','품명','택배사','송장번호'}
    with july.open(encoding='utf-8-sig',newline='') as f:
        reader=csv.DictReader(f)
        if not required.issubset(reader.fieldnames or []): raise ValueError('wms_headers_changed')
        for csv_row,r in enumerate(reader,2):
            if (r['회사구분'].strip(),r['출고창고'].strip(),r['단위'].strip())!=('90','DM001','EA'): continue
            if r['구분'].strip() not in B2C_TYPES: continue
            d=normalize_date(r['출고일']);
            if not PERIOD[0]<=d<=PERIOD[1]: continue
            qty=Decimal(r['수량'].replace(',','').strip())
            if not qty.is_finite(): raise ValueError('invalid_quantity')
            observe_group(cost_groups,'outbound_july',csv_row,d,r['출고번호'],r['주문번호(연동)'],r['단위'],qty)
            observe_group(parcel_groups,'outbound_july',csv_row,d,r['출고번호'],r['주문번호(연동)'],r['단위'],qty,
                          r['품명'],r['택배사'],r['송장번호'],parcel_basis=True)
            july_rows+=1; july_qty+=qty; july_dates[d]+=1
            order=ident(r['주문번호(연동)'])
            if not order: july_blank_order_rows+=1; continue
            x=july_by_order[order]; x['rows']+=1; x['qty']+=qty; x['dates'].add(d); x['outbound_ids'].add(r['출고번호'].strip())

    claim_order_rows=Counter(x['order'] for x in parcel)
    claim_repeat_orders=defaultdict(list)
    for x in parcel: claim_repeat_orders[x['order']].append(x)
    repeated_groups=[v for v in claim_repeat_orders.values() if len(v)>1]
    repeated_groups_distinct_tracking=sum(1 for v in repeated_groups if len({x['tracking'] for x in v})>1)
    claim_orders=set(claim_order_rows)
    july_orders=set(july_by_order)
    matched=claim_orders & july_orders
    unmatched_claim=claim_orders-july_orders
    wms_only=july_orders-claim_orders
    matched_claim_rows=sum(claim_order_rows[k] for k in matched)

    # The user confirmed that June workbook B2C rows belong to 가상센터 despite
    # blank company/warehouse fields. Unit is also blank, so use for order/date only.
    june_rows=0; june_orders=defaultdict(lambda:{'rows':0,'dates':set(),'outbound_ids':set()}); june_dates=Counter()
    june_blank_company=0; june_blank_warehouse=0; june_scientific_order_cells=0
    for n,r in xlsx_rows(str(june),'출고 Data(04월~06월)',set(['A','B','C','I','J','N','T','AQ','Q','AB','AC'])):
        if n==1 or str(r.get('I',{}).get('value','')).strip() not in (B2C_TYPES | {'B2C'}): continue
        raw=ident(r.get('B',{}).get('value'))
        try: d=normalize_date(raw)
        except ValueError: continue
        if not PERIOD[0]<=d<'2029-07-01': continue
        observe_group(cost_groups,'outbound_h1',n,d,r.get('C',{}).get('value'),r.get('J',{}).get('value'),
                      ident(r.get('N',{}).get('value')),r.get('T',{}).get('value'))
        observe_group(parcel_groups,'outbound_h1',n,d,r.get('C',{}).get('value'),r.get('J',{}).get('value'),
                      ident(r.get('N',{}).get('value')),r.get('T',{}).get('value'),r.get('Q',{}).get('value'),
                      r.get('AB',{}).get('value'),r.get('AC',{}).get('value'),parcel_basis=True)
        june_rows+=1; june_dates[d]+=1
        if not ident(r.get('A',{}).get('value')): june_blank_company+=1
        if not ident(r.get('AQ',{}).get('value')): june_blank_warehouse+=1
        raw_order=r.get('J',{}).get('value')
        if scientific_info(raw_order):june_scientific_order_cells+=1
        o=ident(raw_order)
        if o:
            x=june_orders[o];x['rows']+=1;x['dates'].add(d);x['outbound_ids'].add(ident(r.get('C',{}).get('value')))
    june_order_set=set(june_orders)
    matched_all=claim_orders & (july_orders | june_order_set)
    unmatched_claim=claim_orders-matched_all
    wms_only=(july_orders | june_order_set)-claim_orders
    matched_claim_rows=sum(claim_order_rows[k] for k in matched_all)
    matched_july_rows=sum(july_by_order[k]['rows'] for k in (matched_all & july_orders))
    matched_july_qty=sum((july_by_order[k]['qty'] for k in (matched_all & july_orders)),Decimal(0))
    matched_june_rows=sum(june_orders[k]['rows'] for k in (matched_all & june_order_set))
    june_only_matched=claim_orders & (june_order_set-july_orders)
    observed_wms_dates=sorted(set(june_dates)|set(july_dates))
    period_days=[]
    d0=datetime.strptime(PERIOD[0],'%Y-%m-%d').date(); d1=datetime.strptime(PERIOD[1],'%Y-%m-%d').date()
    while d0<=d1:
        period_days.append(d0.isoformat()); d0+=timedelta(days=1)
    no_b2c_rows=[d for d in period_days if d not in set(observed_wms_dates)]

    b2c_claim={n:{'quantity':bill.get(n,{}).get('quantity'),'price':bill.get(n,{}).get('price'),'amount':bill.get(n,{}).get('amount')} for n in [28,29,30,31]}
    result={'scope':'실제 청구 파일 B2C 택배 상세 ↔ 원본 WMS 주문번호 대조',
      'period':{'start':PERIOD[0],'end':PERIOD[1],'basis':'사용자 잠정기간','yearless_date_rule':'16-Jul 형식은 사용자가 승인한 대로 2029년으로 해석'},
      'sources':{'actual_claim':{'path':CLAIM,'sha256':digest(claim),'sheet':'B2C출고_택배','order_key':'A(주문번호)'},
        'raw_wms_july':{'path':WMS_JULY,'sha256':digest(july),'order_key':'주문번호(연동)','date_key':'출고일'},
        'raw_wms_june':{'path':WMS_JUNE,'sha256':digest(june),'sheet':'출고 Data(04월~06월)','scope':'사용자 결정으로 가상센터 비교에 포함; 주문·날짜 연결만'},
        'base_order_comparison_uses_internal_data':False},
      'claim_parcel_detail':{'rows':len(parcel),'unique_order_numbers':len(claim_orders),'repeated_rows':len(parcel)-len(claim_orders),
        'repeated_order_groups':len(repeated_groups),'repeated_groups_with_distinct_tracking':repeated_groups_distinct_tracking,
        'order_numbers_matched_to_scoped_wms':len(matched_all),'claim_rows_with_matched_order':matched_claim_rows,
        'claim_order_numbers_not_found_in_included_wms':len(unmatched_claim)},
      'scoped_july_wms':{'b2c_rows_in_period':july_rows,'b2c_units_EA_in_period':july_qty,'outbound_dates':dict(sorted(july_dates.items())),
        'linked_order_numbers':len(july_orders),'orders_not_in_claim_parcel_detail':len(wms_only),'rows_for_matched_orders':matched_july_rows,
        'EA_for_matched_orders':matched_july_qty,'rows_missing_linked_order_number':july_blank_order_rows,
        'matched_order_keys_with_multiple_outbound_ids':sum(1 for k in (matched_all & july_orders) if len(july_by_order[k]['outbound_ids'])>1)},
      'included_june_wms':{'b2c_rows_2029_06_26_to_30':june_rows,'dates':dict(sorted(june_dates.items())),
        'unique_linked_order_numbers':len(june_orders),'rows_with_blank_company':june_blank_company,'rows_with_blank_warehouse':june_blank_warehouse,
        'claim_order_numbers_found':len(claim_orders & june_order_set),'claim_orders_found_only_in_june':len(june_only_matched),
        'rows_for_matched_orders':matched_june_rows,'quantity_comparable':False,
        'scope_decision':'사용자 지시로 가상센터 비교에 포함; 주문번호·출고일 연결만 사용하고 단위가 공란이므로 EA 수량 대사는 제외',
        'scientific_order_cells_expanded':june_scientific_order_cells},
      'combined_wms':{'orders_not_in_claim_parcel_detail':len(wms_only),'rows_for_matched_orders':matched_july_rows+matched_june_rows,
        'july_EA_for_matched_orders':matched_july_qty,'june_units_not_summed':True,
        'observed_b2c_dates':observed_wms_dates,'period_dates_without_b2c_rows':no_b2c_rows},
      'actual_claim_packaging_totals':b2c_claim,
      'cost_observations':{'groups':observed_groups(cost_groups),'parcel_groups':observed_groups(parcel_groups),'period':list(PERIOD),
                          'basis':'원본 관측 집계. 포장 1건 적용과 단위 해석은 cost_verification에서 사용자 결정에 따라 수행'},
      'limitations':['청구 택배 상세는 주문번호별 운임/행을 가지지만 상품 EA 수량은 가지지 않으므로 WMS 상품 EA와 수량 차액을 직접 계산할 수 없음.',
        '정확한 주문번호 연결은 출고 실적 후보를 연결하지만, 청구 상세 행과 WMS 상품 행은 단위가 달라 행수 동일성을 뜻하지 않음.',
        'WMS 주문번호가 청구 택배 상세에 없는 경우는 후보일 뿐 전체 청구 누락으로 확정하지 않음. 청구 상세가 전체 B2C 주문을 전부 포함하는지 확인되지 않음.',
        '6월 WMS는 사용자 결정에 따라 가상센터 비교에 포함했지만 회사·창고·단위 열이 공란이므로 6월 EA 비교는 하지 않음.',
        '포장비 역산·WMS 잠정 수량/금액 비교는 cost_verification.json의 b2c_charge에 별도 기록. 택배비를 상품 EA당 단가로 적용하지 않음.'],
      'full_invoice_validated':False,'processing_status':'completed','waiting_for_input':False}
    from numeric_key_reconciliation import run as restore_numbers
    restoration=restore_numbers(root,parcel,matched_all,PERIOD,include_reference=include_reference)
    result['sources']['internal_b2c_workbook_used']=restoration['reference_usage']['used']
    result['calculation_executed']=True
    result['llm_used']=False
    result['generated_at_utc']=datetime.now(timezone.utc).isoformat()
    result['calculation_code_sha256']=digest(Path(__file__))
    result['identifier_restoration']={'result_path':'data/demo_company/derived/numeric_key_reconciliation.json',
        'result_sha256':digest(root/'data/demo_company/derived/numeric_key_reconciliation.json'),
        'comparison':{k:v for k,v in restoration['comparison'].items() if not k.endswith('_orders')},
        'optional_reference_used':restoration['reference_usage']['used'],
        'basis':'원본 직접 대사와 보조자료에서 복원한 번호 대사는 별도 결과로 보존'}
    for source in deps:
        if digest(root/source['path']) != source['sha256']:
            raise ValueError('input_changed_during_b2c_reconciliation')
    out=root/'data/demo_company/derived/b2c_actual_claim_wms.json'
    out.write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str)+'\n',encoding='utf-8')
    write_differences(root,parcel,matched_all,unmatched_claim,july_by_order)
    from warehouse_reconciliation_report import refresh
    if not defer_report:
        refresh(root)
    return result

def write_report(root,d):
    # The only user-facing report is the warehouse report.
    from warehouse_reconciliation_report import refresh
    return refresh(root)

def write_differences(root,parcel,matched,unmatched,wms_by_order):
    with (root/'data/demo_company/derived/B2C_실제청구_WMS_차이.csv').open('w',encoding='utf-8-sig',newline='') as f:
        x=csv.writer(f);x.writerow(['청구시트','청구행','필드','대사기준','결과','판정'])
        for r in parcel:
            if r['order'] in unmatched:x.writerow(['B2C출고_택배',r['row'],'A(주문번호)','청구 주문번호 ↔ WMS 주문번호(연동)','WMS에서 미연결','미청구 확정 아님: 해당 상세 범위/기간 판단 필요'])
        for r in parcel:
            if r['order'] in matched:x.writerow(['B2C출고_택배',r['row'],'A(주문번호)','청구 주문번호 ↔ WMS 주문번호(연동)','WMS 주문번호 일치','WMS 출고일·상품행 근거는 JSON 집계에 보존'])

if __name__=='__main__':
    root=Path(sys.argv[1]) if len(sys.argv)>1 else Path(__file__).resolve().parents[2]
    r=run(root, include_reference='--with-reference' in sys.argv, defer_report='--defer-report' in sys.argv)
    print(json.dumps({'claim_rows':r['claim_parcel_detail']['rows'],'claim_unique_orders':r['claim_parcel_detail']['unique_order_numbers'],
      'matched_order_numbers':r['claim_parcel_detail']['order_numbers_matched_to_scoped_wms'],
      'claim_orders_unmatched':r['claim_parcel_detail']['claim_order_numbers_not_found_in_included_wms'],
      'july_wms_rows':r['scoped_july_wms']['b2c_rows_in_period'],'july_wms_dates':[min(r['scoped_july_wms']['outbound_dates']),max(r['scoped_july_wms']['outbound_dates'])],
      'june_wms_rows':r['included_june_wms']['b2c_rows_2029_06_26_to_30'],
      'june_only_claim_matches':r['included_june_wms']['claim_orders_found_only_in_june']},ensure_ascii=False))
