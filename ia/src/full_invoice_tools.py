"""Full claim-line coverage and B2C original-source comparisons. No LLM/verification file."""
from pathlib import Path
from collections import Counter,defaultdict
from decimal import Decimal
import argparse,csv,json,sys
sys.path.insert(0,str(Path(__file__).resolve().parent))
from portable_workbook import open_workbook
from invoice_tools import number,digest,encode,verify_rules,check
from wms_tools import day,xlsx_rows,validated_rows
from claim_wms_rules import july_date
from claim_wms_pipeline import approved_path
from xlsb_scan import scan_rows,filter_subtotals
from identifier_tools import expand_identifier, billable_amount

def value(c,key):return c.get(key,{}).get('value')
def ident(v):
    return expand_identifier(v)

def put(groups,key,qty,ref):
    g=groups.setdefault(key,{'quantity':Decimal(0),'evidence':[]})
    g['quantity']+=qty;g['evidence'].append(ref)

def compare(a,b,label):
    counts=Counter();out=[]
    for k in sorted(a.keys()|b.keys()):
        av=a.get(k);bv=b.get(k)
        kind='wms_only' if av is None else 'claim_only' if bv is None else 'match' if av['quantity']==bv['quantity'] else 'difference'
        counts[kind]+=1
        # Normal rows are represented by the count; preserve row-level references only for exceptions.
        if kind!='match':
            out.append({'key':list(k),'status':kind,'claim_quantity':av['quantity'] if av else None,
                        'wms_quantity':bv['quantity'] if bv else None,
                        'difference':av['quantity']-bv['quantity'] if av and bv else None,
                        'claim_evidence':av['evidence'][:2] if av else [],'wms_evidence':bv['evidence'][:2] if bv else []})
    return {'label':label,'counts':dict(counts),'rows':out,'total_groups':sum(counts.values()),
            'normal_detail_rows_omitted':counts['match'],'billing_omission_confirmed':False}

def raw_groups(book,name,period):
    groups={};shipments={};packages={};orders={};outside=[];invalid=[];profile={'rows':0,'in_period_rows':0}
    tracking='Y' if name=='총 LAW' else 'AH'
    expected={'B':'출고일','C':'출고번호','L':'주문번호(연동)','P':'품번','V':'단위','W':'수량',tracking:'송장번호'}
    for n,c in scan_rows(book,name):
        profile['rows']+=1
        if n==1:
            for k,v in expected.items():
                if value(c,k)!=v:raise ValueError('b2c_header_changed:'+name+':'+k)
            continue
        ref={'source':'b2c','sheet':name,'row':n}
        try:date=day(c.get('B'));qty=number(c.get('W'))
        except ValueError:invalid.append(dict(ref,reason='출고일 또는 상품 수량 없음'));continue
        if value(c,'V')!='EA':raise ValueError('b2c_unit_changed')
        if not period['start']<=date<=period['end']:outside.append(dict(ref,date=date,quantity=qty));continue
        ship=ident(value(c,'C'));sku=ident(value(c,'P'));order=ident(value(c,'L'));track=ident(value(c,tracking))
        if not ship or not sku:invalid.append(dict(ref,reason='출고번호 또는 품번 없음'));continue
        profile['in_period_rows']+=1
        put(groups,(date,ship,sku),qty,ref)
        put(shipments,(date,ship),qty,ref)
        if order:put(orders,(date,order,sku),qty,ref)
        # Stored order + tracking is a document grouping, never a physical box assumption.
        if order and track:put(packages,(order,track),qty,ref)
        else:invalid.append(dict(ref,reason='주문번호 또는 송장번호 없음; 출고번호 수량 비교는 수행'))
    return groups,shipments,packages,{'profile':profile,'outside_period':outside,'invalid_rows':invalid},orders

def combine(a,b):
    out={}
    for groups in [a,b]:
        for k,g in groups.items():
            out.setdefault(k,{'quantity':Decimal(0),'evidence':[]})
            out[k]['quantity']+=g['quantity'];out[k]['evidence']+=g['evidence']
    return out

def load_wms(paths,period,decisions,claim_ships,claim_orders):
    groups={};shipments={};orders={};profiles={};invalid=[]
    def add(date,ship,order,sku,qty,channel,ref):
        if not period['start']<=date<=period['end']:return
        if channel not in ('B2C','B2C주문','비연동(B2C)') and ship not in claim_ships and order not in claim_orders:return
        if order and sku:put(orders,(date,order,sku),qty,ref)
        else:invalid.append(dict(ref,reason='주문번호/품번 없음'))
        if not ship or not sku:invalid.append(dict(ref,reason='출고번호/품번 없음'));return
        put(groups,(date,ship,sku),qty,ref);put(shipments,(date,ship),qty,ref)
    sheet='출고 Data(04월~06월)';count=0
    headers={'B1':'출고일','C1':'출고번호','I1':'구분','J1':'주문번호(연동)','P1':'품번','T1':'수량'}
    for n,c in validated_rows(xlsx_rows(paths['outbound_h1'],sheet,columns={'B','C','I','J','P','T'}),headers):
        if n==1:continue
        count+=1;date=day(c.get('B'))
        if not period['start']<=date<=period['end']:continue
        add(date,ident(value(c,'C')),ident(value(c,'J')),ident(value(c,'P')),number(c.get('T')),value(c,'I'),{'source':'outbound_h1','sheet':sheet,'row':n})
    profiles['outbound_h1_rows']=count
    with paths['outbound_july'].open(encoding='utf-8-sig',newline='') as f:
        reader=csv.DictReader(f)
        required={'회사구분','출고창고','단위','출고일','출고번호','품번','수량','구분'}
        if not required.issubset(reader.fieldnames or []):raise ValueError('wms_csv_headers_changed')
        count=0
        for n,r in enumerate(reader,2):
            if None in r or None in r.values():raise ValueError('malformed_csv')
            if (r['회사구분'],r['출고창고'],r['단위'])!=('90','DM001','EA'):raise ValueError('wms_scope_unit_changed')
            count+=1;qty=Decimal(r['수량'].replace(',',''))
            if not qty.is_finite():raise ValueError('nonfinite_quantity')
            add(july_date(r['출고일'],decisions['yearless_date']),ident(r['출고번호']),ident(r['주문번호(연동)']),ident(r['품번']),qty,r['구분'],{'source':'outbound_july','csv_record':n})
        profiles['outbound_july_rows']=count
    return groups,shipments,profiles,invalid,orders

def parcel_tracking_comparison(claim_book,total_law):
    """Exact invoice tracking number ↔ B2C total-LAW tracking number presence only."""
    claim={}
    for n,c in scan_rows(claim_book,'B2C출고_택배'):
        if n<3:continue
        if not billable_amount(value(c,'D')):continue
        track=ident(value(c,'C'))
        if not track:continue
        claim.setdefault(track,[]).append({'source':'claim','sheet':'B2C출고_택배','row':n,
            'order':ident(value(c,'A')),'freight':value(c,'D')})
    wms={}
    for (order,track),g in total_law[2].items():
        if track:wms.setdefault(track,[]).extend(g['evidence'])
    missing=[{'tracking':t,'claim_evidence':refs[:2]} for t,refs in claim.items() if t not in wms]
    return {'claim_rows':sum(map(len,claim.values())),'unique_claim_tracking':len(claim),
        'unique_wms_tracking_in_period':len(wms),'exact_tracking_matches':len(claim)-len(missing),
        'claim_tracking_not_found_in_registered_wms':missing,
        'wms_only_not_classified':True,
        'limitation':'청구 행별 일자가 없어 기간 적합성을 행 단위로 검증하지 못함. WMS 송장번호 반복은 상품 행일 수 있어 박스 수로 해석하지 않음.'}

def summary_items(values,arithmetic):
    out=[];category='';item='';section=''
    for n,c in sorted(values.items()):
        cv=ident(value(c,'C'));gv=ident(value(c,'G'))
        if cv and not any(k in c for k in ['J','L','M','N']):section=cv;category='';item='';continue
        if cv in ('구분','합 계','합계') or n<20 or n>117:continue
        if not any(k in c for k in ['J','L','M','N']):continue
        if cv:category=cv
        if gv:item=gv
        tests=[x for x in arithmetic if x['id']==f'line.charge.{n}']
        ar=tests[0] if tests else None
        if ar and ar['check_performed']:astatus='completed'
        elif not all(isinstance(value(c,k),(int,float)) and not c[k].get('error') for k in ['L','M','N'] if k in c) or any(k not in c for k in ['L','M','N']):astatus='data_unavailable'
        else:astatus='not_implemented'
        out.append({'id':f'claim.line.{n}','sheet':'청구내역','row':n,'section':section,'category_context':category,
            'item_context':item if gv or not cv else cv,'raw_labels':{'C':value(c,'C'),'G':value(c,'G')},
            'unit':value(c,'J'),'unit_price':value(c,'L'),'quantity':value(c,'M'),'amount':value(c,'N'),
            'arithmetic':{'status':astatus,'result':ar['status'] if ar else None,'evidence':ar['evidence'] if ar else [f'청구내역!L{n}:N{n}'],
                          'reason':None if astatus=='completed' else '단가·수량·금액이 빈칸/문자/별도협의/실비여서 단순 곱셈 불가' if astatus=='data_unavailable' else '산술 규칙 미연결'},
            'detail_comparison':{'status':'not_implemented','reason':'청구 요약행과 상세 항목 연결 미구현'},
            'wms_cost_unit':{'status':'not_implemented','reason':'이 비용의 작업량·과금 단위에 맞춘 WMS/작업증빙 대사 미구현'},
            'contract':{'status':'not_implemented','reason':'계약 적용 조건·기간·항목별 단가 규칙 연결 미구현; 자료 부재로 확정하지 않음'}})
    return out

def run(root, include_reference=False, defer_report=False):
    root=Path(root).resolve();base=root/'data/demo_company/derived'
    cfg=json.loads((root/'ia/config/demo_company_direct_wms.json').read_text('utf-8-sig'))
    decisions=json.loads((root/cfg['decisions_path']).read_text('utf-8-sig'));period=decisions['comparison_period']
    if period.get('state')!='confirmed_by_user':raise ValueError('period_decision_required')
    cases=json.loads((root/'ia/config/coverage_demo.json').read_text('utf-8-sig'))['cases']
    sources=dict(cfg['sources'])
    if include_reference:
        bcase=next(x for x in cases if x['case_id']=='demo_brand_sf_xlsb_2')
        sources['b2c']={'path':bcase['source_path'],'sha256':bcase['source_sha256']}
    paths={k:approved_path(root,v['path'],'data/demo_company/source') for k,v in sources.items()}
    dependencies=[dict(role=k,**v) for k,v in sources.items()]
    for rel in ['ia/config/demo_company_direct_wms.json',cfg['decisions_path'],'ia/config/coverage_demo.json','ia/src/full_invoice_tools.py','ia/src/xlsb_scan.py','ia/src/invoice_tools.py','ia/src/wms_tools.py','ia/src/claim_wms_rules.py','ia/src/claim_wms_pipeline.py']:
        dependencies.append({'role':'implementation','path':rel,'sha256':digest(root/rel)})
    def guard():
        for x in dependencies:
            if digest(root/x['path'])!=x['sha256']:raise ValueError('source_changed:'+x['path'])
    guard();audit=[];sheets={};documents={};checks=[];filter_info=None
    for role in ['claim','claim_summary']:
        print('Reading '+role,flush=True)
        with open_workbook(str(paths[role])) as book:
            if role=='claim':filter_info=filter_subtotals(book,'입고리스트')
            for name in book.sheets:
                rows={};count=0;errors=0
                for n,c in scan_rows(book,name):
                    count+=1;errors+=sum(x['error'] for x in c.values())
                    if name!='B2C RAW':rows[n]=c
                audit.append({'source':role,'sheet':name,'nonempty_rows':count,'error_cells':errors,
                    'read_status':'completed','business_check_status':'not_implemented','reason':'시트 읽기와 전체 업무 검증은 다름'})
                if role=='claim':sheets[name]=rows
                if name=='청구내역':documents[role]=rows
    for role in ['claim','claim_summary']:
        case=next(x for x in cases if x['source_path']==sources[role]['path'])
        flat={col+str(n):v for n,c in documents[role].items() for col,v in c.items()}
        for check_ in verify_rules({'청구내역':flat},case.get('python_rules',[])):
            check_['source']=role;checks.append(check_)
    items=summary_items(documents['claim'],[x for x in checks if x['source']=='claim'])
    # Explicit same-document quantity references: comparison, not physical work proof.
    links={20:('입고리스트','L',4,None),21:('입고리스트','M',4,None),34:('B2B출고','L',4,None),35:('B2B출고','M',4,None),36:('B2B출고','N',4,None),37:('B2B출고','O',4,None),51:('B2B출고','P',4,None)}
    for item in items:
        n=item['row']
        if n not in links:continue
        sh,col,start,stop=links[n];vals=[number(c[col]) for rn,c in sheets[sh].items() if rn>=start and (stop is None or rn<=stop) and col in c and isinstance(value(c,col),(int,float)) and not c[col]['error']]
        target=documents['claim'][n].get('M');q=number(target) if target and isinstance(target['value'],(int,float)) else None
        result=check('detail.'+str(n),'청구 요약↔상세 집계',sum(vals,Decimal(0)),q,[f'{sh}!{col}{start}:마지막행',f'청구내역!M{n}'],'document_comparison')
        if n in (20,21) and filter_info['filters'] and col+'1' in filter_info['subtotal_cells']:
            fs=filter_info['filters']
            visible=[(rn,c) for rn,c in sheets[sh].items() if rn>=start and all(str(value(c,f['column'])).casefold() in [x.casefold() for x in f['criteria']] for f in fs)]
            filtered=sum((number(c[col]) for rn,c in visible if col in c and isinstance(value(c,col),(int,float))),Decimal(0))
            result['filter_diagnostic']={'filter':fs,'filtered_sum':filtered,'filtered_rows':[rn for rn,c in visible],
                'saved_subtotal':filter_info['subtotal_cells'][col+'1'],'agrees_with_saved_summary':q==filtered}
            if q==filtered:
                result['reason']='필터 적용 SUBTOTAL 저장값과 전체 상세 합계의 차이. 청구 제외 업무 기준 또는 과다청구로 판정하지 않음.'
                result['difference_class']='filter_dependent_saved_total'
        checks.append(dict(result,source='claim'));item['detail_comparison']={'status':'completed' if result['check_performed'] else 'data_unavailable','result':result['status'],'check_id':result['id'],'reason':result['reason']}
    if include_reference:
        print('Reading B2C sources',flush=True)
        raw={};meta={};pivots={}
        with open_workbook(str(paths['b2c'])) as book:
            for name in book.sheets:
                if name in ('총 LAW','선작업 제외 LAW','선작업 19건','B2B 택배'):
                    raw[name]=raw_groups(book,name,period);meta[name]=raw[name][3]
                    audit.append({'source':'b2c','sheet':name,'nonempty_rows':meta[name]['profile']['rows'],'read_status':'completed','business_check_status':'partial'})
                else:
                    vals=dict(scan_rows(book,name));pivots[name]=vals
                    audit.append({'source':'b2c','sheet':name,'nonempty_rows':len(vals),'read_status':'completed','business_check_status':'partial' if name in ('Sheet5','Sheet6') else 'not_implemented'})
        total=raw['총 LAW'];selected=raw['선작업 제외 LAW'];pre=raw['선작업 19건']
        partition=compare(total[0],combine(selected[0],pre[0]),'총 LAW ↔ 선작업 제외 LAW + 선작업 (동일 일자·출고번호·품번)')
        print('Comparing B2C WMS',flush=True)
        all_ships={k[1] for name in raw for k in raw[name][0]}
        all_orders={k[1] for name in raw for k in raw[name][4]}
        wms,wms_ship,wp,wi,wms_orders=load_wms(paths,period,decisions,all_ships,all_orders)
        order_comparison=compare(total[4],wms_orders,'원형 주문번호 표기 비교 (6·7월 진단; 숫자 치환 안 함)')
        primary_claim={k:v for k,v in total[4].items() if k[0]<'2029-07-01'}
        primary_claim.update({k:v for k,v in total[0].items() if k[0]>='2029-07-01'})
        primary_wms={k:v for k,v in wms_orders.items() if k[0]<'2029-07-01'}
        primary_wms.update({k:v for k,v in wms.items() if k[0]>='2029-07-01'})
        reconciliation=compare(primary_claim,primary_wms,'B2C 총 LAW ↔ WMS: 6월 일자+주문번호+품번 / 7월 일자+출고번호+품번')
        parcel_keys=raw['B2B 택배'][0]
        for row in reconciliation['rows']:
            k=tuple(row['key']);row['key_basis']='주문번호(연동)' if k[0]<'2029-07-01' else '출고번호'
            if row['status']=='wms_only' and k in parcel_keys and parcel_keys[k]['quantity']==row['wms_quantity']:
                row['resolution']='다른 청구 시트 B2B 택배와 수량 일치';row['other_claim_evidence']=parcel_keys[k]['evidence']
        reconciliation['review_counts']=dict(Counter(r['status'] for r in reconciliation['rows'] if not r.get('resolution')))
        reconciliation['other_sheet_quantity_matched']=sum(bool(r.get('resolution')) for r in reconciliation['rows'])
        # June WMS has no outbound_id values. Never report that key's absence as missing execution.
        shipments=compare({k:v for k,v in total[1].items() if k[0]>='2029-07-01'},wms_ship,'7월 보조 비교: 일자+출고번호 합계 (6월 출고번호 열은 빈칸)')
        selected_wms=compare(selected[4],wms_orders,'선작업 제외 LAW ↔ WMS: 일자+주문번호+품번 (선작업 제외 적용 전후 구분)')
        b2b_parcel=compare(raw['B2B 택배'][4],{k:v for k,v in wms_orders.items() if k in raw['B2B 택배'][4]},'B2B 택배 별도시트 주문 수량 비교')
        with open_workbook(str(paths['claim'])) as claim_book:
            b2c_parcel_tracking=parcel_tracking_comparison(claim_book,total)
        # Pivot source connection uses the displayed order/tracking keys. No inferred offsets.
        pivot_groups={};pivot_invalid=[]
        for n,c in pivots['Sheet6'].items():
            if n<2:continue
            if value(c,'A')=='총합계' or value(c,'B')=='총합계' or value(c,'C')=='총합계':continue
            order=ident(value(c,'B'));track=ident(value(c,'C'))
            if not order or not track or track=='(비어 있음)':
                if any(value(c,k) is not None for k in ['B','C','D']):pivot_invalid.append(n)
                continue
            try:q=number(c.get('D'))
            except ValueError:pivot_invalid.append(n);continue
            put(pivot_groups,(order,track),q,{'source':'b2c','sheet':'Sheet6','row':n})
        pivot_link=compare(pivot_groups,selected[2],'Sheet6 ↔ 선작업 제외 LAW (주문번호+송장번호 문서 집계)')
        policy=decisions.get('b2c_prework',{})
        packaging={'status':'awaiting_user_decision','reason':'선작업 제외 기준 답변 필요'}
        # Document quantities first; actual charge eligibility requires the recorded decision.
        if policy.get('mode')=='exclude_prework' and policy.get('state')=='confirmed_by_user':
            packaging={'status':'partial','policy_status':'applied','basis':'사용자 승인: 선작업 제외 LAW 기준','prework_shipments':len(pre[1]),'selected_shipments':len(selected[1]),'not_physical_box_count':True,
                'quantity_rule':'사용자 지시에 따라 문서 기재 합계·산술 비교만 수행; 포장 작업량 독립 산출 안 함'}
            for row,field in [(28,'E'),(31,'F')]:
                quantities=[];missing=[]
                for rn,c in pivots['Sheet6'].items():
                    if rn<2 or not ident(value(c,'B')) or not ident(value(c,'C')):continue
                    if any(value(c,k)=='총합계' for k in ('A','B','C')):continue
                    try:quantities.append(number(c.get(field)))
                    except ValueError:missing.append(rn)
                expected=sum(quantities,Decimal(0)) if not missing else None
                observed=number(documents['claim'][row].get('M'))
                chk=check('b2c.packaging.'+str(row),'B2C 포장 집계↔청구 수량',expected,observed,[f'Sheet6!{field}2:마지막행',f'청구내역!M{row}'],'document_comparison','빈 포장 작업수량은 0으로 채우지 않음' if missing else None)
                chk['missing_rows']=missing;checks.append(dict(chk,source='b2c'))
                next(x for x in items if x['row']==row)['detail_comparison']={'status':'completed' if chk['check_performed'] else 'data_unavailable','check_id':chk['id'],'result':chk['status'],'reason':'기본/추가 포장 수량 빈칸 존재' if missing else '문서 집계 비교; 포장 단위의 WMS 환산과 구분'}
    else:
        def not_requested(label):
            return {'label':label,'status':'not_requested','check_performed':False,
                    'reason':'선택적 내부 검증자료를 사용하지 않은 원본 실행',
                    'counts':{},'rows':[]}
        meta={};wp={};wi=[];pivot_invalid=[]
        reconciliation=not_requested('내부 집계자료 ↔ WMS')
        order_comparison=not_requested('내부 집계자료 주문번호 비교')
        shipments=not_requested('내부 집계자료 출고번호 비교')
        selected_wms=not_requested('내부 선작업 제외자료 ↔ WMS')
        b2b_parcel=not_requested('내부 B2B 택배자료 ↔ WMS')
        partition=not_requested('내부 시트 간 합계')
        pivot_link=not_requested('내부 피벗 ↔ 상세')
        b2c_parcel_tracking=not_requested('청구 ↔ 내부 집계자료 송장번호')
        b2c_parcel_tracking.update(unique_claim_tracking=0,exact_tracking_matches=0,
                                  claim_tracking_not_found_in_registered_wms=[])
        packaging={'status':'not_requested','check_performed':False,
                   'reason':'내부 집계 교차검사는 선택 사항. 실제 청구의 집계·산술과 원본 WMS 연결은 계속 수행'}
    # Per-line coverage: document quantity linkage and WMS charge units are distinct axes.
    for row,refs in {28:[('B2C출고','I',11)],31:[('B2C출고','I',12)],29:[('B2C출고',c,4) for c in 'DEG'],30:[('B2C출고',c,4) for c in 'CF']}.items():
        if row not in documents['claim'] or not billable_amount(value(documents['claim'][row],'N')):continue
        qty=sum((number(sheets[sh][rn].get(col)) for sh,col,rn in refs),Decimal(0))
        chk=check('b2c.summary.'+str(row),'B2C 비용 집계표↔청구 수량',qty,number(documents['claim'][row].get('M')),[','.join(f'{sh}!{col}{rn}' for sh,col,rn in refs),f'청구내역!M{row}'],'document_comparison')
        checks.append(dict(chk,source='claim'))
        next(x for x in items if x['row']==row)['detail_comparison']={'status':'completed','result':chk['status'],'check_id':chk['id'],'reason':'기재된 집계량 비교 완료; RAW부터 과금단위 재산출은 별도 미확정'}
    for item in items:
        n=item['row']
        item['physical_wms']={'status':'not_implemented','reason':'해당 비용의 독립 실적 검사는 미연결'}
        if n in (20,21,34,35,36,37):item['physical_wms']={'status':'partial','reason':'입고/B2B 상품 수량 비교는 direct_wms_result.json에서 수행. BOX/피킹/팩킹 비용 단위 검증과 구분'}
        if n in (28,29,30,31):item['physical_wms']={'status':'partial','reason':'실제 청구 주문↔원본 WMS 연결은 b2c_actual_claim_wms.json에서 수행. 청구에 주문별 EA가 없어 수량 차이는 산출하지 않음; 포장/샤쉐는 승인된 문서 합계·산술만 비교'}
        if n in (57,58):item['physical_wms']={'status':'partial','reason':'임가공6주문만 작업 단위 비교. 전체 임가공 비용 실적 대사는 미구현'}
    # No whole-item pass from arithmetic alone. No waiting item is silently finalized.
    for entry in audit:
        if entry['source']=='claim' and entry['sheet']=='청구내역':entry['business_check_status']='partial';entry['reason']='모든 청구행 목록화, 등록 산술 규칙 및 일부 상세 연결 수행; 계약/WMS 과금단위 검사 남음'
        elif entry['source']=='claim' and entry['sheet'] in ('입고리스트','B2B출고'):entry['business_check_status']='partial';entry['reason']='수량 대사는 기존 원본 도구. 전체 작업비 검증은 미완료'
        elif entry['source']=='claim' and entry['sheet']=='B2C출고_택배':entry['business_check_status']='partial';entry['reason']='실제 청구↔원본 WMS 연결은 별도 직접대사 도구. 내부 집계 비교는 선택적 진단; 행별 날짜·운임 적정성은 미검사'
        elif entry['source']=='claim' and entry['sheet']=='B2C출고':entry['business_check_status']='partial';entry['reason']='일부 B2C 비용 집계와 청구 수량 비교 수행; 전체 원천 과금 검증은 미완료'
        elif entry['source']=='claim' and entry['sheet']=='B2C출고_용차':entry['business_check_status']='not_implemented';entry['reason']='용차 일자·판매처별 WMS 실적 및 과금조건 연결 미구현'
        elif entry['source']=='claim' and entry['sheet']=='임가공-라벨':entry['business_check_status']='partial';entry['reason']='다른 시트 동일 주문 중 6건의 작업 수량만 비교; 전체 비용 검증 아님'
        elif entry['sheet']=='B2C RAW' and entry['nonempty_rows']==0:entry['business_check_status']='data_unavailable';entry['reason']='서식만 존재하고 저장된 값 없음. 실제 청구 B2C 택배 상세와 원본 WMS를 사용'
    pending=[k+': '+v['question'] for k,v in decisions.get('full_invoice_review',{}).items() if v.get('state')=='awaiting_user_decision']
    result={'schema_version':'1.0','company_id':'demo_company','period':period,'sources':sources,'dependencies':dependencies,
        'sheet_inventory':audit,'claim_items':items,'checks':checks,'inbound_filter_evidence':filter_info,'b2c':{'raw_profiles':meta,'wms_profile':wp,'wms_invalid':wi,
        'reconciliation':reconciliation,'order_comparison':order_comparison,'shipments':shipments,'selected_wms':selected_wms,'b2b_parcel':b2b_parcel,'b2c_parcel_tracking':b2c_parcel_tracking,'partition':partition,'pivot_link':pivot_link,'pivot_invalid_rows':pivot_invalid,'packaging':packaging},
        'internal_reference_used':include_reference,
        'pending_decisions':pending,
        'full_invoice_validated':False,'llm_used':False,'processing_status':'awaiting_user_decision' if pending else 'review_pending','waiting_for_input':bool(pending)}
    guard();base.mkdir(parents=True,exist_ok=True)
    path=base/'full_invoice_result.json';tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(result,ensure_ascii=False,default=encode,separators=(',',':')),encoding='utf-8');tmp.replace(path)
    write_reports(base,result,defer_report=defer_report)
    print(json.dumps({'claim_items':len(items),'sheets':len(audit),'arithmetic':dict(Counter(x['status'] for x in checks)),'b2c':reconciliation['counts'],'b2c_parcel':{k:v for k,v in b2c_parcel_tracking.items() if k!='claim_tracking_not_found_in_registered_wms'},'shipment':shipments['counts'],'partition':partition['counts'],'pivot':pivot_link['counts'],'packaging':packaging},ensure_ascii=False,default=encode),flush=True)
    return result

def write_reports(base,d,defer_report=False):
    names={'completed':'검사 완료','not_implemented':'미구현','data_unavailable':'자료 부족','partial':'일부 검사','awaiting_user_decision':'기준 답변 대기'}
    with (base/'청구전체_시트목록.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.writer(f);w.writerow(['원본역할','시트','값이있는행수','읽기','업무검사','사유'])
        for x in d['sheet_inventory']:w.writerow([x['source'],x['sheet'],x['nonempty_rows'],names[x['read_status']],names[x['business_check_status']],x.get('reason','부분 비교 결과는 전체 검사 JSON 참조')])
    with (base/'청구전체_검사범위.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.writer(f);w.writerow(['청구행','구분','항목','단위','청구수량','청구금액','산술','산술결과','상세집계','상품/작업 실적','과금단위','계약','남은 이유'])
        for x in d['claim_items']:
            w.writerow([x['row'],x['category_context'],x['item_context'],x['unit'],x['quantity'],x['amount'],names[x['arithmetic']['status']],x['arithmetic']['result'],names[x['detail_comparison']['status']],names[x['physical_wms']['status']],names[x['wms_cost_unit']['status']],names[x['contract']['status']],'; '.join(v.get('reason') or '' for v in [x['arithmetic'],x['detail_comparison'],x['physical_wms'],x['wms_cost_unit'],x['contract']])])
    with (base/'B2C_대사차이.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.writer(f);w.writerow(['비교','연결키','결과','청구수량','비교수량','차이','청구근거','비교근거'])
        for label in ['reconciliation','partition','pivot_link']:
            for r in d['b2c'][label]['rows']:
                if r['status']=='match' or r.get('resolution'):continue
                w.writerow([label,' / '.join(r['key']),r['status'],r['claim_quantity'],r['wms_quantity'],r['difference'],json.dumps(r['claim_evidence'][:2],ensure_ascii=False),json.dumps(r['wms_evidence'][:2],ensure_ascii=False)])
        for r in d['b2c']['b2c_parcel_tracking']['claim_tracking_not_found_in_registered_wms']:
            w.writerow(['B2C출고_택배 송장번호 존재 비교',r['tracking'],'청구송장 WMS 미연결(기간 미확정)',None,None,None,json.dumps(r['claim_evidence'],ensure_ascii=False),None])
    from warehouse_reconciliation_report import refresh
    if not defer_report:
        refresh(base.resolve().parents[2])

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--root',default=str(Path(__file__).resolve().parents[2]));parser.add_argument('--with-reference',action='store_true');parser.add_argument('--defer-report',action='store_true');args=parser.parse_args();run(args.root,include_reference=args.with_reference,defer_report=args.defer_report)
