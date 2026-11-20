"""Selected invoice amount comparisons using source-stated unit prices.

Catalogue rows are separate from the original exception CSV. They do not claim
an applicable contract, actual work count, or a new global invoice total.
"""
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re

from portable_workbook import open_workbook
from xlsb_scan import scan_rows
from cost_verification import CONTRACT, CONTRACT_HASH, RATES, comparison
from identifier_tools import stored_number, billable_amount

BASE = Path('data/demo_company')
INVENTORY = BASE/'derived/full_invoice_result.json'
CHECK = '선택 단가 금액 비교'
LIMITATIONS = [
    '청구서에 기재된 과금수량×선택 단가를 청구금액과 비교합니다. WMS 수량이나 실제 작업량을 확정하지 않습니다.',
    '계약서 기재 단가 선택은 당월 계약 적용 승인과 다릅니다. 기존 잠정 결정과 원본 단가는 유지합니다.',
    '현재 선택한 7월 청구 항목만 비교합니다. 전체 정산금액·세금·다른 비용과 미래 월에는 자동 반영하지 않습니다.',
]


def _safe(root, relative):
    from chat_intake import _safe_file
    return _safe_file(Path(root).resolve(), Path(relative))


def _json(root, relative):
    return json.loads(_safe(root, relative).read_text('utf-8-sig'))


def _hash(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def _decimal(value):
    return stored_number(value)


def _str(value):
    return '' if value is None else format(value,'f') if isinstance(value,Decimal) else str(value)


def _values(q, rate, amount):
    expected=q*rate if q is not None and rate is not None else None
    result=comparison('selected-rate',expected,amount,[],'선택 단가')
    return {'claim':_str(amount) or None,'comparison':_str(expected) or None,
            'difference':_str(result['difference']) or None,'unit':'KRW',
            'status':{'data_unavailable':'not_determinable'}.get(result['status'],result['status'])}


def inventory(root):
    path=_safe(root,INVENTORY)
    raw=path.read_bytes();fingerprint=hashlib.sha256(raw).hexdigest()
    workflow=_json(root,BASE/'derived/workflow_result.json')
    expected=workflow.get('result_hashes',{}).get(INVENTORY.name)
    if not expected or fingerprint.lower()!=expected.lower():
        raise ValueError('청구 항목 목록과 완료 결과의 버전이 다릅니다. 전체 검증 결과를 확인해 주세요.')
    doc=json.loads(raw.decode('utf-8-sig'));cfg=_json(root,'ia/config/demo_company_direct_wms.json')
    claim=cfg['sources']['claim']
    if doc.get('sources',{}).get('claim',{}).get('path')!=claim['path'] or doc['sources']['claim']['sha256'].lower()!=claim['sha256'].lower():
        raise ValueError('청구 항목 목록의 원본이 현재 등록 청구서와 다릅니다.')
    items=doc.get('claim_items',[])
    if not items or any(type(i.get('row')) is not int or i['row']<1 or i.get('sheet')!='청구내역' for i in items):
        raise ValueError('청구 항목의 시트·행 정보가 올바르지 않습니다.')
    if len({i['row'] for i in items})!=len(items):
        raise ValueError('같은 청구 요약행이 중복되어 있습니다.')
    return items,claim,fingerprint


def catalog_rows(root):
    """Small, hash-verified original inventory; never writes the issue CSV."""
    if not _safe(root,INVENTORY).exists():
        return [],None
    items,claim,fingerprint=inventory(root)
    rows=[]
    for item in sorted(items,key=lambda i:i['row']):
        if not billable_amount(item.get('amount')):continue
        n=item['row'];q=_decimal(item.get('quantity'));rate=_decimal(item.get('unit_price'));amount=_decimal(item.get('amount'))
        values=_values(q,rate,amount)
        label=' · '.join(dict.fromkeys(str(item.get(k) or '') for k in ('category_context','item_context'))).strip(' ·')
        rows.append({'검사':CHECK,'결과':{'match':'정상','difference':'불일치','not_determinable':'미검증'}[values['status']],
            '청구값':_str(values['claim']),'비교값':_str(values['comparison']),'차이':_str(values['difference']),
            '단위':'KRW','일자':'청구 출고일 해당 없음','주문번호':'','입고예정번호':'','품번':'','송장번호':'',
            '청구원본':claim['path'],'청구위치':f'청구내역!L{n}:N{n}',
            '적용기준':f'{label} · 청구내역 {n}행; 청구 기재 수량×기재 단가',
            '사유':'단가 변경 비교용 청구 항목입니다. 정상·불일치 확인 목록과 별도이며 적용은 명시 확인 후 수행합니다.',
            '계산근거':f'full_invoice_result.json#/claim.line.{n}',
            '_catalog_kind':'invoice_rate','_invoice_row':n,'_rate_label':label,
            '_cost_selector':[str(item.get('category_context') or ''),str(item.get('item_context') or item.get('category_context') or ''),str(item.get('unit') or '')],
            '_rate_quantity':_str(q),'_rate_price':_str(rate),'_rate_unit':str(item.get('unit') or '')})
    return rows,fingerprint


def selected(root, record, *, formula=False):
    from chat_intake import _current,_target
    payload=record.get('payload',record)
    if payload.get('intent')!='upsert_rule' or payload.get('month')!='2029-07':
        raise ValueError('현재 7월 청구의 비교 기준 요청을 선택해 주세요.')
    targets=payload.get('targets')
    if not isinstance(targets,list) or not 1<=len(targets)<=100:
        raise ValueError('비교할 청구 항목 1~100개를 선택해 주세요.')
    current=_current(root);items,claim,_=inventory(root);by_row={i['row']:i for i in items};selected=[]
    seen=set()
    for target in targets:
        index=str(target.get('issue_id',''))
        if not index.isdecimal() or int(index)>=len(current['rows']):
            raise ValueError('청구 항목 선택이 변경되었습니다.')
        row=current['rows'][int(index)];actual=_target(row,int(index),current['version'])
        if {k:v for k,v in target.items() if k!='alias'}!=actual or row.get('_catalog_kind')!='invoice_rate':
            raise ValueError('단가 비교용 청구 항목만 함께 선택해 주세요.')
        n=row['_invoice_row']
        if n in seen:raise ValueError('같은 청구 항목이 중복 선택되었습니다.')
        seen.add(n);item=by_row[n]
        if n in (79,111,116) and not formula:
            raise ValueError(f'청구내역 {n}행은 요율·상계 항목으로 단가×수량 비교 대상이 아닙니다.')
        q=_decimal(item.get('quantity'));amount=_decimal(item.get('amount'))
        if (q is None or amount is None) and not formula:
            missing='과금수량(M열)' if q is None else '청구금액(N열)'
            raise ValueError(f'청구서 청구내역 {n}행의 {missing} 값이 없어 단가 금액을 계산할 수 없습니다.')
        if not str(item.get('unit') or '').strip() and not formula:
            raise ValueError(f'청구내역 {n}행의 수량·금액·단위를 별도 확인해야 합니다.')
        selected.append((target,item))
    return selected,claim


def options(root,record):
    try:
        rows,_=selected(root,record)
        sources={}
        if all(_decimal(i.get('unit_price')) is not None and _decimal(i['unit_price'])>=0 for _,i in rows):
            sources['claim']='청구서 기재 단가'
        if all(i['row'] in RATES for _,i in rows) and _safe(root,CONTRACT).is_file():
            if _hash(_safe(root,CONTRACT)).upper()==CONTRACT_HASH:
                sources['contract']='계약서 기재 단가 (당월 적용 확정과 별도)'
        return {'supported':True,'message':'청구서의 과금수량은 유지하고 비교 단가를 선택하거나 직접 입력하세요. 적용 전에 변경 금액을 확인합니다.',
            'choices':([{'kind':'rate_selection','label':'청구·계약 기재 단가로 금액 비교',
                'parameters':[{'name':'rate_source','label':'비교 단가 출처','type':'select','options':sources,
                    'value':next(iter(sources)),'required':True}],
                'description':'청구 기재 과금수량×선택 단가와 청구금액 비교', 'limitations':LIMITATIONS}] if sources else [])+[
                {'kind':'custom_rate','label':'담당자 지정 단가로 금액 비교',
                 'parameters':[{'name':'rate','label':'비교할 단가 (원, 소수 가능)','type':'text','required':True},
                               {'name':'unit','label':'청구서와 같은 과금 단위','type':'text','required':True,
                                'value':str(rows[0][1].get('unit') or '')}],
                 'description':'선택한 행의 단위를 확인하고 입력 단가×원본 과금수량을 계산합니다.',
                 'limitations':LIMITATIONS}]}
    except (ValueError,OSError,KeyError,TypeError) as exc:
        return {'supported':False,'message':str(exc),'choices':[]}


def read_claim(path, row_numbers):
    with open_workbook(str(path)) as book:
        found={n:cells for n,cells in scan_rows(book,'청구내역') if n in row_numbers}
    if set(found)!=set(row_numbers):raise ValueError('원본 청구서에서 선택한 행을 찾지 못했습니다.')
    return found


def calculate(root,record,parameters,custom=False):
    if custom:
        if type(parameters) is not dict or set(parameters)!={'rate','unit'} or type(parameters['rate']) is not str or _decimal(parameters['rate']) is None or abs(_decimal(parameters['rate']))>Decimal('1e15') or not isinstance(parameters['unit'],str) or not parameters['unit'].strip():
            raise ValueError('숫자 단가와 청구서의 과금 단위를 입력해 주세요.')
        custom_rate=_decimal(parameters['rate'])
        chosen_unit=parameters['unit'].strip()
        parameters={'rate_source':'custom'}
    elif type(parameters) is not dict or set(parameters)!={'rate_source'} or parameters['rate_source'] not in ('claim','contract'):
        raise ValueError('비교 단가는 청구서 또는 계약서 기재값만 선택할 수 있습니다.')
    rows,claim=selected(root,record)
    offered=options(root,record)
    if not offered['supported']:raise ValueError(offered['message'])
    source_choice=next((v for v in offered['choices'] if v['kind']=='rate_selection'),None)
    if not custom and (not source_choice or parameters['rate_source'] not in source_choice['parameters'][0]['options']):
        raise ValueError('선택 항목 모두에 대응하는 기재 단가 근거가 없습니다.')
    path=_safe(root,claim['path'])
    if not path.is_relative_to(_safe(root,BASE/'source')) or _hash(path).lower()!=claim['sha256'].lower():
        raise ValueError('청구 원본이 변경되었습니다. 전체 검증을 먼저 실행해 주세요.')
    raw=read_claim(path,{i['row'] for _,i in rows});changes=[]
    for target,item in rows:
        n=item['row'];cells=raw[n]
        q,rate,amount=(_decimal(cells.get(c,{}).get('value')) for c in ('M','L','N'))
        if (q,rate,amount)!=tuple(_decimal(item.get(k)) for k in ('quantity','unit_price','amount')):
            raise ValueError(f'청구내역 {n}행의 원본 숫자와 완료 결과가 다릅니다.')
        # Labels/unit are checked too: equal numbers do not authenticate a moved item.
        for col,value in item.get('raw_labels',{}).items():
            if cells.get(col,{}).get('value')!=value:
                raise ValueError(f'청구내역 {n}행의 항목명이 달라졌습니다.')
        if str(cells.get('J',{}).get('value') or '').strip()!=str(item.get('unit') or '').strip():
            raise ValueError(f'청구내역 {n}행의 과금 단위가 달라졌습니다.')
        if custom and chosen_unit!=str(item.get('unit') or '').strip():
            raise ValueError(f'청구내역 {n}행의 과금 단위는 {item.get("unit")}입니다. 다른 단위의 단가를 그대로 곱하지 않습니다.')
        before=_values(q,rate,amount)
        chosen=custom_rate if custom else rate if parameters['rate_source']=='claim' else Decimal(RATES[n])
        refs=[claim['path']+f' / 청구내역!L{n}:N{n}']
        basis=f'청구 과금수량 {q} ({item["unit"]}) × '+('담당자 지정' if custom else '청구서 기재' if parameters['rate_source']=='claim' else '계약서 기재')+f' 단가 {chosen}원; 청구금액 {amount}원'
        if parameters['rate_source']=='contract':refs.append(CONTRACT+'#page=6')
        changes.append({'target_id':target['target_id'],'check':target['check'],'label':target['label'],
            'before':before,'after':_values(q,chosen,amount),'basis':basis,
            'evidence_refs':refs})
    if _hash(path).lower()!=claim['sha256'].lower():raise ValueError('계산 중 원본 청구서가 변경되었습니다.')
    if parameters['rate_source']=='contract' and _hash(_safe(root,CONTRACT)).upper()!=CONTRACT_HASH:
        raise ValueError('계산 중 계약서가 변경되었습니다.')
    selected(root,record)
    return {'adapter_version':'rate-selection-v1','changes':changes,'limitations':LIMITATIONS,'calculation_executed':True}
