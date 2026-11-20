"""Read-only, bounded invoice formula observations; never execute Excel/macros.

XLSB reference layout: MS-XLSB 2.5.173 / 2.5.98 (Ptgs), 2.4.672.
https://learn.microsoft.com/en-us/openspecs/office_file_formats/ms-xlsb/741826f2-e174-43f8-b099-f97cdf360dee
XLSB supports bounded SUM/COUNTA and arithmetic; XLSX additionally supports
explicit conditional, lookup, rounding and date functions from expression_functions.
Unknown functions, external links and error values fail closed. XLSB shared
references are resolved from the file; no workbook is changed or recalculated.
"""
from decimal import Decimal
import io
import re
import struct
import zipfile
from lxml import etree as ET
from portable_workbook import open_workbook
from pyxlsb import biff12
from pyxlsb.reader import BIFF12Reader
from invoice_tools import column
from identifier_tools import stored_number
from expression_functions import ARITIES, invoke


class UnsupportedFormula(ValueError):
    pass


class MissingValue(ValueError):
    pass


def records(data):
    reader = BIFF12Reader(io.BytesIO(data))
    try:
        while True:
            kind = reader.read_id()
            if kind is None:
                return
            size = reader.read_len()
            raw = reader._fp.read(size)
            if len(raw) != size:
                raise ValueError('잘린 XLSB 수식 레코드')
            yield kind, raw
    finally:
        reader.close()


def local_sheets(book):
    links, xti = [], []
    for kind, raw in records(book._zf.read('xl/workbook.bin')):
        # pyxlsb keeps the continuation bit in its record IDs.
        if kind in (0x2e3, 0x2e5, 0x2e6, 0x59b):
            links.append(kind)
        elif kind == 0x2ea:
            count = struct.unpack_from('<I', raw)[0]
            if len(raw) != 4 + count * 12:
                raise ValueError('잘린 XLSB 시트 참조')
            xti = [struct.unpack_from('<Iii', raw, 4 + n * 12) for n in range(count)]
    return {n: book.sheets[first] for n, (link, first, last) in enumerate(xti)
            if link < len(links) and links[link] == 0x2e5
            and first == last and 0 <= first < len(book.sheets)}


def binary_expression(data, sheet, sheet_links, origin=None):
    stack, p = [], 0
    def pop():
        if not stack:
            raise UnsupportedFormula('수식 피연산자 없음')
        return stack.pop()
    try:
        while p < len(data):
            token = data[p]; p += 1
            base = token & 0x1f
            if token >= 0x20 and base in (4, 5, 12, 13, 26, 27):
                target = sheet
                if base in (26, 27):
                    i = struct.unpack_from('<H', data, p)[0]; p += 2
                    if i not in sheet_links:
                        raise UnsupportedFormula('외부 파일·여러 시트 참조')
                    target = sheet_links[i]
                def location(row, col):
                    if base in (12, 13):
                        if origin is None: raise UnsupportedFormula('상대 수식 기준 셀 없음')
                        if col & 0x8000: row = origin[0] + (row - 1048576 if row & 0x80000 else row)
                        offset = col & 0x3fff
                        if col & 0x4000: offset = origin[1] + (offset - 16384 if offset & 0x2000 else offset)
                        col = offset
                    else: col &= 0x3fff
                    if not 0 <= row < 1048576 or not 0 <= col < 16384: raise UnsupportedFormula('상대 참조 범위 밖')
                    return column(col+1)+str(row+1)
                if base in (4, 12, 26):
                    row, col = struct.unpack_from('<IH', data, p); p += 6
                    stack.append(('ref', target, location(row,col)))
                else:
                    first, last, c1, c2 = struct.unpack_from('<IIHH', data, p); p += 12
                    stack.append(('range', target, location(first,c1), location(last,c2)))
            elif token == 0x1e:
                stack.append(('number', Decimal(struct.unpack_from('<H', data, p)[0]))); p += 2
            elif token == 0x1f:
                value = stored_number(struct.unpack_from('<d', data, p)[0]); p += 8
                if value is None: raise UnsupportedFormula('유한 숫자가 아님')
                stack.append(('number', value))
            elif token in (3, 4, 5, 6):
                right, left = pop(), pop()
                stack.append(({3:'+',4:'-',5:'*',6:'/'}[token], left, right))
            elif token in (0x12, 0x13, 0x14):
                operand = pop()
                stack.append(('*', operand, ('number', Decimal(-1 if token == 0x13 else '.01' if token == 0x14 else 1))))
            elif token == 0x15:
                pass  # parentheses
            elif token == 0x19 and data[p] == 0x10:
                p += 3
                stack.append(('sum', [pop()]))
            elif token in (0x22, 0x42, 0x62):
                count, function = struct.unpack_from('<BH', data, p); p += 3
                if function not in (4,169) or count > len(stack) or count == 0:
                    raise UnsupportedFormula('SUM/COUNTA 이외 함수 또는 잘못된 인수')
                args = stack[-count:]; del stack[-count:]; stack.append(('sum' if function==4 else 'counta', args))
            else:
                raise UnsupportedFormula(f'지원하지 않는 수식 토큰 {token:02x}')
        if len(stack) != 1: raise UnsupportedFormula('수식 스택 오류')
        return stack[0]
    except (struct.error, IndexError) as exc:
        raise UnsupportedFormula('불완전한 수식') from exc


TOKEN = re.compile(r'\s*(?:(?P<ref>(?:\'(?:[^\']|\'\')+\'!|[\w가-힣]+!)?\$?[A-Z]{1,3}\$?\d+(?::\$?[A-Z]{1,3}\$?\d+)?)|(?P<number>(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?)|(?P<string>"(?:[^"]|"")*")|(?P<sum>(?:_xlfn\.)?[A-Z][A-Z0-9.]*)|(?P<op><=|>=|<>|[<>+*/(),%=-]))', re.I)


def text_expression(text, sheet):
    tokens, p = [], 0
    text = text.strip().lstrip('=')
    if len(text)>8000: raise UnsupportedFormula('수식 길이 초과')
    while p < len(text):
        match = TOKEN.match(text, p)
        if not match: raise UnsupportedFormula('지원하지 않는 XLSX 수식')
        tokens.append((match.lastgroup, match.group(match.lastgroup))); p = match.end()
        if len(tokens)>500:raise UnsupportedFormula('수식 토큰 범위 초과')
    depth=0
    for _,value in tokens:
        depth += (value=='(') - (value==')')
        if depth>40:raise UnsupportedFormula('수식 중첩 범위 초과')
    pos = 0
    def expression(minimum=0):
        nonlocal pos
        if pos >= len(tokens): raise UnsupportedFormula('불완전한 XLSX 수식')
        kind, v = tokens[pos]; pos += 1
        if v in ('+', '-'):
            node = ('*', ('number', Decimal(-1 if v == '-' else 1)), expression(3))
        elif v == '(':
            node = expression()
            if pos >= len(tokens) or tokens[pos][1] != ')': raise UnsupportedFormula('괄호 오류')
            pos += 1
        elif kind == 'number': node = ('number', Decimal(v))
        elif kind == 'string': node = ('text', v[1:-1].replace('""','"'))
        elif kind == 'ref':
            target, ref = v.rsplit('!', 1) if '!' in v else (sheet, v)
            target = target.strip("'").replace("''", "'"); ref = ref.replace('$', '').upper()
            node = ('range', target, *ref.split(':')) if ':' in ref else ('ref', target, ref)
        elif kind == 'sum':
            v=v.upper().removeprefix('_XLFN.')
            if v not in set(ARITIES)|{'SUM','COUNTA','IF','ROUND','ABS','MIN','MAX'}:
                raise UnsupportedFormula('지원하지 않는 XLSX 함수: '+v)
            if pos >= len(tokens) or tokens[pos][1] != '(': raise UnsupportedFormula('SUM 인수 없음')
            pos += 1; args = [expression()]
            while pos < len(tokens) and tokens[pos][1] == ',':
                pos += 1; args.append(expression())
            if pos >= len(tokens) or tokens[pos][1] != ')': raise UnsupportedFormula('SUM 괄호 오류')
            pos += 1
            if len(args)>40: raise UnsupportedFormula('함수 인수 40개 초과')
            if v in ARITIES and not ARITIES[v][0]<=len(args)<=ARITIES[v][1]:raise UnsupportedFormula('함수 인수 오류: '+v)
            if v in ('IF','ROUND','ABS') and len(args)!={'IF':3,'ROUND':2,'ABS':1}[v]:raise UnsupportedFormula('함수 인수 오류: '+v)
            node = (v.lower(), args) if v in ('SUM','COUNTA') else ('function',v,args)
        else: raise UnsupportedFormula('수식 값 오류')
        while pos < len(tokens):
            op = tokens[pos][1]
            if op == '%':
                pos += 1; node = ('*', node, ('number', Decimal('.01'))); continue
            precedence = {'=':0,'<>':0,'<':0,'>':0,'<=':0,'>=':0,'+':1,'-':1,'*':2,'/':2}.get(op, -1)
            if precedence < minimum: break
            pos += 1; node = (op, node, expression(precedence + 1))
        return node
    node = expression()
    if pos != len(tokens): raise UnsupportedFormula('미처리 수식')
    return node


def read_formulas(path, sheets):
    out = {}
    def save(sheet, ref, loader):
        try: out[sheet, ref] = loader()
        except UnsupportedFormula as exc: out[sheet, ref] = str(exc)
    if path.suffix.lower() == '.xlsb':
        with open_workbook(str(path)) as book:
            links = local_sheets(book)
            for sheet, target in book._sheets:
                if sheet not in sheets: continue
                row = 0; shared = []; pointers = {}
                for kind, raw in records(book._zf.read('xl/' + target)):
                    if kind == biff12.ROW: row = struct.unpack_from('<I', raw)[0] + 1
                    elif kind == biff12.FORMULA_FLOAT:
                        col = struct.unpack_from('<I', raw)[0] + 1
                        length = struct.unpack_from('<I', raw, 18)[0]
                        if length == 5 and raw[22] == 1 and len(raw) >= 35:
                            anchor_row = struct.unpack_from('<I',raw,23)[0]
                            extra_len = struct.unpack_from('<I',raw,27)[0]
                            if extra_len == 4:
                                pointers[row-1,col-1] = (anchor_row,struct.unpack_from('<I',raw,31)[0])
                        save(sheet, column(col) + str(row), lambda: binary_expression(raw[22:22+length], sheet, links))
                    elif kind == 0x3ab:
                        r1,r2,c1,c2,length = struct.unpack_from('<IIIII',raw)
                        if len(raw) < 20+length: raise ValueError('잘린 공유 수식')
                        shared.append((r1,r2,c1,c2,raw[20:20+length]))
                    elif kind == biff12.SHEETDATA_END: break
                for r1,r2,c1,c2,data in shared:
                    for (r,c),anchor in pointers.items():
                        if anchor == (r1,c1) and r1<=r<=r2 and c1<=c<=c2:
                            save(sheet,column(c+1)+str(r+1),lambda: binary_expression(data,sheet,links,(r,c)))
    else:
        ns = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
        with zipfile.ZipFile(path) as z:
            rels = {e.get('Id'):e.get('Target') for e in ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))}
            for s in ET.fromstring(z.read('xl/workbook.xml')).find(ns+'sheets'):
                sheet = s.get('name')
                if sheet not in sheets: continue
                rid = s.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id')
                target = rels[rid]; target = target.lstrip('/') if target.startswith('/') else 'xl/'+target
                for _, c in ET.iterparse(io.BytesIO(z.read(target)), events=('end',), tag=ns+'c'):
                    f = c.find(ns+'f')
                    if f is not None:
                        if f.get('t') in ('shared','array'):
                            out[sheet,c.get('r')] = '공유·배열 수식은 지원 범위 밖'
                        else: save(sheet, c.get('r'), lambda: text_expression(f.text or '', sheet))
                    c.clear()
    return out


def evaluate(formula, sheets, formulas, *, origin=None):
    refs, constants, active, blank_refs, ignored_text_refs = [], [], set(), [], []
    budget = [0]
    def address(ref):
        match = re.fullmatch(r'([A-Z]{1,3})([1-9]\d*)', ref)
        if not match: raise UnsupportedFormula('셀 주소 오류')
        col = 0
        for ch in match[1]: col = col * 26 + ord(ch) - 64
        if col > 16384 or int(match[2]) > 1048576: raise UnsupportedFormula('셀 범위 초과')
        return col, int(match[2])
    def cell(sheet, ref, in_sum=False, literal=False):
        key = (sheet, ref)
        if key == origin or key in active: raise UnsupportedFormula('순환 참조')
        if sheet not in sheets: raise MissingValue('청구서 참조 시트 없음: '+sheet)
        col, row = address(ref); v = sheets[sheet].get(row, {}).get(column(col), {})
        refs.append(f'{sheet}!{ref}')
        if v.get('error'): raise MissingValue('청구서 오류 셀: '+refs[-1])
        if key in formulas:
            if isinstance(formulas[key], str): raise UnsupportedFormula(formulas[key])
            active.add(key)
            try: return visit(formulas[key])
            finally: active.remove(key)
        n = stored_number(v.get('value'))
        if literal and n is None:return v.get('value')
        if n is None:
            if v.get('value') is None:
                blank_refs.append(refs[-1]); return Decimal(0)  # Excel empty-cell arithmetic, recorded explicitly
            if in_sum:
                ignored_text_refs.append(refs[-1]);return Decimal(0)  # SUM reference text is not a work quantity
            raise MissingValue('청구서 숫자 없음: '+refs[-1])
        return n
    def visit(node, in_sum=False):
        budget[0] += 1
        if budget[0] > 100000 or len(active) > 40: raise UnsupportedFormula('수식 분석 범위 초과')
        kind = node[0]
        if kind == 'number': constants.append(str(node[1])); return node[1]
        if kind == 'text': return node[1]
        if kind == 'function':
            name,args=node[1:]
            def num(v):
                if type(v) is not Decimal or not v.is_finite() or abs(v)>Decimal('1e30'):raise MissingValue('함수에 필요한 숫자가 없습니다.')
                return v
            def boolean(v):
                if type(v) is not bool:raise UnsupportedFormula('조건식의 참/거짓 값이 필요합니다.')
                return v
            def argument(n):
                if n[0]=='range':
                    c1,r1=address(n[2]);c2,r2=address(n[3])
                    if c2<c1 or r2<r1 or (c2-c1+1)*(r2-r1+1)>100:raise UnsupportedFormula('함수 범위는 100개 셀 이내입니다.')
                    return [cell(n[1],column(c)+str(r),literal=True) for r in range(r1,r2+1) for c in range(c1,c2+1)]
                if n[0]=='ref':return cell(n[1],n[2],literal=True)
                return visit(n)
            try:
                if name=='IF':return visit(args[1] if boolean(visit(args[0])) else args[2])
                values=[argument(a) for a in args]
                if name in ARITIES:return invoke(name,values,num,boolean)
                if name=='ABS':return abs(num(values[0]))
                if name in ('MIN','MAX'):
                    flat=[num(v) for arg in values for v in (arg if isinstance(arg,list) else [arg])]
                    return min(flat) if name=='MIN' else max(flat)
                if name=='ROUND':
                    from decimal import ROUND_HALF_UP
                    places=num(values[1])
                    if places!=places.to_integral_value() or abs(places)>10:raise ValueError('반올림 자릿수 범위 초과')
                    return num(values[0]).quantize(Decimal(1).scaleb(-int(places)),rounding=ROUND_HALF_UP)
            except ValueError as exc:raise MissingValue(str(exc)) from exc
            except ArithmeticError as exc:raise MissingValue('함수 숫자 범위 오류') from exc
        if kind == 'ref': return cell(node[1], node[2], in_sum)
        if kind == 'range':
            if not in_sum: raise UnsupportedFormula('SUM 밖 범위 연산')
            c1,r1 = address(node[2]); c2,r2 = address(node[3])
            if c2<c1 or r2<r1: raise UnsupportedFormula('범위 크기 오류')
            if node[1] not in sheets: raise MissingValue('청구서 참조 시트 없음: '+node[1])
            refs.append(f'{node[1]}!{node[2]}:{node[3]}')
            return sum((visit(('ref',node[1],c+str(r)), True)
                        for r,cells in sheets[node[1]].items() if r1<=r<=r2
                        for c in cells if c1<=address(c+str(r))[0]<=c2), Decimal(0))
        if kind == 'sum': return sum((visit(n,True) for n in node[1]),Decimal(0))
        if kind == 'counta':
            count = 0
            for arg in node[1]:
                if arg[0] not in ('ref','range'): raise UnsupportedFormula('COUNTA 참조 인수만 지원')
                sh=arg[1]
                if sh not in sheets: raise MissingValue('청구서 참조 시트 없음: '+sh)
                c1,r1=address(arg[2]); c2,r2=address(arg[3] if arg[0]=='range' else arg[2])
                if c1>c2 or r1>r2: raise UnsupportedFormula('범위 크기 오류')
                refs.append(sh+'!'+':'.join(arg[2:]))
                for r,cells in sheets[sh].items():
                    if r1<=r<=r2:
                        for c,stored in cells.items():
                            if c1<=address(c+str(r))[0]<=c2:
                                if stored.get('error'): raise MissingValue('청구서 오류 셀: '+sh+'!'+c+str(r))
                                if stored.get('value') is not None: count+=1
            return Decimal(count)
        if kind in ('=','<>','<','>','<=','>='):
            def operand(n):return cell(n[1],n[2],literal=True) if n[0]=='ref' else visit(n)
            a,b=operand(node[1]),operand(node[2])
            if type(a)!=type(b):raise MissingValue('비교 수식의 자료형이 다릅니다.')
            return {'=':lambda:a==b,'<>':lambda:a!=b,'<':lambda:a<b,'>':lambda:a>b,'<=':lambda:a<=b,'>=':lambda:a>=b}[kind]()
        a,b=visit(node[1]),visit(node[2])
        if type(a) is not Decimal or type(b) is not Decimal:raise MissingValue('사칙연산에 필요한 숫자가 없습니다.')
        if kind=='+': return a+b
        if kind=='-': return a-b
        if kind=='*': return a*b
        if kind=='/' and b: return a/b
        raise MissingValue('0으로 나누는 수식')
    try:amount = visit(formula)
    except (ArithmeticError,RecursionError) as exc:raise UnsupportedFormula('수식의 계산 범위를 초과했습니다.') from exc
    if type(amount) is not Decimal or not amount.is_finite():raise MissingValue('수식 결과가 유효한 숫자가 아닙니다.')
    return {'value':amount,'refs':list(dict.fromkeys(refs)), 'constants':constants,'blank_refs':blank_refs,
            'ignored_text_refs':ignored_text_refs,
            'physical_work_confirmed':False}
