"""Read original evidence rows locally; no verification outputs are modified."""
import csv
import hashlib
import json
import re
from pathlib import Path
from contextlib import ExitStack
import zipfile


def original_file(root, relative):
    """Resolve legacy filename-only WMS references from the completed run, not a file search."""
    from agent_adapter import safe_file, BASE
    if isinstance(relative, str) and relative and not any(c in relative for c in '/\\:') and relative not in ('.', '..'):
        from calculation_view import _document
        workflow_path = root / BASE / 'workflow_result.json'
        if not workflow_path.is_file():
            raise ValueError('이 파일의 원본 경로를 검증 결과에서 찾지 못했습니다.')
        workflow = json.loads(workflow_path.read_text(encoding='utf-8-sig'))
        expected = workflow.get('result_hashes', {}).get('direct_wms_result.json')
        if not expected:
            raise ValueError('이 파일의 원본 경로를 검증 결과에서 찾지 못했습니다.')
        result_path = safe_file(root, (BASE / 'direct_wms_result.json').as_posix())
        result = _document(str(result_path), (result_path.stat().st_mtime_ns, result_path.stat().st_size), expected)
        candidates = {}
        for source in result.get('sources', {}).values():
            if isinstance(source, dict) and isinstance(source.get('path'), str) and Path(source['path']).name == relative:
                candidates.setdefault(source['path'], set()).add(str(source.get('sha256') or '').lower())
        if not candidates:
            raise ValueError('이 파일은 현재 검증에 사용한 원본 목록에 없습니다.')
        if len(candidates) != 1:
            raise ValueError('검증에 사용한 원본 중 같은 이름의 파일이 여러 개입니다. 파일 경로를 구분해야 합니다.')
        relative, hashes = next(iter(candidates.items()))
        path = safe_file(root, relative)
        if not path.resolve().is_relative_to((root / 'data/demo_company/source').resolve()):
            raise ValueError('원본 파일만 조회합니다.')
        with path.open('rb') as stream:
            actual = hashlib.file_digest(stream, 'sha256').hexdigest()
        if hashes != {actual}:
            raise ValueError('검증 이후 원본 파일이 변경되었습니다. 재검증 후 근거를 조회해 주세요.')
    else:
        path = safe_file(root, relative)
    if not path.resolve().is_relative_to((root / 'data/demo_company/source').resolve()):
        raise ValueError('원본 파일만 조회합니다.')
    return path


def xlsx_selected(path,sheet_name,wanted):
    """Skip decoding the hundreds of thousands of irrelevant WMS rows."""
    from wms_tools import ET, NS, RID
    from decimal import Decimal
    with ExitStack() as resources:
        book=resources.enter_context(zipfile.ZipFile(path));strings=[]
        if 'xl/sharedStrings.xml' in book.namelist():
            for _,e in ET.iterparse(resources.enter_context(book.open('xl/sharedStrings.xml')),events=('end',),tag=NS+'si',resolve_entities=False,no_network=True):
                strings.append(''.join(t.text or '' for t in e.iter(NS+'t')));e.clear();e.getparent().remove(e)
        rels={e.attrib['Id']:e.attrib['Target'] for e in ET.fromstring(book.read('xl/_rels/workbook.xml.rels'))}
        sheets=ET.fromstring(book.read('xl/workbook.xml')).find(NS+'sheets')
        sheet=next((s for s in sheets if s.attrib['name']==sheet_name),None)
        if sheet is None:raise ValueError('원본 시트를 찾지 못했습니다.')
        target=rels[sheet.attrib[RID]];target=target.lstrip('/') if target.startswith('/') else 'xl/'+target
        for _,e in ET.iterparse(resources.enter_context(book.open(target)),events=('end',),tag=NS+'row',resolve_entities=False,no_network=True):
            number=int(e.attrib['r']);row={}
            if number in wanted or number<=20:
                for c in e:
                    col=c.attrib.get('r','').rstrip('0123456789');v=c.find(NS+'v');v=v.text if v is not None else None
                    kind=c.attrib.get('t','n')
                    if kind=='s' and v is not None:v=strings[int(v)]
                    elif kind=='inlineStr':v=''.join(t.text or '' for t in c.iter(NS+'t'))
                    elif kind=='n' and v is not None:v=Decimal(v)
                    if v is not None:row[col]={'value':v}
                if row:yield number,row
            e.clear();e.getparent().remove(e)
            if number>=max(wanted):break


def last_source_row(path, sheet_name):
    """Resolve 'last row' from the original sheet, never from a hard-coded total."""
    from wms_tools import xlsx_rows, xlsb_rows
    with ExitStack() as resources:
        if path.suffix.lower() == '.xlsx':
            rows = xlsx_rows(path, sheet_name)
        elif path.suffix.lower() == '.xlsb':
            from portable_workbook import open_workbook
            book = resources.enter_context(open_workbook(str(path)))
            rows = xlsb_rows(resources.enter_context(book.get_sheet(sheet_name)))
        else:
            raise ValueError('마지막 행 범위는 엑셀 원본에서 조회합니다.')
        try:
            return max((n for n, values in rows if values), default=0)
        finally:
            if hasattr(rows, 'close'):
                rows.close()


def preview(root, issue_id, field, group_index=0, page=1):
    from dashboard_view import source_groups
    from agent_adapter import BASE
    from wms_tools import xlsx_rows, xlsb_rows
    if field not in ('청구위치','WMS위치','_cost_evidence'):raise ValueError('잘못된 근거 요청입니다.')
    if str(issue_id).startswith('normal:'):
        from review_catalog import additional_rows
        row=next((r for r in additional_rows(root) if r['_id']==issue_id),None)
    elif str(issue_id).isdigit():
        with (root/BASE/'검토필요_전체목록.csv').open(encoding='utf-8-sig',newline='') as f:
            row=next((r for i,r in enumerate(csv.DictReader(f)) if i==int(issue_id)),None)
    else:raise ValueError('잘못된 근거 요청입니다.')
    if row is None:raise ValueError('확인 목록에서 항목을 다시 선택해 주세요.')
    position = row.get(field,'')
    if field=='청구위치' and row.get('_cost_evidence'):
        position += '; ' + row['_cost_evidence']
    groups=source_groups(position)
    if not 0<=group_index<len(groups):raise ValueError('없는 근거 위치입니다.')
    group=groups[group_index];relative=group['file'] or (row.get('청구원본') if field in ('청구위치','_cost_evidence') else '')
    path=original_file(root,relative)
    numbers=set();last_row=None
    for ref in group['cells']:
        match=re.fullmatch(r'(?:[A-Z]+)?(\d+)(?::(?:[A-Z]+)?(\d+|마지막행))?',ref)
        if not match:continue
        start=int(match[1])
        if match[2]=='마지막행':
            if last_row is None:last_row=last_source_row(path,group['sheet'])
            end=last_row
        else:end=int(match[2] or start)
        if end<start or end-start>5000:raise ValueError('범위가 너무 큽니다. 개별 근거를 선택하세요.')
        numbers.update(range(start,end+1))
    numbers=sorted(numbers)
    if not numbers:raise ValueError('이 근거에는 조회할 행 번호가 없습니다.')
    pages=(len(numbers)+19)//20
    if not 1<=page<=pages:raise ValueError('없는 근거 페이지입니다.')
    selected=set(numbers[(page-1)*20:page*20]);found=[];headers={};header_row=None
    def col_letter(n):
        result=''
        while n:n,r=divmod(n-1,26);result=chr(65+r)+result
        return result
    with ExitStack() as stack:
        if path.suffix.lower()=='.xlsx':rows=xlsx_selected(path,group['sheet'],selected)
        elif path.suffix.lower()=='.xlsb':
            from portable_workbook import open_workbook
            book=stack.enter_context(open_workbook(str(path)))
            sheet=stack.enter_context(book.get_sheet(group['sheet']))
            rows=xlsb_rows(sheet)
        elif path.suffix.lower()=='.csv':
            # Existing supplied CSVs use UTF-8 BOM; fall back to the original Korean encoding.
            try:
                stream=path.open(encoding='utf-8-sig',newline='');stream.read(4096);stream.seek(0)
            except UnicodeDecodeError:
                stream.close();stream=path.open(encoding='cp949',newline='')
            stack.enter_context(stream)
            rows=((i,{col_letter(j):{'value':v} for j,v in enumerate(values,1) if v}) for i,values in enumerate(csv.reader(stream),1))
        else:raise ValueError('현재 원본 미리보기는 XLSX/XLSB/CSV를 지원합니다.')
        try:
            for number,values in rows:
                if number<=20:
                    labels={col:cell['value'] for col,cell in values.items() if isinstance(cell.get('value'),str)}
                    terms=('주문번호','출고일','품번','수량','단가','금액','입고일','송장번호','픽킹','피킹','팩킹','마킹','라벨')
                    if ((header_row is None or number==header_row+1) and len(labels)==len(values)
                            and sum(any(t in value for t in terms) for value in labels.values())>=2):
                        headers.update({col:value for col,value in labels.items() if len(value)<45})
                        header_row=number
                if number in selected:
                    found.append({'원본 행':number,**{c:str(v.get('value','')) for c,v in values.items() if v.get('value') is not None}})
                if number>=max(selected):break
        finally:
            if hasattr(rows,'close'):rows.close()
    columns=sorted({k for r in found for k in r if k!='원본 행'},key=lambda k:(len(k),k))
    return {'file':path.name,'sheet':group['sheet'],'rows':found,'columns':columns,'headers':headers,
            'total_rows':len(numbers),'pages':pages,'page':page,'missing_rows':sorted(selected-{r['원본 행'] for r in found}),
            'source_mtime_ns':path.stat().st_mtime_ns}
