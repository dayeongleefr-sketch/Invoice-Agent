"""Read-only selected B2C link scenarios. A link never verifies freight or EA."""
from collections import defaultdict
from datetime import date
from functools import lru_cache
from pathlib import Path
import csv
import json
import re

import chat_intake
from identifier_tools import expand_identifier, scientific_info, stored_number, billable_amount
from invoice_tools import digest
from actual_claim_wms_b2c import normalize_date, B2C_TYPES
from portable_workbook import open_workbook
from wms_tools import xlsx_rows
from xlsb_scan import scan_rows

VERSION = 'b2c-link-v1'
BASE = Path('data/demo_company')
CONFIG = Path('ia/config/demo_company_direct_wms.json')
RESULT = BASE/'derived/match_investigation.json'
JOINS = {'order': '주문번호', 'tracking': '송장번호', 'order_tracking': '주문번호 + 송장번호'}
LIMITATIONS = ['연결 결과만 기록합니다. 상품 수량·발송 횟수·운임의 검증 완료를 뜻하지 않습니다.',
              '이번 7월 선택 청구만 비교합니다. 기존 판정·금액은 유지하며 다른 월에 자동 적용하지 않습니다.',
              '송장이 여러 발송에 겹치거나 번호가 축약되어 식별할 수 없으면 연결을 확정하지 않습니다.']


def validate(p):
    if type(p) is not dict or set(p) != {'start','end','join'} or any(type(v) is not str for v in p.values()):
        raise ValueError('WMS 출고기간과 연결 기준을 지정해 주세요.')
    try: start,end=date.fromisoformat(p['start']),date.fromisoformat(p['end'])
    except ValueError as exc: raise ValueError('기간은 YYYY-MM-DD로 입력해 주세요.') from exc
    if (start.isoformat(),end.isoformat())!=(p['start'],p['end']) or not date(2029,6,26)<=start<=end<=date(2029,7,31) or p['join'] not in JOINS:
        raise ValueError('등록된 2029-06-26~2029-07-31 범위와 지원 연결 기준을 선택해 주세요.')
    return p


def _json(root,rel):
    return json.loads(chat_intake._safe_file(root,rel).read_text('utf-8-sig'))


def targets(root,record):
    payload=record.get('payload',record)
    if any(payload.get(k)!=v for k,v in {'intent':'upsert_rule','month':'2029-07','scope':'once','application':'current_case'}.items()):
        raise ValueError('B2C 연결 기준은 현재 7월의 이번 청구만 지원합니다.')
    ts=payload.get('targets')
    if not isinstance(ts,list) or not 1<=len(ts)<=100:
        raise ValueError('확인 목록의 B2C 주문 연결 1~100개를 선택해 주세요.')
    current=chat_intake._current(root); doc=_json(root,RESULT)
    expected=_json(root,BASE/'derived/workflow_result.json').get('result_hashes',{}).get(RESULT.name)
    if not expected or digest(root/RESULT).lower()!=expected.lower():
        raise ValueError('저장된 원본 비교 결과의 버전이 다릅니다.')
    result=[];seen=set()
    for t in ts:
        i=str(t.get('issue_id',''))
        if not i.isdecimal() or int(i)>=len(current['rows']):raise ValueError('선택 목록이 바뀌었습니다.')
        row=current['rows'][int(i)]
        if {k:v for k,v in t.items() if k!='alias'}!=chat_intake._target(row,int(i),current['version']):
            raise ValueError('선택한 청구의 자료·결과가 바뀌었습니다.')
        ref=re.fullmatch(r'match_investigation\.json#/claim_candidates/(\d+)',row.get('계산근거',''))
        if t.get('check')!='B2C 주문 연결' or t.get('unit')!='기재 운임 원' or not ref:
            raise ValueError('B2C 주문 연결 내역만 선택해 주세요.')
        n=int(ref[1]); rows=doc.get('claim_candidates',[])
        if n>=len(rows) or n in seen:raise ValueError('잘못되거나 중복된 연결 근거입니다.')
        c=rows[n];seen.add(n)
        if c.get('kind')!='b2c' or c.get('sheet')!='B2C출고_택배' or not billable_amount(c.get('invoice_stated_freight')):
            raise ValueError('금액 있는 실제 B2C 택배 청구가 아닙니다.')
        if (t.get('order_id')!=c.get('order') or t.get('tracking_id')!=c.get('tracking','')
                or t.get('source')!=Path(doc['sources']['claim']['path']).name
                or t.get('locator')!=f"B2C출고_택배!A{c['row']}:D{c['row']}"):
            raise ValueError('선택한 청구 위치·주문·송장과 원본 비교 근거가 다릅니다.')
        if stored_number(t.get('claim_value'))!=stored_number(c['invoice_stated_freight']) or any(t.get(k) not in ('',None) for k in ('comparison_value','difference')):
            raise ValueError('원래 운임 표시값이 저장 결과와 다릅니다.')
        result.append((t,c))
    return result,doc


def options(root,record):
    try:
        _,doc=targets(root,record)
        return {'supported':True,'message':'WMS에서 다시 찾을 기간과 연결 기준을 선택해 주세요.',
                'choices':[{'kind':'b2c_link','label':'B2C 주문·송장으로 원본 다시 찾기',
                'description':'청구서 운임은 유지하고 원본 WMS의 주문·송장 연결만 다시 확인합니다.',
                'parameters':[{'name':'start','label':'WMS 출고 시작일 (YYYY-MM-DD)','type':'text','value':doc['period'][0],'required':True},
                              {'name':'end','label':'WMS 출고 종료일 (YYYY-MM-DD)','type':'text','value':doc['period'][1],'required':True},
                              {'name':'join','label':'연결 기준','type':'select','options':JOINS,'value':None,'required':True}],
                'limitations':LIMITATIONS}]}
    except (ValueError,OSError,KeyError,TypeError) as exc:
        return {'supported':False,'choices':[],'message':str(exc)}


def exact(value):
    info=scientific_info(value)
    if info and info['last_stored_digit_scale']>0:return ''
    return expand_identifier(value)


def read_claim(path):
    rows=[];header=False
    with open_workbook(str(path)) as book:
        for n,c in scan_rows(book,'B2C출고_택배'):
            get=lambda col:c.get(col,{}).get('value')
            if n==2:
                header=all(get(k)==v for k,v in {'A':'주문번호','B':'운송 방법','C':'송장번호','D':'운임비'}.items())
                if not header:raise ValueError('B2C 청구 열 구성이 변경되었습니다.')
            if n<3 or not billable_amount(get('D')):continue
            rows.append({'row':n,'order':expand_identifier(get('A')),'tracking':expand_identifier(get('C')),
                         'order_exact':exact(get('A')),'tracking_exact':exact(get('C')),
                         'freight':str(stored_number(get('D')))})
    if not header:raise ValueError('B2C 청구 제목 행을 찾지 못했습니다.')
    return rows


def read_wms(paths,include_june):
    result=[]
    def add(role,n,raw_date,order,tracking,shipment,carrier):
        d=normalize_date(raw_date)
        if not '2029-06-26'<=d<='2029-07-31':return
        if role=='outbound_h1' and d>='2029-07-01':return
        result.append(dict(role=role,row=n,date=d,order=exact(order),tracking=exact(tracking),
                           shipment=exact(shipment),carrier=expand_identifier(carrier)))
    with paths['outbound_july'].open(encoding='utf-8-sig',newline='') as stream:
        reader=csv.DictReader(stream)
        required={'회사구분','출고창고','구분','출고일','주문번호(연동)','송장번호','출고번호','택배사'}
        if not required.issubset(reader.fieldnames or []):raise ValueError('WMS의 주문·송장 열이 없습니다.')
        for n,r in enumerate(reader,2):
            if (r['회사구분'].strip(),r['출고창고'].strip())!=('90','DM001') or r['구분'].strip() not in B2C_TYPES:continue
            add('outbound_july',n,r['출고일'],r['주문번호(연동)'],r['송장번호'],r['출고번호'],r['택배사'])
    if include_june:
        header=False
        for n,c in xlsx_rows(str(paths['outbound_h1']),'출고 Data(04월~06월)',{'A','B','C','I','J','AB','AC','AQ'}):
            get=lambda col:c.get(col,{}).get('value')
            if n==1:
                header=all(get(k)==v for k,v in {'B':'출고일','C':'출고번호','J':'주문번호(연동)','AC':'송장번호'}.items())
                if not header:raise ValueError('6월 WMS 제목 열이 변경되었습니다.')
                continue
            if str(get('I') or '').strip() not in B2C_TYPES|{'B2C'}:continue
            if expand_identifier(get('A')) not in ('','90') or expand_identifier(get('AQ')) not in ('','DM001'):continue
            add('outbound_h1',n,get('B'),get('J'),get('AC'),get('C'),get('AB'))
        if not header:raise ValueError('6월 WMS의 제목 행을 찾지 못했습니다.')
    return result


def compare(selected,claims,wms,p,sources,notes_allowed=False):
    by_row={r['row']:r for r in claims};index=defaultdict(list)
    for r in wms:
        if p['start']<=r['date']<=p['end']:
            for mode,key in [('order',(r['order'],)),('tracking',(r['tracking'],)),('order_tracking',(r['order'],r['tracking']))]:
                if all(key):index[mode,key].append(r)
    changes=[]
    for t,c in selected:
        r=by_row.get(c['row'])
        if not r or r['order']!=c['order'] or r['tracking']!=c.get('tracking','') or stored_number(r['freight'])!=stored_number(t['claim_value']):
            raise ValueError('원본 청구 행과 저장된 주문·송장·운임이 다릅니다.')
        order=r['order_exact'];notes=False
        if notes_allowed and 3161<=r['row']<=3167:
            m=re.fullmatch(r'(.+?)\s+(?:추가\s+(?:\d+\s*박스|송장)|\(.*\)|불량\s*재출고|\d+\s*박스)',order)
            if m:order=exact(m[1]);notes=True
        key=(order,) if p['join']=='order' else (r['tracking_exact'],) if p['join']=='tracking' else (order,r['tracking_exact'])
        candidates=index.get((p['join'],key),[]) if all(key) else []
        status='not_determinable';reason='선택한 기간·연결 기준에 대응하는 원본 WMS가 없습니다.'
        if not all(key):reason='청구 번호가 없거나 축약되어 선택한 연결 기준을 확인할 수 없습니다.'
        elif candidates:
            if p['join']=='order':
                status='order_linked';reason='같은 주문번호의 WMS 출고가 있습니다. 개별 송장·발송 횟수·운임 검증과는 구분합니다.'
            else:
                groups={(x['date'],x['shipment'] or x['order'],x['carrier']) for x in candidates}
                peers=[x for x in claims if x['tracking_exact']==r['tracking_exact']]
                if len(groups)!=1 or any(not (x['shipment'] or x['order']) for x in candidates):
                    reason='같은 송장에 여러 출고일·출고번호·택배사 후보가 있거나 식별값이 없습니다.'
                elif len(peers)!=1:
                    reason='같은 송장번호가 여러 청구 줄에 있습니다. 하나의 발송으로 청구 줄별 연결을 확정하지 않습니다.'
                else:
                    status='shipment_linked';reason='같은 송장으로 유일한 WMS 발송이 확인됩니다. 운임·상품 수량은 별도 검사입니다.'
        before={'claim':str(stored_number(t['claim_value'])),'comparison':None,'difference':None,'unit':t['unit'],
                'status':'order_linked' if t.get('status')=='정상' else 'not_determinable'}
        refs=[sources['claim']['path']+' / B2C출고_택배!A'+str(r['row'])+':D'+str(r['row'])]
        refs += [sources[x['role']]['path']+' / '+('CSV' if x['role']=='outbound_july' else '출고 Data(04월~06월)')+'!'+str(x['row']) for x in candidates]
        # Explicitly bound output: an oversized candidate list is not a partial pass.
        if len(refs)>9000:raise ValueError('연결 후보가 너무 많습니다. 기간이나 연결 기준을 좁혀 주세요.')
        basis=f"WMS 출고일 {p['start']}~{p['end']} · {JOINS[p['join']]} · 원본 후보 {len(candidates)}행. {reason}"
        if notes:basis+=' 기존 승인된 해당 청구행의 메모를 분리한 기본 주문번호를 사용했습니다.'
        changes.append(dict(target_id=t['target_id'],label=t['label'],check=t['check'],before=before,
                            after=dict(before,status=status),basis=basis,evidence_refs=refs))
    return dict(adapter_version=VERSION,changes=changes,limitations=LIMITATIONS,calculation_executed=True)


@lru_cache(maxsize=2)
def _load(root_text,source_json,include_june):
    root=Path(root_text);sources=json.loads(source_json);paths={k:root/v['path'] for k,v in sources.items()}
    return read_claim(paths['claim']),read_wms(paths,include_june)


def calculate(root,record,parameters):
    root=Path(root).resolve();p=validate(parameters);selected,doc=targets(root,record)
    cfg=_json(root,CONFIG);sources={k:cfg['sources'][k] for k in ('claim','outbound_july','outbound_h1') if k in cfg['sources']}
    decisions=_json(root,Path(cfg['decisions_path']))
    if decisions.get('comparison_period',{}).get('state')!='confirmed_by_user' or [decisions['comparison_period'].get(k) for k in ('start','end')]!=doc['period']:
        raise ValueError('기존 청구기간 기준이 저장 결과와 다릅니다.')
    yearless=decisions.get('yearless_date',{})
    if (yearless.get('state'),yearless.get('year'),yearless.get('month'))!=('confirmed_by_user',2029,7):
        raise ValueError('원본 WMS의 생략 연도 해석 기준을 확인할 수 없습니다.')
    paths={}
    for role,s in sources.items():
        if s!=doc['sources'].get(role):raise ValueError('등록 원본과 저장 결과의 자료 버전이 다릅니다.')
        path=chat_intake._safe_file(root,Path(s['path']))
        if not path.resolve().is_relative_to((root/BASE/'source').resolve()) or digest(path).lower()!=s['sha256'].lower():
            raise ValueError('원본 자료가 바뀌었습니다. 전체 검증을 먼저 실행해 주세요.')
        paths[role]=path
    include_june=p['start']<'2029-07-01'
    if include_june:
        if 'outbound_h1' not in sources:raise ValueError('6월 WMS가 없습니다.')
        bpath=BASE/'derived/b2c_actual_claim_wms.json';bdoc=_json(root,bpath)
        expected=_json(root,BASE/'derived/workflow_result.json').get('result_hashes',{}).get(bpath.name)
        js=bdoc.get('sources',{}).get('raw_wms_june',{})
        if not expected or digest(root/bpath).lower()!=expected.lower() or any(js.get(k)!=sources['outbound_h1'].get(k) for k in ('path','sha256')) or not bdoc.get('included_june_wms'):
            raise ValueError('6월 WMS의 기존 가상센터 포함 근거를 확인할 수 없습니다.')
    notes=_json(root,Path('ia/config/matching_review_decisions.json'))
    notes_allowed=notes.get('U21',{}).get('state')=='approved' and notes.get('period')==doc['period'] and all(notes.get('source_hashes',{}).get(k,'').lower()==s['sha256'].lower() for k,s in sources.items())
    claims,wms=_load(str(root),json.dumps(sources,sort_keys=True),include_june)
    result=compare(selected,claims,wms,p,sources,notes_allowed)
    if any(digest(path).lower()!=sources[k]['sha256'].lower() for k,path in paths.items()):raise ValueError('비교 중 원본이 변경되었습니다.')
    targets(root,record)
    return result


def baseline_parameters(root):
    doc=_json(root,RESULT)
    return dict(start=doc['period'][0],end=doc['period'][1],join='order')
