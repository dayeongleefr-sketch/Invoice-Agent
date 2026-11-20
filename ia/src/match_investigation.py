"""Explore original invoice/WMS connections. Candidates never change approved rules."""
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path
from itertools import combinations
from functools import lru_cache
import csv, json, re, sys
sys.path.insert(0, str(Path(__file__).resolve().parent))

from portable_workbook import open_workbook
from xlsb_scan import scan_rows, filter_subtotals
from wms_tools import xlsx_rows, day
from actual_claim_wms_b2c import ident, normalize_date
from invoice_tools import digest
from identifier_tools import billable_amount

IDENTIFIERS = ('order', 'erp', 'tracking', 'shipment', 'asn', 'po', 'management')


def review_summary(data, decisions):
    """Apply only source-bound user decisions to connection labels, never quantities."""
    compatible = (decisions.get('period') == data['period'] and decisions.get('source_hashes') == {k:v['sha256'] for k,v in data['sources'].items()}
)
    reference_compatible = (compatible and decisions.get('optional_reference_sha256') is not None
                            and decisions.get('optional_reference_sha256') == data.get('optional_reference_diagnostic',{}).get('source',{}).get('sha256'))
    groups=defaultdict(list)
    for row in data['claim_candidates']:
        if row['kind']=='b2c':groups[row['order']].append(row)
    direct=[];bridged=[];annotated=[];pending_notes=[];remaining=[]
    for order,rows in groups.items():
        if any(r['status']=='existing_order_match' for r in rows):direct.append(order)
        elif reference_compatible and decisions.get('U20',{}).get('state')=='approved' and all(r.get('optional_reference_bridge',{}).get('status')=='all_reference_shipments_qty_equal' for r in rows):bridged.append(order)
        elif all(r.get('precision_investigation',{}).get('status')=='annotation_candidate' for r in rows):
            if compatible and decisions.get('U21',{}).get('state')=='approved':annotated.append(order)
            else:pending_notes.append(order)
        else:remaining.append(order)
    return {'source_bound_decisions_valid':compatible,'reference_bound_decisions_valid':reference_compatible,'direct_orders':direct,'reference_bridge_orders':bridged,
            'annotated_base_orders':annotated,'annotation_pending_orders':pending_notes,'remaining_orders':remaining,
            'counts':{'total':len(groups),'direct':len(direct),'reference_bridge':len(bridged),'annotated_base':len(annotated),'annotation_pending':len(pending_notes),'remaining':len(remaining)},
            'full_invoice_validated':False,'invoice_EA_validated':False,'freight_validated':False}


def refresh_claim_freight(root):
    """Read displayed freight from exact invoice rows; leave every match unchanged."""
    root = Path(root).resolve()
    target = root/'data/demo_company/derived/match_investigation.json'
    data = json.loads(target.read_text('utf-8'))
    source = data['sources']['claim']
    if digest(root/source['path']).upper() != source['sha256'].upper():
        raise ValueError('stale_invoice_freight')
    with open_workbook(str(root/source['path'])) as book:
        original = {n: {k: v.get('value') for k,v in cells.items()} for n,cells in scan_rows(book,'B2C출고_택배')}
    for row in data['claim_candidates']:
        if row['kind'] != 'b2c':
            continue
        n = row['row']
        cells = original[n]
        if row.get('claim_rows', [n]) != [n] or ident(cells.get('A')) != row['order'] or ident(cells.get('C')) != row['tracking']:
            raise ValueError('invoice_freight_row_identity_changed')
        value = decimal(cells.get('D'))
        row['invoice_stated_freight'] = str(value) if value is not None else None
        row['invoice_freight_evidence'] = f'B2C출고_택배!D{n}'
    tmp = target.with_suffix('.tmp')
    tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    tmp.replace(target)
    return data


@lru_cache(maxsize=32768)
def normalized(value):
    return re.sub(r'[\s\-]', '', value).upper()


@lru_cache(maxsize=8192, typed=True)
def date_of(value):
    try:
        return day({'value': value})
    except (ValueError, TypeError):
        try:return normalize_date(value)
        except (ValueError, TypeError):return ''


def decimal(value):
    try:
        d = Decimal(str(value).replace(',', ''))
        return d if d.is_finite() else None
    except Exception:return None


def quantity_subsets(groups, target, limit=100000):
    """Exact subset sums within an already anchored order; never across strangers.

    Counts are preserved, including multiple solutions. Resource caps are explicit.
    """
    states = {Decimal(0): (1, [])}
    for key, quantity in groups:
        if quantity <= 0:
            return {'status': 'not_searched_nonpositive_quantity', 'solutions': None}
        updates = {}
        for subtotal, (count, example) in list(states.items()):
            total = subtotal + quantity
            if total > target:continue
            old = updates.get(total, (0, []))
            updates[total] = (old[0] + count, old[1] or example + [key])
        for total, (count, example) in updates.items():
            old = states.get(total, (0, []))
            states[total] = (old[0] + count, old[1] or example)
        if len(states) > limit:
            # Exact reachability for integral EA: all subsets are represented by
            # bits, without enumerating exponentially many combinations.
            if target == target.to_integral_value() and all(q == q.to_integral_value() and q > 0 for _,q in groups):
                import math,functools
                divisor=functools.reduce(math.gcd,(int(q) for _,q in groups))
                if int(target)%divisor:return {'status':'searched_bitset','solutions':0,'has_match':False}
                t=int(target)//divisor
                if (t+1)*(len(groups)+1)>1024*1024*1024:
                    return {'status':'search_limit','limit_reason':'bitset history >128MiB','solutions':None}
                mask=(1<<(t+1))-1;bits=1;history=[]
                for _,q in groups:
                    history.append(bits);bits=(bits | bits<<(int(q)//divisor))&mask
                found=bool((bits>>t)&1);example=[];remaining=t
                if found:
                    for (key,q),previous in reversed(list(zip(groups,history))):
                        if not ((previous>>remaining)&1):example.append(key);remaining-=int(q)//divisor
                    assert remaining==0
                return {'status':'searched_bitset','has_match':found,'solutions':None if found else 0,'example_components':example,
                        'candidate_only':True,'note':'관련 품번 묶음의 모든 부분합 존재 여부 계산. 가능한 조합 총개수·유일성은 확정하지 않음'}
            return {'status': 'search_limit', 'limit': limit, 'solutions': None}
    count, example = states.get(target, (0, []))
    return {'status': 'searched', 'solutions': count, 'example_components': example,
            'candidate_only': True, 'note': '같은 연결 주문 내 품번 묶음 조합; 소모품·반복행 제외 규칙을 확정하지 않음'}


def run(root, include_reference=False):
    root = Path(root).resolve();base=root/'data/demo_company/derived'
    cfg=json.loads((root/'ia/config/demo_company_direct_wms.json').read_text('utf-8-sig'))
    decisions=json.loads((root/cfg['decisions_path']).read_text('utf-8-sig'))
    start,end=decisions['comparison_period']['start'],decisions['comparison_period']['end']
    roles=['claim','inbound_h1','inbound_july','outbound_h1','outbound_july']
    sources={k:cfg['sources'][k] for k in roles}
    sources['outbound_q1']=cfg['sources']['outbound_h1']
    for source in sources.values():
        if digest(root/source['path']).lower()!=source['sha256'].lower():raise ValueError('source_version_changed')
    claimrows=[];inventory=[];sheet_tokens=defaultdict(list);filters={};sheet_values={}
    print('Reading every actual invoice sheet (hidden/filtered rows included)',flush=True)
    with open_workbook(str(root/sources['claim']['path'])) as book:
        filters=filter_subtotals(book,'입고리스트')
        for name in book.sheets:
            count=0;entities=0;qtymissing=0;data={}
            for n,c in scan_rows(book,name):
                count+=1;v={k:x['value'] for k,x in c.items()};data[n]=v
                # Other sheets: identifier occurrence only, never a quantity match.
                for col,value in v.items():
                    token=ident(value)
                    if re.fullmatch(r'(?:SB|DS|MV|PO|KBT|RN)[A-Za-z0-9-]{5,}',token):
                        sheet_tokens[token].append({'sheet':name,'row':n,'column':col})
                row=None
                if name=='B2C출고_택배' and n>=3 and ident(v.get('A')) and billable_amount(v.get('D')):
                    row={'kind':'b2c','order':ident(v.get('A')),'tracking':ident(v.get('C')),'qty':None,'date':'','customer':'',
                         'invoice_stated_freight':decimal(v.get('D')), 'invoice_freight_evidence':f'B2C출고_택배!D{n}'}
                elif name=='B2B출고' and n>=4 and ident(v.get('A')):
                    row={'kind':'b2b','order':ident(v.get('A')),'erp':ident(v.get('B')),'tracking':ident(v.get('I')),
                         'qty':decimal(v.get('G')),'date':date_of(v.get('D')),'customer':ident(v.get('C'))}
                elif name=='입고리스트' and n>=4 and ident(v.get('B')):
                    row={'kind':'inbound','asn':ident(v.get('B')),'sku':ident(v.get('D')),'barcode':ident(v.get('F')),
                         'qty':decimal(v.get('H')),'date':date_of(v.get('A')),'customer':''}
                if row:
                    row.update(sheet=name,row=n);claimrows.append(row);entities+=1;qtymissing+=row['qty'] is None
            sheet_values[name]=data
            inventory.append({'sheet':name,'nonempty_rows_read':count,'mapped_transaction_rows':entities,
                              'quantity_missing_rows':qtymissing,'row_filter_applied':False,
                              'inspection':'typed_original_transactions' if entities else 'identifier_occurrences_and_sheet_inventory_only'})
    # Combine split lines in approved units; retain each parcel/tracking row.
    aggregated={};parcels=[]
    for row in claimrows:
        if row['kind']=='b2c':
            row['claim_rows']=[row['row']];parcels.append(row);continue
        key=(row['kind'],row['date'],'' if row['kind']=='inbound' and row['date']<'2029-07-01' else row.get('asn') or row.get('order'),row.get('sku',''))
        if key not in aggregated:aggregated[key]={**row,'claim_rows':[row['row']]}
        else:
            old=aggregated[key];old['claim_rows'].append(row['row'])
            old['qty']=old['qty']+row['qty'] if old['qty'] is not None and row['qty'] is not None else None
            for field in IDENTIFIERS:
                if old.get(field,'') != row.get(field,''):
                    old.setdefault('additional_identifiers',{}).setdefault(field,[]).append(row.get(field,''))
    claimrows=parcels+list(aggregated.values())
    exact_tokens={row.get(k,'') for row in claimrows for k in IDENTIFIERS}-{''}
    exact_tokens |= set(sheet_tokens)
    normalized_tokens={normalized(t) for t in exact_tokens}
    groups={};profiles={};invalid=Counter();outside_scope=Counter()
    def collect(role,n,r):
        direction='inbound' if role.startswith('inbound') else 'outbound'
        date=date_of(r.get('date'))
        if not date:invalid[role+'.date']+=1;return
        company=r.get('company','');warehouse=r.get('warehouse','')
        if role not in ('outbound_h1','outbound_q1') and (company not in ('90','[90] DEMO COMPANY') or warehouse not in ('DM001','[DM001]DEMO')):
            outside_scope[role]+=1;return
        in_period=start<=date<=end
        anchor=any(r.get(k,'') in exact_tokens or normalized(r.get(k,'')) in normalized_tokens for k in IDENTIFIERS if r.get(k))
        if not in_period and not anchor:return
        qty=decimal(r.get('qty'))
        if qty is None:invalid[role+'.qty']+=1
        identity=(r.get('asn') or r.get('shipment') or '') if direction=='inbound' else (r.get('shipment') or r.get('order') or r.get('erp') or r.get('tracking') or 'row:'+str(n))
        unit=r.get('unit','')
        key=(role,date,identity,r.get('sku','') if direction=='inbound' else '',unit,r.get('customer',''))
        g=groups.setdefault(key,{'source':role,'direction':direction,'date':date,'unit':unit,'customer':r.get('customer',''),
            'in_period':in_period,'ids':{k:set() for k in IDENTIFIERS},'skus':set(),'barcodes':set(),'types':set(),
            'quantity':Decimal(0),'missing_quantities':0,'consumable_quantity':Decimal(0),'sku_quantities':defaultdict(Decimal),
            'rows':[],'repeat_signatures':{}})
        for k in IDENTIFIERS:
            if r.get(k):g['ids'][k].add(r[k])
        if r.get('sku'):g['skus'].add(r['sku'])
        if r.get('barcode'):g['barcodes'].add(r['barcode'])
        g['types'].add(r.get('type',''));g['rows'].append(n)
        if qty is None:g['missing_quantities']+=1
        else:
            g['quantity']+=qty;g['sku_quantities'][r.get('sku','')]+=qty
            if '(소모품)' in r.get('name',''):g['consumable_quantity']+=qty
            signature=(r.get('sku',''),qty,r.get('lot',''),r.get('expiry',''),r.get('shipment',''))
            sig=g['repeat_signatures'].setdefault(signature,[0,qty,'(소모품)' in r.get('name','')]);sig[0]+=1
    maps={
      'inbound_h1':('입고 Data',{'company':'A','date':'B','customer':'D','sku':'F','name':'G','qty':'I','warehouse':'M','barcode':'O','lot':'L'}),
      'inbound_july':('Sheet1',{'company':'B','date':'D','shipment':'E','asn':'F','customer':'K','type':'M','management':'N','sku':'Q','name':'R','unit':'V','qty':'W','warehouse':'AK','barcode':'AM','po':'AR','lot':'AF','expiry':'AQ'}),
      'outbound_h1':('출고 Data(04월~06월)',{'company':'A','date':'B','shipment':'C','customer':'E','type':'I','order':'J','po':'K','erp':'L','unit':'N','sku':'P','name':'Q','barcode':'S','qty':'T','tracking':'AC','warehouse':'AQ'}),
    }
    maps['outbound_q1']=('출고 Data(01월~03월)',maps['outbound_h1'][1])
    for role,(sheet,mapping) in maps.items():
        print('Scanning full WMS file: '+role,flush=True);count=0;dates=Counter()
        for n,c in xlsx_rows(str(root/sources[role]['path']),sheet,set(mapping.values())):
            if n==1:continue
            r={k:(c.get(col,{}).get('value') if k=='date' else ident(c.get(col,{}).get('value'))) for k,col in mapping.items()}
            if role=='inbound_h1':r['unit']='EA' # explicit 수량(EA) header
            count+=1;collect(role,n,r)
        profiles[role]={'sheet':sheet,'rows_scanned':count,'visual_filter_applied':False}
    role='outbound_july';mapping={'company':'회사구분','date':'출고일','shipment':'출고번호','customer':'거래처','type':'구분','order':'주문번호(연동)','erp':'주문번호(ERP)',
        'po':'Po No.','management':'관리번호','unit':'단위','sku':'품번','name':'품명','barcode':'제품바코드','qty':'수량','tracking':'송장번호','warehouse':'출고창고'}
    print('Scanning full WMS CSV: '+role,flush=True)
    with (root/sources[role]['path']).open(encoding='utf-8-sig',newline='') as stream:
        count=0
        for n,c in enumerate(csv.DictReader(stream),2):
            count+=1;collect(role,n,{k:ident(c.get(col)) for k,col in mapping.items()})
        profiles[role]={'sheet':'CSV','rows_scanned':count,'visual_filter_applied':False}
    print('Comparing keys and anchored quantity combinations',flush=True)
    gs=list(groups.values());exact=defaultdict(set);norm=defaultdict(set);by_date_sku=defaultdict(set);by_date_customer=defaultdict(set)
    for gid,g in enumerate(gs):
        for field,values in g['ids'].items():
            for v in values:exact[field,v].add(gid);norm[field,normalized(v)].add(gid)
        for sku in g['skus']:by_date_sku[g['direction'],g['date'],sku].add(gid)
        if g['customer']:by_date_customer[g['direction'],g['date'],g['customer']].add(gid)
    evidence={};results=[];reverse=defaultdict(set);b2c_order_status=defaultdict(set)
    def brief(gid):
        key=str(gid)
        if key not in evidence:
            g=gs[gid];evidence[key]={k:v for k,v in g.items() if k not in ('repeat_signatures',)}
        return key
    quantity_cache={}
    for row_index,row in enumerate(claimrows):
        if row_index%500==0:print('Candidate groups '+str(row_index)+'/'+str(len(claimrows)),flush=True)
        direction='inbound' if row['kind']=='inbound' else 'outbound'
        hits={};relaxed={}
        for field in IDENTIFIERS:
            token=row.get(field,'')
            if not token:continue
            for target in IDENTIFIERS:
                candidates={i for i in exact.get((target,token),set()) if gs[i]['direction']==direction}
                if candidates:hits[field+'='+target]=candidates
                altered={i for i in norm.get((target,normalized(token)),set()) if gs[i]['direction']==direction}-candidates
                if altered:relaxed[field+'~'+target]=altered
        # Same date+SKU and same date+customer are candidates, never global quantity-only joins.
        anchors=set()
        if row.get('sku') and row.get('date'):
            anchors|=by_date_sku.get((direction,row['date'],row['sku']),set())
        if row.get('customer') and row.get('date'):
            anchors|=by_date_customer.get((direction,row['date'],row['customer']),set())
        allhits=set().union(*hits.values()) if hits else set()
        inperiod={i for i in allhits if gs[i]['in_period']}
        # Original key matching is retained as baseline; additional keys are research only.
        baseline={i for i in hits.get('order=order',set()) if gs[i]['in_period'] and gs[i]['types'] & {'B2C주문','비연동(B2C)','B2C'}} if row['kind']=='b2c' else set()
        intersection={}
        for a,b in combinations(hits,2):
            if a.split('=')[0]==b.split('=')[0]:continue
            common=hits[a]&hits[b]
            intersection[a+' & '+b]=[brief(i) for i in sorted(common)]
        status='existing_order_match' if baseline else 'additional_exact_key_candidate' if inperiod else 'outside_period_key_candidate' if allhits else 'normalized_key_candidate' if relaxed else 'date_sku_or_customer_candidate' if anchors else 'unconnected'
        quantities=[]
        if row['qty'] is not None:
            for method,ids in list(hits.items())+[('date_sku_or_customer',anchors)]:
                scoped={i for i in ids if gs[i]['in_period'] and (not row.get('date') or gs[i]['date']==row['date'])}
                if row.get('sku'):scoped={i for i in scoped if row['sku'] in gs[i]['skus']}
                if not scoped:continue
                if any(gs[i]['unit']!='EA' or gs[i]['missing_quantities'] for i in scoped):
                    quantities.append({'key':method,'status':'unit_or_quantity_unconfirmed','groups':[brief(i) for i in sorted(scoped)]});continue
                total=sum((gs[i]['quantity'] for i in scoped),Decimal(0));cons=sum((gs[i]['consumable_quantity'] for i in scoped),Decimal(0))
                parts=defaultdict(Decimal)
                for i in scoped:
                    for sku,q in gs[i]['sku_quantities'].items():parts[sku]+=q
                cachekey=(tuple(sorted(scoped)),row['qty'])
                if cachekey not in quantity_cache:
                    quantity_cache[cachekey]=quantity_subsets(list(parts.items()),row['qty'],limit=10000) if total!=row['qty'] and row['qty']>0 else {'status':'unnecessary_total_equal'}
                repeat=sum((q*(count-1) for i in scoped for count,q,material in gs[i]['repeat_signatures'].values()),Decimal(0))
                nonmaterial_repeat=sum((q*(count-1) for i in scoped for count,q,material in gs[i]['repeat_signatures'].values() if not material),Decimal(0))
                quantities.append({'key':method,'status':'candidate_comparison','groups':[brief(i) for i in sorted(scoped)],'claim_quantity':row['qty'],'wms_quantity':total,
                    'difference':row['qty']-total,'consumables_excluded_residual':row['qty']-(total-cons),
                    'repeated_signature_excluded_residual':row['qty']-(total-repeat),
                    'consumables_and_repeated_goods_excluded_residual':row['qty']-(total-cons-nonmaterial_repeat),
                    'repeated_signature_note':'동일 주요필드 가설; LOT/유통기한이 없으면 실제 중복 확정 불가','subset':quantity_cache[cachekey]})
        out={**row,'status':status,'exact_key_candidates':{k:[brief(i) for i in sorted(v)] for k,v in hits.items()},
             'normalized_key_candidates':{k:[brief(i) for i in sorted(v)] for k,v in relaxed.items()},
             'independent_key_intersections':intersection,'quantity_scenarios':quantities,
             'in_period_groups':[brief(i) for i in sorted(inperiod)],'baseline_groups':[brief(i) for i in sorted(baseline)],
             'cross_sheet_occurrences':sheet_tokens.get(row.get('order') or row.get('asn',''),[]),
             'adopted_as_verified_match':False}
        results.append(out)
        for i in inperiod:reverse[i].add(row['sheet'])
        if row['kind']=='b2c':b2c_order_status[row['order']].add(status)
    # Entire workbook identifier occurrences for every WMS group, including other cost sheets.
    uncovered=Counter();covered_other=Counter();channel_coverage=defaultdict(Counter)
    for gid,g in enumerate(gs):
        if not g['in_period']:continue
        sheetrefs=[]
        for values in g['ids'].values():
            for token in values:sheetrefs.extend(sheet_tokens.get(token,[]))
        key=(g['direction'],','.join(sorted(g['types'])),g['customer'])
        classification='mapped_claim_identifier' if gid in reverse else 'other_sheet_identifier' if sheetrefs else 'no_invoice_identifier'
        channel_coverage[key][classification]+=1
        if classification=='no_invoice_identifier':uncovered[g['direction']]+=1
        if classification=='other_sheet_identifier':covered_other[g['direction']]+=1
    unique_counts=Counter()
    for statuses in b2c_order_status.values():
        primary=next(k for k in ['existing_order_match','additional_exact_key_candidate','outside_period_key_candidate','normalized_key_candidate','date_sku_or_customer_candidate','unconnected'] if k in statuses)
        unique_counts[primary]+=1
    filter_rows=sheet_values['입고리스트'];visible=[]
    for n,v in filter_rows.items():
        if n>=4 and all(ident(v.get(x['column'])).casefold() in [y.casefold() for y in x['criteria']] for x in filters['filters']):visible.append(n)
    filter_audit={'all_data_rows':sum(n>=4 for n in filter_rows),'visible_rows':visible,'filter_definition':filters,
        'calculation_reads_all_rows':True,'comparison_period':[start,end],'totals':{}}
    for col in ['H','L','M']:
        filter_audit['totals'][col]={'all_rows':sum((decimal(v.get(col)) or Decimal(0) for n,v in filter_rows.items() if n>=4),Decimal(0)),
            'filter_rows':sum((decimal(filter_rows[n].get(col)) or Decimal(0) for n in visible),Decimal(0))}
    for source in sources.values():
        if digest(root/source['path']).lower()!=source['sha256'].lower():raise ValueError('source_changed_during_scan')
    result={'candidate_only':True,'full_invoice_validated':False,'llm_used':False,'period':[start,end],'sources':sources,
        'sheet_inventory':inventory,'wms_profiles':profiles,'invalid_records':dict(invalid),'out_of_scope_rows':dict(outside_scope),
        'key_fields_searched':list(IDENTIFIERS),'b2c_unique_order_status':dict(unique_counts),
        'claim_row_status':dict(Counter(x['kind']+' / '+x['status'] for x in results)),
        'uncovered_group_counts':dict(uncovered),'other_sheet_identifier_group_counts':dict(covered_other),
        'channel_coverage':[dict(direction=k[0],types=k[1],customer=k[2],counts=dict(v)) for k,v in sorted(channel_coverage.items())],
        'filter_audit':filter_audit,'claim_candidates':results,'wms_evidence':evidence,
        'limits':['거래처·수량만 같은 무관 주문은 연결하지 않음','같은 주문/날짜·품번/날짜·거래처 내부에서만 수량 조합 조사',
                  '키 교차·공백/하이픈 정규화는 후보이며 사용자 미승인','품번 묶음 부분합은 양수 수량, 상태 최대 100000개; 한도 도달은 명시',
                  '과금단위·계약 및 상품 EA 없는 청구는 전체 적정성 확정 불가','다른 비용 시트의 번호 존재는 작업수량·금액 일치 아님']}
    def encode(v):
        if isinstance(v,Decimal):return str(v)
        if isinstance(v,set):return sorted(v)
        raise TypeError(type(v).__name__)
    out=base/'match_investigation.json';tmp=out.with_suffix('.tmp');tmp.write_text(json.dumps(result,ensure_ascii=False,default=encode,indent=2)+'\n',encoding='utf-8');tmp.replace(out)
    with (base/'추가연결_후보.csv').open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.writer(stream);writer.writerow(['청구시트','행','주문/입고예정번호','송장번호','날짜','상태','키별후보수','수량조합','확정반영'])
        for row in results:
            writer.writerow([row['sheet'],row['row'],row.get('order') or row.get('asn'),row.get('tracking'),row['date'],row['status'],
                json.dumps({k:len(v) for k,v in row['exact_key_candidates'].items()},ensure_ascii=False),
                json.dumps(row['quantity_scenarios'],ensure_ascii=False,default=encode),False])
    print(json.dumps({'b2c_unique_order_status':dict(unique_counts),'sheets':len(inventory),'wms_rows_scanned':{k:v['rows_scanned'] for k,v in profiles.items()},'filter_audit':filter_audit},ensure_ascii=False,default=encode),flush=True)
    return refine_existing(root, include_reference=include_reference)


def refine_existing(root, include_reference=False):
    """Refine the saved original scan with precision-loss diagnostics; no match adoption."""
    root=Path(root).resolve();base=root/'data/demo_company/derived';path=base/'match_investigation.json'
    d=json.loads(path.read_text('utf-8'));rows=d['claim_candidates'];ev=d['wms_evidence'];start,end=d['period']
    for source in d['sources'].values():
        if digest(root/source['path']).lower()!=source['sha256'].lower():raise ValueError('stale_investigation')
    # June uses the approved date+SKU aggregate, not unavailable ASN keys.
    june={};kept=[]
    for row in rows:
        if row['kind']!='inbound' or row['date']>='2029-07-01':kept.append(row);continue
        key=(row['date'],row['sku'])
        if key not in june:june[key]=row
        else:
            old=june[key];old['qty']=str(Decimal(old['qty'])+Decimal(row['qty']))
            old['claim_rows']+=row.get('claim_rows',[row['row']])
            old.setdefault('additional_identifiers',{}).setdefault('asn',[]).append(row['asn'])
    rows=kept+list(june.values());d['claim_candidates']=rows
    # Recover the early reader's numeric Excel dates, preserving their numeric type.
    if d['invalid_records'].get('inbound_h1.date',0):
        groups={};bad=0
        for n,c in xlsx_rows(str(root/d['sources']['inbound_h1']['path']),'입고 Data',{'A','B','D','F','G','I','M'}):
            if n==1:continue
            dt=date_of(c.get('B',{}).get('value'))
            if not dt:bad+=1;continue
            if not start<=dt<=end:continue
            if ident(c.get('A',{}).get('value'))!='[90] DEMO COMPANY' or ident(c.get('M',{}).get('value'))!='[DM001]DEMO':continue
            sku=ident(c.get('F',{}).get('value'));key=(dt,sku)
            g=groups.setdefault(key,{'date':dt,'sku':sku,'quantity':Decimal(0),'rows':[]})
            g['quantity']+=decimal(c.get('I',{}).get('value')) or Decimal(0);g['rows'].append(n)
        for row in rows:
            if row['kind']!='inbound' or row['date']>='2029-07-01':continue
            g=groups.get((row['date'],row['sku']))
            if not g:continue
            gid='inbound_h1:'+g['date']+':'+g['sku'];ev[gid]={'source':'inbound_h1','sheet':'입고 Data','date':g['date'],'unit':'EA','rows':g['rows'],'quantity':str(g['quantity']),'skus':[g['sku']]}
            row['status']='existing_date_sku_candidate';row['in_period_groups']=[gid]
            row['quantity_scenarios']=[{'key':'date+sku','status':'candidate_comparison','groups':[gid],'claim_quantity':row['qty'],'wms_quantity':str(g['quantity']),
                                      'difference':str(Decimal(row['qty'])-g['quantity']),'note':'기존 승인된 6월 날짜+품번 후보 집계; 입고예정번호 없음'}]
        d['invalid_records']['inbound_h1.date']=bad
    for row in june.values():
        for scenario in row['quantity_scenarios']:
            if scenario.get('wms_quantity') is not None:
                scenario['claim_quantity']=row['qty'];scenario['difference']=str(Decimal(row['qty'])-Decimal(scenario['wms_quantity']))
    july_inbound=[];nontransactions=[];invalid_dates=0
    for n,c in xlsx_rows(str(root/d['sources']['inbound_july']['path']),'Sheet1',{'D','F','K','M','Q','R','V','W'}):
        if n==1:continue
        dt=date_of(c.get('D',{}).get('value'));sku=ident(c.get('Q',{}).get('value'));asn=ident(c.get('F',{}).get('value'))
        if not dt:
            if not sku and not asn:nontransactions.append({'row':n,'reason':'일자·입고예정번호·품번 없는 합계/장식행'})
            else:invalid_dates+=1
            continue
        if start<=dt<=end:july_inbound.append({'row':n,'date':dt,'asn':asn,'sku':sku,'name':ident(c.get('R',{}).get('value')),'customer':ident(c.get('K',{}).get('value')),
                                             'unit':ident(c.get('V',{}).get('value')),'quantity':decimal(c.get('W',{}).get('value'))})
    d['nontransaction_rows']={'inbound_july':nontransactions};d['invalid_records']['inbound_july.date']=invalid_dates
    for row in rows:
        for scenario in row['quantity_scenarios']:
            if scenario.get('subset',{}).get('status')=='search_limit':
                parts=defaultdict(Decimal)
                for gid in scenario['groups']:
                    for sku,q in ev[gid]['sku_quantities'].items():parts[sku]+=Decimal(q)
                scenario['subset']=quantity_subsets(list(parts.items()),Decimal(row['qty']),10000)
    fields={'order':'주문번호(연동)','erp':'주문번호(ERP)','tracking':'송장번호','shipment':'출고번호','po':'Po No.','management':'관리번호'}
    exact=defaultdict(set);numeric=defaultdict(set);buckets=defaultdict(set);steps=defaultdict(set);shipments={};scientific_rows=Counter();scientific_literals=defaultdict(Counter)
    pattern=re.compile(r'^([+-]?\d+(?:\.\d+)?)[Ee]([+-]?\d+)$')
    with (root/d['sources']['outbound_july']['path']).open(encoding='utf-8-sig',newline='') as stream:
        for n,r in enumerate(csv.DictReader(stream),2):
            if r['회사구분'].strip()!='90' or r['출고창고'].strip()!='DM001':continue
            dt=normalize_date(r['출고일'])
            if not start<=dt<=end:continue
            gid=(dt,r['출고번호'].strip() or 'row:'+str(n))
            g=shipments.setdefault(gid,{'date':dt,'outbound':gid[1],'orders':set(),'customers':set(),'types':set(),'rows':[],'quantity':Decimal(0),'units':set(),'sku_quantities':defaultdict(Decimal)})
            g['rows'].append(n);g['orders'].add(r['주문번호(연동)'].strip());g['customers'].add(r['거래처'].strip());g['types'].add(r['구분'].strip());g['units'].add(r['단위'].strip());g['quantity']+=decimal(r['수량']) or Decimal(0)
            g['sku_quantities'][r['품번'].strip()]+=decimal(r['수량']) or Decimal(0)
            for field,col in fields.items():
                value=r[col].strip()
                if not value:continue
                exact[field,value].add(gid)
                m=pattern.fullmatch(value)
                if m:
                    number=Decimal(value);scale=int(m[2])-len(m[1].split('.')[1] if '.' in m[1] else '')
                    scientific_rows[field]+=1;scientific_literals[field][value]+=1
                    numeric[field,number].add(gid);buckets[field,scale,number].add(gid);steps[field].add(scale)
    def sample(ids):
        return [{'date':x[0],'outbound':x[1],'csv_rows':shipments[x]['rows'][:2],'orders':sorted(shipments[x]['orders']),
                 'customers':sorted(shipments[x]['customers'])} for x in sorted(ids)[:3]]
    family_index=defaultdict(set)
    for gid,g in shipments.items():
        for sku in g['sku_quantities']:
            m=re.fullmatch(r'(.+)_CS\d+',sku)
            if m:family_index[g['date'],m[1]].add(gid)
    assemblies=[]
    for receipt in july_inbound:
        for gid in family_index.get((receipt['date'],receipt['sku']),set()):
            g=shipments[gid];pack=[s for s in g['sku_quantities'] if s.startswith(receipt['sku']+'_CS')]
            if len(pack)<2 or receipt['unit']!='EA' or g['units']!={'EA'} or not receipt['quantity']:continue
            assemblies.append({'receipt':{**receipt,'quantity':str(receipt['quantity'])},'outbound':gid[1],'orders':sorted(g['orders']),
                'input_quantity':str(g['quantity']),'input_sku_quantities':{s:str(q) for s,q in g['sku_quantities'].items()},
                'same_family_packaging_skus':pack,'ratios_per_finished_item':{s:str(q/receipt['quantity']) for s,q in g['sku_quantities'].items()},
                'wms_csv_rows':g['rows'],'candidate_only':True,'billing_exclusion_approved':False,
                'note':'같은 날 완성품 품번과 포장재 품번 계열이 연결됨. BOM·작업지시/배치 연결 미확정; 구성품 합계를 완성품 EA로 동일시하지 않음'})
    d['assembly_flow_candidates']=assemblies
    counts=Counter();cross=Counter()
    for row in rows:
        if row['kind']!='b2c' or row['status']=='existing_order_match':continue
        exact_numeric={};rounded={};annotated={}
        for claim_field in ('order','tracking'):
            token=row.get(claim_field,'');target=decimal(token) if re.fullmatch(r'[+-]?\d+(?:\.\d+)?',token) else None
            if target is not None:
                for targetfield in fields:
                    matches=numeric.get((targetfield,target),set())
                    if matches:exact_numeric[claim_field+'='+targetfield]=matches
                candidates=set()
                # Rounding is a diagnostic hypothesis only, never a matching rule.
                for scale in steps[claim_field]:
                    number=target.quantize(Decimal(1).scaleb(scale))
                    candidates|=buckets.get((claim_field,scale,number),set())
                if candidates:rounded[claim_field]=candidates
            m=re.fullmatch(r'(.+?)\s+(?:추가\s+(?:\d+\s*박스|송장)|\(.*\)|불량\s*재출고|\d+\s*박스)',token)
            if m:
                matches=exact.get((claim_field,m[1]),set())
                if matches:annotated[claim_field]=matches
        reversible=set().union(*exact_numeric.values()) if exact_numeric else set()
        annotation=set().union(*annotated.values()) if annotated else set()
        both=set.intersection(*rounded.values()) if rounded else set()
        status='numeric_representation_candidate' if reversible else 'annotation_candidate' if annotation else 'rounding_joint_unique_candidate' if len(both)==1 else 'rounding_joint_ambiguous' if both else 'no_additional_candidate'
        counts[status]+=1
        cross[('order_rounding' if 'order' in rounded else '')+' / '+('tracking_rounding' if 'tracking' in rounded else '')]+=1
        row['precision_investigation']={'status':status,'reversible_numeric_groups':len(reversible),'reversible_numeric_samples':sample(reversible),
            'annotation_groups':len(annotation),'annotation_samples':sample(annotation),
            'rounding_by_field':{k:len(v) for k,v in rounded.items()},'joint_rounding_groups':len(both),'joint_rounding_samples':sample(both),
            'note':'지수표기 복원/반올림/박스 주석 분리는 조사 후보. 과금·연결 기준에 적용하지 않음'}
    # Weak date+quantity candidates for remaining B2B exception, with customer conflict visible.
    for row in rows:
        if row['kind']=='b2b' and row['status']=='unconnected' and row['qty'] is not None:
            matches=[g for g in shipments.values() if g['date']==row['date'] and g['quantity']==Decimal(row['qty']) and g['units']=={'EA'}]
            row['same_day_quantity_only']=[{'date':g['date'],'outbound':g['outbound'],'orders':sorted(g['orders']),'customers':sorted(g['customers']),
                'claim_customer':row['customer'],'quantity':str(g['quantity']),'csv_rows':g['rows'],'adopted':False} for g in matches]
    # Existing approved links are not advertised as new discoveries.
    for row in rows:
        if row['kind']=='b2c' or row['status'] not in ('additional_exact_key_candidate','date_sku_or_customer_candidate'):continue
        methods=('asn=asn',) if row['kind']=='inbound' else ('order=order',)
        ids=set(gid for method in methods for gid in row['exact_key_candidates'].get(method,[]))
        if any(ev[i]['date']==row['date'] and (not row.get('sku') or row['sku'] in ev[i]['skus']) for i in ids):row['status']='existing_registered_key'
    # Optional development reference: original invoice tracking -> reference
    # shipment/SKU quantities -> actual WMS. It never replaces a direct source.
    for row in rows:
        row.pop('optional_reference_bridge', None)
    d.pop('optional_reference_diagnostic', None)
    d['internal_reference_used'] = include_reference
    if include_reference:
        full=json.loads((base/'full_invoice_result.json').read_text('utf-8'))
        reference=full['sources']['b2c'];reference_path=root/reference['path']
        if digest(reference_path).lower()!=reference['sha256'].lower():raise ValueError('optional_reference_changed')
        wanted={r['tracking'] for r in rows if r['kind']=='b2c'};bridge=defaultdict(dict);bridge_scanned=0
        with open_workbook(str(reference_path)) as book:
            for n,c in scan_rows(book,'총 LAW'):
                if n==1:continue
                bridge_scanned+=1;track=ident(c.get('Y',{}).get('value'))
                if track not in wanted:continue
                dt=date_of(c.get('B',{}).get('value'));ship=ident(c.get('C',{}).get('value'));sku=ident(c.get('P',{}).get('value'))
                if not dt or not start<=dt<=end:continue
                g=bridge[track].setdefault((dt,ship),{'rows':[],'sku_quantities':defaultdict(Decimal),'orders':set(),'units':set()})
                g['rows'].append(n);g['orders'].add(ident(c.get('L',{}).get('value')));g['units'].add(ident(c.get('V',{}).get('value')))
                qty=decimal(c.get('W',{}).get('value'))
                if qty is not None:g['sku_quantities'][sku]+=qty
        bridge_status=Counter()
        for row in rows:
            if row['kind']!='b2c':continue
            comparisons=[]
            for key,g in bridge.get(row['tracking'],{}).items():
                w=shipments.get(key)
                status='june_shipment_key_unavailable' if key[0]<'2029-07-01' else 'wms_shipment_not_found' if w is None else 'sku_quantity_equal' if w['sku_quantities']==g['sku_quantities'] and w['units']==g['units']=={'EA'} else 'sku_quantity_difference'
                comparisons.append({'date':key[0],'outbound':key[1],'status':status,'reference_rows':g['rows'],
                    'reference_orders':sorted(g['orders']),'claim_order_equals_reference':row['order'] in g['orders'],
                    'reference_sku_quantity':{k:str(v) for k,v in g['sku_quantities'].items()},
                    'wms_sku_quantity':{k:str(v) for k,v in w['sku_quantities'].items()} if w else None,'wms_csv_rows':w['rows'] if w else [],'wms_orders':sorted(w['orders']) if w else [],
                    'quantity_meaning':'내부자료↔WMS 상품수량; 청구에는 EA가 없어 청구 수량 대사 완료가 아님'})
            row['optional_reference_bridge']={'comparisons':comparisons,'candidate_only':True,'reference_is_required_for_operation':False}
            if row['status']!='existing_order_match':
                status='all_reference_shipments_qty_equal' if comparisons and all(x['status']=='sku_quantity_equal' for x in comparisons) else 'reference_found_but_not_fully_linked' if comparisons else 'reference_not_found'
                row['optional_reference_bridge']['status']=status;bridge_status[status]+=1
        d['optional_reference_diagnostic']={'source':reference,'sheet':'총 LAW','rows_scanned':bridge_scanned,
            'unmatched_claim_row_status':dict(bridge_status),'candidate_only':True,'operation_dependency_added':False,
            'note':'청구 송장↔내부자료 송장, 내부자료 날짜+출고번호+품번별 수량↔WMS를 연결한 보조 조사. 직접 연결로 오인하지 않음'}
    d['claim_row_status']=dict(Counter(r['kind']+' / '+r['status'] for r in rows))
    d['precision_diagnostic']={'candidate_only':True,'unmatched_claim_rows_by_status':dict(counts),'rounding_field_availability':dict(cross),
        'wms_scientific_rows':dict(scientific_rows),'literal_examples':{k:v.most_common(4) for k,v in scientific_literals.items()},
        'reason':'CSV 원문 지수표기의 유효숫자 제한과 누락 필드 때문에 여러 실적이 같은 번호처럼 보일 수 있음. 복원이 원래 번호 증명을 뜻하지 않음'}
    d['b2c_unmatched_unique_order_precision_status']=dict(Counter(next(iter(statuses)) if len(statuses)==1 else 'mixed_candidate_states' for statuses in (
        {r.get('precision_investigation',{}).get('status','') for r in rows if r['kind']=='b2c' and r['order']==order}
        for order in {r['order'] for r in rows if r['kind']=='b2c' and r['status']!='existing_order_match'})))
    d['limits']=[x.replace('100000','10000') for x in d['limits']]
    review_path=root/'ia/config/matching_review_decisions.json'
    review=review_summary(d,json.loads(review_path.read_text('utf-8')) if review_path.exists() else {})
    d['connection_review']=review
    for row in rows:
        if row['kind']!='b2c':continue
        row['connection_review']='approved_reference_bridge' if row['order'] in review['reference_bridge_orders'] else 'approved_annotated_base_order' if row['order'] in review['annotated_base_orders'] else 'existing_direct_order' if row['order'] in review['direct_orders'] else 'unresolved_or_pending'
    path.write_text(json.dumps(d,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    with (base/'추가연결_후보.csv').open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.writer(stream);writer.writerow(['청구시트','대표행','전체청구행','주문/입고예정번호','송장번호','날짜','기존키상태','숫자표기조사','공동반올림후보수','키별후보수','수량조합','확정반영'])
        for row in rows:
            precision=row.get('precision_investigation',{})
            writer.writerow([row['sheet'],row['row'],','.join(map(str,row.get('claim_rows',[row['row']]))),row.get('order') or row.get('asn'),row.get('tracking'),row['date'],row['status'],precision.get('status'),precision.get('joint_rounding_groups'),
                json.dumps({k:len(v) for k,v in row['exact_key_candidates'].items()},ensure_ascii=False),json.dumps(row['quantity_scenarios'],ensure_ascii=False),row.get('connection_review','candidate_only')])
    print(json.dumps({'precision':d['precision_diagnostic'],'unique_orders':d['b2c_unmatched_unique_order_precision_status'],'invalid_records':d['invalid_records']},ensure_ascii=False),flush=True)
    return d


if __name__=='__main__':
    root=sys.argv[1] if len(sys.argv)>1 else Path(__file__).resolve().parents[2]
    refine_existing(root, include_reference='--with-reference' in sys.argv) if '--refine-only' in sys.argv else run(root, include_reference='--with-reference' in sys.argv)
