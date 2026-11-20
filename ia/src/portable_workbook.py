"""Read XLSX and XLSB through the existing tool interface; inputs remain real files."""
from pathlib import Path
import zipfile
from xml.etree import ElementTree as ET

NS='{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
RID='{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id'


class Sheet:
    def __init__(self, book, name):self.xlsx_path=book.xlsx_path;self.name=name
    def __enter__(self):return self
    def __exit__(self,*args):pass


class Workbook:
    def __init__(self,path):
        self.xlsx_path=Path(path);self._zf=zipfile.ZipFile(path)
        relations={r.attrib['Id']:r.attrib['Target'] for r in ET.fromstring(self._zf.read('xl/_rels/workbook.xml.rels'))}
        self._sheets=[(s.attrib['name'],relations[s.attrib[RID]]) for s in ET.fromstring(self._zf.read('xl/workbook.xml')).find(NS+'sheets')]
        self.sheets=[n for n,t in self._sheets]
    def get_sheet(self,name):
        if isinstance(name,int):name=self.sheets[name-1]
        if name not in self.sheets:raise ValueError('sheet_missing:'+name)
        return Sheet(self,name)
    def __enter__(self):return self
    def __exit__(self,*args):self._zf.close()
    def xml(self,name):
        target=next(t for n,t in self._sheets if n==name)
        return ET.fromstring(self._zf.read(target.lstrip('/') if target.startswith('/') else 'xl/'+target))


def open_workbook(path,*args,**kwargs):
    if Path(path).suffix.lower() in {'.xlsx','.xlsm'}:return Workbook(path)
    from pyxlsb import open_workbook as binary
    return binary(path,*args,**kwargs)


def read_rows(book,name):
    from wms_tools import xlsx_rows
    # Legacy invoice code uses int/float type checks for stated numeric cells.
    # Match the legacy XLSB numeric interface; integral identifiers stay integers.
    from decimal import Decimal
    for n,row in xlsx_rows(book.xlsx_path,name):
        for cell in row.values():
            v=cell['value']
            if isinstance(v,Decimal):cell['value']=int(v) if v==v.to_integral_value() else float(v)
        yield n,row


def read_cells(sheet):
    for n,row in read_rows(sheet,sheet.name):
        for col,cell in row.items():yield col+str(n),cell


def merged_ranges(book,name):
    import re
    def ref(value):
        m=re.fullmatch(r'([A-Z]+)(\d+)',value);col=0
        for c in m[1]:col=col*26+ord(c)-64
        return int(m[2]),col
    out=[]
    for entry in book.xml(name).iter(NS+'mergeCell'):
        first,last=map(ref,entry.attrib['ref'].split(':'));out.append((first[0],last[0],first[1],last[1]))
    return out


def filter_subtotals(book,name):
    # The independent synthetic files contain no filters or subtotal formulas.
    # Do not silently claim support for filtered third-party XLSX layouts here.
    xml=book.xml(name)
    if xml.find(NS+'autoFilter') is not None or any('SUBTOTAL' in (f.text or '').upper() for f in xml.iter(NS+'f')):
        raise ValueError('XLSX filter/subtotal layout requires explicit adapter support')
    return {'range':None,'filters':[],'subtotal_cells':{},'scope':'원본 XLSX에 필터·SUBTOTAL 수식 없음 확인'}


def formula_links(book,name):
    formulas={c.attrib.get('r'):c.find(NS+'f') for c in book.xml(name).iter(NS+'c')};out={}
    import re
    for row in (79,111):
        formula=formulas.get('N'+str(row))
        if formula is None or formula.attrib.get('t') in ('shared','array'):continue
        text=formula.text or ''
        m=re.fullmatch(r'=?\s*([A-Z]+\d+|\d+(?:\.\d+)?)\s*\*\s*([A-Z]+\d+|\d+(?:\.\d+)?)\s*',text)
        if m:out[row]=[{'cell':v} if re.fullmatch(r'[A-Z]+\d+',v) else {'constant':v} for v in m.groups()]
    return out
