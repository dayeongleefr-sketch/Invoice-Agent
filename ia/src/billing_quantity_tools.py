"""Stated detail totals and approved, provisional WMS-to-carton comparisons.

Never equate a carton quotient with proof of picking/packing work or a payable fee.
"""
from collections import Counter, defaultdict
from identifier_tools import expand_identifier
from decimal import Decimal
import csv
import hashlib
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from portable_workbook import open_workbook
from xlsb_scan import scan_rows
from wms_tools import xlsx_rows, day
from invoice_tools import digest

BASE = Path('data/demo_company/derived')
MASTERS = {'master': {'path': 'data/demo_company/source/master.xlsx', 'sha256': '2A4BAF6C99FA4A098A32B42F4241AD017407A1B6F14DC3090EC02F5F59D449B7'}, 'master_reply': {'path': 'data/demo_company/source/master_reply.xlsx', 'sha256': '2A4BAF6C99FA4A098A32B42F4241AD017407A1B6F14DC3090EC02F5F59D449B7'}}
SUMMARY = {20:('입고리스트','L','BOX'),21:('입고리스트','M','PCS'),
           34:('B2B출고','L','BOX픽킹'),35:('B2B출고','M','PCS픽킹'),
           36:('B2B출고','N','BOX팩킹'),37:('B2B출고','O','PCS팩킹')}


def read(p):
    return json.loads(p.read_text('utf-8-sig'))


def decimal(value):
    if value is None or isinstance(value,bool) or value=='':
        return None
    try:
        number=Decimal(str(value))
        return number if number.is_finite() else None
    except Exception:
        return None


def split_units(quantity, pack):
    quantity,pack=decimal(quantity),decimal(pack)
    if quantity is None or pack is None or quantity<0 or pack<=0 or quantity%1 or pack%1:
        raise ValueError('positive_integral_pack_and_nonnegative_EA_required')
    boxes=quantity//pack
    return boxes,quantity-boxes*pack


def master_index(root):
    versions=[]
    for source in MASTERS.values():
        path=root/source['path']
        if digest(path)!=source['sha256']:
            raise ValueError('master_version_changed')
        rows=[]
        for n,c in xlsx_rows(str(path),'제품 Master Data'):
            if n==2:
                if c.get('B',{}).get('value')!='품번' or c.get('E',{}).get('value')!='아웃박스수량(EA)':
                    raise ValueError('master_headers_changed')
            if n<3 or not c.get('B',{}).get('value'):
                continue
            rows.append({'row':n,'sku':expand_identifier(c['B']['value']), 'product_code':expand_identifier(c.get('A',{}).get('value')),
                         'name':str(c.get('C',{}).get('value','')), 'outer':decimal(c.get('E',{}).get('value'))})
        versions.append(rows)
    if versions[0]!=versions[1]:
        raise ValueError('master_versions_conflict')
    grouped=defaultdict(list)
    for row in versions[0]:grouped[row['sku']].append(row)
    return dict(grouped)


def rate_for(master,sku):
    entries=master.get(sku,[])
    values={x['outer'] for x in entries}
    if not entries:return None,'Master에 품번 없음'
    if len(values)!=1:return None,'Master 동일 품번의 박스당 수량 충돌'
    value=next(iter(values))
    if value is None or value<=0 or value%1:return None,'Master 아웃박스수량 빈칸·0 또는 유효값 아님'
    return value,None


def resolved_rate(master,sku,candidates,policy):
    value,reason=rate_for(master,sku)
    if value is not None:return value,None,'master',[]
    if policy.get('state')=='confirmed_by_user' and policy.get('mode')=='claim_inner_when_master_missing':
        valid=[x for x in candidates if x['value'] is not None and x['value']>0 and x['value']%1==0]
        values={x['value'] for x in valid}
        if len(values)==1:return next(iter(values)),None,'claim_inner_auxiliary',[x['row'] for x in valid]
        reason='청구 내품수량 후보 충돌' if len(values)>1 else 'Master·청구 내품수량 모두 유효한 환산 근거 없음'
    return None,reason,'unresolved',[]


def group_parts(direct,investigation):
    """Reuse source-verified original WMS groups; require exact row and EA coverage."""
    result=defaultdict(lambda:defaultdict(Decimal)); wanted={};covered=set();refs=defaultdict(list)
    for row in direct['rows']:
        if not row['claim_present'] or not row['wms_present']:continue
        key=(row['scope'],*row['key'])
        if row['scope'].startswith('inbound'):
            result[key][row['key'][-1]]=Decimal(str(row['wms_quantity']))
        else:
            for ev in row['wms_evidence']:
                ref=(ev['source'],ev.get('row',ev.get('csv_record')))
                if ref in wanted:raise ValueError('WMS_row_reused_across_claim_groups')
                wanted[ref]=key
    for group in investigation['wms_evidence'].values():
        if group.get('direction')!='outbound':continue
        all_refs={(group['source'],n) for n in group['rows']}
        selected=all_refs & wanted.keys()
        if not selected:continue
        keys={wanted[x] for x in selected}
        if selected!=all_refs or len(keys)!=1 or selected & covered:
            raise ValueError('WMS_group_cannot_be_allocated_exactly')
        key=next(iter(keys));covered.update(selected)
        if group.get('missing_quantities') or not group.get('sku_quantities'):
            raise ValueError('WMS_SKU_quantity_missing')
        parts={sku:Decimal(str(q)) for sku,q in group['sku_quantities'].items()}
        if sum(parts.values(),Decimal(0))!=Decimal(str(group['quantity'])):
            raise ValueError('WMS_SKU_total_mismatch')
        for sku,q in parts.items():result[key][sku]+=q
        refs[key].append({'source':group['source'],'unit':group.get('unit',''),'rows':sorted(n for _,n in selected)})
    if covered!=wanted.keys():raise ValueError('WMS_row_coverage_incomplete')
    for row in direct['rows']:
        key=(row['scope'],*row['key'])
        if key in result and sum(result[key].values(),Decimal(0))!=Decimal(str(row['wms_quantity'])):
            raise ValueError('WMS_group_total_mismatch')
    return dict(result),dict(refs)


def document_checks(sheets,master):
    totals=[];decomposition=[];pack_differences=[]
    for bill_row,(sheet,col,unit) in SUMMARY.items():
        selected=[(n,c) for n,c in sheets[sheet].items() if n>=4 and c.get('A',{}).get('value')
                  and (sheet!='입고리스트' or c.get('B',{}).get('value'))]
        values=[decimal(c.get(col,{}).get('value')) for n,c in selected]
        value=sum((q for q in values if q is not None),Decimal(0)) if any(q is not None for q in values) else None
        claim=decimal(sheets['청구내역'][bill_row].get('M',{}).get('value'))
        totals.append({'claim_row':bill_row,'detail_sheet':sheet,'detail_column':col,'unit':unit,
                       'detail_quantity':value,'claim_quantity':claim,'difference':claim-value if claim is not None and value is not None else None,
                       'status':('match' if claim==value else 'difference') if claim is not None and value is not None else 'data_unavailable',
                       'blank_detail_cells':sum(q is None for q in values),'row_count':len(selected),
                       'basis':'기재된 숫자 합계 비교. 빈 작업량을 실제 0건으로 확정하지 않음'})
    for n,c in sheets['입고리스트'].items():
        if n<4 or not c.get('B',{}).get('value'):continue
        qty,boxes,pack,pcs=[decimal(c.get(k,{}).get('value')) for k in ['H','I','J','K']]
        expected=boxes*pack+pcs if all(x is not None for x in (boxes,pack,pcs)) else None
        decomposition.append({'row':n,'quantity':qty,'expected':expected,
                              'status':'match' if qty==expected and qty is not None else 'difference' if qty is not None and expected is not None else 'data_unavailable'})
        sku=expand_identifier(c.get('D',{}).get('value'));rate,reason=rate_for(master,sku)
        if pack is not None and rate is not None and pack!=rate:
            pack_differences.append({'row':n,'sku':sku,'claim_inner_quantity':pack,'master_outer_quantity':rate,
                                     'master_rows':[x['row'] for x in master[sku]]})
    return totals,decomposition,pack_differences


def run(root,decisions):
    root=Path(root).resolve();base=root/BASE
    cfg=read(root/'ia/config/demo_company_direct_wms.json')
    d=read(base/'direct_wms_result.json');m=read(base/'match_investigation.json')
    for role,source in cfg['sources'].items():
        if digest(root/source['path'])!=source['sha256'] or d['sources'][role]['sha256']!=source['sha256']:
            raise ValueError('billing_quantity_source_changed')
        if role in m['sources'] and m['sources'][role]['sha256']!=source['sha256']:
            raise ValueError('billing_quantity_investigation_stale')
    master=master_index(root)
    with open_workbook(str(root/cfg['sources']['claim']['path'])) as book:
        sheets={name:dict(scan_rows(book,name)) for name in ['청구내역','입고리스트','B2B출고']}
    totals,decomposition,pack_differences=document_checks(sheets,master)
    inner_by_sku=defaultdict(list)
    for n,c in sheets['입고리스트'].items():
        if n<4 or not c.get('B',{}).get('value'):continue
        date=day(c.get('A'))
        if d['period']['start']<=date<=d['period']['end']:
            inner_by_sku[expand_identifier(c.get('D',{}).get('value'))].append({'row':n,'value':decimal(c.get('J',{}).get('value'))})
    parts,units=group_parts(d,m)
    questions=[]
    policies={'U27':('master_quantity_basis','Master 적용','BOX/PCS 단위가 다를 때 Master를 잠정 적용할지 결정'),
              'U28':('billing_quantity_grouping','환산 집계','같은 일자·번호·품번을 합산 후 환산할지 결정'),
              'U29':('june_b2b_unit','6월 B2B 단위','6월 B2B의 공란 단위를 이번 비교에서 EA로 볼지 결정')}
    for id_,(name,category,text) in policies.items():
        policy=decisions.get(name,{})
        expected_mode={'U27':'same_unit_first_master_for_conversion','U28':'date_number_sku','U29':'EA'}[id_]
        if policy.get('state')!='confirmed_unresolvable_by_user' and not (policy.get('state')=='confirmed_by_user' and policy.get('mode')==expected_mode):
            questions.append({'id':id_,'category':category,'text':text,'skip_scope':name})
    rows=[];missing=[]
    basis=decisions.get('master_quantity_basis',{});grouping=decisions.get('billing_quantity_grouping',{});june=decisions.get('june_b2b_unit',{})
    for row in d['rows']:
        if not row['claim_present']:continue
        key=(row['scope'],*row['key']);inbound=row['scope'].startswith('inbound')
        sheet='입고리스트' if inbound else 'B2B출고';cols=['L','M'] if inbound else ['L','M','N','O']
        claim_rows=[ev['row'] for ev in row['claim_evidence']]
        amounts={col:[decimal(sheets[sheet][n].get(col,{}).get('value')) for n in claim_rows] for col in cols}
        claimed={col:sum(v,Decimal(0)) if v and all(x is not None for x in v) else None for col,v in amounts.items()}
        blank_columns=[c for c,q in claimed.items() if q is None]
        blank_policy=decisions.get('blank_billing_work_quantity',{})
        effective=dict(claimed)
        if blank_policy.get('state')=='confirmed_by_user' and blank_policy.get('mode')=='blank_as_zero':
            effective={c:(Decimal(0) if q is None else q) for c,q in claimed.items()}
        entry={'scope':row['scope'],'key':row['key'],'claim_sheet':sheet,'claim_rows':claim_rows,
               'claim_EA':row['claim_quantity'],'wms_EA':row['wms_quantity'],'claimed_work':claimed,
               'effective_claimed_work':effective,'blank_columns':blank_columns,'pending_ids':[],
               'wms_evidence':row['wms_evidence'],
               'wms_parts':[], 'expected_work':None,'differences':None,'billing_work_proven':False,
               'amount_appropriateness_confirmed':False}
        status='awaiting_user_decision'
        if not row['wms_present']:
            status='wms_unlinked';entry['reason']='기존 청구↔WMS 미연결 유지; 환산하지 않음'
        elif any(p.get('state')=='confirmed_unresolvable_by_user' for p in (basis,grouping)):
            status='not_determinable_by_user';entry['reason']='사용자 확정불가 범위: BOX·PCS 환산'
        elif basis.get('state')==grouping.get('state')=='confirmed_by_user' and basis.get('mode')=='same_unit_first_master_for_conversion' and grouping.get('mode')=='date_number_sku':
            old_units=any(u['source']=='outbound_h1' and not u['unit'] for u in units.get(key,[]))
            if old_units and not (june.get('state')=='confirmed_by_user' and june.get('mode')=='EA'):
                status='not_determinable_by_user' if june.get('state')=='confirmed_unresolvable_by_user' else 'awaiting_user_decision'
                entry['reason']='6월 B2B 단위 공란'
            else:
                blocked=[];box_total=Decimal(0);pcs_total=Decimal(0)
                for sku,qty in sorted(parts[key].items()):
                    candidates=([{'row':n,'value':decimal(sheets[sheet][n].get('J',{}).get('value'))} for n in claim_rows]
                                if inbound else inner_by_sku.get(sku,[]))
                    rate,reason,rate_basis,aux_rows=resolved_rate(master,sku,candidates,decisions.get('missing_billing_quantity_basis',{}))
                    part={'sku':sku,'EA':qty,'outer_EA':rate,'master_rows':[x['row'] for x in master.get(sku,[])],
                          'rate_basis':rate_basis,'auxiliary_claim_rows':aux_rows}
                    if reason is None:
                        try:box,pcs=split_units(qty,rate)
                        except ValueError:reason='EA가 음수·비정수여서 완박스/잔량 환산 기준 필요'
                    if reason:
                        part['reason']=reason;blocked.append(sku)
                        missing.append({'kind':'master','scope':row['scope'],'key':row['key'],'sku':sku,'reason':reason,'claim_rows':claim_rows})
                    else:
                        part.update(BOX=box,PCS=pcs);box_total+=box;pcs_total+=pcs
                    entry['wms_parts'].append(part)
                if blocked:
                    status='awaiting_user_decision';entry['reason']='일부 품번의 환산 근거 부족; 나머지 품번으로 주문 전체를 일치 처리하지 않음'
                    entry['pending_ids'].append('U30')
                else:
                    expected={'L':box_total,'M':pcs_total}
                    if not inbound:expected.update(N=box_total,O=pcs_total)
                    entry['expected_work']=expected
                    entry['differences']={c:effective[c]-q if effective[c] is not None else None for c,q in expected.items()}
                    diff_values=list(entry['differences'].values())
                    status=('partial_' if any(x is None for x in diff_values) else '')+('scenario_difference' if any(x not in (None,0) for x in diff_values) else 'scenario_match')
                    if any(x is None for x in diff_values):entry['reason']='기재된 작업량은 비교; 빈 작업량만 미비교'
                if blank_columns and blank_policy.get('mode')!='blank_as_zero':
                    missing.append({'kind':'blank_claim','scope':row['scope'],'key':row['key'],'sku':'전체(주문 합계)',
                                    'reason':'청구 작업수량 일부 미기재','columns':blank_columns,'claim_rows':claim_rows})
                    entry['pending_ids'].append('U31')
                if inbound and entry['expected_work']:
                    original=[sheets[sheet][n] for n in claim_rows]
                    inner={decimal(c.get('J',{}).get('value')) for c in original}
                    master_rates={p['outer_EA'] for p in entry['wms_parts']}
                    entry['cause_candidates']=[]
                    if inner!=master_rates:
                        entry['cause_candidates'].append('청구 내품수량과 Master 아웃박스수량이 다름')
                    if row['claim_quantity']==row['wms_quantity'] and inner==master_rates and len(claim_rows)>1 and any(x not in (None,0) for x in entry['differences'].values()):
                        entry['cause_candidates'].append('같은 날짜·번호·품번의 분할 행을 합산 후 환산한 영향 후보; 개별 입고 박스의 실제 합포장 확정 아님')
        entry['status']=status;rows.append(entry)
    # Bind a skip to the exact source versions and unavailable item set.
    fingerprints={}
    fallback_applied=decisions.get('missing_billing_quantity_basis',{}).get('mode')=='claim_inner_when_master_missing'
    unresolved_id='U32' if fallback_applied else 'U30'
    for qid,kind,policy_key,text in [(unresolved_id,'master','unresolved_billing_quantity_basis' if fallback_applied else 'missing_billing_quantity_basis','Master·청구 내품수량으로도 정할 수 없는 품번의 환산만 확정불가로 남길지 결정'),
                                    ('U31','blank_claim','blank_billing_work_quantity','청구 작업수량 빈칸의 비교만 확정불가로 남길지 결정')]:
        affected=[x for x in missing if x['kind']==kind]
        fingerprint=hashlib.sha256(json.dumps({'sources':cfg['sources'],'masters':MASTERS,'missing':affected},ensure_ascii=False,sort_keys=True,default=str).encode()).hexdigest()
        fingerprints[qid]=fingerprint;policy=decisions.get(policy_key,{})
        skipped=((policy.get('state')=='confirmed_unresolvable_by_user' or policy.get('state')=='confirmed_by_user' and policy.get('mode')=='exclude_unconvertible') and policy.get('scope_fingerprint')==fingerprint
                 or qid=='U31' and policy.get('state')=='confirmed_by_user' and policy.get('mode')=='exclude_blank')
        if not skipped and policy.get('state') in ('confirmed_unresolvable_by_user','confirmed_by_user'):
            from demo_company_case_runtime import verified_reuse
            skipped=verified_reuse(root,qid,policy) and (policy.get('state')=='confirmed_unresolvable_by_user' or policy.get('mode')=='exclude_unconvertible')
        if qid=='U32':
            for entry in rows:
                entry['pending_ids']=['U32' if x=='U30' else x for x in entry['pending_ids']]
        if affected and not skipped:
            questions.append({'id':qid,'category':'Master 환산 근거 부재' if kind=='master' else '청구 작업수량 빈칸','text':text,
                              'skip_scope':policy_key,'scope_fingerprint':fingerprint,
                              'affected_skus':sorted({x['sku'] for x in affected}),'affected_groups':len({(x['scope'],*x['key']) for x in affected}),
                              'evidence_ref':'과금수량_검사.csv'})
        if skipped:
            for entry in rows:
                if qid in entry['pending_ids']:entry['pending_ids'].remove(qid)
    for entry in rows:
        if entry['status']=='awaiting_user_decision' and not entry['pending_ids'] and entry.get('wms_parts'):
            entry['status']='not_determinable_by_user'
    from billing_quantity_causes import annotate
    cause_analysis=annotate(rows,sheets,cfg['sources'],MASTERS,decisions,root=root)
    questions.extend(cause_analysis['questions'])
    output={'sources':MASTERS,'source_result_sha256':digest(base/'direct_wms_result.json'),
            'investigation_sha256':digest(base/'match_investigation.json'),'code_sha256':digest(Path(__file__)),
            'period':d['period'],'basis':'동일 단위 상품 수량 비교 유지. 사용자 지정 일자·번호·품번 합산과 Master 기준 잠정 환산. 실제 피킹/포장 발생 및 청구금액 승인 아님.',
            'master_sku_count':len(master),'master_rows':sum(len(x) for x in master.values()),
            'summary_checks':totals,'claim_decomposition':decomposition,'claim_master_pack_differences':pack_differences,
            'rows':rows,'counts':dict(Counter(x['status'] for x in rows)),'missing_basis':missing,
            'questions':questions,'waiting_for_input':bool(questions),'scope_fingerprints':fingerprints,
            'cause_analysis':cause_analysis,
            'decisions_applied':{name:decisions.get(name,{}) for name in (
                'master_quantity_basis','billing_quantity_grouping','june_b2b_unit',
                'missing_billing_quantity_basis','blank_billing_work_quantity','unresolved_billing_quantity_basis',
                'inbound_work_pcs_unit','outbound_repeated_box_work')},
            'b2c_policy':'U13 문서 합계·산술만 비교 유지; B2C 포장 독립환산 수행 안 함'}
    with (base/'과금수량_검사.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.writer(f);writer.writerow(['구분','일자','주문번호','입고예정번호','품번','청구 위치','청구EA','WMSEA','청구BOX픽킹/입고','청구PCS픽킹/입고','청구BOX팩킹','청구PCS팩킹','환산BOX','환산PCS','BOX차이','PCS차이','팩킹BOX차이','팩킹PCS차이','결과','사유','환산근거 없는 품번','청구 원본파일','WMS 근거','원인 후보','환산 근거','Master 원본파일','원인 분류','상세BOX합계','상세낱개합계','작업PCS와낱개차이','상세와환산BOX차이','상세와환산PCS차이','기재박스수','잔량있는품번수','판정 처리'])
        for x in rows:
            key=x['key'];expected=x['expected_work'] or {};diff=x['differences'] or {};claimed=x['claimed_work']
            inbound=x['scope'].startswith('inbound');number=key[1] if len(key)>2 or not inbound else '미기재'
            writer.writerow(['입고' if inbound else 'B2B 출고',key[0],number if not inbound else '',number if inbound else '',key[-1] if inbound else '전체(주문 합계)',
                             x['claim_sheet']+'!'+','.join(map(str,x['claim_rows'])),x['claim_EA'],x['wms_EA'],
                             *[claimed.get(c) for c in 'LMNO'],expected.get('L'),expected.get('M'),*[diff.get(c) for c in 'LMNO'],
                             x['status'],x.get('reason','잠정 환산 비교; 실제 작업 발생·과금 확정 아님'),
                             ';'.join(p['sku'] for p in x['wms_parts'] if p.get('reason')),cfg['sources']['claim']['path'],
                             ';'.join(cfg['sources'][e['source']]['path']+' / '+e.get('sheet','CSV')+'!'+str(e.get('row',e.get('csv_record'))) for e in x['wms_evidence'][:2]),
                             ';'.join(x.get('cause_candidates',[])),
                             ';'.join(p['sku']+':'+p['rate_basis']+' '+str(p['outer_EA'])+'EA/BOX'+(' 입고리스트!'+','.join(map(str,p['auxiliary_claim_rows'][:2])) if p['auxiliary_claim_rows'] else '')+(' 제품 Master Data!'+','.join(map(str,p['master_rows'])) if p['rate_basis']=='master' else '') for p in x['wms_parts']),
                             MASTERS['master']['path'],x.get('cause_analysis',{}).get('label',''),
                             *[x.get('cause_analysis',{}).get('recorded_detail',{}).get(c) for c in ('BOX','PCS')],
                             x.get('cause_analysis',{}).get('work_minus_detail',{}).get('M'),
                             *[x.get('cause_analysis',{}).get('detail_minus_approved_conversion',{}).get(c) for c in ('L','M')],
                             x.get('cause_analysis',{}).get('recorded_boxes'),x.get('cause_analysis',{}).get('remainder_sku_count'),
                             x.get('cause_analysis',{}).get('decision_scope','원인 조사; 기존 차이·판정 유지' if x.get('cause_analysis') else '')])
    return output
