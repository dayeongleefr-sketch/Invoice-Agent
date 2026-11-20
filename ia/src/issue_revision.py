"""Compare the same checks across completed versions; absence is never a pass."""
from collections import Counter, defaultdict
from decimal import Decimal
import csv
import hashlib
import json
from pathlib import Path
import re

BASE = Path('data/demo_company/derived')
SNAPSHOT = 'verification_checks.json'
COMPARISON = 'issue_revision_comparison.json'
CSV = '수정전후_문제비교.csv'


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def read(path):
    return json.loads(path.read_text('utf-8-sig'))


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str)+'\n', encoding='utf-8')


def semantic_policy(value):
    # Source hashes change on a revision; the applied policy must not change.
    ignored = {'source_hashes', 'scope_fingerprint', 'revision_reuse_evidence', 'updated_at', 'recorded_at'}
    if isinstance(value, dict):
        return {k: semantic_policy(v) for k,v in value.items() if k not in ignored}
    if isinstance(value, list):
        return [semantic_policy(v) for v in value]
    return value


def build_snapshot(root):
    root=Path(root);base=root/BASE
    workflow=read(base/'workflow_result.json')
    docs={name:read(base/name) for name in ('direct_wms_result.json','full_invoice_result.json',
          'match_investigation.json','cost_verification.json')}
    for name,doc in docs.items():
        actual=hashlib.sha256((base/name).read_bytes()).hexdigest()
        if actual.upper()!=workflow['result_hashes'][name].upper():
            raise ValueError('issue_snapshot_result_changed:'+name)
    d,f,i,c=[docs[n] for n in docs]
    def basis(files, policies):
        code={n:hashlib.sha256((root/'ia/src'/n).read_bytes()).hexdigest() for n in files}
        settings={n:semantic_policy(read(root/'ia/config'/n)) for n in policies if (root/'ia/config'/n).is_file()}
        return fingerprint({'code':code,'settings':settings,'comparison_version':1})
    qty_basis=basis(['claim_wms_rules.py','claim_wms_pipeline.py','wms_tools.py','work_quantity_tools.py'],
                    ['demo_company_july_wms_decisions.json'])
    b2c_basis=basis(['actual_claim_wms_b2c.py','match_investigation.py','numeric_key_reconciliation.py','identifier_tools.py'],
                    ['matching_review_decisions.json'])
    cost_basis=basis(['cost_verification.py','cost_detail_tools.py','b2c_charge_tools.py'],['demo_company_cost_decisions.json'])
    bill_basis=basis(['billing_quantity_tools.py','billing_quantity_causes.py'],
                    ['demo_company_cost_decisions.json','demo_company_july_wms_decisions.json'])
    rows=[]
    def add(check,target,unit,status,performed,rule,values,evidence,stable=True):
        rows.append({'identity':{'check':check,'target':target,'unit':unit},
            'check_key':fingerprint([check,target,unit]),'status':status,'performed':bool(performed),
            'rule_fingerprint':rule,'identity_stable':bool(stable),'values':values,'evidence':evidence})
    for idx,r in enumerate(d['rows']):
        status='pass' if r['kind']=='quantity_match' else 'difference'
        add('quantity.'+r['scope'],r['key'],'EA',status,True,qty_basis,
            {'claim':r['claim_quantity'],'wms':r['wms_quantity'],'difference':r['difference_claim_minus_wms'],'kind':r['kind']},
            {'result':f'direct_wms_result.json#/rows/{idx}','claim':r.get('claim_evidence',[])[:2],'wms':r.get('wms_evidence',[])[:2]})
        if r.get('claim_present'):
            add('claim_quantity_present.'+r['scope'],r['key'],'EA','pass',True,qty_basis,
                {'claim':r['claim_quantity']},{'result':f'direct_wms_result.json#/rows/{idx}'})
    for idx,r in enumerate(d.get('missing_claim_quantity',[])):
        add('claim_quantity_present.outbound',r['key'],'EA','unavailable',False,qty_basis,
            {'claim':None,'freight':r.get('freight'),'boxes':r.get('box')},
            {'result':f'direct_wms_result.json#/missing_claim_quantity/{idx}','claim':[r]})
    for idx,r in enumerate(d.get('work_quantity_checks',[])):
        add('work_quantity.'+r['kind'],[r.get('date'),r.get('output_sku'),sorted(r.get('orders',[]))],r['unit'],
            'pass' if r['status']=='match' else 'difference' if r['status']=='difference' else 'unavailable',
            r['status'] in ('match','difference'),qty_basis,
            {k:r.get(k) for k in ('claim_quantity','wms_quantity','difference','quantity_origins')},
            {'result':f'direct_wms_result.json#/work_quantity_checks/{idx}'})
    review=i['connection_review']
    all_orders=set().union(*(set(review.get(k,[])) for k in ('direct_orders','reference_bridge_orders','annotated_base_orders','remaining_orders','annotation_pending_orders')))
    connected=set().union(*(set(review.get(k,[])) for k in ('direct_orders','reference_bridge_orders','annotated_base_orders')))
    for order in sorted(all_orders):
        add('b2c_order_connection',[order],'order','pass' if order in connected else 'difference',True,b2c_basis,
            {'connected':order in connected,'quantity_or_freight_approved':False},
            {'result':'match_investigation.json#/connection_review','order':order})
    items={str(x['row']):x for x in f['claim_items']}
    for idx,r in enumerate(c['checks']):
        family,_,number=r['id'].rpartition('.')
        item=items.get(number) if family!='cost.work' else None
        target=r.get('business_target')
        if target is None and item:
            target=[item['sheet'],item['section'],item['category_context'],item['item_context'],item['unit']]
        stable=target is not None
        target=target if stable else [r['id'],r['label']]
        check_basis=fingerprint([cost_basis,r.get('basis'),r.get('formula_operands'),
            c['sources'].get('contract',{}).get('sha256') if family=='cost.contract_rate' else None])
        add(family,target,r.get('unit',r.get('currency','KRW')),
            'pass' if r['status']=='match' else r['status'],r['check_performed'],check_basis,
            {k:r.get(k) for k in ('expected','actual','difference')},
            {'result':f'cost_verification.json#/checks/{idx}','locations':r['evidence'][:2]},stable)
    for idx,r in enumerate(c.get('billing_quantity',{}).get('rows',[])):
        conversion_basis=fingerprint([bill_basis,sorted(
            [(p.get('sku'),str(p.get('outer_EA')),p.get('rate_basis')) for p in r.get('wms_parts',[])],key=str)])
        for col,actual in r.get('claimed_work',{}).items():
            expected=(r.get('expected_work') or {}).get(col)
            difference=(r.get('differences') or {}).get(col)
            complete=r['status'] in ('scenario_match','scenario_difference') and actual is not None and expected is not None
            add('billing_quantity.'+r['scope']+'.'+col,r['key'],'BOX' if col in ('L','N') else 'PCS',
                ('pass' if difference is not None and Decimal(str(difference))==0 else 'difference') if complete else 'unavailable',
                complete,conversion_basis,{'claim':actual,'expected':expected,'difference':difference,'calculation_status':r['status']},
                {'result':f'cost_verification.json#/billing_quantity/rows/{idx}','sheet':r['claim_sheet'],'rows':r['claim_rows'][:2]})
    # Limits remain independent checks. A connected order never passes a monetary test.
    for item in c['items']:
        source=items[str(item['row'])]
        target=[source['sheet'],source['section'],source['category_context'],source['item_context'],source['unit']]
        for dimension in ('contract_application','actual_cost_application'):
            if dimension in item:
                add(dimension,target,'KRW',item[dimension],False,cost_basis,{}, {'locations':item['evidence'][:2]})
    scope={k:workflow.get(k) for k in ('company_id','warehouse_id','invoice_month','period','input_mode')}
    scope['currency']='KRW'
    return {'schema_version':1,'scope':scope,'processing_status':workflow['processing_status'],
            'result_hashes':workflow['result_hashes'],'checks':rows,
            'counts':dict(Counter(r['status'] for r in rows))}


def compare(previous,current):
    """Only an explicit same-key, same-unit, same-rule executed pass resolves."""
    output={'schema_version':1,'baseline_available':previous is not None,'counts':{},'changes':[],
            'source_result_hashes':{'previous':(previous or {}).get('result_hashes',{}),'current':current['result_hashes']},
            'scope':current['scope'],'amounts_summed':False}
    if previous is None:
        output['status']='baseline_created';return output
    old=defaultdict(list);new=defaultdict(list)
    for r in previous['checks']:old[r['check_key']].append(r)
    for r in current['checks']:new[r['check_key']].append(r)
    scope_same=previous['scope']==current['scope']
    completed=current['processing_status'] in ('completed','completed_with_limitations')
    for key in sorted(old.keys()|new.keys()):
        before=old[key];after=new[key]
        if all(r['status']=='pass' for r in before+after):continue
        b=before[0] if len(before)==1 else None;a=after[0] if len(after)==1 else None
        reason=''
        if not scope_same:state='not_comparable';reason='company_warehouse_period_currency_or_input_mode_changed'
        elif len(before)>1 or len(after)>1:state='not_comparable';reason='ambiguous_repeated_business_key'
        elif not b:state='new_issue';reason='new_check_or_new_failure'
        elif not a:state='not_observed';reason='removed_or_not_executed_is_not_resolution'
        elif not b['identity_stable'] or not a['identity_stable']:state='not_comparable';reason='stable_business_identity_unavailable'
        elif b['rule_fingerprint']!=a['rule_fingerprint']:state='not_comparable';reason='applied_rule_or_decision_changed'
        elif b['status']=='pass':state='new_issue';reason='previous_pass_now_not_passed'
        elif a['status']=='pass' and a['performed'] and completed:state='resolved';reason='same_check_executed_and_passed'
        elif not a['performed'] or not completed:state='unverified';reason='not_executed_excluded_unavailable_or_execution_pending'
        else:state='remaining';reason='same_check_still_not_passed'
        output['changes'].append({'check_key':key,'identity':(a or b or (before+after)[0])['identity'],
            'state':state,'reason':reason,'before':before,'after':after})
    output['counts']=dict(Counter(r['state'] for r in output['changes']))
    output['status']='compared' if scope_same else 'not_comparable'
    output['previous_snapshot_sha256']=fingerprint(previous)
    output['current_snapshot_sha256']=fingerprint(current)
    return output


def prepare(root,previous_root):
    root=Path(root);previous_root=Path(previous_root)
    previous_path=previous_root/BASE/SNAPSHOT
    previous=read(previous_path) if previous_path.is_file() else None
    if previous is not None:
        old_workflow=read(previous_root/BASE/'workflow_result.json')
        bound=old_workflow.get('revision_comparison',{}).get('snapshot_sha256')
        if not bound or hashlib.sha256(previous_path.read_bytes()).hexdigest().upper()!=bound.upper():
            raise ValueError('previous_check_snapshot_unverified')
        if previous['result_hashes']!=old_workflow['result_hashes']:
            raise ValueError('previous_snapshot_result_version_mismatch')
    current=build_snapshot(root)
    result=compare(previous,current)
    write(root/BASE/SNAPSHOT,current);write(root/BASE/COMPARISON,result)
    labels={'resolved':'해소','remaining':'차이 유지','new_issue':'새 문제','unverified':'검사 미완료·판정 제외',
            'not_comparable':'비교 불가','not_observed':'이번 결과에 없음(해소 아님)'}
    with (root/BASE/CSV).open('w',encoding='utf-8-sig',newline='') as out:
        writer=csv.writer(out);writer.writerow(['검사','확인 대상','단위','수정 전후','이유','이전 값·근거','현재 값·근거'])
        for row in result['changes']:
            ident=row['identity']
            writer.writerow([ident['check'],json.dumps(ident['target'],ensure_ascii=False),ident['unit'],labels[row['state']],row['reason'],
                json.dumps([{'status':v['status'],'values':v['values'],'evidence':v['evidence']} for v in row['before']],ensure_ascii=False),
                json.dumps([{'status':v['status'],'values':v['values'],'evidence':v['evidence']} for v in row['after']],ensure_ascii=False)])
    workflow=read(root/BASE/'workflow_result.json')
    workflow['revision_comparison']={'status':result['status'],'counts':result['counts'],
        'resolved_issues':[v['check_key'] for v in result['changes'] if v['state']=='resolved'],
        'previous_result_hashes':result['source_result_hashes']['previous'],
        'evidence':str(BASE/COMPARISON).replace('\\','/'),
        'sha256':hashlib.sha256((root/BASE/COMPARISON).read_bytes()).hexdigest().upper(),
        'snapshot_sha256':hashlib.sha256((root/BASE/SNAPSHOT).read_bytes()).hexdigest().upper()}
    write(root/BASE/'workflow_result.json',workflow)
    return workflow
