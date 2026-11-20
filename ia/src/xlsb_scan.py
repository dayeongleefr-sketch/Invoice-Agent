"""Cached-value reader using pinned pyxlsb handlers; skips formatted blank cells."""
import struct,io
from pyxlsb import biff12
from pyxlsb.reader import BIFF12Reader, RecordReader
from invoice_tools import column

def scan_rows(book, name):
    if hasattr(book,"xlsx_path"):
        from portable_workbook import read_rows
        yield from read_rows(book,name);return

    target=next(t for n,t in book._sheets if n==name)
    data=book._zf.read('xl/'+target);p=0;row=None;values={}
    while p<len(data):
        kind=0
        for shift in range(0,32,8):
            b=data[p];p+=1;kind|=b<<shift
            if not b&128:break
        else:raise ValueError('invalid_record_id')
        size=0
        for shift in range(0,28,7):
            b=data[p];p+=1;size|=(b&127)<<shift
            if not b&128:break
        else:raise ValueError('invalid_record_size')
        end=p+size
        if end>len(data):raise ValueError('truncated_record')
        if kind==biff12.ROW:
            if values:yield row,values
            row=struct.unpack_from('<I',data,p)[0]+1;values={}
        elif biff12.NUM<=kind<=biff12.FORMULA_BOOLERR:
            if row is None:raise ValueError('cell_before_row')
            with RecordReader(data[p:end]) as reader:
                cell=BIFF12Reader.handlers[kind].read(reader,kind,size)
            value=book.stringtable[cell.v] if kind==biff12.STRING else cell.v
            if value is not None:
                values[column(cell.c+1)]={'value':value,'row':row,
                    'error':kind in (biff12.BOOLERR,biff12.FORMULA_BOOLERR),
                    'formula_cached':kind in (8,9,10,11)}
        elif kind==biff12.SHEETDATA_END:break
        p=end
    if values:yield row,values

def subtotal_expression(tokens):
    """Recognize only PtgInt(9)+PtgArea+PtgFuncVar(SUBTOTAL), not arbitrary formulas.

    MS-XLSB ListTotalRowFunction / BrtFmlaNum specify this token layout.
    https://learn.microsoft.com/en-us/openspecs/office_file_formats/ms-xlsb/d22dc6ae-e349-41b0-9e4d-3efc734af4ce
    """
    if len(tokens)!=20 or tokens[:3]!=bytes.fromhex('1e0900') or tokens[3]!=0x25 or tokens[-4:]!=bytes.fromhex('42025801'):return None
    first,last,c1,c2=struct.unpack_from('<IIHH',tokens,4)
    c1&=0x3fff;c2&=0x3fff
    return {'function':'SUBTOTAL','function_num':9,'first_row':first+1,'last_row':last+1,
            'first_column':column(c1+1),'last_column':column(c2+1),
            'formula':f'SUBTOTAL(9,{column(c1+1)}{first+1}:{column(c2+1)}{last+1})'}

def filter_subtotals(book,name):
    if hasattr(book,"xlsx_path"):
        from portable_workbook import filter_subtotals as read
        return read(book,name)

    """Read simple text filters and a deliberately narrow subtotal formula pattern."""
    target=next(t for n,t in book._sheets if n==name)
    reader=BIFF12Reader(io.BytesIO(book._zf.read('xl/'+target)))
    row=0;range_=None;filter_=None;filters=[];formulas={}
    while True:
        kind=reader.read_id()
        if kind is None:break
        size=reader.read_len();raw=reader._fp.read(size)
        if len(raw)!=size:raise ValueError('truncated_filter_record')
        if kind==biff12.ROW:row=struct.unpack_from('<I',raw)[0]+1
        elif kind==biff12.FORMULA_FLOAT and row==1:
            col=struct.unpack_from('<I',raw)[0]+1;cce=struct.unpack_from('<I',raw,18)[0]
            formula=subtotal_expression(raw[22:22+cce])
            if formula:formulas[column(col)+str(row)]=dict(formula,saved_value=struct.unpack_from('<d',raw,8)[0])
        elif kind==biff12.AUTOFILTER:range_=struct.unpack('<IIII',raw)
        elif kind==biff12.FILTERCOLUMN:
            filter_={'relative_column':struct.unpack_from('<I',raw)[0],'criteria':[]};filters.append(filter_)
        elif kind==biff12.FILTER and filter_ is not None:
            length=struct.unpack_from('<I',raw)[0];filter_['criteria'].append(raw[4:4+length*2].decode('utf-16le'))
    reader.close()
    if range_:
        for f in filters:f['column']=column(range_[2]+f['relative_column']+1)
    return {'range':list(range_) if range_ else None,'filters':filters,'subtotal_cells':formulas,
            'scope':'단순 텍스트 필터와 SUBTOTAL(9,단일범위)만 해석. 전체 수식 계산 엔진 아님'}
