"""Read registered claim/WMS originals directly. Never reads the verification workbook."""
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path
import argparse
import csv
import json
import sys
import uuid
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent))
from invoice_tools import digest, encode, number
from wms_tools import xlsx_rows, xlsb_rows, validated_rows, day, key_text
from claim_wms_rules import explain, july_date
from work_quantity_tools import read_work_specs, compare_work_quantities
from portable_workbook import open_workbook
from identifier_tools import expand_identifier

CONFIG = 'ia/config/demo_company_direct_wms.json'

def approved_path(root, relative, boundary):
    path=(root/relative).resolve()
    if not path.is_relative_to((root/boundary).resolve()):
        raise ValueError('path_outside_scope')
    return path

def add(groups, key, qty, evidence, name='', channel='', signature=None):
    g=groups.setdefault(key, {'quantity':Decimal(0),'consumables':Decimal(0),'rows':[], 'types':[], 'signatures':{}})
    g['quantity']+=qty
    if '(소모품)' in name:g['consumables']+=qty
    g['rows'].append(evidence)
    if channel and channel not in g['types']:g['types'].append(channel)
    if signature is not None:
        entry=g['signatures'].setdefault(signature,{'quantity':qty,'consumable': '(소모품)' in name,'rows':[]})
        entry['rows'].append(evidence)

def outcome(key, claim, wms, scope):
    r=explain(claim,wms)
    r.update(key=list(key),scope=scope,claim_evidence=claim['rows'] if claim else [],
             wms_evidence=wms['rows'] if wms else [],wms_types=wms['types'] if wms else [],
             candidates=[],judgment='quantity_comparison_only')
    if r['candidate']:
        r['candidates'].append({'name':r['candidate'],'residual':r['residual_after_candidate'],
                                'confirmed':False,'basis':'WMS 품명에 (소모품) 명시된 수량'})
    if claim and wms and r['difference_claim_minus_wms']:
        repeated=[v for v in wms['signatures'].values() if len(v['rows'])>1]
        removed=sum((v['quantity']*(len(v['rows'])-1) for v in repeated),Decimal(0))
        if removed:
            r['candidates'].append({'name':'동일 주요필드 반복행을 한 번씩 집계하는 가설',
                'removed_quantity':removed,'residual':claim['quantity']-(wms['quantity']-removed),
                'confirmed':False,'evidence':repeated,'note':'실제 분할인지 중복인지 미확정. 원문 집계는 유지.'})
            if wms['consumables']:
                non_material_removed=sum((v['quantity']*(len(v['rows'])-1) for v in repeated if not v['consumable']),Decimal(0))
                r['candidates'].append({'name':'소모품 제외 + 상품 반복행 한 번 집계 가설',
                    'residual':claim['quantity']-(wms['quantity']-wms['consumables']-non_material_removed),
                    'confirmed':False,'note':'소모품 반복분은 소모품 제외에 이미 포함되므로 이중 차감하지 않음.'})
    return r

def run(root, output=None):
    root=Path(root).resolve()
    cp=approved_path(root,CONFIG,'ia/config');config=json.loads(cp.read_text('utf-8-sig'))
    dp=approved_path(root,config['decisions_path'],'ia/config');decisions=json.loads(dp.read_text('utf-8-sig'))
    period=decisions['comparison_period']
    if period.get('state')!='confirmed_by_user':raise ValueError('period_decision_required')
    if decisions['outbound_types'].get('mode')!='all_types_for_claim_order':raise ValueError('outbound_decision_required')
    start,end=period['start'],period['end']
    paths={k:approved_path(root,v['path'],'data/demo_company/source') for k,v in config['sources'].items()}
    dependencies=[dict(role=k,**v) for k,v in config['sources'].items()]
    dependencies += [{'role':'config','path':CONFIG,'sha256':digest(cp)},
                     {'role':'decisions','path':config['decisions_path'],'sha256':digest(dp)}]
    for name in ['claim_wms_pipeline.py','claim_wms_rules.py','wms_tools.py','invoice_tools.py','work_quantity_tools.py','identifier_tools.py']:
        p=root/'ia/src'/name
        dependencies.append({'role':'code','path':p.relative_to(root).as_posix(),'sha256':digest(p)})
    def validate_sources():
        for s in dependencies:
            if digest(root/s['path'])!=s['sha256']:raise ValueError('input_changed:'+s['role'])
    validate_sources()
    claims={'inbound_june':{},'inbound_july':{},'outbound':{}}
    wms={k:{} for k in claims}; missing=[]; outside_claim=[]; claim_signatures=Counter(); profiles={}
    with open_workbook(str(paths['claim'])) as book:
        for direction,mapping in config['claim_mappings'].items():
            m=mapping['claim'];sheet_name=mapping['claim_sheet']
            with book.get_sheet(sheet_name) as sheet:
                for n,cells in validated_rows(xlsb_rows(sheet),mapping['claim_headers']):
                    if n<mapping['claim_start'] or not any(k in cells for k in m['key']):continue
                    date=day(cells.get(m['date']))
                    ev={'source':'claim','sheet':sheet_name,'row':n}
                    if not start<=date<=end:
                        outside_claim.append(dict(ev,date=date));continue
                    key=(date,)+tuple(key_text(cells.get(k)) for k in m['key'])
                    group='outbound' if direction=='outbound' else 'inbound_june' if date<'2029-07-01' else 'inbound_july'
                    if group=='inbound_june':key=(date,key[-1])
                    try:qty=number(cells.get(m['quantity']))
                    except ValueError:
                        missing.append(dict(ev,key=list(key),reason='상품 수량 빈칸',box=cells.get('F',{}).get('value'),freight=cells.get('J',{}).get('value')));continue
                    add(claims[group],key,qty,ev,str(cells.get(m.get('name'),{}).get('value','')))
                    if direction=='outbound':
                        signature=tuple((c,str(v['value'])) for c,v in sorted(cells.items()) if c in 'ABCDEFGHIKLMNOP')
                        claim_signatures[signature]+=1
    summary_signatures=Counter()
    with open_workbook(str(paths['claim_summary'])) as book:
        with book.get_sheet('B2B출고') as sheet:
            for n,cells in validated_rows(xlsb_rows(sheet),config['claim_mappings']['outbound']['claim_headers']):
                if n<4 or 'A' not in cells:continue
                date=day(cells.get('D'))
                if start<=date<=end and 'G' in cells:
                    summary_signatures[tuple((c,str(v['value'])) for c,v in sorted(cells.items()) if c in 'ABCDEFGHIKLMNOP')]+=1
    document_difference={'columns':'B2B출고 A:P 중 J운임 제외(날짜·수량·피킹·팩킹 등)',
        'claim_only':[{'values':dict(k),'count':v} for k,v in (claim_signatures-summary_signatures).items()],
        'summary_only':[{'values':dict(k),'count':v} for k,v in (summary_signatures-claim_signatures).items()]}
    claim_orders={k[1] for k in claims['outbound']}
    deferred=defaultdict(dict); outside=Counter(); outside_qty=defaultdict(Decimal)
    # Read every in-period inbound record, including keys absent from the claim.
    for role,sheet,cols,expected in [
        ('inbound_h1','입고 Data',{'date':'B','keys':['F'],'qty':'I','name':'G','type':None},
         {'A1':'회사구분','B1':'입고일','F1':'품번','I1':'수량(EA)','M1':'입고창고'}),
        ('inbound_july','Sheet1',{'date':'D','keys':['F','Q'],'qty':'W','name':'R','type':'M'},
         {'B1':'회사구분','D1':'입고일','F1':'입고예정번호','Q1':'품번','V1':'단위','W1':'수량','AK1':'입고창고'})]:
        rows_seen=0;total=Decimal(0);types=Counter()
        for n,cells in validated_rows(xlsx_rows(paths[role],sheet),expected):
            if n==1:continue
            if not any(c in cells for c in cols['keys']):continue
            rows_seen+=1;date=day(cells.get(cols['date']));qty=number(cells.get(cols['qty']))
            if not start<=date<=end:outside[role]+=1;outside_qty[role]+=qty;continue
            company=key_text(cells.get('A' if role=='inbound_h1' else 'B'));warehouse=key_text(cells.get('M' if role=='inbound_h1' else 'AK'))
            if company!='[90] DEMO COMPANY' or warehouse!='[DM001]DEMO':raise ValueError('inbound_scope_mismatch')
            if role=='inbound_july' and key_text(cells.get('V'))!='EA':raise ValueError('inbound_unit_mismatch')
            key=(date,)+tuple(key_text(cells.get(c)) for c in cols['keys'])
            channel=str(cells.get(cols['type'],{}).get('value','')) if cols['type'] else '구분열 없음'
            target='inbound_june' if role=='inbound_h1' else 'inbound_july'
            ev={'source':role,'sheet':sheet,'row':n}
            add(wms[target],key,qty,ev,str(cells.get(cols['name'],{}).get('value','')),channel)
            types[channel]+=1;total+=qty
        profiles[role]={'scanned_records':rows_seen,'in_period_quantity':total,'in_period_types':dict(types)}
    # Outbound dates/order keys are independent of the optional verification workbook.
    def collect_out(role,date,order,sku,name,qty,channel,ev,shipment,lot,expiry):
        order, sku, shipment = map(expand_identifier, (order, sku, shipment))
        if not start<=date<=end:
            outside[role]+=1;outside_qty[role]+=qty
            if order in claim_orders:add(deferred['same_order_outside_period'],(date,order),qty,ev,name,channel)
            return
        # Other B2C orders cannot become B2B omission candidates merely by absence.
        if order not in claim_orders and channel in config['outbound_unbilled_types']['out_of_scope']:
            outside['outbound_b2c_records']+=1;outside_qty['outbound_b2c_records']+=qty;return
        if not order:
            add(deferred['missing_order'],(date,shipment or str(ev)),qty,ev,name,channel);return
        key=(date,order)
        sig=json.dumps([shipment,order,sku,lot,expiry,str(qty),channel],ensure_ascii=False)
        add(wms['outbound'],key,qty,ev,name,channel,sig if shipment else None)
    role='outbound_h1';sheet='출고 Data(04월~06월)';count=0;types=Counter()
    expected={'B1':'출고일','I1':'구분','J1':'주문번호(연동)','P1':'품번','T1':'수량'}
    header=next(xlsx_rows(paths[role],sheet))[1]
    lot_col=next((k for k,v in header.items() if v['value']=='제조LOT'),None)
    expiry_col=next((k for k,v in header.items() if v['value']=='유통기한'),None)
    columns={'B','C','I','J','P','Q','T'}|{c for c in [lot_col,expiry_col] if c}
    for n,c in validated_rows(xlsx_rows(paths[role],sheet,columns=columns),expected):
        if n==1:continue
        count+=1;date=day(c.get('B'));qty=number(c.get('T'))
        channel=str(c.get('I',{}).get('value',''));types[channel]+=1
        collect_out(role,date,str(c.get('J',{}).get('value','')).strip(),str(c.get('P',{}).get('value','')),
            str(c.get('Q',{}).get('value','')),qty,channel,{'source':role,'sheet':sheet,'row':n},
            str(c.get('C',{}).get('value','')),str(c.get(lot_col,{}).get('value','')),str(c.get(expiry_col,{}).get('value','')))
    profiles[role]={'scanned_records':count,'scanned_types':dict(types),'scope':'등록파일 범위; 회사/창고 빈 열은 별도 식별 근거 아님'}
    role='outbound_july';count=0;types=Counter()
    with paths[role].open(encoding='utf-8-sig',newline='') as f:
        reader=csv.DictReader(f)
        required={'회사구분','출고창고','단위','출고일','주문번호(연동)','품번','품명','수량','구분','출고번호'}
        if not required.issubset(reader.fieldnames or []):raise ValueError('csv_headers_changed')
        for n,r in enumerate(reader,2):
            if None in r or None in r.values():raise ValueError('malformed_csv')
            if (r['회사구분'],r['출고창고'],r['단위'])!=('90','DM001','EA'):raise ValueError('csv_scope_unit_mismatch')
            qty=Decimal(r['수량'].replace(',','').strip())
            if not qty.is_finite():raise ValueError('invalid_quantity')
            date=july_date(r['출고일'],decisions['yearless_date']);count+=1;types[r['구분']]+=1
            collect_out(role,date,r['주문번호(연동)'].strip(),r['품번'],r['품명'],qty,r['구분'],
                {'source':role,'csv_record':n,'physical_end_line':reader.line_num},r['출고번호'],r.get('제조LOT',''),r.get('유통기한',''))
    profiles[role]={'scanned_records':count,'scanned_types':dict(types)}
    # Trace other invoiced work before describing missing B2B rows as unbilled.
    other_cost={};unmatched_orders={k[1] for k in wms['outbound'] if k not in claims['outbound']}
    search_sheets=['입고 외 기타','B2B 기타','B2B 반품','반품리스트','청구누락','임가공-라벨','차감항목','B2C출고_용차']
    with open_workbook(str(paths['claim'])) as book:
        for sheet_name in search_sheets:
            if sheet_name not in book.sheets:continue
            with book.get_sheet(sheet_name) as sheet:
                for n,cells in xlsb_rows(sheet):
                    hits={str(v['value']).strip() for v in cells.values()} & unmatched_orders
                    for order in hits:
                        other_cost.setdefault(order,[]).append({'source':'claim','sheet':sheet_name,'row':n,
                            'work_label':str(cells.get('A',{}).get('value','')),
                            'quantity_raw':cells.get('E',{}).get('value'),
                            'unit_price_raw':cells.get('F',{}).get('value'),
                            'amount_raw':cells.get('G',{}).get('value'),
                            'note':'다른 작업/단위일 수 있어 출고 EA와 직접 합산하지 않음.'})
    other_orders={k:v for k,v in wms['outbound'].items() if k not in claims['outbound'] and k[1] in other_cost}
    work_specs=read_work_specs(paths['claim'],{k[1] for k in other_orders})
    work_checks,work_pending=compare_work_quantities(other_orders,work_specs,wms['inbound_july'],decisions.get('work_quantity_units',{}))
    rows=[];exclusions=[];questions=[]
    approved_exclusions={tuple(k) for k in config['approved_inbound_exclusions']['keys']}
    allowed_types=set(config['approved_inbound_exclusions']['types'])
    for scope in claims:
        for key in sorted(set(claims[scope])|set(wms[scope])):
            c,w=claims[scope].get(key),wms[scope].get(key)
            r=outcome(key,c,w,scope)
            if scope=='inbound_july' and c is None and key in approved_exclusions and set(w['types']).issubset(allowed_types):
                r['judgment']='excluded_by_user';exclusions.append(r);continue
            if c is None:
                product=decisions.get('product_inbound_decision',{})
                if scope=='inbound_july' and list(key)==product.get('key') and w['quantity']==product.get('quantity') and product.get('state')=='confirmed_unresolvable_by_user':
                    r['judgment']='not_determinable_by_user';r['skip_only']='billing_omission_judgment'
                elif scope=='outbound':
                    known=set(config['outbound_unbilled_types']['confirmed_b2b'])
                    r['scope_status']='explicit_b2b' if set(w['types']).issubset(known) else 'business_scope_unconfirmed'
                    r['judgment']='awaiting_user_decision'
                    r['other_cost_evidence']=other_cost.get(key[1],[])
                    if r['other_cost_evidence']:
                        r['cause']='다른 청구 작업 항목에 동일 주문번호 기재'
                        r['cause_status']='원문 확인; B2B 청구 대상 제외 여부는 별도 기준'
                        r['cause_evidence']='B2B출고표에는 없지만 '+','.join(sorted({x['sheet'] for x in r['other_cost_evidence']}))+'에 있음. 출고수량과 임가공수량을 같은 단위로 가정하지 않음.'
                        if decisions.get('other_sheet_policy',{}).get('state')=='confirmed_by_user':
                            r['judgment']='excluded_from_b2b_omission_quantity_check_required'
                            r['show_as_separate_issue']=False
                else:r['judgment']='awaiting_user_decision'
            elif w is None:
                if key==('2029-07-10','DEMO-LEGACY-ORDER') and c['quantity']==17 and decisions['skip_decisions'].get('outbound_claim_wms.unlinked')=='confirmed_unresolvable_by_user':
                    r['judgment']='not_determinable_by_user'
                else:r['judgment']='awaiting_user_decision'
                r['other_date_candidates']=[{'key':list(k),'quantity':g['quantity'],'rows':g['rows']} for k,g in wms[scope].items() if k[1:]==key[1:] and k!=key]
            for decision in decisions.get('direct_wms_unbilled',[]):
                if (scope==decision.get('scope') and list(key)==decision.get('key') and c is None
                    and w['quantity']==decision.get('quantity') and decision.get('state')=='confirmed_by_user'):
                    action=decision.get('action')
                    if action=='separate_other_cost' and r.get('other_cost_evidence'):
                        r['judgment']='other_cost_item_by_user'
                    elif action=='skip_omission_judgment':r['judgment']='not_determinable_by_user'
                    elif action=='exclude_from_b2b':r['judgment']='excluded_from_b2b_by_user'
                    elif action=='retain_without_judgment':
                        r['judgment']='retained_for_review_by_user'
                        r['omission_assessed']=False
                        r['cause']='사용자 요청으로 내역 유지; 이동/비청구 여부 미확정'
                        r['cause_evidence']='번호 접두어로 WMS 유형이나 청구 대상 여부를 확정하지 않음. 원문 유형 표기 유지.'
                    r['user_decision']=decision
            rows.append(r)
    # Missing quantity is preserved as unknown, not assigned the entire base order.
    for item in missing:
        item['judgment']='awaiting_user_decision'
        if item['key']==['2029-07-07','DEMO-ED435D9507 불량 재출고'] and item['box']==4 and item['freight']==150000 and decisions.get('reship_quantity',{}).get('state')=='confirmed_unresolvable_by_user':
            item['judgment']='not_determinable_by_user'
    pending=Counter((r['scope'],r['kind']) for r in rows if r['judgment']=='awaiting_user_decision')
    for (scope,kind),count in pending.items():
        questions.append({'id':scope+'.'+kind,'scope':scope,'kind':kind,'count':count,'state':'awaiting_user_decision'})
    if any(x['judgment']=='awaiting_user_decision' for x in missing):questions.append({'id':'missing_quantity','state':'awaiting_user_decision'})
    if deferred.get('missing_order'):questions.append({'id':'wms_missing_order','count':len(deferred['missing_order']),'state':'awaiting_user_decision'})
    for kind in work_pending:questions.append({'id':'work_quantity.'+kind,'state':'awaiting_user_decision'})
    review=[{'key':r['key'],'quantity':r['wms_quantity'],'types':r['wms_types']} for r in rows if r['judgment']=='retained_for_review_by_user']
    validate_sources()
    result={'schema_version':'1.0','execution_id':datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'_'+uuid.uuid4().hex[:8],
        'scope':'DEMO COMPANY 가상센터 입고·B2B 청구 중심 WMS 직접 대사','period':period,'sources':config['sources'],
        'dependencies':dependencies,'reference_workbook_used':False,'prior_results_used':False,
        'llm_used':False,'agent_runtime_connected':False,'full_invoice_validated':False,
        'original_quantities_preserved':True,'wms_rows_deduplicated':False,
        'verification_workbook_duplicate_policy_applied':False,
        'notes':['기존 지정 두 주문의 반복행 조정은 검증파일에 대한 정책. WMS 원문에는 자동 적용하지 않음.',
                 '6월입고는 입고예정번호가 없어 날짜+품번 후보 비교. 7월은 날짜+입고예정번호+품번.',
                 '수량 차이/후보 일치는 금액 적정성이나 계약상 과금 승인을 뜻하지 않음.'],
        'rows':rows,'excluded_by_user':exclusions,'missing_claim_quantity':missing,'outside_claim':outside_claim,
        'document_difference':document_difference,'profiles':profiles,'outside_scope_records':dict(outside),
        'other_cost_search_sheets':search_sheets,
        'work_quantity_checks':work_checks,'work_specs':work_specs,'review_items':review,
        'outside_scope_quantities':dict(outside_qty),'deferred':{k:[{'key':list(key),**g} for key,g in v.items()] for k,v in deferred.items()},
        'counts':dict(Counter(r['kind'] for r in rows)),
        'unmatched_by_scope_type':dict(Counter(r['scope']+' / '+','.join(r['wms_types']) for r in rows if r['kind']=='wms_only')),
        'questions':questions,'processing_status':'awaiting_user_decision' if questions else 'review_pending' if review else 'completed','waiting_for_input':bool(questions)}
    out=approved_path(root,output or 'data/demo_company/derived/direct_wms_result.json','data/demo_company')
    out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(result,ensure_ascii=False,indent=2,default=encode)+'\n',encoding='utf-8')
    print(json.dumps({'output':out.relative_to(root).as_posix(),'counts':result['counts'],
        'unmatched':result['unmatched_by_scope_type'],'questions':questions,'reference_workbook_used':False},ensure_ascii=False),flush=True)
    return result

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--root',default=str(Path(__file__).resolve().parents[2]));parser.add_argument('--output')
    args=parser.parse_args();run(args.root,args.output)
