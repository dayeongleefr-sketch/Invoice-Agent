"""Confirmed conditions/formulas over original values, separate from base checks."""
from decimal import Decimal,localcontext
from pathlib import Path
from calculation_expression import compile_expression,evaluate
from identifier_tools import stored_number
import cost_references as refs
import formula_dependencies as dependencies

KIND='formula'
FIELDS={'operation','condition','expression','unit','category','item','billing_unit','start_month','target_scope'}
QUANTITY={'청구수량','WMS수량','소모품수량','상품수량','일자','번호','품번','거래처'}
COST={'청구수량','청구단가','비용구분','항목명','과금단위'}
OPERATIONS={'inbound':'입고 수량','outbound':'B2B 수량','cost':'청구 비용'}
LIMITS=['계산 결과는 새 기준 비교로 저장하며 원본·기본 판정·전체 청구금액은 변경하지 않습니다.',
        '없는 원본 값은 추정하지 않습니다. 조건에 해당하지 않는 건은 이 수식 적용에서만 제외합니다.',
        '사칙연산·IF·반올림·SUM/AVERAGE·SUMIF/SUMIFS·COUNTIF/COUNTIFS·정확 XLOOKUP·날짜 함수를 지원합니다. 범위는 [값1, 값2]로 지정합니다.']

def variables(p):return COST | refs.names(p.get('references', [])) | dependencies.names(p.get('result_references', [])) if p['operation']=='cost' else QUANTITY

def validate(p):
    if type(p) is not dict or not FIELDS <= set(p) <= FIELDS|{'references','result_references'} or any(type(p[k]) is not str or len(p[k])>1000 for k in FIELDS):
        raise ValueError('수식의 대상·조건·계산식·단위를 확인해 주세요.')
    specs=refs.decode(p.get('references', []))
    derived=dependencies.decode(p.get('result_references', []))
    if (specs or derived) and p['operation']!='cost':raise ValueError('다른 비용 금액은 청구 비용 계산에서만 참조할 수 있습니다.')
    if p['operation'] not in OPERATIONS or p['unit']!=('KRW' if p['operation']=='cost' else 'EA'):
        raise ValueError('수량은 EA, 비용은 원(KRW)으로 비교합니다.')
    if p['target_scope'] not in ('selected','all_operation','condition_matches','same_cost'):
        raise ValueError('수식 적용 범위를 선택해 주세요.')
    compile_expression(p['condition'],variables(p));compile_expression(p['expression'],variables(p))
    if specs or derived:
        import ast
        used={n.id for text in (p['condition'],p['expression']) for n in ast.walk(compile_expression(text,variables(p))[0]) if isinstance(n,ast.Name)}
        if specs and not used & refs.names(specs):raise ValueError('선택한 참조 비용을 수식에 사용해 주세요. 예: 참조합계 * 0.05')
        if derived and not used & dependencies.names(derived):raise ValueError('선택한 계산 결과를 수식에 사용해 주세요. 예: 계산결과1 * 0.05')

def binding(record,p):
    validate(p);v=record.get('payload',record)
    if v['scope']=='future':
        from recurring_rules import binding as bind
        bind(record,p['start_month'])
        if p['operation']=='cost':
            if p['target_scope']!='same_cost':raise ValueError('비용 수식은 같은 비용 구분·항목·과금 단위에 적용합니다.')
            keys=[tuple(s[k] for k in ('category','item','billing_unit')) for s in refs.decode(p.get('references',[]))]
            if len(keys)!=len(set(keys)) or (p['category'],p['item'],p['billing_unit']) in keys:
                raise ValueError('향후 월 참조는 같은 비용·단위가 유일해야 하며 계산 대상 자체를 참조할 수 없습니다.')
        elif p['target_scope'] not in ('all_operation','condition_matches'):
            raise ValueError('앞으로 적용할 업무 전체 또는 조건 해당 건을 명시해 주세요.')
        if p['target_scope']=='condition_matches' and p['condition'].strip()=='True':
            raise ValueError('조건 해당 건에 적용할 조건식을 입력해 주세요.')
    elif p['target_scope']!='selected' or p['start_month']!=v['month']:
        raise ValueError('이번 비교는 선택한 청구건에만 적용합니다.')

def definition(root,record):
    import rule_adapters as a
    v=record.get('payload',record);targets=v.get('targets',[])
    checks={t.get('check') for t in targets}
    if checks=={'선택 단가 금액 비교'}:
        import rate_rules as r
        rows,_=r.selected(root,record,formula=True)
        selectors={(str(i.get('category_context') or ''),str(i.get('item_context') or i.get('category_context') or ''),str(i.get('unit') or '')) for _,i in rows}
        if v['scope']=='future' and (len(rows)!=1 or len(selectors)!=1):raise ValueError('앞으로 사용할 비용 수식은 한 비용 항목씩 등록해 주세요.')
        cat,item,unit=next(iter(selectors)) if len(selectors)==1 else ('','','')
        return dict(operation='cost',unit='KRW',category=cat,item=item,billing_unit=unit)
    if checks not in ({'입고 상품 수량'},{'B2B 상품 수량'}):raise ValueError('입고·B2B 상품 수량 또는 청구 단가 비교에서 같은 업무 대상을 선택해 주세요.')
    a._targets(Path(root),record)
    return dict(operation='inbound' if checks=={'입고 상품 수량'} else 'outbound',unit='EA',category='',item='',billing_unit='')

def options(root,record):
    try:fixed=definition(root,record)
    except (ValueError,OSError,KeyError,TypeError):return None
    v=record.get('payload',record);future=v['scope']=='future'
    fixed['start_month']=v['effective_from_month'] if future else v['month']
    fields=[{'name':k,'label':{'operation':'비교 대상','unit':'계산 결과 단위','category':'비용 구분','item':'비용 항목','billing_unit':'과금 단위','start_month':'적용 시작월'}[k],
             'type':'select','options':{val:OPERATIONS.get(val,val) or '해당 없음'},'value':val} for k,val in fixed.items()]
    scopes={'same_cost':'같은 비용 구분·항목·과금 단위'} if future and fixed['operation']=='cost' else {'all_operation':'해당 업무의 청구 전체','condition_matches':'조건에 맞는 청구만'} if future else {'selected':'이번 선택한 청구건'}
    fields.append({'name':'target_scope','label':'적용 대상 범위','type':'select','options':scopes,'value':None if future else 'selected','required':True})
    if fixed['operation']=='cost':
        fields.append({'name':'references','label':'참조할 다른 비용 (선택 순서대로 참조금액1, 2…)','type':'multi_select',
                       'options':refs.choices(root,record),'value':[]})
        fields.append({'name':'result_references','label':'참조할 저장 계산식 (선택 순서대로 계산결과1, 2…)','type':'multi_select',
                       'options':dependencies.choices(root,record),'value':[]})
    fields += [{'name':'condition','label':'적용 조건 (전체 선택 건이면 True)','type':'text','value':'True','required':True},
               {'name':'expression','label':'비교 수량 또는 금액 계산식','type':'text','required':True}]
    example='WMS수량 - 소모품수량' if fixed['operation']!='cost' else 'ROUND(청구수량 * 청구단가 * 0.95, 0)'
    return {'kind':KIND,'label':'조건·계산식 직접 지정','parameters':fields,
            'description':'변수: '+', '.join(sorted(variables(fixed)))+' / 수식 예: '+example+(' / 비용 참조 예: ROUND(참조합계 * 0.05, 0)' if fixed['operation']=='cost' else ''),
            'limitations':LIMITS+(['참조금액은 청구서 원금액, 계산결과는 선택한 저장 계산식의 최신 유효 기준으로 원본을 다시 계산한 값입니다. 앞 계산의 중지·오류·순환 참조는 미확정으로 남깁니다.'] if fixed['operation']=='cost' else [])+(['미리보기는 현재 청구 계산 예시이며, 이후 월은 등록한 원본으로 다시 계산합니다.'] if future else [])}

def compare(p,facts,claim,unit):
    try:
        if not evaluate(p['condition'],facts,condition=True,variables=variables(p)):
            return dict(claim=claim,comparison=None,difference=None,status='excluded',unit=unit),'조건에 해당하지 않음'
        expected=evaluate(p['expression'],facts,variables=variables(p))
        if unit=='EA' and (expected<0 or expected!=expected.to_integral_value()):raise ValueError('EA 결과는 0 이상의 정수여야 합니다.')
        if claim is None:raise ValueError('청구 비교값이 없습니다.')
        with localcontext() as ctx:
            ctx.prec=100
            diff=Decimal(claim)-expected
        return dict(claim=claim,comparison=str(expected),difference=str(diff),status='match' if diff==0 else 'difference',unit=unit),''
    except ValueError as exc:
        return dict(claim=claim,comparison=None,difference=None,status='not_determinable',unit=unit),str(exc)

def cost_originals(root,record):
    import rate_rules as r
    rows,source=r.selected(root,record,formula=True);path=r._safe(root,source['path'])
    if not path.is_relative_to(r._safe(root,r.BASE/'source')) or r._hash(path).lower()!=source['sha256'].lower():raise ValueError('청구 원본이 변경되었습니다.')
    raw=r.read_claim(path,{i['row'] for _,i in rows});changes=[];facts=[]
    for target,i in rows:
        n=i['row'];c=raw[n];q,rate,amount=(stored_number(c.get(k,{}).get('value')) for k in ('M','L','N'))
        if (q,rate,amount)!=tuple(stored_number(i.get(k)) for k in ('quantity','unit_price','amount')):raise ValueError('청구 원본 숫자와 저장된 결과가 다릅니다.')
        if any(c.get(k,{}).get('value')!=v for k,v in i.get('raw_labels',{}).items()) or str(c.get('J',{}).get('value') or '').strip()!=str(i.get('unit') or '').strip():raise ValueError('청구 항목·단위가 변경되었습니다.')
        before=r._values(q,rate,amount)
        changes.append(dict(target_id=target['target_id'],check=target['check'],label=target['label'],before=before,after=dict(before),basis='',evidence_refs=[source['path']+f' / 청구내역!L{n}:N{n}']))
        facts.append({'청구수량':q,'청구단가':rate,'비용구분':i.get('category_context') or '',
                      '항목명':i.get('item_context') or i.get('category_context') or '', '과금단위':i.get('unit') or ''})
    if r._hash(path).lower()!=source['sha256'].lower():raise ValueError('계산 중 청구 원본이 변경되었습니다.')
    r.selected(root,record,formula=True)
    return dict(adapter_version='formula-v1',changes=changes,limitations=LIMITS,calculation_executed=True),facts

def calculate(root,record,p,*,baseline=False,_stack=(),_events=None):
    binding(record,p);fixed=definition(root,record)
    if any(p[k]!=v for k,v in fixed.items()):raise ValueError('선택한 원본의 업무·비용·단위와 수식 적용 범위가 다릅니다.')
    reference_values,reference_refs,reference_notes={},[],[]
    if p['operation']=='cost':
        result,facts=cost_originals(root,record)
        if not baseline:
            reference_values,reference_refs,reference_notes=refs.original(root,record,p.get('references',[]))
            derived,positions,notes=dependencies.original(root,record,p.get('result_references',[]),stack=_stack,events=_events)
            reference_values.update(derived);reference_refs+=positions;reference_notes+=notes
    else:
        from rule_adapters import quantity_calculation
        result,facts=quantity_calculation(root,record,{'consumables':'include'},with_facts=True)
    for change,values in zip(result['changes'],facts):
        values.update(reference_values)
        change['evidence_refs']+=reference_refs
        b=change['before']
        change['after'],reason=(dict(b),'기준 적용 중지 · 기본 비교값 유지') if baseline else compare(p,values,b['claim'],b['unit'])
        # Only referenced inputs are shown; never treat a result index as source evidence.
        tree,_=compile_expression(p['condition']+' and True',variables(p))
        import ast
        used={n.id for t in (tree,compile_expression(p['expression'],variables(p))[0]) for n in ast.walk(t) if isinstance(n,ast.Name)}
        inputs=' / '.join(k+'='+str(values[k]) for k in sorted(used & values.keys()))
        change['basis']=(f"조건: {p['condition']} / 계산식: {p['expression']} / {inputs}"+(' / '+reason if reason else '')+(' / '+' / '.join(reference_notes) if reference_notes else ''))[:4000]
    result.update(adapter_version='formula-v2',limitations=options(root,record)['limitations'])
    return result


def apply_monthly(root,case,result,parts,state):
    """Evaluate after invoices are combined; WMS is shared and counted once."""
    from collections import defaultdict
    from recurring_rules import applicable
    from rule_commands import code_fingerprint
    from monthly_verification import FIELDS
    events=applicable(case,state,KIND)
    if not events:return
    code=code_fingerprint(root);candidates=[]
    context=next((p['wms'] for p in parts if p),None)
    for role in ('inbound','outbound'):
        combined={}
        for part in parts:
            for key,g in part.get('groups',{}).get(role,{}).items():
                dst=combined.setdefault(key,{'qty':Decimal(0),'missing':False,'counterparties':[],'refs':[],'sources':[]})
                dst['qty']+=g['qty'];dst['missing']|=g['missing'];dst['counterparties']+=g['counterparties']
                dst['refs'] += [part['claim_path']+' / '+r for r in g['refs']];dst['sources'].append(part['claim_path'])
        for key,g in combined.items():
            rr=context['rows'][role].get(key,[]) if context else []
            incomplete=not context or bool(context['failed'][role]) or any(r['date']==key[0] and (not r['order'] or r['order']==key[1]) and (role=='outbound' or not r['sku'] or r['sku']==key[2]) for r in context['invalid'][role])
            total=sum((r['quantity'] for r in rr),Decimal(0)) if rr and not incomplete else None
            consumables=sum((r['quantity'] for r in rr if '(소모품)' in r['name']),Decimal(0)) if total is not None and all(r['name'].strip() for r in rr) else None
            channels=set(g['counterparties'])
            qty=None if g['missing'] else g['qty']
            facts={'청구수량':qty,'WMS수량':total,'소모품수량':consumables,
                '상품수량':total-consumables if total is not None and consumables is not None else None,
                '일자':key[0],'번호':key[1],'품번':key[2] if role=='inbound' else None,
                '거래처':next(iter(channels)) if len(channels)==1 and '' not in channels else None}
            candidates.append(dict(operation=role,facts=facts,claim=None if qty is None else str(qty),unit='EA',
                refs=g['refs'],sources=g['sources'],wms=[r['ref'] for r in rr],key=key))
    for item in result['fee_items']:
        from document_layouts import fee_location
        facts={'청구수량':stored_number(item['quantity']),'청구단가':stored_number(item['rate']),
               '비용구분':item['category'],'항목명':item['item'],'과금단위':item['unit']}
        candidates.append(dict(operation='cost',facts=facts,claim=str(item['amount']),unit='KRW',
            refs=[item['claim_path']+' / '+fee_location(item)],sources=[item['claim_path']],wms=[],key=(item['row'],),item=item))
    for candidate in candidates:
        matches=[]
        for event in events:
            p=event['payload']['spec']['parameters'];validate(p)
            if p['operation']!=candidate['operation']:continue
            if p['operation']=='cost' and (p['category'],p['item'],p['billing_unit'])!=tuple(candidate['facts'][k] for k in ('비용구분','항목명','과금단위')):continue
            values=dict(candidate['facts']);ref_positions=[];ref_problems=[]
            if p['operation']=='cost':
                reference_values,ref_positions,ref_problems=refs.monthly(p.get('references',[]),result['fee_items'],candidate['item'])
                values.update(reference_values)
                derived,positions,problems=dependencies.monthly(p.get('result_references',[]),result['fee_items'],case,state,event,code)
                values.update(derived);ref_positions+=positions;ref_problems+=problems
            try:yes=evaluate(p['condition'],values,condition=True,variables=variables(p))
            except ValueError:yes=None
            if yes is not False:matches.append((event,values,ref_positions,ref_problems))
        if not matches:continue
        event,values,ref_positions,ref_problems=matches[0];p=event['payload']['spec']['parameters']
        after,reason=compare(p,values,candidate['claim'],candidate['unit'])
        if ref_problems:
            after.update(comparison=None,difference=None,status='not_determinable');reason=' / '.join(ref_problems)
        if any(c['검사']=='청구 파일 읽기' and c['status']=='not_implemented' for c in result['checks']):
            after.update(comparison=None,difference=None,status='not_determinable')
            reason='읽지 못한 청구 파일이 있어 전체 적용 대상의 합산·중복 여부를 확인할 수 없습니다.'
        if len(matches)>1 or event['invalidated'] or event['code_fingerprint']!=code:
            after.update(comparison=None,difference=None,status='not_determinable')
            reason='같은 대상에 여러 수식이 적용되거나 저장 기준의 유효 상태가 변경되었습니다.'
        # Duplicate cost selectors across documents have no allocation agreement.
        if p['operation']=='cost' and sum(1 for c in candidates if c['operation']=='cost' and tuple(c['facts'][k] for k in ('비용구분','항목명','과금단위'))==(p['category'],p['item'],p['billing_unit']))>1:
            after.update(comparison=None,difference=None,status='not_determinable');reason='동일 비용·단위가 여러 청구행에 있어 수식의 합산·개별 적용 범위 미확정'
        status={'not_determinable':'data_unavailable'}.get(after['status'],after['status'])
        label={'match':'정상','difference':'불일치','data_unavailable':'미검증','excluded':'적용 대상 아님'}[status]
        group='저장 수식 '+OPERATIONS[p['operation']];row={k:'' for k in FIELDS}
        row.update(검사=group,status=status,결과=label,조회구분=group+' / '+label,
            사유구분='자료 부족' if status=='data_unavailable' else '차이' if status=='difference' else '',
            사유=f"조건: {p['condition']} / 계산식: {p['expression']}"+(' / '+reason if reason else ''),
            청구값=after['claim'] or '',비교값=after['comparison'] or '',차이=after['difference'] or '',단위=after['unit'],
            청구위치='; '.join(candidate['refs']),청구원본='; '.join(dict.fromkeys(candidate['sources'])),
            WMS위치='; '.join(candidate['wms']),적용기준='사용자가 확인한 조건·수식',계산근거='; '.join(candidate['refs']+ref_positions),
            recurring_rule_id=event['rule_id'])
        if p['operation']!='cost':
            key=candidate['key'];row.update(일자=key[0],주문번호=key[1] if p['operation']=='outbound' else '',입고예정번호=key[1] if p['operation']=='inbound' else '',품번=key[2] if p['operation']=='inbound' else '')
        result['checks'].append(row)
        result['recurring_rules'].append(dict(kind=KIND,rule_id=event['rule_id'],rule_version=event['rule_version'],
            parameters=p,inputs={k:str(v) if isinstance(v,Decimal) else v for k,v in values.items()},reference_sources=ref_positions,
            comparison=after['comparison'],status=status,applied=after['comparison'] is not None,base_calculation_unchanged=True,
            claim_sources=candidate['refs'],wms_sources=candidate['wms']))
