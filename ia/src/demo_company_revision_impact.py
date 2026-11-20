"""Conservative, read-only reuse assessment for existing DEMO COMPANY decisions.

This module does not approve rules, update registered hashes, or run billing checks.
Only already approved decisions may be reused by the caller after this assessment.
"""
import hashlib
import json
import math
from pathlib import Path
import re
import struct
import zipfile
from datetime import date

SCOPE_FIELDS = ('company_id', 'warehouse_id', 'invoice_month', 'service_period', 'currency')
WMS = ('inbound_h1', 'inbound_july', 'outbound_h1', 'outbound_july')
MASTERS = ('master', 'master_reply')
DECISIONS = ('U20', 'U21', 'U25', 'U26', 'U27', 'U28', 'U29', 'U30',
             'U31', 'U32', 'U33', 'U34', 'legacy_inbound_outbound_exceptions')
KNOWN_ROLES = {'claim', 'claim_summary', 'contract', *WMS, *MASTERS, 'optional_internal'}
RATE_ROWS = (20, 21, 28, 31, 34, 35, 36, 37)
ACTUAL_COST_ROWS = (69, 70, 71, 73, 78, 80, 83, 105, 108, 110, 116)
COST_DETAIL_SHEETS = ('입고 외 기타', 'B2B 기타', 'B2B 반품', '반품리스트',
                      'B2C출고_택배', 'B2C출고_용차', '차감항목', '임가공-라벨')


def _sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest().upper()


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode('utf-8')).hexdigest().upper()


def _zip_payload_manifest(path):
    """Hash uncompressed members in bounded chunks, never trust ZIP CRC equality."""
    if not zipfile.is_zipfile(path):
        return None
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        names = [member.filename for member in members]
        if len(names) != len(set(names)):
            raise ValueError('duplicate_zip_member_name')
        result = {}
        for member in members:
            digest = hashlib.sha256()
            with archive.open(member) as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b''):
                    digest.update(chunk)
            result[member.filename] = digest.hexdigest().upper()
        return result


def _raw_value(value):
    # Type, whitespace and precision are evidence; do not normalize identifiers or sum cells.
    if value is None:
        return ['null', None]
    if type(value) in (str, bool, int):
        return [type(value).__name__, value]
    if isinstance(value, float) and math.isfinite(value):
        return ['float', repr(value)]
    from decimal import Decimal
    if isinstance(value, Decimal) and value.is_finite():
        return ['Decimal', str(value)]
    raise ValueError('unsupported_raw_cell_value')


def _cell(value):
    if not isinstance(value, dict):
        return [_raw_value(value), False, False, None]
    if 'value' not in value:
        raise ValueError('raw_cell_value_missing')
    formula = bool(value.get('formula_cached'))
    formula_record = value.get('formula_record_sha256')
    if formula and not formula_record:
        raise ValueError('formula_record_evidence_missing')
    return [_raw_value(value['value']), bool(value.get('error')), formula, formula_record]


def _formula_records(book, name, *, tokens_only=False, layout=None):
    """Bind formula cells to their original binary records, including stored formula tokens."""
    from pyxlsb import biff12
    target = next(target for sheet, target in book._sheets if sheet == name)
    data = book._zf.read('xl/' + target)
    pos = 0
    row = None
    records = {}
    if layout is not None:
        layout.update(rows=0, cells=0, blank_cells=0)
        positions = hashlib.sha256()
    in_data = False
    extra_records = hashlib.sha256()
    while pos < len(data):
        kind = 0
        for shift in range(0, 32, 8):
            byte = data[pos]; pos += 1; kind |= byte << shift
            if not byte & 128:
                break
        else:
            raise ValueError('invalid_biff_record')
        size = 0
        for shift in range(0, 28, 7):
            byte = data[pos]; pos += 1; size |= (byte & 127) << shift
            if not byte & 128:
                break
        else:
            raise ValueError('invalid_biff_size')
        end = pos + size
        if end > len(data):
            raise ValueError('truncated_biff_record')
        raw = data[pos:end]
        if kind == biff12.ROW:
            row = struct.unpack_from('<I', raw)[0] + 1
            if layout is not None:
                layout['rows'] += 1
                positions.update(b'R' + struct.pack('<I', row))
        elif biff12.BLANK <= kind <= biff12.FORMULA_BOOLERR:
            if row is None:
                raise ValueError('cell_without_row')
            if layout is not None:
                layout['cells'] += 1
                layout['blank_cells'] += kind == biff12.BLANK
                positions.update(b'C' + struct.pack('<II', row, struct.unpack_from('<I', raw)[0]))
            if kind not in (8, 9, 10, 11):
                pos = end
                continue
            from invoice_tools import column
            col = column(struct.unpack_from('<I', raw)[0] + 1)
            payload = raw
            if tokens_only:
                # Cell header is eight bytes. Ignore the cached value, retain
                # flags, parsed formula tokens and extra formula data.
                if kind == biff12.FORMULA_FLOAT:
                    offset = 16
                elif kind in (biff12.FORMULA_BOOL, biff12.FORMULA_BOOLERR):
                    offset = 9
                else:
                    offset = 12 + 2 * struct.unpack_from('<I', raw, 8)[0]
                if len(raw) < offset + 6:
                    raise ValueError('truncated_formula_evidence')
                payload = raw[offset:]
            records[row, col] = hashlib.sha256(payload).hexdigest().upper()
        elif kind == biff12.SHEETDATA_END:
            break
        elif kind == biff12.SHEETDATA:
            in_data = True
        elif in_data:
            # Shared/array formula definitions and unsupported data records are
            # part of the evidence even when cell cached values do not change.
            extra_records.update(struct.pack('<II', kind, size) + raw)
        pos = end
    if layout is not None:
        layout['positions_sha256'] = positions.hexdigest().upper()
        layout['extra_data_records_sha256'] = extra_records.hexdigest().upper()
    if records:
        extra = extra_records.digest()
        records = {key:hashlib.sha256(value.encode('ascii') + extra).hexdigest().upper()
                   for key,value in records.items()}
    return records


def load_claim_sheets(path):
    from portable_workbook import open_workbook
    from xlsb_scan import scan_rows
    result = {}
    wanted = {'청구내역', '입고리스트', 'B2B출고', 'B2C출고_택배', *COST_DETAIL_SHEETS}
    with open_workbook(str(path)) as book:
        for name in book.sheets:
            if name not in wanted:
                continue
            formulas = _formula_records(book, name)
            result[name] = {}
            for row, cells in scan_rows(book, name):
                result[name][row] = {}
                for col, cell in cells.items():
                    result[name][row][col] = dict(cell)
                    if cell.get('formula_cached'):
                        result[name][row][col]['formula_record_sha256'] = formulas.get((row, col))
    return result


def load_claim_structure(path):
    """Read all sheets; stream blank positions into hashes, retain nonempty cells."""
    from portable_workbook import open_workbook
    from xlsb_scan import scan_rows
    class ClaimSheets(dict):
        pass
    result = ClaimSheets()
    result.layout_signatures = {}
    with open_workbook(str(path)) as book:
        for name in book.sheets:
            cells = {}
            layout = {}
            formulas = _formula_records(book, name, tokens_only=True, layout=layout)
            result.layout_signatures[name] = layout
            for row, values in scan_rows(book, name):
                cells.setdefault(row, {})
                for col, value in values.items():
                    cells[row][col] = dict(value)
                    if value.get('formula_cached'):
                        cells[row][col]['formula_tokens_sha256'] = formulas.get((row, col))
            result[name] = cells
    return result


def _structure_cells(sheets):
    from decimal import Decimal
    result = {}
    numeric = {}
    for sheet, rows in sheets.items():
        result[sheet, 0, ''] = ['sheet']
        if sheet in getattr(sheets, 'layout_signatures', {}):
            result[sheet, 0, '__stored_layout__'] = sheets.layout_signatures[sheet]
        for row, cells in rows.items():
            if not isinstance(row, int) or row < 1:
                raise ValueError('invalid_structure_row')
            result[sheet, row, ''] = ['row']
            for col, value in cells.items():
                if not re.fullmatch('[A-Z]+', col):
                    raise ValueError('invalid_structure_column')
                record = value if isinstance(value, dict) else {'value': value}
                if 'value' not in record:
                    raise ValueError('missing_structure_cell_value')
                raw = record['value']
                is_number = type(raw) in (int, float, Decimal)
                shape = ['number'] if is_number else _raw_value(raw)
                if is_number:
                    _raw_value(raw)  # rejects nonfinite values
                    numeric[sheet, row, col] = _raw_value(raw)
                formula = bool(record.get('formula_cached'))
                tokens = record.get('formula_tokens_sha256') if formula else None
                if formula and not tokens:
                    raise ValueError('formula_tokens_unavailable')
                result[sheet, row, col] = [shape, bool(record.get('error')), formula, tokens]
    return result, numeric


def assess_claim_structure(root, previous_source, current_source, *, sheet_loader=None):
    """Allow numeric value edits only within an unchanged parsed claim structure.

    This is an adapter compatibility check, not approval of changed values or rules.
    Decision impact must be assessed separately even when same_structure is true.
    """
    root = Path(root).resolve()
    output = {'same_structure': False, 'status': 'not_implemented',
              'changed_locations': [], 'changed_location_count': 0, 'evidence': []}
    checked = {}
    paths = {}
    for version, source in [('previous', previous_source), ('current', current_source)]:
        path = (root / source.get('path', '')).resolve()
        if not path.is_relative_to(root / 'data/demo_company/source'):
            raise ValueError('source_outside_active_company')
        expected = source.get('sha256', '').upper()
        if not path.is_file() or not re.fullmatch('[A-F0-9]{64}', expected) or _sha(path) != expected:
            output['status'] = 'blocked'
            output['evidence'].append({'version': version, 'reason': 'source_integrity_not_verified'})
            return output
        paths[version] = path
        checked[path] = expected
    if previous_source['sha256'].upper() == current_source['sha256'].upper():
        output.update(same_structure=True, status='same_structure')
        output['evidence'].append({'comparison': 'identical_verified_bytes'})
    else:
        loader = sheet_loader or load_claim_structure
        try:
            previous_payload = _zip_payload_manifest(paths['previous'])
            current_payload = _zip_payload_manifest(paths['current'])
            if previous_payload and previous_payload == current_payload:
                output.update(same_structure=True, status='same_structure')
                output['evidence'].append({'comparison': 'identical_zip_member_payloads',
                    'member_count': len(previous_payload), 'member_manifest_sha256': _digest(previous_payload),
                    'payload_hash_algorithm': 'SHA256', 'archive_metadata_ignored': True,
                    'numeric_value_changes': 0, 'amount_or_policy_approval': False})
            else:
                old, old_numbers = _structure_cells(loader(paths['previous']))
                new, new_numbers = _structure_cells(loader(paths['current']))
                changed = [k for k in sorted(old.keys() | new.keys()) if old.get(k) != new.get(k)]
                output['changed_location_count'] = len(changed)
                output['changed_locations'] = [{'sheet': s, 'row': r or None, 'column': c or None,
                                               'reason': 'added' if (s,r,c) not in old else 'removed' if (s,r,c) not in new else 'label_type_unit_or_formula_changed'}
                                              for s,r,c in changed[:10]]
                output.update(same_structure=not changed, status='same_structure' if not changed else 'not_implemented')
                output['evidence'].append({'comparison': 'all_sheets_rows_cells_text_and_formula_tokens',
                    'previous_structure_sha256': _digest([[list(k),v] for k,v in sorted(old.items())]),
                    'current_structure_sha256': _digest([[list(k),v] for k,v in sorted(new.items())]),
                    'numeric_value_changes': sum(old_numbers.get(k) != new_numbers.get(k) for k in old_numbers.keys() & new_numbers.keys()),
                    'amount_or_policy_approval': False})
        except (ValueError, KeyError, TypeError, IndexError, OSError, struct.error, zipfile.BadZipFile, RuntimeError) as exc:
            output['evidence'].append({'reason': 'structure_reader_could_not_verify', 'error': type(exc).__name__ + ': ' + str(exc)})
    for path, expected in checked.items():
        if not path.is_file() or _sha(path) != expected:
            output.update(same_structure=False, status='blocked')
            output['evidence'].append({'reason': 'source_changed_during_structure_read'})
            break
    output['evidence'].append({'source_hashes': {'previous': previous_source['sha256'], 'current': current_source['sha256']},
                               'originals_modified_by_tool': False})
    return output


def _sheet_signature(sheets, name, columns=None, rows=None, required=True):
    if name not in sheets:
        if required:
            raise ValueError('required_claim_sheet_missing:' + name)
        return {'sheet': name, 'present': False}
    content = sheets[name]
    if rows is not None and required and any(row not in content for row in rows):
        raise ValueError('required_claim_rows_missing:' + name)
    cells = []
    for row, record in sorted(content.items()):
        if not isinstance(row, int) or row < 1 or not isinstance(record, dict):
            raise ValueError('invalid_raw_sheet_structure')
        if rows is not None and row not in rows:
            continue
        for col, value in sorted(record.items()):
            if not re.fullmatch('[A-Z]+', col):
                raise ValueError('invalid_raw_cell_column')
            if columns is None or col in columns:
                cells.append([row, col, _cell(value)])
    return {'sheet': name, 'present': True, 'cells': cells}


def claim_signatures(sheets):
    """Position-preserving signatures. Moving or splitting rows never proves equivalence."""
    profiles = {}
    specs = {
        'b2c_connection': [('B2C출고_택배', set('ABCD'), None, True)],
        'quantity': [('입고리스트', set('ABD GHIJKLM'.replace(' ', '')), None, True),
                     ('B2B출고', set('ADFGKLMNO'), None, True)],
        'rate': [('청구내역', None, set(range(1, 20)) & set(sheets.get('청구내역', {})), True),
                 ('청구내역', set('ABCGJL'), set(RATE_ROWS), True)],
        'actual_cost': [('청구내역', None, set(ACTUAL_COST_ROWS), True)] +
                       [(name, None, None, False) for name in COST_DETAIL_SHEETS],
        'legacy_inbound_outbound': [('입고리스트', None, None, True), ('B2B출고', None, None, True)],
    }
    for profile, fields in specs.items():
        try:
            sections = [_sheet_signature(sheets, *spec) for spec in fields]
            if profile == 'rate' and not sections[0]['cells']:
                raise ValueError('claim_header_dates_unavailable')
            if not any(section.get('cells') for section in sections):
                raise ValueError('empty_evidence_profile')
            profiles[profile] = {'sha256': _digest(sections), 'cells': sum(len(s.get('cells', [])) for s in sections)}
        except (ValueError, TypeError) as exc:
            profiles[profile] = {'error': str(exc)}
    return profiles


def assess_revision(root, previous_case, current_case, *, sheet_loader=None):
    root = Path(root).resolve()
    old = previous_case.get('sources', {})
    new = current_case.get('sources', {})
    changed_scope = [k for k in SCOPE_FIELDS if not previous_case.get(k) or not current_case.get(k)
                     or previous_case.get(k) != current_case.get(k)]
    changed_roles = sorted(k for k in old.keys() | new.keys() if old.get(k) != new.get(k))
    result = {'schema_version': '1.0', 'status': 'review_required', 'scope_changed': bool(changed_scope),
              'changed_scope_fields': changed_scope, 'changed_roles': changed_roles,
              'verified_reusable_decisions': [], 'review_required': [], 'evidence': [],
              'source_hashes': {'previous': {k:v.get('sha256') for k,v in old.items()},
                                'current': {k:v.get('sha256') for k,v in new.items()}},
              'approval_semantics': 'Reuses only an existing decision; never creates approval or changes registered hashes.'}

    def review_all(reason):
        result['review_required'] = [{'decision_id': x, 'reason': reason} for x in DECISIONS]
        return result

    for case in (previous_case, current_case):
        period = case.get('service_period')
        try:
            valid_period = isinstance(period, dict) and date.fromisoformat(period['start']) <= date.fromisoformat(period['end'])
        except (KeyError, ValueError, TypeError):
            valid_period = False
        if not valid_period or not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', str(case.get('invoice_month', ''))):
            result['scope_changed'] = True
            result['status'] = 'blocked'
            return review_all('invalid_or_missing_period_metadata')
    if changed_scope:
        return review_all('company_warehouse_month_period_or_currency_changed_or_missing')
    if previous_case['company_id'] != 'demo_company':
        result['status'] = 'blocked'
        return review_all('outside_active_company')
    if previous_case['warehouse_id'] != 'demo_center':
        result['status'] = 'blocked'
        return review_all('unsupported_warehouse_adapter')
    unknown = set(changed_roles) - KNOWN_ROLES
    if unknown:
        result['evidence'].append({'unknown_changed_roles': sorted(unknown)})
        return review_all('unmapped_material_role_changed')

    verified = {}
    paths = {}
    for version, sources in [('previous', old), ('current', new)]:
        for role, source in sources.items():
            path = (root / source.get('path', '')).resolve()
            if not path.is_relative_to(root / 'data/demo_company/source'):
                raise ValueError('source_outside_active_company:' + role)
            expected = source.get('sha256', '').upper()
            if not re.fullmatch('[A-F0-9]{64}', expected) or not path.is_file():
                result['status'] = 'blocked'
                result['evidence'].append({'version': version, 'role': role, 'error': 'missing_source_or_registered_hash'})
                return review_all('source_integrity_not_verified')
            if path not in verified:
                verified[path] = _sha(path)
            actual = verified[path]
            if actual != expected:
                result['status'] = 'blocked'
                result['evidence'].append({'version': version, 'role': role, 'registered': expected, 'actual': actual})
                return review_all('source_integrity_not_verified')
            paths[version, role] = path

    def same_roles(roles):
        return all(k in old and k in new and old[k]['sha256'].upper() == new[k]['sha256'].upper() for k in roles)

    if same_roles(('claim',)):
        signatures = None
        claim_equal = {name: True for name in ('b2c_connection', 'quantity', 'rate', 'actual_cost', 'legacy_inbound_outbound')}
        result['evidence'].append({'claim_comparison': 'identical_verified_bytes', 'path_changed': old['claim']['path'] != new['claim']['path']})
    else:
        loader = sheet_loader or load_claim_sheets
        signatures = {}
        for version in ('previous', 'current'):
            try:
                signatures[version] = claim_signatures(loader(paths[version, 'claim']))
            except Exception as exc:
                signatures[version] = {'read_error': type(exc).__name__ + ': ' + str(exc)}
        claim_equal = {name: bool(signatures['previous'].get(name, {}).get('sha256')) and
                       signatures['previous'][name]['sha256'] == signatures['current'].get(name, {}).get('sha256')
                       for name in ('b2c_connection', 'quantity', 'rate', 'actual_cost', 'legacy_inbound_outbound')}
        result['evidence'].append({'claim_comparison': 'raw_cells_at_same_positions', 'profiles': signatures,
                                   'row_movement_or_numeric_coincidence_not_accepted': True})

    dependencies = {
        'U20': ('b2c_connection', ('outbound_h1', 'outbound_july')),
        'U21': ('b2c_connection', ('outbound_h1', 'outbound_july')),
        'U25': ('rate', ('contract',)),
        'U26': ('actual_cost', ()),
        **{q: ('quantity', (*WMS, *MASTERS)) for q in ('U27','U28','U29','U30','U31','U32','U33','U34')},
        'legacy_inbound_outbound_exceptions': ('legacy_inbound_outbound', WMS),
    }
    if 'optional_internal' in old or 'optional_internal' in new:
        dependencies['U20'] = ('b2c_connection', ('outbound_h1', 'outbound_july', 'optional_internal'))
    for decision, (profile, roles) in dependencies.items():
        if claim_equal[profile] and same_roles(roles):
            result['verified_reusable_decisions'].append(decision)
        else:
            result['review_required'].append({'decision_id': decision, 'reason': 'relevant_claim_evidence_or_source_changed_or_missing',
                'claim_profile': profile, 'claim_evidence_same': claim_equal[profile],
                'changed_or_missing_roles': [k for k in roles if not same_roles((k,))]})
    # Do not accept files modified while their cells were being read.
    for path, expected in verified.items():
        if not path.is_file() or _sha(path) != expected:
            result['status'] = 'blocked'
            result['verified_reusable_decisions'] = []
            return review_all('source_changed_during_assessment')
    result['status'] = 'review_required' if result['review_required'] else 'existing_decisions_reusable'
    result['evidence'].append({'source_hashes_verified_before_and_after': True,
                              'decisions_not_modified': True, 'sources_not_modified': True})
    return result
