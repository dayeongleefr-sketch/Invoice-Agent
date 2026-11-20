"""Selected BOX/PCS scenarios from original invoice, WMS and product Master.

No base result is published here. Work occurrence and invoice approval remain
outside this reference conversion; the rule ledger requires explicit approval.
"""
from collections import defaultdict
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
import json
import re

import chat_intake
import billing_quantity_tools as billing
import quantity_rematch as rematch
from identifier_tools import expand_identifier, stored_number
from invoice_tools import digest
from portable_workbook import open_workbook
from wms_tools import day, key_text, validated_rows, xlsb_rows

VERSION = 'pack-conversion-v1'
BASE = Path('data/demo_company')
RESULT = BASE/'derived/cost_verification.json'
CHECKS = {'입고 BOX·PCS 잠정 환산', 'B2B BOX·PCS 잠정 환산'}
METHODS = {'group_sku': '같은 일자·번호·품번 합산 후 환산',
           'wms_row': 'WMS 원본 각 행 환산 후 합산'}
LIMITATIONS = ['선택한 박스·낱개 참고 수량만 비교합니다. 실제 작업 횟수·작업비 적정성은 확정하지 않습니다.',
              '청구서의 빈 작업량은 채우지 않습니다. 환산할 수 없는 품번이 있으면 전체 일치로 처리하지 않습니다.',
              '이번 7월 선택 건에만 적용합니다. 상품 EA·금액·기존 판정과 다른 월은 변경하지 않습니다.']


def baseline_parameters(root=None):
    return dict(aggregation='group_sku', sku='', ea_per_box='')


def validate(p):
    if type(p) is not dict or set(p) != {'aggregation','sku','ea_per_box'} or any(type(v) is not str for v in p.values()):
        raise ValueError('환산 순서와 특정 품번의 박스당 수량을 확인해 주세요.')
    if p['aggregation'] not in METHODS or len(p['sku']) > 200:
        raise ValueError('지원하는 환산 순서를 선택해 주세요.')
    if bool(p['sku'].strip()) != bool(p['ea_per_box'].strip()):
        raise ValueError('박스당 수량을 지정하려면 품번과 수량을 함께 입력해 주세요.')
    if p['sku'] != p['sku'].strip():
        raise ValueError('품번 앞뒤 공백을 제거해 주세요.')
    if p['ea_per_box']:
        n=stored_number(p['ea_per_box'])
        if n is None or n <= 0 or n % 1 or n > 10**9:
            raise ValueError('박스당 수량은 1~1,000,000,000 사이의 정수로 입력해 주세요.')
    return p


def description(p):
    return METHODS[p['aggregation']] + (f" · {p['sku']} 1BOX={p['ea_per_box']}EA" if p['sku'] else ' · 기존 Master/청구 내품수량')


def targets(root,record):
    import rule_adapters as a
    payload=a._record(record)
    if any(payload.get(k)!=v for k,v in {'intent':'upsert_rule','month':'2029-07','scope':'once','application':'current_case'}.items()):
        raise ValueError('박스·낱개 환산 변경은 이번 7월 청구만 지원합니다.')
    ts=payload.get('targets')
    if not isinstance(ts,list) or not 1<=len(ts)<=100:
        raise ValueError('박스·낱개 참고 비교 대상 1~100개를 선택해 주세요.')
    current=chat_intake._current(root); doc=a._json(root,RESULT)
    expected=a._json(root,BASE/'derived/workflow_result.json').get('result_hashes',{}).get(RESULT.name)
    if not expected or digest(root/RESULT).upper()!=expected.upper():
        raise ValueError('저장된 과금수량 결과의 버전이 다릅니다.')
    selected=[];seen=set()
    for t in ts:
        i=str(t.get('issue_id',''))
        if not i.isdecimal() or int(i)>=len(current['rows']):raise ValueError('선택 목록이 변경되었습니다.')
        r=current['rows'][int(i)]
        if {k:v for k,v in t.items() if k!='alias'}!=chat_intake._target(r,int(i),current['version']):
            raise ValueError('선택한 청구의 자료·결과가 변경되었습니다.')
        ref=re.fullmatch(r'cost_verification\.json#/billing_quantity/rows/(\d+)',r.get('계산근거',''))
        if t.get('check') not in CHECKS or t.get('unit') not in ('BOX','PCS') or not ref:
            raise ValueError('입고 또는 B2B 박스·낱개 참고 비교만 선택해 주세요.')
        n=int(ref[1]); rows=doc['billing_quantity']['rows']
        if n>=len(rows):raise ValueError('계산 근거를 찾지 못했습니다.')
        c=rows[n];cells=re.findall(r'(?:!|,)([LMNO])(\d+)',r.get('청구위치',''))
        cols={x for x,_ in cells}
        if (len(cols)!=1 or sorted(int(y) for _,y in cells)!=sorted(c['claim_rows'])
                or t['source']!=Path(doc['sources']['claim']['path']).name
                or r.get('청구위치','')!=c['claim_sheet']+'!'+','.join(x+y for x,y in cells)):
            raise ValueError('선택한 청구 셀과 저장된 작업량 근거가 다릅니다.')
        col=next(iter(cols)); operation=rematch.direction(c['scope'])
        if col not in ('LM' if operation=='inbound' else 'LMNO') or t['unit']!=('BOX' if col in 'LN' else 'PCS'):
            raise ValueError('작업량 열과 박스·낱개 단위가 다릅니다.')
        if t['check']!=('입고 BOX·PCS 잠정 환산' if operation=='inbound' else 'B2B BOX·PCS 잠정 환산'):
            raise ValueError('입고·출고 대상이 혼합되었습니다.')
        if (n,col) in seen:raise ValueError('같은 수량 칸을 중복 선택했습니다.')
        seen.add((n,col)); selected.append((t,c,col))
    if len({rematch.direction(c['scope']) for _,c,_ in selected})!=1:
        raise ValueError('입고와 B2B 출고는 각각 선택해 주세요.')
    return selected,doc


def options(root,record):
    try:
        targets(root,record)
        return dict(supported=True,message='환산할 수량의 집계 방식과 박스당 수량을 확인해 주세요.',choices=[dict(
            kind='pack_conversion',label='박스·낱개 환산 순서와 박스당 수량 변경',
            description='품번·박스당 수량은 변경할 때만 함께 입력합니다. 다른 품번에는 기존 기준을 사용합니다.',
            parameters=[dict(name='aggregation',label='환산 순서',type='select',options=METHODS,value=None,required=True),
                        dict(name='sku',label='박스당 수량을 변경할 품번 (선택)',type='text'),
                        dict(name='ea_per_box',label='해당 품번 1박스당 상품 개수 (EA, 선택)',type='text')],limitations=LIMITATIONS)])
    except (ValueError,OSError,KeyError,TypeError) as exc:
        return dict(supported=False,choices=[],message=str(exc))


def read_claim(path,cfg,operation):
    """Read work columns and invoice inner quantities, not an internal check file."""
    rows=[];inners=[]
    with open_workbook(str(path)) as book:
        for op in dict.fromkeys([operation,'inbound']):
            if op not in cfg['claim_mappings']:continue
            mapping=cfg['claim_mappings'][op];m=mapping['claim']
            with book.get_sheet(mapping['claim_sheet']) as sheet:
                for n,c in validated_rows(xlsb_rows(sheet),mapping['claim_headers']):
                    if n<mapping.get('claim_start',4):continue
                    d=day(c.get(m['date']))
                    if not '2029-06-26'<=d<='2029-07-25':continue
                    sku=key_text(c.get(m.get('sku','D'))) if op=='inbound' else ''
                    if op=='inbound':inners.append(dict(sku=sku,row=n,value=stored_number(c.get('J',{}).get('value'))))
                    if op!=operation:continue
                    if not any(c.get(k,{}).get('value') not in (None,'') for k in m['key']):continue
                    rows.append(dict(direction=op,date=d,number=key_text(c.get(m['key'][0])),sku=sku,
                        quantity=stored_number(c.get(m['quantity'],{}).get('value')),row=n,sheet=mapping['claim_sheet'],
                        work={k:stored_number(c.get(k,{}).get('value')) for k in ('LM' if op=='inbound' else 'LMNO')}))
    return rows,inners


def values(claim,comparison,unit):
    diff=None if claim is None or comparison is None else claim-comparison
    return dict(claim=rematch._num(claim),comparison=rematch._num(comparison),difference=rematch._num(diff),
                status='not_determinable' if diff is None else 'match' if diff==0 else 'difference',unit=unit)


def convert(rows,master,inners,policy,p,claim_rows,operation):
    if not rows:return None,[],['대응 WMS 출고·입고 내역 없음']
    groups=defaultdict(Decimal);rates={};refs=[];problems=[]
    for r in rows:
        sku=r['sku'];q=stored_number(r.get('quantity'))
        if r.get('issue') or not sku or q is None or q<0 or q%1:
            problems.append('WMS 품번·EA 단위·수량 확인 불가');continue
        if sku not in rates:
            candidates=[x for x in inners if x['sku']==sku and (operation=='outbound' or x['row'] in claim_rows)]
            rate,reason,basis,locations=billing.resolved_rate(master,sku,candidates,policy)
            if p['sku']==sku:rate=stored_number(p['ea_per_box']);reason=None;basis='지정 박스당 수량';locations=[]
            if reason:problems.append(f'{sku}: {reason}')
            rates[sku]=rate
            refs.extend(('claim_inner',n) for n in locations)
            if basis=='master':refs.extend(('master',x['row']) for x in master.get(sku,[]))
        group=(sku,) if p['aggregation']=='group_sku' else (sku,r['role'],r['row'])
        groups[group]+=q
    if problems:return None,refs,list(dict.fromkeys(problems))
    boxes=pieces=Decimal(0)
    for key,q in groups.items():
        b,s=billing.split_units(q,rates[key[0]]);boxes+=b;pieces+=s
    return {'BOX':boxes,'PCS':pieces},refs,[]


def compare(selected,claims,inners,wms,master,policy,p,sources,period):
    if p['sku'] and not any(r['sku']==p['sku'] for r in wms):
        raise ValueError('지정한 품번이 선택 대상의 WMS에 없습니다. 품번을 다시 확인해 주세요.')
    changes=[]
    for t,c,col in selected:
        key=tuple(c['key']);operation=rematch.direction(c['scope'])
        cr=[r for r in claims if rematch.baseline_key(r)==key]
        wr=[r for r in wms if rematch.baseline_key(r)==key and period['start']<=r['date']<=period['end']]
        if sorted(r['row'] for r in cr)!=sorted(c['claim_rows']):raise ValueError('원본 청구 행과 저장된 대상이 다릅니다.')
        if any(r['quantity'] is None for r in cr) or sum((r['quantity'] for r in cr),Decimal(0))!=stored_number(c['claim_EA']):
            raise ValueError('청구 상품 수량이 변경되었습니다. 전체 검증을 먼저 실행해 주세요.')
        if wr and not any(r.get('issue') or r['quantity'] is None for r in wr):
            if sum((Decimal(str(r['quantity'])) for r in wr),Decimal(0))!=stored_number(c['wms_EA']):
                raise ValueError('WMS 상품 수량이 저장 결과와 다릅니다.')
        stored_refs={(e['source'],e.get('row',e.get('csv_record'))) for e in c['wms_evidence']}
        if {(r['role'],r['row']) for r in wr}!=stored_refs:raise ValueError('WMS 원본 연결 행이 저장 결과와 다릅니다.')
        claimed=None if any(r['work'][col] is None for r in cr) else sum((r['work'][col] for r in cr),Decimal(0))
        if claimed!=stored_number(c['claimed_work'][col]):raise ValueError('원본 청구 작업량이 변경되었습니다.')
        baseline,_,_=convert(wr,master,inners,policy,baseline_parameters(),c['claim_rows'],operation)
        old=stored_number((c.get('expected_work') or {}).get(col))
        if old is not None and (baseline is None or baseline[t['unit']]!=old):
            raise ValueError('기존 환산값을 원본에서 재현하지 못했습니다. 전체 검증을 먼저 실행해 주세요.')
        before=values(claimed,old,t['unit'])
        for field,k in [('claim_value','claim'),('comparison_value','comparison'),('difference','difference')]:
            if stored_number(t.get(field))!=stored_number(before[k]):raise ValueError('저장된 비교 수치가 선택 대상과 다릅니다.')
        calculated,rate_refs,problems=convert(wr,master,inners,policy,p,c['claim_rows'],operation)
        after=values(claimed,calculated[t['unit']] if calculated else None,t['unit'])
        refs=[sources['claim']['path']+f" / {c['claim_sheet']}!{col}{r['row']}" for r in cr]
        refs.extend(sources[r['role']]['path']+f" / {r['sheet']}!{r['row']}" for r in wr)
        for role,n in rate_refs:
            refs.append(sources['claim' if role=='claim_inner' else 'master']['path']+f" / {'입고리스트!J' if role=='claim_inner' else '제품 Master Data!E'}{n}")
        basis=description(p)+'. '+('청구 작업량이 빈칸이므로 비교하지 않습니다. ' if claimed is None else '')
        basis+='; '.join(problems) if problems else '완박스 몫과 남은 낱개를 각각 합산했습니다.'
        changes.append(dict(target_id=t['target_id'],label=t['label'],check=t['check'],before=before,after=after,
                            basis=basis,evidence_refs=list(dict.fromkeys(refs))))
    return dict(adapter_version=VERSION,changes=changes,limitations=LIMITATIONS,calculation_executed=True)


@lru_cache(maxsize=3)
def load(root_text,cfg_text,operation,keys,decisions_text,cost_text,hashes):
    root=Path(root_text);cfg=json.loads(cfg_text)
    paths={role:root/s['path'] for role,s in cfg['sources'].items() if role in dict(hashes)}
    claims,inners=read_claim(paths['claim'],cfg,operation)
    wms=rematch.read_wms(paths,operation,set(keys),set(keys),{'join':'baseline'},json.loads(decisions_text),json.loads(cost_text))
    return claims,inners,wms,billing.master_index(root)


def calculate(root,record,parameters):
    import rule_adapters as a
    root=Path(root).resolve();p=validate(parameters);selected,doc=targets(root,record)
    cfg=a._json(root,a.CONFIG);decisions=a._json(root,Path(cfg['decisions_path']));cost=a._json(root,Path('ia/config/demo_company_cost_decisions.json'))
    period=decisions['comparison_period'];operation=rematch.direction(selected[0][1]['scope'])
    if period.get('state')!='confirmed_by_user' or any(period[k]!=doc['billing_quantity']['period'][k] for k in ('start','end')):
        raise ValueError('기존 비교기간과 결정이 다릅니다.')
    policy=cost.get('missing_billing_quantity_basis',{})
    # Existing Master and unique invoice-inner fallback approvals are not broadened.
    paths={};hashes=[]
    for role in ['claim',operation+'_h1',operation+'_july','master','master_reply']:
        source=doc['sources'].get(role)
        if source is None:
            if role in ('claim','master','master_reply'):raise ValueError('필수 환산 원본이 없습니다.')
            continue
        registered=billing.MASTERS.get(role) or cfg['sources'].get(role)
        if source!=registered:raise ValueError('저장 결과와 등록된 원본이 다릅니다.')
        path=chat_intake._safe_file(root,Path(source['path']))
        if not path.resolve().is_relative_to((root/BASE/'source').resolve()):raise ValueError('원본 자료 경계를 벗어났습니다.')
        if digest(path).upper()!=source['sha256'].upper():raise ValueError('원본 자료가 바뀌었습니다. 전체 검증을 먼저 실행해 주세요.')
        paths[role]=path;hashes.append((role,source['sha256']))
    dump=lambda v:json.dumps(v,ensure_ascii=False,sort_keys=True)
    raw=load(str(root),dump(cfg),operation,tuple(sorted({tuple(c['key']) for _,c,_ in selected})),dump(decisions),dump(cost),tuple(hashes))
    result=compare(selected,*raw,policy,p,doc['sources'],period)
    for role,path in paths.items():
        if digest(path).upper()!=doc['sources'][role]['sha256'].upper():raise ValueError('재계산 중 원본이 변경되었습니다.')
    targets(root,record)
    return result
