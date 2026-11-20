"""Explicit DEMO COMPANY materials/cases; registration never approves billing rules.

Source files and live calculation configurations are read only.  New-month
registration is supported, while the current calculation adapter remains July.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

DEFAULT_REGISTRY = 'ia/config/demo_company_material_registry.json'
CURRENT_CASE = 'demo_company_demo_center_202907'
SOURCE_ROOT = Path('data/demo_company/source')
REQUIRED_ROLES = ('claim', 'claim_summary', 'inbound_h1', 'outbound_h1',
                  'inbound_july', 'outbound_july', 'contract', 'master', 'master_reply')
ROLE_EXTENSIONS = {
    'claim': '.xlsb', 'claim_summary': '.xlsb', 'inbound_h1': '.xlsx',
    'outbound_h1': '.xlsx', 'inbound_july': '.xlsx', 'outbound_july': '.csv',
    'contract': '.pdf', 'master': '.xlsx', 'master_reply': '.xlsx',
    'reference_optional': '.xlsb',
}
ROLE_IMPACTS = {
    'claim': ['arithmetic', 'claim_detail_totals', 'wms_connection', 'billing_quantity', 'contract_rate', 'report'],
    'claim_summary': ['document_comparison', 'report'],
    'inbound_h1': ['inbound_connection', 'billing_quantity', 'report'],
    'inbound_july': ['inbound_connection', 'work_quantity', 'billing_quantity', 'report'],
    'outbound_h1': ['outbound_connection', 'billing_quantity', 'report'],
    'outbound_july': ['outbound_connection', 'number_recovery', 'work_quantity', 'billing_quantity', 'report'],
    'contract': ['contract_rate', 'billing_applicability', 'report'],
    'master': ['billing_quantity', 'unit_conversion', 'report'],
    'master_reply': ['billing_quantity', 'unit_conversion', 'report'],
    'reference_optional': ['optional_reference_connection', 'report'],
}


def _role_family(role):
    # Two received Master copies may have identical bytes without being independent
    # transactional evidence. Preserve both roles/paths; never permit claim↔WMS reuse.
    return 'master' if role in ('master', 'master_reply') else role


def _hash(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest().upper()


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode('utf-8')).hexdigest()


def _path(root, path, boundary):
    root = Path(root).resolve()
    candidate = (root / path).resolve()
    allowed = (root / boundary).resolve()
    # Do not let a redirected allowed directory widen the workspace boundary.
    if allowed != root / boundary or not allowed.is_relative_to(root) or not candidate.is_relative_to(allowed):
        raise ValueError('path_outside_allowed_scope')
    return candidate


def _metadata(value):
    if not isinstance(value, dict):
        raise ValueError('explicit_case_metadata_required')
    if value.get('company_id') != 'demo_company' or value.get('warehouse_scope') != 'demo_center':
        raise ValueError('unsupported_company_or_warehouse')
    month = value.get('invoice_month', '')
    if not isinstance(month, str) or not re.fullmatch(r'20\d{2}-(0[1-9]|1[0-2])', month):
        raise ValueError('explicit_invoice_month_required')
    period = value.get('service_period', {})
    if not isinstance(period, dict):
        raise ValueError('explicit_service_period_required')
    try:
        start, end = date.fromisoformat(period['start']), date.fromisoformat(period['end'])
    except (KeyError, TypeError, ValueError):
        raise ValueError('explicit_service_period_required') from None
    if start > end:
        raise ValueError('invalid_service_period')
    if not isinstance(period.get('basis'), str) or not period['basis'].strip():
        raise ValueError('period_basis_required')
    if not isinstance(period.get('user_decision'), str) or not period['user_decision'].strip():
        raise ValueError('period_user_decision_required')
    currency = value.get('currency', '')
    if not isinstance(currency, str) or not re.fullmatch(r'[A-Z]{3}', currency):
        raise ValueError('explicit_currency_required')
    return {'company_id': value['company_id'], 'warehouse_scope': value['warehouse_scope'],
            'invoice_month': month, 'service_period': {'start': start.isoformat(), 'end': end.isoformat(),
                'basis': period['basis'].strip(), 'user_decision': period['user_decision'].strip()},
            'currency': currency}


def _scope(metadata):
    value = _metadata(metadata)
    # Evidence of why the period was chosen is retained, but is not an invoice identity.
    value['service_period'] = {k: value['service_period'][k] for k in ('start', 'end')}
    return value


def load_registry(root, registry_path=DEFAULT_REGISTRY):
    path = _path(root, registry_path, 'ia/config')
    if not path.is_file():
        return {'schema_version': '1.0', 'materials': {}, 'cases': {}}
    value = json.loads(path.read_text('utf-8-sig'))
    if value.get('schema_version') != '1.0' or not isinstance(value.get('materials'), dict) or not isinstance(value.get('cases'), dict):
        raise ValueError('invalid_material_registry')
    return value


@contextmanager
def _locked_registry(root, registry_path):
    path = _path(root, registry_path, 'ia/config')
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix(path.suffix + '.lock')
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise ValueError('registration_in_progress') from None
    try:
        os.close(fd)
        yield path
    finally:
        lock.unlink(missing_ok=True)


def _write(path, value):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile('w', dir=path.parent, suffix='.tmp',
                                         encoding='utf-8', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def validate_material(root, material):
    """Verify recorded path/hash; this is not a content/header parser verdict."""
    source = _path(root, material['source_path'], SOURCE_ROOT)
    if not source.is_file():
        return {'status': 'data_unavailable', 'reason': 'registered_source_missing',
                'material_id': material.get('material_id')}
    if _hash(source) != material['sha256']:
        raise ValueError('registered_source_changed:' + material['source_path'])
    return {'status': 'verified', 'material_id': material.get('material_id'),
            'content_profile_status': 'not_inspected_by_registration'}


def get_case(root, case_id, registry_path=DEFAULT_REGISTRY):
    registry = load_registry(root, registry_path)
    if case_id not in registry['cases']:
        raise ValueError('case_not_registered')
    case = deepcopy(registry['cases'][case_id])
    case['sources'] = {role: deepcopy(registry['materials'][mid]) for role, mid in case['materials'].items()}
    return case


def plan_revision(previous, current):
    """Use explicit roles, never filename dates, to describe revalidation impact."""
    prior = previous.get('materials', {}) if previous else {}
    changed = sorted(role for role in set(prior) | set(current['materials'])
                     if prior.get(role) != current['materials'].get(role))
    same_scope = previous is not None and _scope(previous['metadata']) == _scope(current['metadata'])
    return {'changed_roles': changed,
            'affected_checks': sorted({check for role in changed for check in ROLE_IMPACTS[role]}),
            'same_scope': same_scope,
            'decision_reuse': 'scope_and_evidence_fingerprint_check_required' if changed else 'unchanged_materials',
            'decision_review_reasons': [role + ': 원본·기간·대상 지문이 달라지는 결정만 재검토; 기존 승인 취소 아님' for role in changed],
            'previous_revision_id': previous.get('revision_id') if previous else None,
            'current_revision_id': current['revision_id'],
            'automatic_calculation_or_approval': False}


def register_case(root, case_id, metadata, materials, *, registry_path=DEFAULT_REGISTRY,
                  supersedes_case_id=None):
    """Atomically register explicit sources; a revision never updates live adapter config.

    materials: [{role, source_path, supersedes_id?}]. Existing sources may also
    be referenced by {role, material_id}. Missing roles remain data_unavailable.
    """
    root = Path(root).resolve()
    if not isinstance(case_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', case_id):
        raise ValueError('invalid_case_id')
    meta = _metadata(metadata)
    if not isinstance(materials, list):
        raise ValueError('materials_list_required')
    with _locked_registry(root, registry_path) as path:
        before = _hash(path) if path.is_file() else None
        registry = load_registry(root, registry_path)
        if supersedes_case_id and supersedes_case_id != case_id and case_id in registry['cases']:
            raise ValueError('case_id_already_registered')
        old = registry['cases'].get(supersedes_case_id or case_id)
        if supersedes_case_id and old is None:
            raise ValueError('superseded_case_not_found')
        if old and _scope(old['metadata']) != _scope(meta):
            raise ValueError('case_scope_changed_use_new_case_id')
        selected = {}
        checked = []
        for item in materials:
            if not isinstance(item, dict):
                raise ValueError('material_object_required')
            role = item.get('role')
            if role not in ROLE_EXTENSIONS:
                raise ValueError('unsupported_material_role')
            if role in selected:
                raise ValueError('ambiguous_material_role:' + role)
            if item.get('material_id'):
                record = registry['materials'].get(item['material_id'])
                if not record or record['role'] != role or _scope(record['metadata']) != _scope(meta):
                    raise ValueError('material_reference_scope_mismatch')
                if validate_material(root, record)['status'] != 'verified':
                    raise ValueError('material_reference_missing')
                if any(key in item for key in ('source_path', 'supersedes_id')):
                    raise ValueError('ambiguous_material_reference')
                if old and old['materials'].get(role) not in (None, record['material_id']):
                    raise ValueError('explicit_material_supersedes_required:' + role)
            else:
                source = _path(root, item['source_path'], SOURCE_ROOT)
                if not source.is_file():
                    raise ValueError('new_source_missing')
                if source.suffix.lower() != ROLE_EXTENSIONS[role]:
                    raise ValueError('unsupported_format:' + role)
                relative = source.relative_to(root).as_posix()
                digest = _hash(source)
                for prior in registry['materials'].values():
                    if prior['source_path'] == relative and prior['sha256'] != digest:
                        raise ValueError('registered_source_changed:' + relative)
                    if prior['sha256'] == digest and _role_family(prior['role']) != _role_family(role):
                        raise ValueError('same_content_conflicting_role')
                mid = 'mat_' + _fingerprint({'role': role, 'scope': _scope(meta), 'sha256': digest})[:24]
                previous_role = old.get('materials', {}).get(role) if old else None
                if previous_role and previous_role != mid and item.get('supersedes_id') != previous_role:
                    raise ValueError('explicit_material_supersedes_required:' + role)
                record = registry['materials'].get(mid)
                if record is None:
                    supersedes = item.get('supersedes_id')
                    if previous_role and supersedes != previous_role:
                        raise ValueError('explicit_material_supersedes_required:' + role)
                    if supersedes:
                        prior = registry['materials'].get(supersedes)
                        if not prior or prior['role'] != role or _scope(prior['metadata']) != _scope(meta):
                            raise ValueError('superseded_material_scope_mismatch')
                    record = {'material_id': mid, 'role': role, 'metadata': deepcopy(meta),
                              'source_path': relative, 'sha256': digest, 'supersedes_id': supersedes,
                              'registration_status': 'registered', 'content_profile_status': 'not_inspected_by_registration'}
                    registry['materials'][mid] = record
                elif item.get('supersedes_id') not in (None, record.get('supersedes_id'), mid, previous_role):
                    raise ValueError('conflicting_supersedes_identity')
            selected[role] = record['material_id']
            checked.append(record)
        # Omission must not silently remove a previously registered source.
        if old and set(old['materials']) - set(selected):
            raise ValueError('explicit_source_removal_not_supported')
        revision = 'rev_' + _fingerprint({'case_id': case_id, 'metadata': meta, 'materials': selected})[:24]
        for record in checked:
            if validate_material(root, record)['status'] != 'verified':
                raise ValueError('source_disappeared_during_registration')
        if old and old.get('revision_id') == revision and old['case_id'] == case_id:
            return {'status': 'unchanged', 'case': deepcopy(old), 'impact': plan_revision(old, old)}
        case = {'case_id': case_id, 'metadata': meta, 'materials': selected,
                'input_mode': 'original_sources_only', 'revision_id': revision,
                'previous_revision_id': old.get('revision_id') if old else None,
                'registered_at': datetime.now(timezone.utc).isoformat()}
        registry['cases'][case_id] = case
        for record in checked:
            if validate_material(root, record)['status'] != 'verified':
                raise ValueError('source_disappeared_during_registration')
        if (_hash(path) if path.is_file() else None) != before:
            raise ValueError('registry_changed_during_registration')
        _write(path, registry)
        return {'status': 'registered', 'case': deepcopy(case), 'impact': plan_revision(old, case)}


def current_adapter_sources(root):
    """Known current source versions; inspecting them does not mutate registration."""
    root = Path(root)
    cfg = json.loads((root/'ia/config/demo_company_direct_wms.json').read_text('utf-8-sig'))
    # Imported modules contain constants only; neither run() function is called.
    from billing_quantity_tools import MASTERS
    from cost_verification import CONTRACT, CONTRACT_HASH
    return {**cfg['sources'], 'contract': {'path': CONTRACT, 'sha256': CONTRACT_HASH}, **MASTERS}


def inspect_case(root, case_id, registry_path=DEFAULT_REGISTRY):
    case = get_case(root, case_id, registry_path)
    missing = [role for role in REQUIRED_ROLES if role not in case['sources']]
    versions = {}
    for role, record in case['sources'].items():
        if role == 'reference_optional':
            continue
        versions[role] = validate_material(root, record)
        if versions[role]['status'] == 'data_unavailable':
            missing.append(role)
    meta = case['metadata']
    period = meta['service_period']
    supported = (meta['invoice_month'] == '2029-07' and meta['currency'] == 'KRW'
                 and period['start'] == '2029-06-26' and period['end'] == '2029-07-25')
    limitations = []
    if not supported:
        limitations.append({'status': 'not_implemented', 'reason': 'current_adapter_supports_2029_07_only'})
    if missing:
        limitations.append({'status': 'data_unavailable', 'reason': 'required_materials_missing', 'roles': sorted(set(missing))})
    changed = []
    if supported and not missing:
        active = current_adapter_sources(root)
        changed = [role for role in REQUIRED_ROLES
                   if active.get(role, {}).get('sha256', '').upper() != case['sources'][role]['sha256']
                   or active.get(role, {}).get('path', '').replace('\\', '/') != case['sources'][role]['source_path']]
        if changed:
            limitations.append({'status': 'not_implemented', 'reason': 'revision_adapter_and_decision_review_required', 'roles': changed})
    return {'case_id': case_id, 'revision_id': case['revision_id'], 'metadata': meta,
            'input_mode': 'original_sources_only', 'material_status': versions,
            'calculation_status': 'not_implemented' if any(x['status']=='not_implemented' for x in limitations)
                                  else 'data_unavailable' if missing else 'ready_for_current_adapter',
            'executable': not limitations, 'limitations': limitations,
            'source_roles_changed_from_active_adapter': changed,
            'fallback_to_july_permitted': False,
            'source_content_profile': 'existing_adapter_fingerprint' if not limitations else 'not_validated_for_execution'}


def bootstrap_current_case(root, registry_path=DEFAULT_REGISTRY):
    root = Path(root)
    decisions = json.loads((root/'ia/config/demo_company_july_wms_decisions.json').read_text('utf-8-sig'))
    period = decisions['comparison_period']
    if period.get('state') != 'confirmed_by_user':
        raise ValueError('confirmed_period_required')
    sources = current_adapter_sources(root)
    for value in sources.values():
        if _hash(_path(root, value['path'], SOURCE_ROOT)) != value['sha256'].upper():
            raise ValueError('current_adapter_source_changed')
    metadata = {'company_id':'demo_company','warehouse_scope':'demo_center',
                'invoice_month':'2029-07','currency':'KRW',
                'service_period':{'start':period['start'],'end':period['end'],
                                  'basis':'사용자 지정 잠정 비교기간','user_decision':period['answer']}}
    return register_case(root, CURRENT_CASE, metadata,
                         [{'role': role, 'source_path': value['path']} for role, value in sources.items()],
                         registry_path=registry_path)
