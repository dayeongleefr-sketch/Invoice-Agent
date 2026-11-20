"""All registered files participate; source identities survive aggregation."""
from collections import defaultdict
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from zipfile import BadZipFile
from monthly_cases import identity, verify_sources


def run_all(root,case,answers,recurring_state,runner):
    from monthly_verification import names,rows,tabular,numeric,text,finish,FIELDS
    from recurring_rules import snapshot
    root=Path(root);verify_sources(root,case)
    recurring_state=snapshot(root) if recurring_state is None else recurring_state
    claims=[m for m in case['materials'] if m['role']=='claim']
    result={'case':case,'identity':identity(case),'checks':[],'profiles':{},'fee_items':[],
            'cost_evidence':[],'contract_documents':[],'recurring_rules':[],'recurring_snapshot_hash':recurring_state['fingerprint'],
            'full_invoice_validated':False,'llm_used':False,'material_coverage':[],'layout_observations':[]}
    # Decode each shared WMS/Master/rate file once during a multi-invoice run.
    cache={}
    def tables(path,required):
        key=(str(path),tuple(sorted(required)))
        if len(claims)==1:
            yield from tabular(path,required);return
        if key not in cache:
            values=[];error=None
            try:values=list(tabular(path,required))
            except (ValueError,UnicodeError) as exc:error=exc
            cache[key]=(values,error)
        values,error=cache[key]
        if error:raise error
        yield from values
    claim_sheets=[];valid=[]
    for claim in claims:
        try:
            available=names(root/claim['path'])
            claim_sheets.append({s:dict(rows(root/claim['path'],s)) for s in ('입고리스트',) if s in available})
            valid.append(claim)
        except (ValueError,KeyError,OSError,BadZipFile) as exc:
            row={k:'' for k in FIELDS}
            row.update(검사='청구 파일 읽기',status='not_implemented',결과='미검증',사유구분='기능 미구현',
                       사유='이 청구 파일을 읽지 못했습니다: '+str(exc),청구원본=claim['path'],조회구분='청구 파일 읽기 / 미검증')
            result['checks'].append(row)
    if not valid:raise ValueError('등록한 청구서를 읽지 못했습니다. 마지막 완료 결과를 유지합니다.')
    results=[]
    for claim in valid:
        part=runner(root,case,answers,recurring_state,claim=claim,table_reader=tables,claim_sheets=claim_sheets)
        for key in ('fee_items','cost_evidence','recurring_rules'):
            for item in part[key]:item['claim_path']=claim['path']
            result[key].extend(part[key])
        known={d['path'] for d in result['contract_documents']}
        result['contract_documents'].extend(d for d in part.get('contract_documents',[]) if d['path'] not in known)
        result['profiles'].update(part['profiles']);results.append(part)
        result['layout_observations'].extend(part.get('layout_observations',[]))
    # A WMS-only row must be missing from every invoice, not just one file.
    def wms_key(r):return tuple(r.get(k,'') for k in ('일자','입고예정번호','주문번호','품번','WMS위치'))
    missing_sets=[{wms_key(r) for r in p['checks'] if r['검사']=='WMS에만 있는 실적'} for p in results]
    missing=set.intersection(*missing_sets)
    shared=set()
    shared_groups={'WMS 일자·단위 확인','WMS 번호 확인','WMS 반복 내역','WMS 양식',
                   'Master 양식','Master 환산 기준 충돌','박스·낱개 참고 환산','단가표 양식','계약 문서 해석',
                   '계약 단가표 읽기','계약 파일 읽기','계약 기재기간 비교','계약 적용·실비 적정성'}
    for part in results:
        for row in part['checks']:
            if row['검사']=='WMS에만 있는 실적':
                key=wms_key(row)
                if key not in missing or ('missing',key) in shared:continue
                shared.add(('missing',key));row['청구원본']=''
                if len(valid)!=len(claims):row['사유']='읽을 수 있는 청구서에서 연결되지 않음. 읽지 못한 청구서가 있어 전체 청구의 누락 여부는 판단 불가.'
            elif row['검사'] in shared_groups:
                key=tuple(row.get(k,'') for k in ('검사','사유','WMS위치','계약위치','일자','주문번호','품번'))
                if key in shared:continue
                shared.add(key)
            result['checks'].append(row)
    if len(valid)>1:
        # Same business key across invoices is one quantity comparison. Preserve
        # the source contributions; do not compare each part to the full WMS.
        def quantity_key(row):
            return tuple(row.get(k,'') for k in ('일자','주문번호','입고예정번호','품번','단위'))
        base_sources=defaultdict(set);rule_rows=defaultdict(list)
        for row in result['checks']:
            if row['검사'] in ('입고 상품 수량','B2B 상품 수량'):
                base_sources[(row['검사'],quantity_key(row))].add(row['청구원본'])
            elif row['검사'] in ('저장 기준 입고 수량','저장 기준 B2B 출고 수량'):
                rule_rows[(row['검사'],quantity_key(row))].append(row)
        for (group,key),members in rule_rows.items():
            base_group='입고 상품 수량' if group=='저장 기준 입고 수량' else 'B2B 상품 수량'
            # Conditional criteria may cover only one invoice's contribution.
            # Never compare that partial claim to the whole shared WMS order.
            incomplete={r['청구원본'] for r in members}!=base_sources[(base_group,key)]
            conflicting=len({r.get('recurring_rule_id') for r in members})!=1
            if incomplete or conflicting:
                for row in members:
                    row.update(비교값='',차이='',status='data_unavailable',결과='미검증',사유구분='자료 부족',
                        사유='여러 청구 파일의 같은 입출고 대상에 적용 조건이 다릅니다. WMS 전체 수량을 일부 청구에 배분하지 않습니다.',
                        조회구분=group+' / 미검증')
                for item in result['recurring_rules']:
                    if item.get('kind')!='recurring_quantity':continue
                    business_key=item.get('key',[])
                    expected_key=[key[0],key[2],key[3]] if base_group=='입고 상품 수량' else [key[0],key[1]]
                    if item['operation']==('inbound' if base_group=='입고 상품 수량' else 'b2b_outbound') and business_key==expected_key:
                        item.update(applied=False,status='data_unavailable',comparison=None,
                            limitation='같은 입출고 대상의 여러 청구 파일에 적용 조건이 달라 전체 비교 미확정')
        groups=defaultdict(list)
        for row in result['checks']:
            if row['검사'] in ('입고 상품 수량','B2B 상품 수량','저장 기준 입고 수량','저장 기준 B2B 출고 수량'):
                groups[tuple(row.get(k,'') for k in ('검사','일자','주문번호','입고예정번호','품번','단위'))].append(row)
        replaced=set();merged=[]
        for members in groups.values():
            if len(members)<2:continue
            row=deepcopy(members[0]);values=[numeric(r['청구값']) for r in members]
            comparison={numeric(r['비교값']) for r in members}
            a=sum(values,Decimal(0)) if all(v is not None for v in values) else None
            b=next(iter(comparison)) if len(comparison)==1 else None
            diff=a-b if a is not None and b is not None else None
            row.update(청구값=text(a),비교값=text(b),차이=text(diff),status='data_unavailable' if diff is None else 'match' if diff==0 else 'difference')
            row.update(결과={'match':'정상','difference':'불일치','data_unavailable':'미검증'}[row['status']],
                       사유='같은 일자·번호'+('·품번' if '입고' in row['검사'] else '')+'의 모든 청구 파일 수량 합계와 WMS 합계 비교 / '+members[0]['사유'])
            row['사유구분']='자료 부족' if diff is None else '차이' if diff else ''
            row['claim_sources']=[{'path':r['청구원본'],'location':r['청구위치'],'quantity':r['청구값']} for r in members]
            row['청구원본']='; '.join(r['청구원본'] for r in members)
            row['청구위치']='; '.join(r['청구원본']+' / '+r['청구위치'] for r in members)
            row['계산근거']=row['청구위치'];row['조회구분']=row['검사']+' / '+row['결과']
            replaced.update(id(r) for r in members);merged.append(row)
        result['checks']=[r for r in result['checks'] if id(r) not in replaced]+merged
        # A whole-period packing charge cannot be independently validated against
        # the same whole WMS in each invoice. Record that allocation limitation.
        packing=[r for r in result['checks'] if r['검사']=='B2C WMS 포장비 잠정 비교']
        if len({r['청구원본'] for r in packing})>1:
            for row in packing:
                row.update(status='data_unavailable',결과='미검증',사유구분='자료 부족',조회구분=row['검사']+' / 미검증',
                           사유='여러 청구서에 포장비가 있습니다. 전체 WMS 잠정액은 비교값에 보존하며 청구서별 포장 범위가 없어 개별 일치 판정을 하지 않습니다.')
        # Duplicate parcel identifiers across files may be revisions or separate
        # charges. Keep both bills and explicitly flag overlap instead of silently
        # counting the same shipment as two verified claims.
        parcels=defaultdict(list)
        for row in result['checks']:
            if row['검사']=='B2C 주문 연결':parcels[(row['주문번호'],row['송장번호'])].append(row)
        for members in parcels.values():
            if len({r['청구원본'] for r in members})<2:continue
            for row in members:
                row['connection_status']=row['status']
                row.update(status='data_unavailable',결과='미검증',사유구분='자료 부족',조회구분='B2C 주문 연결 / 미검증',
                           사유='같은 주문·송장이 여러 청구 파일에 기재됨. 연결 근거는 유지하며 수정본 중복인지 별도 청구인지 미확정.')
    from formula_rules import apply_monthly
    apply_monthly(root,case,result,[p.pop('_formula_input',{}) for p in results],recurring_state)
    for i,row in enumerate(result['checks'],1):row['id']=str(i)
    for m in case['materials']:
        issues=[r for r in result['checks'] if r['status']=='not_implemented' and (r.get('청구원본')==m['path'] or m['path'] in r.get('WMS위치','') or m['path'] in r.get('계약위치',''))]
        result['material_coverage'].append({'role':m['role'],'path':m['path'],'sha256':m['sha256'],
            'status':'not_implemented' if issues else 'considered','limitations':list(dict.fromkeys(r['사유'] for r in issues))})
    verify_sources(root,case)
    return finish(result,answers)
