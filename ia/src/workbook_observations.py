"""Recalculate stored amount formulas from physical cells; never run VBA."""
import zipfile
import re
from monthly_formula_tools import read_formulas,evaluate,UnsupportedFormula,MissingValue


class AmountFormulas:
    def __init__(self,path,sheet_names,reader):
        self.path=path;self.names=sheet_names;self.reader=reader
        self.formulas=read_formulas(path,sheet_names);self.sheets={}

    def compare(self,sheet,ref):
        node=self.formulas.get((sheet,ref))
        if node is None:return None
        seen=set()
        def load(n):
            if not isinstance(n,tuple):return
            if n[0] in ('ref','range'):
                sh=n[1]
                if sh in self.names and sh not in self.sheets:self.sheets[sh]=dict(self.reader(self.path,sh))
                # Include formula dependencies in the referenced range as well.
                def pos(cell):
                    m=re.fullmatch(r'([A-Z]+)(\d+)',cell);c=0
                    for ch in m[1]:c=c*26+ord(ch)-64
                    return c,int(m[2])
                c1,r1=pos(n[2]);c2,r2=pos(n[3] if n[0]=='range' else n[2])
                for key,sub in self.formulas.items():
                    if key[0]!=sh or key in seen:continue
                    c,r=pos(key[1])
                    if c1<=c<=c2 and r1<=r<=r2:
                        seen.add(key)
                        if not isinstance(sub,str):load(sub)
            else:
                for child in n[1:]:
                    if isinstance(child,list):
                        for c in child:load(c)
                    else:load(child)
        try:
            if isinstance(node,str):raise UnsupportedFormula(node)
            if sheet not in self.sheets:self.sheets[sheet]=dict(self.reader(self.path,sheet))
            load(node)
            result=evaluate(node,self.sheets,self.formulas,origin=(sheet,ref))
            return dict(status='calculated',**result)
        except UnsupportedFormula as exc:return {'status':'not_implemented','reason':str(exc)}
        except (MissingValue,RecursionError) as exc:return {'status':'data_unavailable','reason':str(exc)}


def macro_observation(path):
    with zipfile.ZipFile(path) as book:
        if any(n.lower().endswith('vbaproject.bin') or '/macrosheets/' in n.lower() for n in book.namelist()):
            return {'section':'매크로','status':'not_implemented',
                    'reason':'매크로가 있는 파일입니다. 저장된 셀 값과 지원 수식은 읽지만 매크로 실행·생성 결과의 재현은 수행하지 않습니다.'}
    return None
