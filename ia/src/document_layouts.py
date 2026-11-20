"""Header-based local adapters. No guessed columns, workbook edits or macro execution."""
from copy import deepcopy
import re


def key(value):
    return re.sub(r'[\s_\-·():/\[\]]', '', str(value or '')).lower()


ALIASES = {
    '입고일': ['입고일자', '입고날짜', 'receipt date'],
    '출고일': ['출고일자', '출고날짜', 'ship date', 'outbound date'],
    '입고예정번호': ['입고번호', '입고예정 번호', 'receipt id'],
    '주문번호(연동)': ['주문번호', '연동주문번호', 'order id', 'order number'],
    '품번': ['상품코드', '제품코드', 'sku', 'item code'],
    '수량': ['수량(EA)', '상품수량', '실적수량', 'qty', 'quantity'],
    '단위': ['수량단위', '과금단위', 'unit', 'uom'],
    '회사구분': ['회사명', '화주명', '고객사명', 'company'],
    '입고창고': ['입고센터', 'warehouse'],
    '출고창고': ['출고센터', 'warehouse'],
    '품명': ['상품명', '제품명', 'item name'],
    '구분': ['출고유형', '입고유형', '유형'],
    '송장번호': ['운송장번호', 'tracking', 'tracking number'],
    '출고번호': ['출고ID', 'outbound id'],
    '택배사': ['운송사', 'carrier'],
    '청구항목': ['비용항목', '항목명', 'charge item'],
    '단가': ['기재단가', '계약단가', '요율', 'unit price', 'rate'],
}


def header_map(cells, fields, aliases):
    result = {}; used = set()
    for canonical in fields:
        accepted = {key(canonical), *(key(v) for v in aliases.get(canonical, []))}
        matches = [col for col, value in cells.items() if key(value) in accepted]
        if len(matches) > 1:
            raise ValueError('같은 의미의 열이 여러 개입니다: ' + canonical)
        if matches:
            if matches[0] in used:
                raise ValueError('하나의 열을 여러 의미로 해석할 수 없습니다: ' + str(matches[0]))
            result[canonical] = matches[0]; used.add(matches[0])
    return result


def table_header(cells, required):
    # Optional aliases cannot steal a column needed by a required field.
    mapping = header_map(cells, sorted(required), ALIASES)
    if not required.issubset(mapping):
        return None
    remaining = {col: value for col, value in cells.items() if col not in mapping.values()}
    optional = set(ALIASES) - required - {'입고일', '출고일', '입고창고', '출고창고', '청구항목', '단가'}
    mapping.update(header_map(remaining, sorted(optional), ALIASES))
    for col, value in cells.items():
        if col not in mapping.values() and value and str(value) not in mapping:
            mapping[str(value)] = col
    return mapping


# Only semantically identifiable fields are relocated. Unknown work-count columns
# are omitted, so existing calculators report insufficient support instead of
# interpreting an unrelated column as a charge quantity.
CLAIM = {
    '청구내역': {
        'required': {'G', 'L', 'M', 'N'}, 'header': 19, 'data': 20,
        'labels': {'C':'구분','G':'항목','J':'단위','L':'단가','M':'수량','N':'금액'},
        'aliases': {'C':['비용구분','대분류','category'], 'G':['청구항목','비용항목','항목명','item'],
                    'J':['과금단위','규격단위','unit'], 'L':['기재단가','unit price'],
                    'M':['청구수량','qty','quantity'], 'N':['청구금액','청구액','공급가액','amount']}},
    '입고리스트': {
        'required': {'A','B','D','H'}, 'header': 2, 'data': 4,
        'labels': {'A':'입고일자','B':'입고예정번호','D':'품번','E':'품명','H':'납품수량','I':'내품수량'},
        'aliases': {'A':['입고일','receipt date'],'B':['입고번호','receipt id'],
                    'D':['상품코드','sku'],'E':['상품명','item name'],
                    'H':['입고수량','상품수량','quantity'],'I':['박스당수량','ea per box']}},
    'B2B출고': {
        'required': {'A','D','G'}, 'header': 2, 'data': 4,
        'labels': {'A':'ERP','C':'채널명','D':'출고일','G':'수량'},
        'aliases': {'A':['주문번호','order id'],'C':['거래처','channel'],
                    'D':['출고일자','ship date'],'G':['출고수량','상품수량','quantity']}},
    'B2C출고_택배': {
        'required': {'A','C','D'}, 'header': 2, 'data': 3,
        'labels': {'A':'주문번호','B':'운송 방법','C':'송장번호','D':'금액'},
        'aliases': {'A':['order id'],'B':['운송방법'],'C':['운송장번호','tracking number'],
                    'D':['운임','청구금액','amount']}},
}


def discover(path, sheet_names, read_rows, wanted):
    """Return canonical views plus reversible source coordinates.

    Established SF sheets stay byte-for-byte-equivalent in memory. New or
    shuffled headers are mapped only when a section has exactly one candidate.
    """
    originals = {}; candidates = {s: [] for s in CLAIM}; observations = []; ambiguous = {}
    for sheet in sheet_names:
        if sheet in wanted:
            data = dict(read_rows(path, sheet)); originals[sheet] = data
            first = [(n, c) for n, c in data.items() if n <= 50]
        else:
            first = []
            for n, c in read_rows(path, sheet):
                if n > 50: break
                first.append((n,c))
        for section, spec in CLAIM.items():
            aliases = {spec['labels'][c]: spec['aliases'].get(c,[]) for c in spec['labels']}
            for n, cells in first:
                labels = {col: cell.get('value') for col, cell in cells.items()}
                try:
                    mapped = header_map(labels, list(aliases), aliases)
                except ValueError as exc:
                    # A duplicate header in an otherwise recognisable layout must
                    # not fall back to fixed columns, even if the sheet name matches.
                    present = {c for c,label in spec['labels'].items() if any(
                        key(v) in {key(label), *(key(a) for a in spec['aliases'].get(c,[]))}
                        for v in labels.values())}
                    if spec['required'] <= present:
                        ambiguous[section] = f'{sheet}!{n}: {exc}'
                    continue
                bycol = {c: mapped[label] for c,label in spec['labels'].items() if label in mapped}
                # Original SF summary doesn't repeat category/item headers on row 19.
                original_summary = section == sheet == '청구내역' and n < 20 and all(
                    key(labels.get(c)) in {key(spec['labels'][c]), *(key(x) for x in spec['aliases'].get(c,[]))}
                    for c in ('L','M','N'))
                if spec['required'] <= bycol.keys() or original_summary:
                    candidates[section].append((sheet,n,bycol,original_summary)); break
    sheets = dict(originals); mappings = {}
    for section,spec in CLAIM.items():
        if section in ambiguous:
            sheets.pop(section,None)
            observations.append({'section':section,'status':'ambiguous','reason':ambiguous[section]})
            continue
        options = candidates[section]
        if len(options) != 1:
            if len(options) > 1:
                sheets.pop(section,None)
                observations.append({'section':section,'status':'ambiguous','reason':'같은 업무의 시트·헤더 후보가 여러 개라 자동 선택하지 않음',
                                     'candidates':[f'{s}!{n}' for s,n,_,_ in options]})
            continue
        sheet,header,cols,original_summary = options[0]
        unchanged = sheet == section and (original_summary or
            header == spec['header'] and all(cols.get(c) == c for c in spec['required']))
        if unchanged:
            continue
        data = originals.get(sheet)
        if data is None: data = dict(read_rows(path,sheet))
        offset = spec['data'] - (header+1)
        normal = {spec['header']:{c:{'value':spec['labels'][c]} for c in cols}}
        normal.update({n+offset:{c:deepcopy(cells[src]) for c,src in cols.items() if src in cells}
                       for n,cells in data.items() if n>header})
        sheets[section] = normal
        mappings[section] = {'sheet':sheet,'header':header,'offset':offset,'columns':cols}
        observations.append({'section':section,'status':'mapped','sheet':sheet,'header':header,
            'columns':{spec['labels'][c]:col for c,col in cols.items()}})
    return sheets, mappings, observations


def location(value, mappings):
    """Map cell-by-cell, never turn shuffled columns into a false contiguous range."""
    if not isinstance(value,str): return value
    from invoice_tools import column
    def colno(s):
        v=0
        for c in s: v=v*26+ord(c)-64
        return v
    for canonical, spec in mappings.items():
        pattern = re.escape(canonical) + r'!([A-Z]*)(\d+)(?::([A-Z]*)(\d+))?'
        def replace(m):
            c1,n1,c2,n2 = m.groups(); n1=int(n1)-spec['offset']; n2=int(n2)-spec['offset'] if n2 else n1
            if not c1: return f"{spec['sheet']}!{n1}" + (f':{n2}' if n1!=n2 else '')
            cs = [column(i) for i in range(colno(c1),colno(c2 or c1)+1)]
            actual = [spec['columns'][c] for c in cs if c in spec['columns']]
            if not actual: return f"{spec['sheet']}!{n1}"  # row exists; no invented column
            return '; '.join(f"{spec['sheet']}!{c}{n1}"+(f':{c}{n2}' if n2!=n1 else '') for c in actual)
        value=re.sub(pattern,replace,value)
    return value


def restore_result(result, mappings):
    def walk(v):
        if isinstance(v,dict): return {k:(x if k in ('WMS위치','계약위치','layout_observations') else walk(x)) for k,x in v.items()}
        if isinstance(v,list): return [walk(x) for x in v]
        return location(v,mappings)
    physical_refs={r['id']:r.get('계산근거','') for r in result['checks'] if r.get('_physical_formula_refs')}
    result=walk(result)
    for r in result['checks']:
        if r.pop('_physical_formula_refs',False):r['계산근거']=physical_refs[r['id']]
    spec=mappings.get('청구내역')
    if spec:
        for item in result['fee_items']:
            item['canonical_row']=item['row']; item['row']-=spec['offset']
            item['source_sheet']=spec['sheet']; item['source_columns']=spec['columns']
            item['source_location']=location(f"청구내역!L{item['canonical_row']}:N{item['canonical_row']}",mappings)
        # Cost evidence uses invoice row identities for subsequent supporting documents.
        for collection in ('cost_evidence','recurring_rules'):
            for item in result.get(collection,[]):
                for k in ('row','invoice_row','claim_row'):
                    if isinstance(item.get(k),int): item[k]-=spec['offset']
    return result


def fee_location(item):
    return item.get('source_location') or f"청구내역!L{item['row']}:N{item['row']}"
