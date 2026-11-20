"""Parameterised original-document checks using existing Decimal and workbook tools.

Supports SF columns and unambiguous header-mapped invoice layouts.
All month-specific exceptions are explicit questions; July decisions are never read.
"""
from collections import Counter, defaultdict
from datetime import datetime, date
from decimal import Decimal, InvalidOperation
import csv
import io
import json
from pathlib import Path
import re
import zipfile
from lxml import etree as ET
from portable_workbook import open_workbook
from invoice_tools import check, number
from wms_tools import xlsx_rows, xlsb_rows, day, NS
from monthly_cases import verify_sources, identity
from identifier_tools import expand_identifier, stored_number

FIELDS=['조회구분','검사','결과','사유구분','사유','일자','주문번호','입고예정번호','품번','송장번호',
        '청구원본','청구위치','WMS위치','계약위치','청구값','비교값','차이','단위','적용기준','계산근거']
LABELS={'match':'정상','difference':'불일치','data_unavailable':'미검증','not_implemented':'미검증'}


def text(value):
    return expand_identifier(value)


def numeric(value):
    return stored_number(value)


def names(path):
    if path.suffix.lower()=='.xlsb':
        with open_workbook(str(path)) as b:return list(b.sheets)
    with zipfile.ZipFile(path) as z:
        return [s.attrib['name'] for s in ET.fromstring(z.read('xl/workbook.xml')).find(NS+'sheets')]


def rows(path,sheet):
    if path.suffix.lower()=='.xlsb':
        with open_workbook(str(path)) as b:
            with b.get_sheet(sheet) as s:yield from xlsb_rows(s)
    else:yield from xlsx_rows(path,sheet)


def raw(c,col):
    cell=c.get(col,{})
    return None if cell.get('error') else cell.get('value')


def date_value(v,case):
    if v is None:raise ValueError('일자 값 없음')
    try:return day({'value':v})
    except ValueError:pass
    s=str(v).strip()
    try:return datetime.fromisoformat(s).date().isoformat()
    except ValueError:pass
    if case['rules']['yearless_date']=='period':
        for fmt in ('%d-%b','%m/%d','%m-%d'):
            try:
                part=datetime.strptime(s,fmt)
                options=[]
                for yr in {int(case['period']['start'][:4]),int(case['period']['end'][:4])}:
                    d=date(yr,part.month,part.day).isoformat()
                    if case['period']['start']<=d<=case['period']['end']:options.append(d)
                if len(options)==1:return options[0]
            except ValueError:continue
    raise ValueError('일자 해석 불가: '+s)


def tabular(path, required):
    from document_layouts import table_header
    if path.suffix.lower()=='.csv':
        with path.open(encoding='utf-8-sig',newline='') as f:
            reader=csv.DictReader(f)
            if len(set(reader.fieldnames))!=len(reader.fieldnames):raise ValueError('중복된 열 제목')
            mapping=table_header({h:h for h in reader.fieldnames or []},required)
            if mapping is None:raise ValueError('CSV 필수 열을 식별하지 못함: '+','.join(sorted(required)))
            for n,row in enumerate(reader,2):
                if None in row or None in row.values():raise ValueError('CSV 열 개수 오류')
                yield 'CSV',n,{h:row[col] for h,col in mapping.items()}
    else:
        found=[]
        for sheet in names(path):
            for n,cells in rows(path,sheet):
                if n>50:break
                mapping=table_header({col:c.get('value') for col,c in cells.items()},required)
                if mapping is not None:
                    found.append((sheet,n,mapping));break
        if len(found)!=1:raise ValueError('필수 헤더가 있는 WMS 시트를 하나로 식별할 수 없음')
        sheet,start,mapping=found[0]
        for n,c in rows(path,sheet):
            if n>start:yield sheet,n,{h:raw(c,col) for h,col in mapping.items()}


def run(root,case,answers=None,recurring_state=None):
    from monthly_multi import run_all
    return run_all(root,case,answers,recurring_state,_run_single)


def _run_single(root,case,answers=None,recurring_state=None,*,claim=None,table_reader=None,claim_sheets=None):
    tabular=table_reader or globals()['tabular']
    root=Path(root);verify_sources(root,case)
    from recurring_rules import snapshot as recurring_snapshot,applicable
    recurring_state=recurring_snapshot(root) if recurring_state is None else recurring_state
    quantity_enabled=bool(applicable(case,recurring_state,'recurring_quantity') or applicable(case,recurring_state,'formula'))
    quantity_context={'rows':{r:defaultdict(list) for r in ('inbound','outbound')},
                      'invalid':{r:[] for r in ('inbound','outbound')},'failed':{r:[] for r in ('inbound','outbound')}}
    checks=[];coverage=[];profiles={};answers=answers or {}
    claim=claim or next(m for m in case['materials'] if m['role']=='claim')
    claimpath=root/claim['path'];sheet_names=names(claimpath)
    wanted={'청구내역','입고리스트','B2B출고','B2C출고_택배','임가공-라벨','B2C반품',
            '입고 외 기타','B2B 기타','B2B 반품','B2C출고','B2C출고_용차','차감항목','반품리스트','청구누락'}
    from document_layouts import discover, restore_result
    sheets,layout_mapping,layout_observations=discover(claimpath,sheet_names,rows,wanted)
    from workbook_observations import AmountFormulas,macro_observation
    amount_formulas=AmountFormulas(claimpath,sheet_names,rows)
    formulas={key:value for key,value in amount_formulas.formulas.items()
              if key[0] in sheets and key[0] not in layout_mapping}
    macro=macro_observation(claimpath)
    if macro:layout_observations.append(macro)
    def emit(group,status,reason='',category='',**values):
        item={k:'' for k in FIELDS}
        item.update(검사=group,결과=LABELS[status],사유구분=category or ('기능 미구현' if status=='not_implemented' else '자료 부족' if status=='data_unavailable' else '차이' if status=='difference' else ''),
                    사유=reason,청구원본=claim['path'],적용기준='해당 월 등록 기준 · 기존 7월 예외 미승계',**values)
        item.update(조회구분=group+' / '+LABELS[status],status=status,id=str(len(checks)+1))
        checks.append(item);return item
    def compare(group,claimed,expected,refs,reason,**values):
        r=check('monthly.'+str(len(checks)),group,expected,claimed,refs)
        return emit(group,r['status'],reason if r['check_performed'] else '청구서 또는 WMS에 숫자·연결 근거가 없음: '+reason,
                    청구값=text(claimed),비교값=text(expected),차이=text(r['difference']),계산근거='; '.join(refs),**values)
    start,end=case['period']['start'],case['period']['end']
    if macro:emit('매크로 실행','not_implemented',macro['reason'])
    # Parse known invoice headers before adopting their columns. Fail closed per section.
    expected={'입고리스트':{'A2':'입고일자','B2':'입고예정번호','D2':'품번','H2':'납품수량'},
              'B2B출고':{'A2':'ERP','D2':'출고일','G2':'수량'}}
    supported={}
    for sheet,headers in expected.items():
        supported[sheet]=sheet in sheets and all(text(raw(sheets[sheet].get(int(ref.lstrip('ABCDEFGHIJKLMNOPQRSTUVWXYZ')),{}),ref.rstrip('0123456789')))==v for ref,v in headers.items())
        if not supported[sheet]:emit(sheet+' 양식','not_implemented','필수 시트·열 제목이 기존 SF 양식과 다릅니다. 새 양식 연결 필요.',청구위치=sheet)
    summary=sheets.get('청구내역',{})
    # Existing SF summary uses unit J / rate L / quantity M / amount N.
    summary_headers=' '.join(text(raw(c,k)) for n,c in summary.items() if n<20 for k in ('J','L','M','N'))
    summary_valid=all(any(word in summary_headers for word in synonyms) for synonyms in (('단가',),('수량',),('금액','청구액')))
    fee_items=[];category='';label=''
    if summary_valid:
        for n,c in sorted(summary.items()):
            if n<20 or not any(k in c for k in ('J','L','M','N')):continue
            # Summary totals are not another cost item. Totals often live in
            # M (not C), and must never inherit the preceding item's name.
            total_names={'청구금액','차감비용','합계','부가세','총계','VAT(부가세)','최종금액(VAT포함)'}
            if any(re.sub(r'\s+','',text(raw(c,k))) in total_names for k in ('C','G','M')):continue
            if text(raw(c,'C')):
                category=text(raw(c,'C'))
                if not text(raw(c,'G')):label=''
            if text(raw(c,'G')):label=text(raw(c,'G'))
            if any(t in text(raw(c,'C')) for t in ('합계','합 계','부가세','총계')):continue
            q,rate,amount=[numeric(raw(c,k)) for k in ('M','L','N')]
            if amount is None or amount == 0:continue
            if not any(raw(c,k) is not None for k in ('M','L','N')):continue
            name=(category+' '+label).strip()
            ref=f'청구내역!L{n}:N{n}'
            expected_amount=q*rate if q is not None and rate is not None else None
            refs=[ref];reason=name+' · 청구서 수량×단가와 청구금액 비교'
            if '핸들링' in name and ('청구내역','N'+str(n)) in formulas:
                from monthly_formula_tools import evaluate,UnsupportedFormula,MissingValue
                node=formulas['청구내역','N'+str(n)]
                try:
                    if isinstance(node,str):raise UnsupportedFormula(node)
                    observed=evaluate(node,sheets,formulas,origin=('청구내역','N'+str(n)))
                    expected_amount=observed['value'];refs+=observed['refs'];reason=name+' · 청구서 수식의 기초금액×요율 재계산'
                except (UnsupportedFormula,MissingValue) as exc:reason+=' / '+str(exc)
            compare('청구·상세 금액 계산',amount,expected_amount,refs,reason,청구위치=ref,단위='KRW')
            physical=layout_mapping.get('청구내역',{})
            real_sheet=physical.get('sheet','청구내역');real_row=n-physical.get('offset',0)
            real_cell=physical.get('columns',{}).get('N','N')+str(real_row)
            observation=amount_formulas.compare(real_sheet,real_cell)
            if observation:
                if observation['status']=='calculated':
                    checkrow=compare('청구금액 저장 수식',amount,observation['value'],observation['refs'],
                        '원본 수식과 참조 셀로 저장된 청구금액 재계산; 실제 작업 발생 확인과 구분',청구위치=ref,단위='KRW')
                else:
                    checkrow=emit('청구금액 저장 수식',observation['status'],observation['reason'],청구위치=ref,청구값=text(amount),단위='KRW')
                checkrow['_physical_formula_refs']=True
            fee_items.append({'row':n,'label':name,'category':category,'item':label or category,'rate':rate,'quantity':q,'amount':amount,'unit':text(raw(c,'J'))})
    else:emit('청구 요약 양식','not_implemented','청구내역의 단가·수량·금액 헤더를 식별하지 못했습니다.',청구위치='청구내역!J:N')
    # WMS normalisation keeps every registered file, including previous-month rows.
    wms={'inbound':defaultdict(list),'outbound':defaultdict(list)};invalid=Counter();seen_signatures=set()
    for material in case['materials']:
        role=material['role']
        if role not in wms:continue
        inbound=role=='inbound';date_col='입고일' if inbound else '출고일';key_col='입고예정번호' if inbound else '주문번호(연동)'
        scope_col='입고창고' if inbound else '출고창고'
        required={date_col,key_col,'품번','수량','단위','회사구분',scope_col}
        scanned=0;kept=0;dates=Counter();ignored=Counter()
        try:
            for sheet,n,r in tabular(root/material['path'],required):
                if not any(v is not None and str(v).strip() for v in r.values()):continue
                scanned+=1;ref=f"{material['path']} / {sheet}!{n}"
                try:d=date_value(r.get(date_col),case)
                except ValueError as exc:
                    if quantity_enabled:quantity_context['failed'][role].append(ref)
                    emit('WMS 일자·단위 확인','data_unavailable',str(exc),WMS위치=ref);continue
                if not start<=d<=end:ignored['기간 밖']+=1;continue
                if text(r['회사구분'])!=case['rules']['company'] or text(r[scope_col])!=case['rules']['warehouse']:
                    ignored['다른 회사·창고']+=1;continue
                kind=text(r.get('구분'))
                if inbound and kind in case['rules'].get('inbound_exclusions',[]):ignored['사용자 지정 입고 유형 제외']+=1;continue
                consumable_excluded=case['rules']['consumables']=='exclude' and '(소모품)' in text(r.get('품명'))
                if consumable_excluded and not quantity_enabled:ignored['명시 소모품 제외']+=1;continue
                unit=text(r['단위']) or ('EA' if case['rules']['blank_unit']=='EA' else '')
                qty=numeric(r['수량']);key=text(r[key_col]);sku=text(r['품번'])
                if qty is None or unit!='EA' or not key or not sku:
                    if quantity_enabled:quantity_context['invalid'][role].append({'date':d,'order':key,'sku':sku,'ref':ref})
                    if consumable_excluded:ignored['명시 소모품 제외']+=1;continue
                    emit('WMS 일자·단위 확인','data_unavailable','WMS 수량·EA 단위·연결번호·품번 중 빈칸 또는 미지원 값이 있음',일자=d,WMS위치=ref);continue
                if re.fullmatch(r'[\d.]+[eE][+-]?\d+',key):
                    if quantity_enabled:quantity_context['invalid'][role].append({'date':d,'order':'','sku':sku,'ref':ref})
                    if consumable_excluded:ignored['명시 소모품 제외']+=1;continue
                    emit('WMS 번호 확인','data_unavailable','지수 표기 번호는 원형 식별 근거 없이 연결하지 않음',일자=d,주문번호=key,WMS위치=ref);continue
                k=(d,key,sku) if inbound else (d,key)
                quantity_row={'quantity':qty,'ref':ref,'sku':sku,'type':kind,'tracking':text(r.get('송장번호')),'outbound':text(r.get('출고번호')),
                              'name':str(r.get('품명') or ''),'carrier':text(r.get('택배사')),'raw_order':r[key_col],'raw_tracking':r.get('송장번호')}
                if quantity_enabled:quantity_context['rows'][role][k].append(quantity_row)
                if consumable_excluded:ignored['명시 소모품 제외']+=1;continue
                signature=(role,d,key,sku,text(r.get('출고번호')),text(r.get('제조LOT')),text(r.get('유통기한')),str(qty),kind)
                # A repeated row is retained, not silently removed. Ask about overlap.
                if signature in seen_signatures:
                    emit('WMS 반복 내역','data_unavailable','같은 일자·번호·품번·수량·LOT 등이 반복됩니다. 분할 작업인지 중복 파일인지 미확정; 현재는 두 행 모두 합산.',일자=d,주문번호=key,품번=sku,WMS위치=ref)
                seen_signatures.add(signature)
                wms[role][k].append(quantity_row)
                dates[d]+=1;kept+=1
        except (ValueError,UnicodeError) as exc:
            if quantity_enabled:quantity_context['failed'][role].append(material['path'])
            emit('WMS 양식','not_implemented',str(exc),WMS위치=material['path'])
        profiles[material['path']]={'scanned':scanned,'included':kept,'dates':dict(dates),'excluded':dict(ignored)}
    used={'inbound':set(),'outbound':set()};parcel_used=set();claim_groups={'inbound':{},'outbound':{}}
    for role,sheet,datecol,keycol,qtycol in [('inbound','입고리스트','A','B','H'),('outbound','B2B출고','D','A','G')]:
        if not supported[sheet]:continue
        groups={}
        for n,c in sheets[sheet].items():
            if n<4 or not raw(c,keycol):continue
            ref=f'{sheet}!{n}';order=text(raw(c,keycol));sku=text(raw(c,'D')) if role=='inbound' else ''
            try:d=date_value(raw(c,datecol),case)
            except ValueError as exc:
                emit('청구 일자 확인','data_unavailable',str(exc),청구위치=ref,주문번호=order);continue
            if not start<=d<=end:
                emit('청구기간 밖 내역','data_unavailable','청구서 일자가 사용자가 지정한 비교기간 밖입니다. 기간 조정 또는 이월 여부 확인 필요.',일자=d,청구위치=ref,주문번호=order);continue
            key=(d,order,sku) if role=='inbound' else (d,order)
            g=groups.setdefault(key,{'qty':Decimal(0),'missing':False,'refs':[],'counterparties':[]})
            if role=='outbound':
                # This selector is a claim field, never inferred from WMS parties.
                header=text(raw(sheets[sheet].get(2,{}),'C'))
                g['counterparties'].append(text(raw(c,'C')) if header=='채널명' else '')
            qty=numeric(raw(c,qtycol));g['refs'].append(ref)
            if qty is None:g['missing']=True
            else:g['qty']+=qty
        claim_groups[role]=groups
        for key,g in groups.items():
            other=wms[role].get(key,[])
            if other:used[role].add(key)
            a=None if g['missing'] else g['qty'];b=sum((r['quantity'] for r in other),Decimal(0)) if other else None
            compare('입고 상품 수량' if role=='inbound' else 'B2B 상품 수량',a,b,g['refs'],'동일 일자·번호'+('·품번' if role=='inbound' else '')+' 전체 수량 비교',일자=key[0],
                    주문번호=key[1] if role=='outbound' else '',입고예정번호=key[1] if role=='inbound' else '',품번=key[2] if role=='inbound' else '전체(주문 합계)',
                    청구위치='; '.join(g['refs']),WMS위치='; '.join(r['ref'] for r in other),단위='EA')
    # Actual invoice parcel detail: each charge row retained even for repeated orders.
    parcel=sheets.get('B2C출고_택배',{})
    parcel_header=next((c for n,c in parcel.items() if n<=2 and text(raw(c,'A'))=='주문번호'),{})
    tracking_col=next((col for col,c in parcel_header.items() if text(c.get('value'))=='송장번호'),None)
    if not tracking_col:
        emit('B2C 청구 양식','not_implemented','실제 청구 택배 상세의 주문번호·송장번호 헤더를 확인할 수 없음',청구위치='B2C출고_택배!A:E')
    else:
        by_order=defaultdict(list)
        for key,rr in wms['outbound'].items():by_order[key[1]].append(key)
        for n,c in parcel.items():
            order=text(raw(c,'A'))
            if n<3 or not order:continue
            if numeric(raw(c,'D')) in (None,0):continue
            found=by_order.get(order,[])
            parcel_used.update(found)
            emit('B2C 주문 연결','match' if found else 'data_unavailable','주문번호 원형 연결. 상품 수량·운임 적정성 확인과 구분.' if found else '청구 주문번호와 같은 WMS 주문번호를 비교기간에서 찾지 못함',
                 주문번호=order,송장번호=text(raw(c,tracking_col)),청구위치=f'B2C출고_택배!A{n}:E{n}',
                 WMS위치='; '.join(r['ref'] for k in found for r in wms['outbound'][k]),단위='청구 줄')
        if any('포장' in i['label'] for i in fee_items):
            emit('택배 규격 적정성','data_unavailable','원본 WMS에 택배 실측 중량·규격 근거가 없습니다. 포장비 잠정 계산·기재 금액 산술과 구분합니다.',청구위치='B2C출고_택배 / 청구내역')
    for role,groups in wms.items():
        for key,rr in groups.items():
            if key in used[role] or (role=='outbound' and key in parcel_used):continue
            emit('WMS에만 있는 실적','data_unavailable','입고·B2B·B2C 청구에서 대응 내역을 찾지 못함. 다른 비용 시트·제외 대상 가능성이 있어 누락 청구로 확정하지 않음.',
                 일자=key[0],입고예정번호=key[1] if role=='inbound' else '',주문번호=key[1] if role=='outbound' else '',품번=key[2] if role=='inbound' else '',
                 비교값=text(sum((r['quantity'] for r in rr),Decimal(0))),WMS위치='; '.join(r['ref'] for r in rr),단위='EA')
    rates=[m for m in case['materials'] if m['role']=='rates']
    if rates:
        rate_map=defaultdict(list);failed=False
        for material in rates:
            try:
                for sh,n,r in tabular(root/material['path'],{'청구항목','단가','단위'}):
                    rate_map[(text(r['청구항목']),text(r['단위']))].append((numeric(r['단가']),f"{material['path']} / {sh}!{n}"))
            except (ValueError,UnicodeError) as exc:
                failed=True;emit('단가표 양식','not_implemented',str(exc),WMS위치=material['path'])
        for item in fee_items:
            matches=rate_map.get((item['label'],item['unit']),[])
            choices={m[0] for m in matches}
            expected_rate=next(iter(choices)) if len(choices)==1 and not failed else None
            reason=item['label']+' · 모든 등록 단가표의 항목명·단위 비교'
            if len(choices)>1:reason+=' · 단가 충돌: '+', '.join(sorted(text(v) or '숫자 없음' for v in choices))
            if failed:reason+=' · 읽지 못한 단가표가 있어 적용값 확정 불가'
            compare('등록 단가표 비교',item['rate'],expected_rate,[m[1] for m in matches],reason,청구위치=f"청구내역!L{item['row']}",단위='KRW/기재단위')
    elif not any(m['role']=='contract' for m in case['materials']):
        emit('계약 적용·실비 적정성','data_unavailable','해당 월에 적용할 항목별 단가표·작업/실비 근거가 등록되지 않음. 계약 PDF 등록만으로 적용 단가를 확정하지 않습니다.')
    from monthly_contracts import inspect_contracts
    contract_documents=inspect_contracts(root,case,fee_items,emit,tabular)
    from monthly_cost_tools import augment
    cost_evidence=augment(root,case,sheets,fee_items,wms,compare,emit,tabular,claim_sheets=claim_sheets,formulas=formulas)
    from recurring_rate_rules import apply as apply_recurring
    recurring_evidence=apply_recurring(root,case,fee_items,recurring_state,emit)
    from recurring_quantity_rules import apply as apply_quantity
    recurring_evidence+=apply_quantity(root,case,claim_groups,quantity_context,recurring_state,emit)
    result={'case':case,'identity':identity(case),'checks':checks,'profiles':profiles,'fee_items':fee_items,'cost_evidence':cost_evidence,
            '_formula_input':{'groups':claim_groups,'wms':quantity_context,'claim_path':claim['path']},
            'contract_documents':contract_documents,
            'recurring_rules':recurring_evidence,'recurring_snapshot_hash':recurring_state['fingerprint'],
            'full_invoice_validated':False,'llm_used':False,
            'layout_observations':[dict(v,path=claim['path']) for v in layout_observations]}
    if layout_mapping:
        result=restore_result(result,layout_mapping)
    return result


def finish(result,answers=None):
    case=result['case'];checks=result['checks'];answers=answers or {}
    # Questions bind the whole case revision, including every registered file.
    pending=[]
    for group in sorted({r['검사'] for r in checks if r['status']=='data_unavailable'}):
        group_rows=[r for r in checks if r['검사']==group and r['status']=='data_unavailable']
        import hashlib
        qid='M'+hashlib.sha256((identity(case)+group).encode()).hexdigest()[:10]
        reply=answers.get(qid,'')
        if reply in ('확정할 수 없음 — 해당 판정만 제외','확정할 수 없음','확정 불가'):
            for r in group_rows:r['decision']='사용자 확정불가 · 해당 판정 제외';r['사유']+=' / 사용자 확정불가: 해당 판정만 제외'
        elif case['rules'].get('unresolved_policy')=='record':
            for r in group_rows:r['decision']='미확정 유지 · 가능한 검사 계속'
        else:
            pending.append({'id':qid,'text':f"{group}: {len(group_rows):,}개 대상의 기준·근거가 미확정입니다. 파일/기간을 수정하거나 해당 판정을 확정할 수 없는지 답해 주세요.",
                            'evidence':group_rows[0]['사유'],'group':group,'count':len(group_rows)})
    result.update(questions=pending,waiting_for_input=bool(pending),processing_status='awaiting_user_decision' if pending else 'completed_with_limitations',
                  implementation_limits=sorted({r['검사'] for r in checks if r['status']=='not_implemented'}))
    return result


def render(result):
    checks=result['checks'];case=result['case'];summary=[]
    lines=[f"# DEMO COMPANY 가상센터 · {case['month']} 청구 검증",'',f"비교기간: {case['period']['start']} ~ {case['period']['end']}",'',
           '**전체 청구금액 적정성은 미확정입니다.** 기존 7월 잠정 결정은 적용하지 않았습니다.','',
           ('**사용자 답변 대기:** 가능한 비교 결과이며 최종 완료가 아닙니다.' if result['waiting_for_input'] else '가능한 검사를 마쳤습니다. 판단 제외·기능 미구현은 정상 검증과 구분합니다.'),'',
           '### 검사별 요약','','| 확인 항목 | 세는 단위 | 정상 | 불일치 | 미검증 |','|---|---|---:|---:|---:|']
    for group in dict.fromkeys(r['검사'] for r in checks):
        subset=[r for r in checks if r['검사']==group];counts=Counter(r['결과'] for r in subset)
        summary.append({'group':group,**{s:counts[s] for s in ('정상','불일치','미검증')}})
        lines.append(f"| {group} | 比較 대상 1개 | {counts['정상']:,} | {counts['불일치']:,} | {counts['미검증']:,} |".replace('比較','비교'))
    lines+=['','### 확인할 문제','','| 구분 | 대상 수 | 사유·대표 원본 |','|---|---:|---|']
    for group in dict.fromkeys(r['검사'] for r in checks if r['결과']!='정상'):
        rr=[r for r in checks if r['검사']==group and r['결과']!='정상'];first=rr[0]
        detail=(first['사유구분']+' · '+first['사유']+' / '+(first['청구위치'] or first['WMS위치'] or first.get('계약위치',''))).replace('|','/').replace('\n',' ')
        lines.append(f"| {group} | {len(rr):,} | {detail} |")
    lines+=['','전체 대상과 원본 위치는 **확인목록.csv**에서 검증 항목으로 필터하세요. 같은 청구에 여러 비교가 있어 건수를 주문 수로 합산하지 않습니다.','']
    buf=io.StringIO(newline='');writer=csv.DictWriter(buf,fieldnames=FIELDS,extrasaction='ignore');writer.writeheader()
    for r in checks:
        if r['결과']!='정상':writer.writerow(r)
    result['summary']=summary
    return '\n'.join(lines),buf.getvalue()
