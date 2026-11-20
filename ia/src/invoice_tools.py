"""Deterministic, local invoice tools. Never calls a model or evaluates Excel code."""
from __future__ import annotations
import argparse
import hashlib
import json
import math
import re
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from portable_workbook import open_workbook
from pyxlsb import biff12
from identifier_tools import stored_number


def digest(path):
    with open(path, 'rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest().upper()


def registered(root, case_id):
    policy = json.loads((root / 'ia/config/project_scope.json').read_text('utf-8-sig'))
    cases = json.loads((root / 'ia/config/coverage_demo.json').read_text('utf-8-sig'))['cases']
    selected = [c for c in cases if c['case_id'] == case_id]
    if len(selected) != 1 or selected[0]['company_id'] not in policy['allowed_company_ids']:
        raise ValueError('unsupported_case')
    case = selected[0]
    source = (root / case['source_path']).resolve()
    boundary = (root / 'data/demo_company/source').resolve()
    if not source.is_relative_to(boundary):
        raise ValueError('source_outside_scope')
    cursor = root / case['source_path']
    while cursor != root:
        if cursor.is_symlink() or cursor.is_junction():
            raise ValueError('source_reparse_point')
        cursor = cursor.parent
    if digest(source) != case['source_sha256'].upper():
        raise ValueError('source_hash_mismatch')
    return case, source


def column(n):
    text = ''
    while n:
        n, remainder = divmod(n - 1, 26)
        text = chr(65 + remainder) + text
    return text


def cells(sheet):
    if hasattr(sheet,"xlsx_path"):
        from portable_workbook import read_cells
        yield from read_cells(sheet);return

    """Pinned pyxlsb adapter retains formula-cache/error types lost by rows()."""
    reader = sheet._reader
    reader.seek(sheet._data_offset)
    row = None
    for kind, record in reader:
        if kind == biff12.ROW:
            row = record.r + 1
        elif biff12.BLANK <= kind <= biff12.FORMULA_BOOLERR:
            if row is None:
                raise ValueError('cell_before_row')
            value = record.v
            if kind == biff12.STRING:
                value = sheet._stringtable[value]
            if value is None:
                continue
            yield column(record.c + 1) + str(row), {
                'value': value, 'error': kind in (biff12.BOOLERR, biff12.FORMULA_BOOLERR),
                'formula_cached': kind in (8, 9, 10, 11), 'row': row,
            }
        elif kind == biff12.SHEETDATA_END:
            break


def number(cell):
    if cell is None or cell['error'] or isinstance(cell['value'], bool):
        raise ValueError('numeric_value_missing_or_error')
    v = stored_number(cell['value'])
    if v is None:
        raise ValueError('numeric_value_missing_or_error')
    return v


def check(identifier, label, expected, observed, evidence, kind='internal_arithmetic', reason=None):
    performed = expected is not None and observed is not None
    diff = observed - expected if performed else None
    return {'id': identifier, 'label': label,
            'status': ('match' if abs(diff) <= Decimal('0.000001') else 'difference') if performed else 'data_unavailable',
            'expected': expected, 'observed': observed, 'difference': diff, 'evidence': evidence,
            'kind': kind, 'check_performed': performed, 'confirmed_overcharge': None,
            'reason': reason or ('원본 저장값을 Decimal로 재계산했습니다. 실제 작업·계약 적용 확정과 다릅니다.' if performed else '비교에 필요한 숫자가 빈칸·문자·오류값입니다. 0으로 대체하지 않았습니다.')}


def verify_rules(sheets, rules):
    out = []
    for rule in rules:
        values = sheets[rule['sheet']]
        observed = expected = None
        try:
            observed = number(values.get(rule['target']))
            operands = [number(values.get(ref)) for ref in rule['operands']]
            if rule['kind'] == 'product':
                expected = Decimal(1)
                for value in operands:
                    expected *= value
            elif rule['kind'] == 'sum':
                expected = sum(operands, Decimal(0))
            elif rule['kind'] == 'subtract':
                expected = operands[0] - operands[1]
            else:
                raise RuntimeError('unimplemented_rule')
        except ValueError:
            pass
        out.append(check(rule['id'], rule['label'], expected, observed,
                         [rule['sheet'] + '!' + rule['target'], rule['sheet'] + '!' + ','.join(rule['operands'])]))
    return out


def inspect(root, case_id):
    case, source = registered(root, case_id)
    profile = []
    if case['source_format'] == 'xlsb':
        with open_workbook(str(source)) as book:
            for name in book.sheets:
                with book.get_sheet(name) as sheet:
                    d = sheet.dimension
                    profile.append({'sheet': name, 'declared_rows': d.h, 'declared_columns': d.w})
    return {'case_id': case_id, 'source_integrity': 'verified', 'source_format': case['source_format'],
            'sheets': profile, 'registered_rule_count': len(case.get('python_rules', [])),
            'reader': 'pyxlsb 1.0.10' if profile else 'existing_xlsx_tool',
            'note': '시트 범위는 실제 거래 건수가 아닙니다. XLSB 수식은 저장된 결과만 읽으며 수식 토큰 자동 해석은 미구현입니다.'}


def verify(root, case_id):
    case, source = registered(root, case_id)
    if case['source_format'] != 'xlsb':
        raise ValueError('python_adapter_requires_xlsb')
    wanted = set(case.get('sheet_names', []))
    sheets, profile = {}, []
    with open_workbook(str(source)) as book:
        for name in book.sheets:
            if name not in wanted:
                continue
            with book.get_sheet(name) as sheet:
                values = dict(cells(sheet))
                sheets[name] = values
                profile.append({'sheet': name, 'nonempty_cells': len(values),
                                'formula_cache_cells': sum(c['formula_cached'] for c in values.values()),
                                'error_cells': sum(c['error'] for c in values.values())})
    if wanted != set(sheets):
        raise ValueError('registered_sheet_missing')
    checks = verify_rules(sheets, case.get('python_rules', []))
    for comparison in case.get('python_document_comparisons', []):
        reference_case, reference_path = registered(root, comparison['reference_case_id'])
        with open_workbook(str(reference_path)) as book:
            with book.get_sheet(comparison['sheet']) as sheet:
                reference = dict(cells(sheet))
        current = sheets[comparison['sheet']]
        expected = observed_value = None
        try:
            if current.get('E9', {}).get('value') != reference.get('E9', {}).get('value'):
                raise ValueError('document_title_period_differs')
            # Units and named line are explicit mapping evidence, never inferred from row number alone.
            unit = comparison['unit']
            row = re.search(r'[0-9]+', comparison['cell']).group()
            if current.get('J'+row, {}).get('value') != unit or reference.get('J'+row, {}).get('value') != unit:
                raise ValueError('document_units_differ')
            expected = number(reference.get(comparison['cell']))
            observed_value = number(current.get(comparison['cell']))
        except ValueError:
            expected = observed_value = None
        checks.append(check(comparison['id'], comparison['label'], expected, observed_value,
                            [reference_case['source_path']+'#'+comparison['sheet']+'!'+comparison['cell'],
                             case['source_path']+'#'+comparison['sheet']+'!'+comparison['cell']],
                            'document_comparison', '같은 7월 표기의 청구집계와 항목단가 파일의 '+comparison['unit']+' 수량 차이입니다. 어느 문서가 최종본인지·과다청구 금액은 확정하지 않았습니다.'))
        checks[-1]['unit'] = comparison['unit']
        if digest(reference_path) != reference_case['source_sha256'].upper():
            raise ValueError('reference_changed_during_read')
    observed = []
    for total in case.get('claim_totals', []):
        try:
            amount = number(sheets[total['sheet']].get(total['cell']))
            observed.append({'label': total['label'], 'value': amount, 'currency': total['currency'],
                             'evidence': total['sheet'] + '!' + total['cell'], 'status': 'invoice_stated_total',
                             'note': '문서 기재 합계이며 다른 문서와 중복 합산하지 않습니다.'})
        except ValueError:
            checks.append(check('total.' + total['cell'], total['label'], None, None, [total['sheet']+'!'+total['cell']]))
    # B2C detail: compare the stored pivot quantities, not physical boxes or a contract period.
    if 'Sheet5' in sheets and 'Sheet6' in sheets:
        a, b = sheets['Sheet5'], sheets['Sheet6']
        sums = []
        counts = []
        for values, start in ((a, 4), (b, 2)):
            quantities = [number(c) for addr, c in values.items()
                          if re.fullmatch(r'D[0-9]+', addr) and c['row'] >= start
                          and values.get('C'+str(c['row']), {}).get('value') not in (None, '', '총합계')
                          and not c['error'] and isinstance(c['value'], (int, float)) and not isinstance(c['value'], bool)]
            sums.append(sum(quantities, Decimal(0)))
            counts.append(Decimal(len(quantities)))
        for suffix, label, values in (('quantity', 'B2C 집계표 간 상품수량 합계', sums), ('rows', 'B2C 집계표 간 송장 표기 행수', counts)):
            checks.append(check('b2c.pivot.'+suffix, label, values[0], values[1], ['Sheet5!C:D', 'Sheet6!C:D'], 'document_comparison', '같은 수신 파일의 두 집계표 비교입니다. 독립 WMS 대사나 송장 1개=박스 1개 확정이 아닙니다.'))
    wms_evidence = None
    if case.get('wms_rule_set'):
        if case['wms_rule_set'] != 'demo_company_wms_rules.json':
            raise ValueError('unsupported_wms_rule_set')
        from wms_tools import compare_documents, comparison_checks
        rule_path = root / 'ia/config/demo_company_wms_rules.json'
        rule_hash = digest(rule_path)
        wms_evidence = compare_documents(root, source, json.loads(rule_path.read_text('utf-8-sig')))
        wms_evidence['rules_sha256'] = rule_hash
        if digest(rule_path) != rule_hash:
            raise ValueError('wms_rules_changed_during_read')
        checks.extend(comparison_checks(wms_evidence))
    for limit in case.get('limitations', []):
        checks.append({'id': limit['id'], 'label': limit['label'], 'status': limit['status'],
                       'expected': None, 'observed': None, 'difference': None,
                       'reason': limit['reason'], 'evidence': [case['source_path']], 'kind': 'evidence_gap',
                       'check_performed': False, 'confirmed_overcharge': None})
    if digest(source) != case['source_sha256'].upper():
        raise ValueError('source_changed_during_read')
    return {'schema_version': '2.0', 'case_id': case_id, 'company_id': case['company_id'],
            'warehouse_id': case['warehouse_id'], 'service_month': case['service_month'], 'scope': case,
            'source': {'path': case['source_path'], 'sha256': case['source_sha256'], 'format': 'xlsb', 'read_status': 'read_cached_values'},
            'checks': checks, 'observed_totals': observed, 'reader_profile': profile,
            'full_invoice_validated': False, 'independent_wms_validated': False, 'confirmed_overcharge': None,
            'calculation_engine': 'python_decimal', 'llm_used': False, 'wms_evidence': wms_evidence}


def encode(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    raise TypeError(type(value).__name__)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['inspect', 'verify'])
    parser.add_argument('--root', required=True)
    parser.add_argument('--case-id', required=True)
    args = parser.parse_args()
    result = (inspect if args.action == 'inspect' else verify)(Path(args.root).resolve(), args.case_id)
    print(json.dumps(result, ensure_ascii=False, default=encode, allow_nan=False))
