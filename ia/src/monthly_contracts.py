"""Contract transcription comparison, separate from effective-date approval.

No July decisions are loaded. No contract contents are sent to a model.
"""
from decimal import Decimal
from pathlib import Path
import subprocess
from pypdf.errors import PdfReadError

from contract_pdf import extract, compact, unit, rate_key


def period_observation(document, period):
    intervals=document.get('periods',[])
    known=[p for p in intervals if p.get('text_verified')]
    renewal=any(c['kind']=='renewal' for c in document.get('clauses',[]))
    if not known:
        return 'unknown','계약의 시작일·종료일을 확정할 수 없습니다.'+(' 자동연장 조건의 충족 여부도 확인되지 않았습니다.' if renewal else '')
    choices={(p['start'],p['end']) for p in known}
    if len(choices)!=1:return 'ambiguous','문서에 기간이 여러 개 있어 해당 청구에 적용할 기간을 정할 수 없습니다.'
    start,end=next(iter(choices))
    if start<=period['start'] and period['end']<=end:
        return 'within_written_period',f'청구기간이 문서 기재기간 {start} ~ {end} 안에 있습니다. 계약 적용 승인과는 구분합니다.'
    if renewal:return 'renewal_unconfirmed','문서 기재기간 밖입니다. 자동연장 조건의 충족 여부가 확인되지 않았습니다.'
    return 'outside_written_period',f'청구기간이 문서 기재기간 {start} ~ {end} 밖입니다.'


def reference(doc, row):
    return doc['path']+' / '+(str(row['page'])+'쪽' if row.get('page') else row.get('location',''))


def inspect_contracts(root, case, fee_items, emit, tabular):
    """Read every contract; fail only the affected checks, never choose a conflict."""
    from monthly_verification import numeric, text
    documents=[];incomplete=False
    for material in (m for m in case['materials'] if m['role']=='contract'):
        doc={'path':material['path'],'name':material.get('name',Path(material['path']).name),
             'sha256':material['sha256'],'rates':[],'clauses':[],'periods':[],'read_status':'read'}
        try:
            if Path(material['path']).suffix.lower()=='.pdf':
                source=extract(root,material)
                doc.update({k:v for k,v in source.items() if k!='pages'})
                doc['page_count']=len(source['pages'])
                if not doc['rates']:
                    incomplete=True;doc['read_status']='unsupported_rate_layout'
                    emit('계약 단가표 읽기','not_implemented','등록 계약의 단가표를 현재 지원하는 항목·단위·단가 열로 읽지 못했습니다.',계약위치=material['path'])
            else:
                for sh,n,r in tabular(Path(root)/material['path'],{'청구항목','단가','단위'}):
                    rate=numeric(r['단가'])
                    if rate is None:continue
                    doc['rates'].append({'label':text(r['청구항목']),'unit':unit(r['단위']),'rate':text(rate),
                        'condition':'','location':f'{sh}!{n}','key':rate_key(r['청구항목'],r['단위']),
                        'method':'spreadsheet','text_verified':True})
                if not doc['rates']:raise ValueError('항목·단위·숫자 단가가 있는 계약표를 찾지 못했습니다.')
        except (OSError,ValueError,UnicodeError,subprocess.TimeoutExpired,ImportError,PdfReadError) as exc:
            incomplete=True;doc['read_status']='failed';doc['error']=str(exc)
            emit('계약 파일 읽기','not_implemented','등록 계약을 읽지 못했습니다: '+str(exc),계약위치=material['path'])
        # Native literal date range is checked; relative/conditional clauses are
        # observations only. Text availability is not effective-contract approval.
        pstatus,preason=period_observation(doc,case['period'])
        doc['period_status']=pstatus;doc['period_reason']=preason
        locs=[reference(doc,p) for p in doc['periods']]
        emit('계약 기재기간 비교','match' if pstatus=='within_written_period' else 'data_unavailable',preason,
             계약위치='; '.join(locs) or doc['path'],단위='계약 문서')
        documents.append(doc)
    if not documents:return []
    for item in fee_items:
        label=item['label'];key=rate_key(label,item['unit'])
        candidates=[]
        for doc in documents:
            for row in doc['rates']:
                if (key is not None and row['key']==key) or (compact(row['label'])==compact(label) and row['unit']==unit(item['unit'])):
                    candidates.append((doc,row))
        refs=[reference(d,r) for d,r in candidates]
        values={Decimal(r['rate']) for _,r in candidates}
        verified=bool(candidates) and all(r.get('text_verified') for _,r in candidates)
        reason='계약서 기재 단가와 청구서 기재 단가 비교입니다. 해당 월 적용·실제 작업 발생의 승인과는 구분합니다.'
        expected=next(iter(values)) if len(values)==1 and verified and not incomplete else None
        if incomplete:reason='읽지 못한 계약·단가표가 있어 등록 계약 전체의 적용값을 정할 수 없습니다.'
        elif len(values)>1:reason='등록 계약에 같은 항목·단위의 단가가 서로 다릅니다. 어느 값을 적용할지 미확정입니다.'
        elif candidates and not verified:reason='스캔에서 읽은 단가 후보입니다. 원문 숫자가 확인되지 않아 일치·불일치를 판정하지 않았습니다.'
        elif not candidates:reason='등록 계약에서 같은 청구 항목·과금단위의 숫자 단가를 연결하지 못했습니다. 실비·별도협의 조건은 계약서 분석에서 확인할 수 있습니다.'
        claim_rate=item['rate']
        delta=claim_rate-expected if claim_rate is not None and expected is not None else None
        if claim_rate is None:reason='청구서 해당 항목의 단가가 숫자로 기재되지 않아 계약 단가와 숫자 비교를 할 수 없습니다.'
        emit('계약서 기재단가 비교','data_unavailable' if delta is None else 'match' if delta==0 else 'difference',reason,
            청구위치=f"청구내역!L{item['row']}",청구값=text(claim_rate),비교값=text(expected),차이=text(delta),
            계약위치='; '.join(refs),계산근거='; '.join(refs),단위='원/청구 과금단위',
            contract_candidates=[{'path':d['path'],**r} for d,r in candidates])
    refs=[reference(d,c) for d in documents for c in d['clauses']]
    emit('계약 적용·실비 적정성','data_unavailable',
         '계약서 기재 단가·기간은 별도로 비교했습니다. 계약 변경·자동연장 조건의 충족, 실비 영수증과 별도 합의금액의 적정성은 현재 등록 근거로 확정하지 않았습니다.',
         계약위치='; '.join(dict.fromkeys(refs)) or '; '.join(d['path'] for d in documents))
    return documents
