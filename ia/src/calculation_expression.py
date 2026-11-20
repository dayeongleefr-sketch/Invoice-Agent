"""Small Decimal expression language. No Python evaluation, IO or dynamic calls."""
import ast
from expression_functions import ARITIES, invoke
from decimal import Decimal, InvalidOperation, localcontext, ROUND_HALF_UP, ROUND_FLOOR, ROUND_CEILING

FUNCTIONS={'IF':3,'ABS':1,'ROUND':2,'FLOOR':1,'CEIL':1,'CONTAINS':2,'STARTSWITH':2}
ALLOWED=(ast.Expression,ast.Constant,ast.Name,ast.Load,ast.BinOp,ast.UnaryOp,ast.BoolOp,
         ast.Compare,ast.Call,ast.List,ast.Add,ast.Sub,ast.Mult,ast.Div,ast.FloorDiv,ast.Mod,
         ast.UAdd,ast.USub,ast.Not,ast.And,ast.Or,ast.Eq,ast.NotEq,ast.Lt,ast.LtE,ast.Gt,ast.GtE)

def compile_expression(text, variables):
    if not isinstance(text,str) or not text.strip() or len(text)>1000:
        raise ValueError('조건·수식은 1~1,000자로 입력해 주세요.')
    try: tree=ast.parse(text.strip(),mode='eval')
    except (SyntaxError,RecursionError) as exc: raise ValueError('조건·수식의 괄호와 연산자를 확인해 주세요.') from exc
    nodes=list(ast.walk(tree))
    if len(nodes)>150:raise ValueError('수식이 너무 복잡합니다. 나누어 등록해 주세요.')
    def depth(n):return 1+max((depth(c) for c in ast.iter_child_nodes(n)),default=0)
    if depth(tree)>24:raise ValueError('수식의 괄호 중첩이 너무 깊습니다.')
    for n in nodes:
        if not isinstance(n,ALLOWED):raise ValueError('지원하지 않는 수식 구문입니다.')
        if isinstance(n,ast.Constant) and type(n.value) not in (str,int,float,bool):raise ValueError('지원하지 않는 값입니다.')
        if isinstance(n,ast.List) and not 1<=len(n.elts)<=100:raise ValueError('범위는 값 1~100개로 지정해 주세요.')
        if isinstance(n,ast.Name) and n.id not in set(variables)|set(FUNCTIONS)|set(ARITIES)|{'MIN','MAX'}:
            raise ValueError('사용할 수 없는 변수·함수: '+n.id)
        if isinstance(n,ast.Call):
            if not isinstance(n.func,ast.Name) or n.keywords:raise ValueError('지정된 함수만 사용할 수 있습니다.')
            f=n.func.id
            if not ((f in FUNCTIONS and len(n.args)==FUNCTIONS[f]) or (f in ('MIN','MAX') and 1<=len(n.args)<=20)
                    or (f in ARITIES and ARITIES[f][0]<=len(n.args)<=ARITIES[f][1])):
                raise ValueError('함수 이름·인수 개수를 확인해 주세요: '+f)
    return tree,text.strip()

def evaluate(text, values, *, condition=False, variables=None):
    tree,source=compile_expression(text,variables if variables is not None else values)
    def number(v):
        if type(v) is not Decimal or not v.is_finite() or abs(v)>Decimal('1e30') or v.adjusted() < -30 and v!=0:
            raise ValueError('숫자 값이 없거나 계산 범위를 벗어났습니다.')
        return v
    def boolean(v):
        if type(v) is not bool:raise ValueError('조건은 비교식 또는 True/False로 입력해 주세요.')
        return v
    def run(n):
        if isinstance(n,ast.List):return [run(v) for v in n.elts]
        if isinstance(n,ast.Constant):
            return number(Decimal(ast.get_source_segment(source,n))) if type(n.value) in (int,float) else n.value
        if isinstance(n,ast.Name):
            if n.id not in values or values[n.id] is None:raise ValueError('원본에서 값을 확인할 수 없습니다: '+n.id)
            return values[n.id]
        if isinstance(n,ast.UnaryOp):
            v=run(n.operand)
            return not boolean(v) if isinstance(n.op,ast.Not) else number(-number(v) if isinstance(n.op,ast.USub) else number(v))
        if isinstance(n,ast.BoolOp):
            for child in n.values:
                b=boolean(run(child))
                if isinstance(n.op,ast.And) and not b:return False
                if isinstance(n.op,ast.Or) and b:return True
            return isinstance(n.op,ast.And)
        if isinstance(n,ast.Compare):
            a=run(n.left)
            for op,child in zip(n.ops,n.comparators):
                b=run(child)
                if type(a)!=type(b):raise ValueError('조건에서 문자와 숫자를 섞어 비교할 수 없습니다.')
                if isinstance(a,Decimal):number(a);number(b)
                result=({ast.Eq:lambda:a==b,ast.NotEq:lambda:a!=b,ast.Lt:lambda:a<b,
                         ast.LtE:lambda:a<=b,ast.Gt:lambda:a>b,ast.GtE:lambda:a>=b}[type(op)])()
                if not result:return False
                a=b
            return True
        if isinstance(n,ast.BinOp):
            a,b=number(run(n.left)),number(run(n.right))
            return number({ast.Add:lambda:a+b,ast.Sub:lambda:a-b,ast.Mult:lambda:a*b,
                ast.Div:lambda:a/b,ast.FloorDiv:lambda:(a/b).to_integral_value(rounding=ROUND_FLOOR),
                ast.Mod:lambda:a-b*(a/b).to_integral_value(rounding=ROUND_FLOOR)}[type(n.op)]())
        if isinstance(n,ast.Call):
            f=n.func.id
            if f=='IF':return run(n.args[1] if boolean(run(n.args[0])) else n.args[2])
            args=[run(a) for a in n.args]
            if f in ARITIES:return invoke(f,args,number,boolean)
            if f in ('CONTAINS','STARTSWITH'):
                if any(type(a) is not str for a in args):raise ValueError('문자 조건에는 문자 값이 필요합니다.')
                return args[1] in args[0] if f=='CONTAINS' else args[0].startswith(args[1])
            args=[number(a) for a in args]
            if f=='ROUND':
                if args[1]!=args[1].to_integral_value() or abs(args[1])>10:raise ValueError('반올림 자릿수는 -10~10의 정수입니다.')
                return number(args[0].quantize(Decimal(1).scaleb(int(-args[1])),rounding=ROUND_HALF_UP))
            return number({'MIN':lambda:min(args),'MAX':lambda:max(args),'ABS':lambda:abs(args[0]),
                'FLOOR':lambda:args[0].to_integral_value(rounding=ROUND_FLOOR),
                'CEIL':lambda:args[0].to_integral_value(rounding=ROUND_CEILING)}[f]())
        raise ValueError('지원하지 않는 수식입니다.')
    try:
        with localcontext() as ctx:
            ctx.prec=50
            result=run(tree.body)
            return boolean(result) if condition else number(result)
    except (ArithmeticError,InvalidOperation) as exc:raise ValueError('계산할 수 없습니다. 0으로 나누기·숫자 범위를 확인해 주세요.') from exc
