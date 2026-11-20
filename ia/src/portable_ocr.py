"""Offline scan OCR: PDFium rendering + Tesseract TSV, with unverified provenance."""
import csv
import hashlib
import io
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import time


def run(args, timeout=30):
    try:
        result=subprocess.run(args,capture_output=True,text=True,encoding='utf-8',errors='replace',
                              timeout=timeout,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    except subprocess.TimeoutExpired:raise ValueError('로컬 OCR 제한 시간을 초과했습니다.') from None
    if result.returncode:raise ValueError('로컬 OCR 실행 실패. 05_SETUP_OCR.command에서 설치·언어를 확인하세요.')
    return result.stdout


def engine():
    command=shutil.which('tesseract')
    if not command:raise ValueError('로컬 OCR이 없습니다. 05_SETUP_OCR.command를 실행하세요.')
    version=run([command,'--version']).splitlines()[0]
    listing=run([command,'--list-langs'])
    languages={s.strip() for s in listing.splitlines()[1:]}
    if not {'kor','eng'}<=languages:raise ValueError('OCR 한국어·영어 언어 자료가 필요합니다. 05_SETUP_OCR.command를 실행하세요.')
    match=re.search(r'List of available languages in ["\'](.+?)["\']',listing)
    if not match:raise ValueError('OCR 언어 자료 위치를 확인하지 못했습니다.')
    folder=Path(match.group(1))
    hashes={name:hashlib.sha256((folder/(name+'.traineddata')).read_bytes()).hexdigest() for name in ('kor','eng')}
    return {'command':command,'version':version,'languages':['kor','eng'],'language_hashes':hashes,
            'binary_hash':hashlib.sha256(Path(command).read_bytes()).hexdigest()}


def cache_stamp():
    try:
        from importlib.metadata import version
        value=engine();value.pop('command')
        value['pdfium']=version('pypdfium2');value['pillow']=version('Pillow')
        return value
    except (ValueError,OSError,ImportError) as exc:return {'unavailable':type(exc).__name__}


def parse_tsv(raw,page,width,height):
    words=[];lines={}
    for row in csv.DictReader(io.StringIO(raw),delimiter='\t',quoting=csv.QUOTE_NONE):
        if row.get('level')!='5' or not row.get('text','').strip():continue
        try:
            x,y,w,h=[int(row[k]) for k in ('left','top','width','height')]
            confidence=float(row['conf'])
            key=tuple(int(row[k]) for k in ('block_num','par_num','line_num'))
        except (ValueError,KeyError,TypeError):raise ValueError('OCR 좌표/신뢰도 형식이 올바르지 않습니다.') from None
        if min(x,y,w,h)<0 or x+w>width+1 or y+h>height+1 or not math.isfinite(confidence) or not 0<=confidence<=100:
            raise ValueError('OCR 좌표 또는 신뢰도 범위를 벗어났습니다.')
        word={'text':row['text'].strip(),'x':x,'y':y,'w':w,'h':h,'confidence':confidence}
        words.append(word);lines.setdefault(key,[]).append(word)
    if not words:raise ValueError(f'{page}쪽에서 문자를 인식하지 못했습니다. 선명한 자료를 등록하세요.')
    text='\n'.join(' '.join(w['text'] for w in sorted(line,key=lambda w:w['x'])) for line in lines.values())
    return {'page':page,'text':text,'words':words,'width':width,'height':height,
            'method':'tesseract_ocr','text_verified':False}


def read_pages(path,work,pages):
    import pypdfium2 as pdfium
    identity=engine();deadline=time.monotonic()+180
    result=[]
    with pdfium.PdfDocument(str(path)) as document:
        if len(document)>100:raise ValueError('계약 PDF 100쪽 초과')
        if not pages or len(set(pages))!=len(pages) or any(type(n)!=int or not 1<=n<=len(document) for n in pages):
            raise ValueError('잘못된 OCR 페이지 범위')
        for number in pages:
            if time.monotonic()>=deadline:raise ValueError('로컬 OCR 전체 제한 시간 초과')
            image_path=work/f'ocr_page_{number}.png'
            page=document[number-1]
            try:
                width,height=page.get_size()
                if min(width,height)<=0:raise ValueError('잘못된 PDF 페이지 크기')
                scale=min(300/72,4000/max(width,height),math.sqrt(12000000/(width*height)))
                bitmap=page.render(scale=scale)
                try:
                    image=bitmap.to_pil()
                    try:
                        size=image.size;image.save(image_path)
                    finally:image.close()
                finally:bitmap.close()
            finally:page.close()
            try:
                raw=run([identity['command'],str(image_path),'stdout','-l','kor+eng','--psm','6','tsv'],
                        timeout=max(1,min(60,deadline-time.monotonic())))
                value=parse_tsv(raw,number,*size)
                value['ocr_engine']={k:v for k,v in identity.items() if k!='command'}
                result.append(value)
            finally:image_path.unlink(missing_ok=True)
    if engine()!=identity:raise ValueError('OCR 실행 중 엔진/언어 자료가 변경되었습니다.')
    return result


if __name__=='__main__':
    value=engine();value.pop('command')
    print(json.dumps({'ready':True,**value},ensure_ascii=False,indent=2))
