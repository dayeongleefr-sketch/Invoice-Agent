"""Local PDF extraction with page provenance; OCR is never silently verified."""
from datetime import date
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

VERSION = 'contract-pdf-portable-3'


def compact(value):
    return re.sub(r'\s+', '', str(value or ''))


def unit(value):
    s=compact(value).upper().replace('당','').replace('/월','').replace('／월','')
    s=re.sub(r'^[1I](?=PCS|PLT|BOX|SET|CONTAINER)', '', s)
    if s=='LCONTAINER':s='CONTAINER'
    return {'PALLET':'PLT','컨테이너':'CONTAINER','평':'평','㎡':'㎡','M2':'㎡'}.get(s,s)


def rate_key(label, u):
    """Finite semantic aliases, never similarity/quantity based assignment."""
    s=compact(label).upper().replace('_','-');u=unit(u)
    if '보관' in s and u in ('평','㎡','PLT'):return 'storage.'+u
    if ('검수비' in s or '입고비' in s) and u in ('PCS','BOX'):return 'inbound.'+u
    if '상차' in s and u=='PLT':return 'loading.PLT'
    if 'CPNP' in s and '라벨' in s and u=='PCS':return 'cpnp.PCS'
    if any(v in s for v in ('샤쉐3종','사쉐3종')) and u=='SET':return 'sachet3.SET'
    if '라벨' in s and ('아마존' in s or '라벨부착(B2B)' in s) and u in ('PCS','라벨,마킹'):return 'label.PCS'
    if any(v in s for v in ('밴딩','밴닝')) and '#' not in s and u=='PLT':return 'banding.PLT'
    if 'AJPLT' in s and u=='PLT':return 'aj.PLT'
    if '기본포장' in s and (u=='PCS' or re.fullmatch(r'1[~～-]3EA',u)):return 'packing.base'
    if '추가포장' in s and (u=='PCS' or u.startswith('1EA')):return 'packing.extra'
    if 'B2B' in s and any(v in s for v in ('작업비','출고')) and '반품' not in s and u in ('BOX','PCS'):
        return 'outbound.'+('export.' if '수출' in s else 'domestic.')+u
    if ('인스펙션' in s or 'INSPECTION' in s) and u=='PLT':return 'inspection.PLT'
    if ('쇼링' in s or '쇼링' in u) and u in ('CONTAINER','쇼링'):return 'shoring.CONTAINER'
    if '반품' in s and u=='BOX':
        if 'B2B' in s:return 'return.B2B.BOX'
        if 'B2C' in s:return 'return.B2C.BOX'
    return None


def _ocr_rows(page):
    words=page.get('words',[])
    # Recognise column positions from the table header, not a fixed page/row.
    header={t:next((w for w in words if w['text']==t),None) for t in ('단위','단가','비고')}
    if not all(header.values()):return []
    centers={k:v['x']+v['w']/2 for k,v in header.items()}
    if not centers['단위']<centers['단가']<centers['비고']:return []
    u0=centers['단위']-.7*(centers['단가']-centers['단위'])
    u1=(centers['단위']+centers['단가'])/2
    p1=(centers['단가']+centers['비고'])/2
    bottom=max(w['y']+w['h'] for w in header.values())
    prices=[w for w in words if u1<w['x']+w['w']/2<p1 and w['y']>bottom
            and re.fullmatch(r'(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?',w['text'])]
    result=[];previous=''
    for price in sorted(prices,key=lambda w:w['y']):
        y=price['y']+price['h']/2
        same=sorted((w for w in words if abs(w['y']+w['h']/2-y)<max(15,price['h']*.65)),key=lambda w:w['x'])
        label=' '.join(w['text'] for w in same if w['x']+w['w']/2<u0)
        u=''.join(w['text'] for w in same if u0<=w['x']+w['w']/2<u1)
        notes=' '.join(w['text'] for w in same if w['x']+w['w']/2>=p1)
        # Merged description cell in storage rows only.
        if not label and '보관' in previous:label=previous
        if label:previous=label
        if not label or not u:continue
        result.append({'label':label,'unit':unit(u),'rate':price['text'].replace(',',''),
                       'condition':notes,'page':page['page'],'method':page['method'],'text_verified':False,
                       'bbox':[min(w['x'] for w in same),min(w['y'] for w in same),max(w['x']+w['w'] for w in same),max(w['y']+w['h'] for w in same)]})
    return result


def _text_rows(page):
    result=[];active=False;columns=None
    for line in page['text'].splitlines():
        # Keep empty table cells: otherwise a missing unit shifts the rate/notes.
        if '|' in line:
            cells=[v.strip() for v in line.strip().strip('|').split('|')]
        elif '\t' in line:
            cells=[v.strip() for v in line.strip().split('\t')]
        else:
            cells=[v.strip() for v in re.split(r' {2,}',line.strip()) if v.strip()]
        from document_layouts import header_map
        aliases={'항목':['비용항목','청구항목','항목명','품명','item'],
                 '단위':['과금단위','unit'],'단가':['계약단가','요율','rate','unit price'],'비고':['조건','condition']}
        try:mapping=header_map(dict(enumerate(cells)),list(aliases),aliases)
        except ValueError:
            active=False;columns=None;continue
        if {'항목','단위','단가'}<=mapping.keys():active=True;columns=mapping;continue
        if '단위' in line and '단가' in line:active=True;columns=None;continue
        if not active:continue
        if columns:
            if max(columns[k] for k in ('항목','단위','단가'))>=len(cells):continue
            price=cells[columns['단가']]
            if not cells[columns['항목']] or not cells[columns['단위']]:continue
            if not re.fullmatch(r'(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\s*원?',price):continue
            result.append({'label':cells[columns['항목']],'unit':unit(cells[columns['단위']]),
                'rate':re.sub(r'[,원\s]','',price),'condition':cells[columns['비고']] if '비고' in columns and columns['비고']<len(cells) else '',
                'page':page['page'],'method':'pdf_text','text_verified':True,'text':line.strip()})
            continue
        # Explicit table item / unit / numeric rate [/ condition] only.
        if len(cells)<3:continue
        for i in range(1,len(cells)-1):
            if unit(cells[i]) not in ('PCS','BOX','PLT','SET','EA','평','㎡','CONTAINER'):continue
            if not re.fullmatch(r'(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\s*원?',cells[i+1]):continue
            result.append({'label':' '.join(cells[:i]),'unit':unit(cells[i]),'rate':re.sub(r'[,원\s]','',cells[i+1]),
                'condition':' '.join(cells[i+2:]),'page':page['page'],'method':'pdf_text','text_verified':True,'text':line.strip()})
            break
    return result


def parse_pages(pages):
    rates=[];clauses=[];periods=[]
    date_pattern=r'(20\d{2})\s*(?:년|[./-])\s*(\d{1,2})\s*(?:월|[./-])\s*(\d{1,2})\s*일?'
    for p in pages:
        rates.extend(_ocr_rows(p) if p['method'] in ('windows_ocr','tesseract_ocr') else _text_rows(p))
        text=p['text'];flat=re.sub(r'\s+',' ',text)
        for kind,pattern in [('renewal',r'자동\s*연장'),('actual_cost',r'실비|At\s+cost'),
                             ('agreement',r'별도\s*(?:합의|협의)|견적|사전\s*협의|협의되지'),
                             ('amendment',r'계약의\s*변경|서면으로\s*동의'),('surcharge',r'추가\s*(?:비용|운임)|도서\s*산간'),
                             ('period',r'계약기간|적용기간|유효기간')]:
            for m in list(re.finditer(pattern,flat,re.I))[:12]:
                clauses.append({'kind':kind,'page':p['page'],'excerpt':flat[max(0,m.start()-35):m.end()+190],
                                'method':p['method'],'text_verified':p['method']=='pdf_text'})
        # Only literal start/end range labelled as a contract/application period.
        pattern=r'(?:계약기간|적용기간|유효기간)\s*[:：]?\s*'+date_pattern+r'\s*(?:~|～|부터|[-–])\s*'+date_pattern
        for m in re.finditer(pattern,flat):
            try:start=date(*map(int,m.groups()[:3])).isoformat();end=date(*map(int,m.groups()[3:])).isoformat()
            except ValueError:continue
            if start<=end:periods.append({'start':start,'end':end,'page':p['page'],'text_verified':p['method']=='pdf_text','excerpt':m.group(0)})
    for r in rates:r['key']=rate_key(r['label'],r['unit'])
    return {'rates':rates,'clauses':clauses,'periods':periods}


def _read_pages(path, work):
    from pypdf import PdfReader
    reader=PdfReader(path)
    if reader.is_encrypted:raise ValueError('암호가 설정된 계약 PDF입니다.')
    if len(reader.pages)>100:raise ValueError('계약 PDF 100쪽 초과: 문서 분리가 필요합니다.')
    pages=[]
    for i,p in enumerate(reader.pages,1):
        text=p.extract_text(extraction_mode='layout') or ''
        pages.append({'page':i,'text':text,'method':'pdf_text'})
    blank=[p['page'] for p in pages if len(compact(p['text']))<20]
    if blank:
        if os.name!='nt':
            from portable_ocr import read_pages
            scanned={p['page']:p for p in read_pages(path,work,blank)}
            return [scanned[p['page']] if p['page'] in blank else p for p in pages]
        output=work/'ocr.json'
        command=['powershell.exe','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',
                 str(Path(__file__).with_name('Read-ContractPdf.ps1')),'-SourcePath',str(path),'-OutputPath',str(output)]
        proc=subprocess.run(command,capture_output=True,timeout=180,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if proc.returncode or not output.exists():raise ValueError('스캔 계약 PDF의 로컬 문자 인식에 실패했습니다.')
        scanned={p['page']:p for p in json.loads(output.read_text('utf-8-sig'))['pages']}
        pages=[scanned[p['page']] if p['page'] in blank else p for p in pages]
    return pages


def extract(root, material):
    """Cache only fresh source+parser hashes. Runtime cache is not decision storage."""
    root=Path(root).resolve();path=(root/material['path']).resolve()
    if not path.is_relative_to(root/'data/demo_company/source') or path.suffix.lower()!='.pdf':
        raise ValueError('등록한 회사 자료의 PDF만 읽습니다.')
    sha=hashlib.sha256(path.read_bytes()).hexdigest()
    if sha.lower()!=material['sha256'].lower():raise ValueError('계약 원본이 등록 후 변경되었습니다.')
    codehash=hashlib.sha256(Path(__file__).read_bytes()+Path(__file__).with_name('Read-ContractPdf.ps1').read_bytes()+Path(__file__).with_name('document_layouts.py').read_bytes()).hexdigest()
    from portable_ocr import cache_stamp
    ocr_context=cache_stamp() if os.name!='nt' else {}
    codehash=hashlib.sha256((codehash+json.dumps(ocr_context,sort_keys=True)).encode()+Path(__file__).with_name('portable_ocr.py').read_bytes()).hexdigest()
    cache=root/'ia/.runtime/contract_pdf';cache.mkdir(parents=True,exist_ok=True)
    if cache.resolve()!=cache:raise ValueError('잘못된 계약 캐시 경로')
    dest=cache/(sha+'_'+codehash+'.json')
    if dest.exists():
        try:
            wrapper=json.loads(dest.read_text('utf-8'));raw=wrapper['document']
            if wrapper['digest']!=hashlib.sha256(json.dumps(raw,sort_keys=True,ensure_ascii=False).encode()).hexdigest():raise ValueError('cache_hash')
        except (ValueError,KeyError,TypeError):raw=None
    else:raw=None
    if raw is None:
        with tempfile.TemporaryDirectory(prefix='read_',dir=cache) as td:pages=_read_pages(path,Path(td))
        raw={'sha256':sha,'parser':VERSION,'pages':pages,**parse_pages(pages)}
        wrapper={'document':raw,'digest':hashlib.sha256(json.dumps(raw,sort_keys=True,ensure_ascii=False).encode()).hexdigest()}
        with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=cache,delete=False) as f:
            json.dump(wrapper,f,ensure_ascii=False);tmp=Path(f.name)
        tmp.replace(dest)
    # Human checked transcription is tied to identical PDF bytes and exact rate.
    reviews=root/'ia/config/contract_pdf_verified.json'
    review_bytes=reviews.read_bytes() if reviews.exists() else b'{}'
    verified=json.loads(review_bytes.decode('utf-8-sig')).get(sha,{})
    for r in raw['rates']:
        token=f"{r['page']}|{r['key']}|{r['unit']}"
        if r['key'] and str(verified.get('rates',{}).get(token,''))==r['rate']:
            r['text_verified']=True;r['verification']='동일 원본의 페이지와 숫자 확인'
    raw['path']=material['path'];raw['name']=material.get('name',path.name)
    raw['transcription_registry_sha256']=hashlib.sha256(review_bytes).hexdigest()
    raw['period_text_verified']=verified.get('period_pages',[])
    return raw
