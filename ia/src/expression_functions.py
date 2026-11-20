"""Bounded spreadsheet-style functions for confirmed formulas, without Excel/VBA."""
from datetime import date, timedelta
from decimal import Decimal, ROUND_DOWN, ROUND_UP
import re

ARITIES = {
    'SUM': (1, 40), 'AVERAGE': (1, 40), 'COUNT': (1, 40),
    'SUMIF': (2, 3), 'SUMIFS': (3, 39), 'COUNTIF': (2, 2), 'COUNTIFS': (2, 40),
    'XLOOKUP': (3, 4), 'ROUNDUP': (2, 2), 'ROUNDDOWN': (2, 2),
    'AND': (1, 40), 'OR': (1, 40), 'NOT': (1, 1),
    'DATE': (3, 3), 'YEAR': (1, 1), 'MONTH': (1, 1), 'DAY': (1, 1),
    'DAYS': (2, 2), 'EOMONTH': (2, 2),
}


def invoke(name, args, number, boolean):
    def vector(value):
        if not isinstance(value, list) or not 1 <= len(value) <= 100:
            raise ValueError('범위는 값 1~100개의 목록으로 지정해 주세요.')
        if any(isinstance(v, list) or v is None for v in value):
            raise ValueError('중첩 범위 또는 값이 없는 범위는 계산하지 않습니다.')
        return value

    def integer(value):
        v = number(value)
        if v != v.to_integral_value():
            raise ValueError('날짜·반올림 인수는 정수여야 합니다.')
        return int(v)

    def day(value):
        if type(value) is not str:
            raise ValueError('날짜는 YYYY-MM-DD 문자열로 지정해 주세요.')
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError('유효한 날짜가 아닙니다: ' + value) from exc

    def criterion(value, expected):
        if value is None:
            raise ValueError('조건 비교에 필요한 원본 값이 없습니다.')
        op = '='
        if isinstance(expected, str):
            match = re.fullmatch(r'(<=|>=|<>|=|<|>)(.*)', expected)
            if match:
                op, expected = match.groups()
                if isinstance(value, Decimal):
                    try:
                        expected = number(Decimal(expected))
                    except ArithmeticError as exc:
                        raise ValueError('숫자 조건의 값이 올바르지 않습니다.') from exc
        if type(value) is not type(expected):
            raise ValueError('조건 범위와 조건값의 자료형이 다릅니다.')
        if isinstance(value, Decimal):
            number(value); number(expected)
        return {'=': lambda: value == expected, '<>': lambda: value != expected,
                '<': lambda: value < expected, '>': lambda: value > expected,
                '<=': lambda: value <= expected, '>=': lambda: value >= expected}[op]()

    if name in ('AND', 'OR', 'NOT'):
        values = [boolean(v) for v in args]
        return not values[0] if name == 'NOT' else all(values) if name == 'AND' else any(values)
    if name in ('ROUNDUP', 'ROUNDDOWN'):
        places = integer(args[1])
        if abs(places) > 10:
            raise ValueError('반올림 자릿수는 -10~10의 정수입니다.')
        return number(number(args[0]).quantize(Decimal(1).scaleb(-places),
                      rounding=ROUND_UP if name == 'ROUNDUP' else ROUND_DOWN))
    if name in ('SUM', 'AVERAGE', 'COUNT'):
        flat = [v for arg in args for v in (vector(arg) if isinstance(arg, list) else [arg])]
        if name == 'COUNT':
            return Decimal(sum(isinstance(v, Decimal) for v in flat))
        values = [number(v) for v in flat]
        total = sum(values, Decimal(0))
        return number(total / len(values) if name == 'AVERAGE' else total)
    if name in ('SUMIF', 'SUMIFS', 'COUNTIF', 'COUNTIFS'):
        if name == 'SUMIF':
            sums = vector(args[2] if len(args) == 3 else args[0])
            pairs = [(vector(args[0]), args[1])]
        elif name == 'COUNTIF':
            sums = None; pairs = [(vector(args[0]), args[1])]
        else:
            offset = 1 if name == 'SUMIFS' else 0
            if (len(args) - offset) % 2:
                raise ValueError('조건 범위와 조건값을 쌍으로 지정해 주세요.')
            sums = vector(args[0]) if offset else None
            pairs = [(vector(args[i]), args[i+1]) for i in range(offset, len(args), 2)]
        size = len(pairs[0][0])
        if any(len(v) != size for v, _ in pairs) or sums is not None and len(sums) != size:
            raise ValueError('합계 범위와 조건 범위의 길이가 다릅니다.')
        flags = [[criterion(v, c) for v in vs] for vs, c in pairs]
        selected = [i for i in range(size) if all(f[i] for f in flags)]
        return number(sum((number(sums[i]) if sums is not None else Decimal(1) for i in selected), Decimal(0)))
    if name == 'XLOOKUP':
        keys, values = vector(args[1]), vector(args[2])
        if len(keys) != len(values):
            raise ValueError('조회 범위와 반환 범위의 길이가 다릅니다.')
        matches = [i for i, key in enumerate(keys) if type(key) is type(args[0]) and key == args[0]]
        if len(matches) > 1:
            raise ValueError('조회값이 여러 행에 있어 반환값을 확정할 수 없습니다.')
        if not matches:
            if len(args) == 4:
                return args[3]
            raise ValueError('조회값에 해당하는 행이 없습니다.')
        return values[matches[0]]
    try:
        if name == 'DATE':
            year, month, days = [integer(v) for v in args]
            if not 1900 <= year <= 9998 or not 1 <= month <= 12 or not 1 <= days <= 31:
                raise ValueError('날짜의 연·월·일 범위를 확인해 주세요.')
            return date(year, month, days).isoformat()
        if name in ('YEAR', 'MONTH', 'DAY'):
            return Decimal(getattr(day(args[0]), name.lower()))
        if name == 'DAYS':
            return Decimal((day(args[0]) - day(args[1])).days)
        if name == 'EOMONTH':
            base = day(args[0]); shift = integer(args[1])
            if abs(shift) > 1200:
                raise ValueError('월 이동은 1,200개월 이내로 지정해 주세요.')
            year, month = divmod(base.year * 12 + base.month - 1 + shift + 1, 12)
            return (date(year, month + 1, 1) - timedelta(days=1)).isoformat()
    except (OverflowError, ValueError) as exc:
        raise ValueError('날짜 계산 범위를 확인해 주세요.') from exc
    raise ValueError('지원하지 않는 함수: ' + name)
