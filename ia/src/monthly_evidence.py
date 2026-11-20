"""Version-bound monthly supporting evidence and deterministic comparisons.

The registry is part of the atomically published monthly result, never a July
policy. Uploaded bytes are preserved under the selected month; registration
only becomes active when the month execution successfully publishes a result.
"""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import shutil
import uuid

from monthly_cases import (case_dir, current_result, digest, identity, load, safe,
                           verify_sources)
from supporting_evidence import source_value, numeric

SOURCE = Path('data/demo_company/source/월별증빙')
UPLOADS = Path('frontend/.runtime/uploads')
AXES = {'work': ('작업량', 'quantity', 'M'),
        'contract': ('적용 단가', 'rate', 'L'),
        'actual_cost': ('실비', 'amount', 'N')}
FORMATS = {'.csv': 'csv', '.xlsx': 'xlsx', '.xlsb': 'xlsb', '.pdf': 'reviewed_transcription',
           '.png': 'reviewed_transcription', '.jpg': 'reviewed_transcription',
           '.jpeg': 'reviewed_transcription'}


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _case(root, month):
    case = load(case_dir(root, month) / 'case.json')
    if not case:
        raise ValueError('먼저 해당 월의 청구서와 입고·출고 WMS를 등록해 주세요.')
    if case.get('month') != month or case.get('company_id') != 'demo_company' or case.get('warehouse') != '가상센터':
        raise ValueError('등록 회사·센터·청구월이 일치하지 않습니다.')
    verify_sources(root, case)
    return case


def claim_items(root, case):
    """Read supported summary columns without executing WMS reconciliation."""
    items=[]
    claims=[m for m in case['materials'] if m['role']=='claim']
    for claim in claims:
        for item in _claim_items(root,case,claim):
            item.update(claim_path=claim['path'],claim_name=claim.get('name') or Path(claim['path']).name,
                        target_key=(claim['sha256']+':'+str(item['invoice_row'])) if len(claims)>1 else item['invoice_row'])
            items.append(item)
    return items


def claims_hash(case):
    hashes=[m['sha256'] for m in case['materials'] if m['role']=='claim']
    return hashes[0] if len(hashes)==1 else _hash(hashes)


def entry_claim(case,entry):
    claims=[m['path'] for m in case['materials'] if m['role']=='claim']
    path=entry.get('claim_path') or (claims[0] if len(claims)==1 else None)
    if path not in claims:raise ValueError('증빙을 연결할 청구 파일을 선택해 주세요.')
    return path


def _claim_items(root,case,claim):
    from monthly_verification import names, rows, raw, text
    path = Path(root) / claim['path']
    from document_layouts import discover
    sheets,mapping,_=discover(path,names(path),rows,{'청구내역'})
    if '청구내역' not in sheets:
        raise ValueError('청구내역 시트가 없어 증빙의 청구 대상 연결을 지원하지 않습니다.')
    cells = sheets['청구내역'];layout=mapping.get('청구내역',{})
    headers = ' '.join(text(raw(c, k)) for n, c in cells.items() if n < 20 for k in ('J', 'L', 'M', 'N'))
    if not all(any(word in headers for word in synonyms) for synonyms in (('단가',), ('수량',), ('금액', '청구액'))):
        raise ValueError('청구 단가·수량·금액 양식을 식별할 수 없어 증빙을 연결하지 않았습니다.')
    items = []; category = ''; label = ''
    for n, c in sorted(cells.items()):
        if n < 20 or not any(k in c for k in ('J', 'L', 'M', 'N')):
            continue
        if text(raw(c, 'C')): category = text(raw(c, 'C'))
        if text(raw(c, 'G')): label = text(raw(c, 'G'))
        if any(t in text(raw(c, 'C')) for t in ('합계', '합 계', '부가세', '총계')):
            continue
        if not any(raw(c, k) is not None for k in ('M', 'L', 'N')):
            continue
        if numeric(raw(c,'N')) in (None,0):continue
        items.append({'invoice_row': n-layout.get('offset',0), 'label': (category + ' ' + label).strip(),
                      'unit': text(raw(c, 'J')), 'quantity': text(numeric(raw(c, 'M'))),
                      'rate': text(numeric(raw(c, 'L'))), 'amount': text(numeric(raw(c, 'N')))})
    verify_sources(root, case)
    return items


def registry(root, case):
    """Select only the current registration's committed or pending registry."""
    for pending in (True, False):
        result = current_result(root, case['month'], pending=pending)
        if result and result.get('identity') == identity(case) and result.get('evidence_registry'):
            value = result['evidence_registry']
            if value.get('case_identity') != identity(case):
                raise ValueError('증빙 기록과 월별 자료 버전이 일치하지 않습니다.')
            return deepcopy(value)
    return {'schema_version': 1, 'month': case['month'], 'case_revision': case['revision'],
            'case_identity': identity(case), 'claim_sha256': claims_hash(case),
            'period': deepcopy(case['period']), 'currency': case['currency'],
            'amount_basis': 'supply_amount', 'revision': 0, 'entries': [],
            'july_decisions_applied': False}


def targets(root, month):
    root = Path(root).resolve(); case = _case(root, month)
    current = registry(root, case)
    items = claim_items(root, case)
    for item in items:
        item['expected_unit_by_axis'] = {'work': item['unit'], 'contract': item['unit'], 'actual_cost': 'KRW'}
    return {'month': month, 'revision': case['revision'], 'case_identity': identity(case),
            'claim_sha256': current['claim_sha256'], 'period': deepcopy(case['period']),
            'currency': case['currency'], 'registry_revision': current['revision'],
            'targets': items, 'registered_entries': len(current['entries'])}


def _key(entry):
    return entry.get('claim_path',''),entry['axis'], entry['invoice_row']


def _selector_identity(source):
    selector = {k: v for k, v in source['selector'].items() if k not in ('value', 'confirmed')}
    return source['sha256'].lower(), _hash(selector)


def verify_registry(root, case, current):
    if any(current.get(k) != value for k, value in {
        'month': case['month'], 'case_revision': case['revision'], 'case_identity': identity(case),
        'claim_sha256': claims_hash(case), 'period': case['period'], 'currency': case['currency'],
        'amount_basis': 'supply_amount'}.items()):
        raise ValueError('증빙의 청구월·자료 버전·기간·통화가 현재 등록과 다릅니다.')
    used = set(); keys = set()
    for entry in current['entries']:
        entry_claim(case,entry)
        if _key(entry) in keys:
            raise ValueError('같은 청구행·비교항목의 증빙이 중복되었습니다.')
        keys.add(_key(entry))
        for source in entry['sources']:
            path = safe(root, source['path'], SOURCE / case['month'] / case['revision'])
            if not path.is_file() or digest(path).lower() != source['sha256'].lower():
                raise ValueError('등록 증빙 원본이 없거나 변경되었습니다.')
            key = _selector_identity(source)
            if key in used:
                raise ValueError('같은 증빙 파일의 같은 숫자 위치를 두 번 합산할 수 없습니다.')
            used.add(key)


def prepare_registration(root, case, payload):
    """Called under the month's execution lock. No result pointer changes here."""
    root = Path(root).resolve(); old = registry(root, case)
    if not isinstance(payload, dict):
        raise ValueError('증빙 등록 내용이 올바르지 않습니다.')
    expected = {'month': case['month'], 'revision': case['revision'], 'case_identity': identity(case),
                'claim_sha256': old['claim_sha256'], 'period': case['period'],
                'currency': case['currency'], 'amount_basis': 'supply_amount'}
    for field, value in expected.items():
        if payload.get(field) != value:
            raise ValueError('증빙 등록 범위가 다릅니다: ' + field)
    if type(payload.get('expected_registry_revision')) is not int or payload['expected_registry_revision'] != old['revision']:
        raise ValueError('다른 증빙이 먼저 등록되었습니다. 현재 청구 대상을 다시 조회해 주세요.')
    if type(payload.get('replace', False)) is not bool:
        raise ValueError('기존 증빙 교체 여부를 명시해 주세요.')
    incoming = payload.get('entries')
    if not isinstance(incoming, list) or not 1 <= len(incoming) <= 100:
        raise ValueError('증빙 연결 항목은 1~100개로 지정해 주세요.')
    items = {(i['claim_path'],i['invoice_row']): i for i in claim_items(root, case)}
    entries = {_key(e): deepcopy(e) for e in old['entries']}; seen = set(); planned = []
    for raw in incoming:
        if not isinstance(raw, dict) or raw.get('axis') not in AXES or type(raw.get('invoice_row')) is not int:
            raise ValueError('증빙을 연결할 청구행·비교 항목을 확인해 주세요.')
        path=entry_claim(case,raw)
        if (path,raw['invoice_row']) not in items:raise ValueError('선택 청구 파일에 해당 청구행이 없습니다.')
        key = _key(raw)
        if key in seen: raise ValueError('같은 청구행·비교항목을 두 번 등록할 수 없습니다.')
        seen.add(key)
        if key in entries and payload.get('replace') is not True:
            raise ValueError('이미 증빙이 있는 청구항목입니다. 기존 증빙 교체를 명시해 주세요.')
        if any(raw.get(k) is not True for k in ('scope_confirmed', 'coverage_complete', 'allocation_confirmed')):
            raise ValueError('해당 청구행·전체 기간·전체 금액 또는 작업량 범위와 배분을 확인해 주세요.')
        item = items[(path,raw['invoice_row'])]
        unit = 'KRW' if raw['axis'] == 'actual_cost' else item['unit']
        if not unit or raw.get('unit') != unit:
            raise ValueError('증빙과 청구행의 수량·단가 단위가 다릅니다.')
        sources = raw.get('sources')
        if not isinstance(sources, list) or not 1 <= len(sources) <= 100 or (raw['axis'] == 'contract' and len(sources) != 1):
            raise ValueError('숫자 위치를 지정한 증빙이 필요합니다. 적용 단가는 한 값으로 지정해 주세요.')
        entry = {k: deepcopy(raw[k]) for k in ('axis', 'invoice_row', 'unit', 'scope_confirmed', 'coverage_complete', 'allocation_confirmed')}
        if raw.get('claim_path'):entry['claim_path']=path
        entry['sources'] = []
        for source in sources:
            token = source.get('token') if isinstance(source, dict) else None
            if not isinstance(token, str) or Path(token).name != token or not token or '/' in token or '\\' in token or ':' in token:
                raise ValueError('증빙 업로드 식별자가 올바르지 않습니다.')
            upload = safe(root, UPLOADS / token, UPLOADS)
            suffix = upload.suffix.lower(); selector = source.get('selector')
            if not upload.is_file() or suffix not in FORMATS or not isinstance(selector, dict) or selector.get('format') != FORMATS[suffix]:
                raise ValueError('CSV·XLSX 숫자 위치 또는 확인한 PDF·이미지 전사값을 지정해 주세요.')
            if selector['format'] == 'reviewed_transcription' and (selector.get('confirmed') is not True or not str(selector.get('reference', '')).strip()):
                raise ValueError('PDF·이미지의 원본 페이지·위치와 숫자 전사 확인이 필요합니다.')
            sha = digest(upload)
            relative = SOURCE / case['month'] / case['revision'] / (sha + suffix)
            dest = safe(root, relative, SOURCE / case['month'] / case['revision'])
            planned.append((upload, dest, sha))
            entry['sources'].append({'path': relative.as_posix(), 'sha256': sha, 'name': token,
                                     'selector': deepcopy(selector)})
        entries[key] = entry
    proposed = deepcopy(old)
    proposed.update(revision=old['revision'] + 1, entries=list(entries.values()),
                    registered_at=datetime.now(timezone.utc).isoformat())
    # Reject duplicate source positions before any source copy or pointer write.
    positions = [_selector_identity(s) for e in proposed['entries'] for s in e['sources']]
    if len(positions) != len(set(positions)):
        raise ValueError('같은 증빙 파일의 같은 숫자 위치를 두 번 합산할 수 없습니다.')
    for upload, dest, sha in planned:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            temporary = dest.with_name(dest.name + '.' + uuid.uuid4().hex + '.tmp')
            try:
                shutil.copyfile(upload, temporary)
                if digest(upload) != sha or digest(temporary) != sha:
                    raise ValueError('등록 중 증빙이 변경되었습니다. 다시 업로드해 주세요.')
                temporary.replace(dest)
            finally:
                temporary.unlink(missing_ok=True)
        if digest(upload) != sha or digest(dest) != sha:
            raise ValueError('등록 중 증빙이 변경되었습니다. 다시 업로드해 주세요.')
    verify_registry(root, case, proposed)
    for entry in proposed['entries']:
        for source in entry['sources']: source_value(root, source)
    return proposed


def apply_to_result(root, case, result, current):
    """Append independently scoped proof comparisons; preserve all base checks."""
    from monthly_verification import FIELDS, text
    verify_registry(root, case, current)
    items = {(item.get('claim_path') or entry_claim(case,{}),item['row']): item for item in result['fee_items']}
    checks = []
    for entry in current['entries']:
        claim_path=entry_claim(case,entry)
        item = items.get((claim_path,entry['invoice_row']))
        if item is None: raise ValueError('등록 증빙의 청구행을 재계산 결과에서 찾지 못했습니다.')
        axis = entry['axis']; label, field, col = AXES[axis]
        source_sheet=item.get('source_sheet','청구내역');col=item.get('source_columns',{}).get(col,col)
        unit = 'KRW' if axis == 'actual_cost' else str(item['unit'] or '')
        if not unit or unit != entry['unit']: raise ValueError('재계산한 청구와 증빙 단위가 다릅니다.')
        values = [source_value(root, source) for source in entry['sources']]
        expected = sum((v for v, _ in values), Decimal(0)); actual = numeric(item[field])
        diff = actual - expected if actual is not None else None
        status = 'data_unavailable' if diff is None else 'match' if diff == 0 else 'difference'
        result_label = {'match': '정상', 'difference': '불일치', 'data_unavailable': '미검증'}[status]
        group = '추가 증빙 · ' + label
        row = {k: '' for k in FIELDS}
        row.update(조회구분=group + ' / ' + result_label, 검사=group, 결과=result_label,
                   사유구분='자료 부족' if diff is None else '차이' if diff else '',
                   사유=(f'청구서 {source_sheet}!{col}{entry["invoice_row"]}에 비교할 숫자가 없음' if diff is None else
                       item['label'] + ' · 담당자가 지정한 청구행·기간·전체 범위의 증빙 숫자와 비교'),
                   청구원본=claim_path, 청구위치=f'{source_sheet}!{col}{entry["invoice_row"]}',
                   청구값=text(actual), 비교값=text(expected), 차이=text(diff),
                   단위=('KRW/' + unit if axis == 'contract' else unit),
                   적용기준='선택 청구행·기간·단위·공급가액·전체 범위를 확인한 증빙 비교. 다른 검사의 승인 아님.',
                   계산근거='; '.join(ref for _, ref in values))
        row.update(id='evidence.' + _hash([claim_path,axis,entry['invoice_row']])[:20], status=status,
                   check_kind='monthly_evidence', axis=axis, invoice_row=entry['invoice_row'],
                   evidence_registry_revision=current['revision'])
        checks.append(row)
    result['checks'].extend(checks)
    result['evidence_comparisons'] = deepcopy(checks)
    result['evidence_registry'] = deepcopy(current)
    result['full_invoice_validated'] = False
    return result


def register_and_execute(root, month, payload, use_model=False, model=None):
    from monthly_workflow import execute
    return execute(root, month, request=f'{month} 추가 증빙 등록 후 재검증',
                   use_model=use_model, model=model, evidence_registration=payload)
