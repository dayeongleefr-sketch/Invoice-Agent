"""Exact identifier display conversion; never invent missing significant digits."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, ROUND_HALF_UP, localcontext
from functools import lru_cache
import re

SCI = re.compile(r'^[+]?(\d+(?:\.\d+)?)[Ee]([+-]?\d+)$')


def stored_identifier_text(value):
    """Unwrap Excel text literals only; never evaluate a formula or use float."""
    text = str(value).strip()
    if text.startswith('="') and text.endswith('"') and '"' not in text[2:-1]:
        text = text[2:-1].strip()
    if text.startswith("'"):
        text = text[1:].strip()
    return text


def stored_number(value):
    """Exact numeric stored value, including numeric text; blanks/errors stay None."""
    if value is None or isinstance(value, bool):
        return None
    text = stored_identifier_text(value)
    if not re.fullmatch(r'[+-]?(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?(?:[Ee][+-]?[0-9]{1,3})?', text):
        return None
    try:
        value = Decimal(text.replace(',', ''))
        return value if value.is_finite() else None
    except InvalidOperation:
        return None


def billable_amount(value):
    number = stored_number(value)
    return number is not None and number != 0


def scientific_match(text):
    match = SCI.fullmatch(text)
    # Business IDs such as DEMO-E-EXAMPLE contain E but are not Excel
    # scientific notation. Require a decimal mantissa or signed exponent.
    if match and ('.' in match[1] or match[2].startswith(('+', '-'))):
        return match
    return None


def expand_identifier(value):
    if value is None or isinstance(value, bool):
        return ''
    text = stored_identifier_text(value)
    numeric = isinstance(value, (int, float, Decimal))
    return _expand_identifier_text(text, numeric)


@lru_cache(maxsize=32768)
def _expand_identifier_text(text, numeric):
    match = scientific_match(text)
    # CSV/text exports commonly retain the .0 of a numeric identifier. Strip
    # only a zero fractional part, keeping literal leading zeroes and prefixes.
    if not numeric and not match:
        integer = re.fullmatch(r'([+]?[0-9]+)\.0+', text)
        if integer:
            return integer[1].lstrip('+')
        grouped = re.fullmatch(r'([+]?[1-9][0-9]{0,2}(?:,[0-9]{3})+)(?:\.0+)?', text)
        if grouped:
            return grouped[1].replace(',', '').lstrip('+')
    if not numeric and not match:
        return text  # Preserve leading zeroes and alphanumeric identifiers.
    if match and abs(int(match[2])) > 100:
        raise ValueError('identifier_exponent_out_of_range')
    number = Decimal(text)
    if not number.is_finite():
        raise ValueError('non_finite_identifier')
    return str(int(number)) if number == number.to_integral_value() else format(number, 'f')


def scientific_info(value):
    text = stored_identifier_text(value)
    match = scientific_match(text)
    if not match:
        return None
    plain = expand_identifier(text)
    if not re.fullmatch(r'\d+', plain):
        return None
    fractional_digits = len(match[1].partition('.')[2])
    scale = int(match[2]) - fractional_digits
    return {'raw': text, 'expanded': plain, 'last_stored_digit_scale': scale}


def compatible_at_stored_precision(raw, full):
    info = scientific_info(raw)
    if not info or not re.fullmatch(r'\d+', str(full)):
        return False
    with localcontext() as ctx:
        ctx.prec = max(128, len(str(full)) + 10)
        quantum = Decimal(1).scaleb(info['last_stored_digit_scale'])
        number = Decimal(full)
        return any(number.quantize(quantum, rounding=mode) == Decimal(raw)
                   for mode in (ROUND_HALF_EVEN, ROUND_HALF_UP))


def recover_identifier(raw, full_values, evidence_equal):
    """A unique full value from the same independently identified shipment only."""
    if not evidence_equal:
        return None, 'evidence_not_equal'
    values = {str(v).strip() for v in full_values if str(v).strip()}
    # Competing values remain ambiguous even if only one passes rounding.
    if len(values) != 1:
        return None, 'full_value_missing' if not values else 'multiple_full_values'
    full = next(iter(values))
    if not re.fullmatch(r'\d+', full) or not compatible_at_stored_precision(raw, full):
        return None, 'full_value_incompatible'
    return full, 'evidence_restored'
