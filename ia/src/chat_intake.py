"""C1: read-only, version-bound chat target and scope intake.

This module only returns conversation drafts. It never writes decisions, applies
rules, changes a verdict, or executes a reconciliation. Raw identifiers remain
local; model-facing candidates have opaque aliases.
"""
from __future__ import annotations

import copy
import csv
from functools import lru_cache
import hashlib
import io
import json
from pathlib import Path
import re
import uuid

BASE = Path('data/demo_company')
ISSUES = BASE / 'derived/검토필요_전체목록.csv'
POLICY = Path('ia/config/demo_company_supporting_evidence.json')
IDENTIFIERS = ('주문번호', '입고예정번호', '송장번호', '품번')
INTENTS = {'conversation', 'query_results', 'inspect_evidence', 'investigate_issue',
           'run_reconciliation', 'set_disposition', 'upsert_rule', 'revoke_rule',
           'replay_version', 'activate_revision', 'register_materials',
           'register_evidence', 'cancel_job', 'unsupported_request', 'clarify', 'cancel_draft'}
CHANGE_INTENTS = {'set_disposition', 'upsert_rule'}
PROPOSAL_FIELDS = {'intent', 'request_mode', 'month', 'target_aliases', 'target_selection',
                   'scope', 'application', 'summary', 'reason', 'criterion', 'effective_from_month',
                   'missing_fields', 'reply'}
MONTH_RE = re.compile(r'20\d{2}-(?:0[1-9]|1[0-2])\Z')


def _digest(value):
    data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(data).hexdigest()


def _json(path, default=None):
    return json.loads(path.read_text('utf-8-sig')) if path.is_file() else default


def _safe_file(root, relative):
    path = root / relative
    if not path.resolve().is_relative_to(root):
        raise ValueError('현재 작업 폴더 밖의 자료는 선택할 수 없습니다.')
    for item in (path, *path.parents):
        if item == root:
            break
        if item.is_symlink() or (hasattr(item, 'is_junction') and item.is_junction()):
            raise ValueError('외부 경로가 연결된 자료는 선택할 수 없습니다.')
    return path


def _current(root):
    root = Path(root).resolve()
    policy = _json(_safe_file(root, POLICY), {})
    month = policy.get('invoice_month')
    if not isinstance(month, str) or not MONTH_RE.fullmatch(month):
        return {'month': None, 'version': None, 'rows': []}
    path = _safe_file(root, ISSUES)
    if not path.is_file():
        return {'month': month, 'version': None, 'rows': []}
    raw = path.read_bytes()
    rows = list(csv.DictReader(io.StringIO(raw.decode('utf-8-sig'))))
    files = {ISSUES.as_posix(): _digest(raw)}
    paths = [POLICY, BASE / 'derived/workflow_result.json', Path('ia/config/project_scope.json')]
    paths.extend(p.relative_to(root) for p in (root / 'ia/config').glob('demo_company*decisions.json'))
    for relative in sorted(set(paths), key=str):
        p = _safe_file(root, relative)
        files[str(relative)] = _digest(p.read_bytes()) if p.is_file() else None
    issue_count=len(rows)
    rate_error=None
    if month=='2029-07':
        from rate_rules import catalog_rows
        try:
            extra,fingerprint=catalog_rows(root)
            rows.extend(extra)
            if fingerprint:files['invoice_rate_catalog']=fingerprint
        except (ValueError,OSError,KeyError,TypeError) as exc:
            rate_error=str(exc)
            files['invoice_rate_catalog_error']=rate_error
    # Cheap change detection is intentionally identified as metadata, not a claim
    # that each large source workbook has been re-hashed/reverified here.
    sources = []
    source_dir = _safe_file(root, BASE / 'source')
    if source_dir.exists():
        for p in sorted(source_dir.rglob('*')):
            _safe_file(root, p.relative_to(root))
            if p.is_file() and not p.name.startswith('~$'):
                stat = p.stat()
                sources.append([p.relative_to(root).as_posix(), stat.st_size, stat.st_mtime_ns])
    version = _digest({'month': month, 'result_and_decision_hashes': files, 'source_metadata': sources})
    return {'month': month, 'version': version, 'rows': rows, 'issue_count':issue_count, 'rate_catalog_error':rate_error,
            'source_names': [Path(item[0]).name for item in sources]}


def _target(row, index, version):
    field = next((key for key in IDENTIFIERS if row.get(key) and row[key] not in ('미기재', '전체(주문 합계)', '기재 없음')), None)
    identifier = f'{field}: {row[field]}' if field else row.get('적용기준', '비용 요약행').split(';')[0][:90]
    locator = row.get('청구위치') or row.get('WMS위치') or ''
    # Avoid expanding dozens of WMS row references into the target selection card.
    short_locator = locator.split(';')[0].split(' / ')[-1]
    source = row.get('청구원본', '')
    if not source and ' / ' in locator:
        source = locator.split(' / ')[0]
    target = {'target_id': 'target-' + _digest({'version': version, 'row_index': index, 'row': row})[:32],
            'issue_id': str(index), 'label': f"{row.get('검사', '확인 항목')} · {identifier}",
            'check': row.get('검사', ''), 'status': row.get('결과', ''),
            'source': Path(source).name if source else '원본 위치 미기재', 'locator': short_locator,
            'date': row.get('일자', ''), 'order_id': row.get('주문번호', ''),
            'inbound_id': row.get('입고예정번호', ''), 'item_id': row.get('품번', ''),
            'tracking_id': row.get('송장번호', ''), 'claim_value': row.get('청구값', ''),
            'comparison_value': row.get('비교값', ''), 'difference': row.get('차이', ''),
            'unit': row.get('단위', ''), 'reason': row.get('사유', ''),
            'applied_criterion': row.get('적용기준', '')}
    if row.get('_catalog_kind') == 'invoice_rate' and row.get('_cost_selector'):
        target['cost_selector'] = row['_cost_selector']
    return target


def select_targets(root, issue_ids, conversation_id):
    if not isinstance(conversation_id, str) or not conversation_id:
        raise ValueError('대화를 먼저 선택해 주세요.')
    if not isinstance(issue_ids, (list, tuple)) or not issue_ids or len(issue_ids) > 1000:
        raise ValueError('확인 목록에서 청구건을 선택해 주세요.')
    current = _current(root)
    targets = []
    for value in dict.fromkeys(str(v) for v in issue_ids):
        if not value.isdecimal() or int(value) >= len(current['rows']):
            raise ValueError('확인 목록이 변경되었습니다. 대상을 다시 선택해 주세요.')
        targets.append(_target(current['rows'][int(value)], int(value), current['version']))
    return {'token': uuid.uuid4().hex, 'conversation_id': conversation_id,
            'month': current['month'], 'version': current['version'], 'targets': targets,
            'source_version_kind': 'result_decision_hashes_and_source_metadata'}


def validate_selection(root, selection):
    if not isinstance(selection, dict) or not selection.get('token') or not selection.get('targets'):
        raise ValueError('선택한 청구건이 없습니다.')
    current = _current(root)
    if selection.get('version') != current['version'] or selection.get('month') != current['month']:
        raise ValueError('자료나 검증 결과가 바뀌었습니다. 확인 목록에서 다시 선택해 주세요.')
    for target in selection['targets']:
        index = str(target.get('issue_id', ''))
        if not index.isdecimal() or int(index) >= len(current['rows']):
            raise ValueError('선택 대상이 현재 확인 목록에 없습니다.')
        actual = _target(current['rows'][int(index)], int(index), current['version'])
        if target.get('target_id') != actual['target_id']:
            raise ValueError('선택 대상의 원본 위치가 달라졌습니다. 다시 선택해 주세요.')
    return True


def _mentioned(value, text, field=None):
    if not isinstance(value, str) or len(value.strip()) < 4 or value in ('미기재', '기재 없음') or value.startswith('전체('):
        return False
    if value.isdecimal() and (not field or field not in text):
        return False
    return re.search(r'(?<![A-Za-z0-9])' + re.escape(value) + r'(?![A-Za-z0-9])', text, re.I) is not None


def _unresolved_identifiers(request, rows):
    """An explicit but absent identifier must not fall back to the old target."""
    known = {str(row.get(field, '')).casefold() for row in rows for field in IDENTIFIERS}
    labelled = re.findall(r'(?:주문번호|입고예정번호|송장번호|품번)(?:\([^)]*\))?\s*(?:은|는|이|가|[:=])?\s*([A-Za-z0-9][A-Za-z0-9_-]{3,})', request)
    codes = re.findall(r'(?<![A-Za-z0-9])(?:PO|SB|DS|MV|KBT|SC)[A-Za-z0-9_-]*\d[A-Za-z0-9_-]*(?![A-Za-z0-9])', request, re.I)
    mentioned = [str(row[field]) for row in rows for field in IDENTIFIERS if _mentioned(row.get(field), request, field)]
    return [value for value in dict.fromkeys(labelled + codes) if value.casefold() not in known
            and not any(full.casefold().startswith(value.casefold()) for full in mentioned)]


@lru_cache(maxsize=8)
def _replacement_regex(keys):
    return re.compile('|'.join(re.escape(key) for key in sorted(keys, key=len, reverse=True)), re.I) if keys else None


def sanitize_text(text, context):
    text = str(text)
    replacements = {k.casefold(): v for k, v in context.get('identifier_replacements', {}).items()}
    pattern = _replacement_regex(tuple(replacements))
    if pattern:
        text = pattern.sub(lambda m: replacements[m[0].casefold()], text)
    # History/reasons can contain an absent identifier from an earlier turn,
    # which is not in this turn's replacement map. Mask explicit identifier
    # fields after alias substitution, without treating ordinary amounts or
    # unlabelled prose as target identifiers.
    labelled_identifier = r'((?:주문\s*번호|입고\s*예정\s*번호|송장\s*번호|품번)(?:\([^)]*\))?\s*(?:은|는|이|가|[:=])?\s*[\"\']?)([A-Za-z0-9][A-Za-z0-9_.:/-]{3,})'
    def mask_labelled(match):
        value = match[2]
        is_code = any(c.isdigit() or c in '_-.:/' for c in value) or value.isupper()
        return match[1] + '[식별자 제외]' if is_code else match[0]
    text = re.sub(labelled_identifier, mask_labelled, text)
    text = re.sub(r'[\w.+-]+@[\w.-]+', '[이메일 제외]', text)
    text = re.sub(r'(?:[A-Za-z]:[\\/]|data/)[^\s]+', '[파일 경로 제외]', text)
    text = re.sub(r'(?<!\d)\d{8,}(?!\d)', '[식별번호 제외]', text)
    text = re.sub(r'\b(?:PO|SB|DS|MV|KBT|SC)[A-Z0-9_-]+\b', '[주문 식별자 제외]', text)
    return text[:2000]


def prepare_context(root, request, month=None, selection=None, pending=None):
    current = _current(root)
    error = None
    if pending is not None and (not isinstance(pending, dict) or not isinstance(pending.get('targets', []), list)
                               or any(not isinstance(t, dict) or not t.get('target_id') or not str(t.get('issue_id', '')).isdecimal()
                                      for t in pending.get('targets', []))):
        pending = None
        error = '정리 중인 요청의 대상 정보가 올바르지 않습니다. 청구건을 다시 선택해 주세요.'
    active_pending = pending if pending and pending.get('status') not in ('cancelled', 'version_conflict', 'unsupported', 'read_only') else None
    month = month or (active_pending or {}).get('month')
    selected = []
    if selection:
        try:
            validate_selection(root, selection)
            if month and month != selection['month']:
                error = '요청한 월과 선택한 청구의 월이 다릅니다. 해당 월의 청구를 다시 선택해 주세요.'
            else:
                selected = [_target(current['rows'][int(t['issue_id'])], int(t['issue_id']), current['version']) for t in selection['targets']]
                month = month or selection['month']
        except ValueError as exc:
            error = str(exc)
    if pending and pending.get('version') and pending['version'] != current['version']:
        error = '자료나 검증 결과가 바뀌어 이전 요청을 사용할 수 없습니다. 대상을 다시 선택해 주세요.'
    candidates = []
    replacements = {row[key]: '[식별자 제외]' for row in current['rows'] for key in IDENTIFIERS
                    if row.get(key) and len(row[key]) >= 4 and row[key] not in ('미기재', '기재 없음') and not row[key].startswith('전체(')}
    source_names = set(current.get('source_names', []))
    source_names.update(Path(row['청구원본']).name for row in current['rows'] if row.get('청구원본'))
    replacements.update({name: '[파일명 제외]' for name in source_names if name})
    for name in source_names:
        for person in re.findall(r'(?:팀|담당자)\s*([가-힣]{2,4})(?=[_.(\s]|$)', name):
            replacements[person] = '[담당자명 제외]'
    if month in (None, current['month']) and current['version']:
        for i, row in enumerate(current['rows']):
            if any(_mentioned(row.get(key), request, key) for key in IDENTIFIERS):
                candidates.append(_target(row, i, current['version']))
            elif row.get('_catalog_kind')=='invoice_rate':
                row_refs=re.findall(r'청구내역\s*[!·:]?\s*[LMN]?\s*(\d{1,3})\s*(?:행)?',request)
                label=row.get('_rate_label','')
                explicit_name=any(len(part.strip())>=3 and part.strip() in request for part in label.split(' · '))
                if str(row['_invoice_row']) in row_refs or (not row_refs and explicit_name and re.search(r'단가|기준|요금',request)):
                    candidates.append(_target(row,i,current['version']))
        if candidates:
            month = month or current['month']
    unresolved_identifiers = _unresolved_identifiers(request, current['rows'])
    replacements.update({value: '[찾지 못한 식별자]' for value in unresolved_identifiers})
    mentioned = bool(candidates)
    old_targets = (active_pending or {}).get('targets') or selected
    target_changed = bool(old_targets) and (bool(unresolved_identifiers) or mentioned and {t['target_id'] for t in candidates} != {t['target_id'] for t in old_targets})
    if mentioned and selected and {t['target_id'] for t in candidates} != {t['target_id'] for t in selected}:
        selected = []
    if unresolved_identifiers:
        selected = []
        candidates = []
    elif not candidates:
        candidates = selected or copy.deepcopy((active_pending or {}).get('targets', []))
    if pending and month and pending.get('month') != month:
        candidates = selected or []
    candidate_replacements = {}
    for index, target in enumerate(candidates[:8], 1):
        target['alias'] = f'T{index}'
        for key in ('order_id', 'inbound_id', 'tracking_id', 'item_id'):
            value = target.get(key)
            if value and len(value) >= 4 and not value.startswith('전체('):
                candidate_replacements.setdefault(value, []).append(target['alias'])
    replacements.update({value: '[대상 ' + '/'.join(aliases) + ']' for value, aliases in candidate_replacements.items()})
    output = {'month': month, 'version': current['version'], 'selection': copy.deepcopy(selection),
              'candidates': candidates, 'selected_targets': selected, 'candidate_count': len(candidates),
              'identifier_replacements': replacements, 'selection_error': error, 'target_changed': target_changed,
              'unresolved_target': bool(unresolved_identifiers),
              'target_support': month in (None, current['month']) and bool(current['version'])}
    output['model_context'] = {'month': month, 'candidate_count': len(candidates),
            'has_explicit_selection': bool(selected), 'selection_valid': not error,
            'target_support': output['target_support'],
            'unresolved_target': output['unresolved_target'],
            'candidates': [{'alias': t['alias'], 'check': sanitize_text(t['check'], output), 'status': sanitize_text(t['status'], output)} for t in candidates[:8]],
            'pending': {k: (pending or {}).get(k) for k in ('intent', 'scope', 'application', 'effective_from_month', 'status')},
            'business_write_supported': False}
    output['sanitized_request'] = sanitize_text(request, output)
    return output


def _proposal_check(proposal):
    if not isinstance(proposal, dict) or set(proposal) - PROPOSAL_FIELDS:
        raise ValueError('지원하지 않는 명령 필드입니다.')
    if proposal.get('intent') not in INTENTS:
        raise ValueError('지원하지 않는 명령입니다.')
    for key, values in [('request_mode', ('request', 'question', 'negated')),
                        ('scope', ('once', 'future', 'unspecified')),
                        ('application', ('current_case', 'future_only', 'current_and_future', 'unspecified')),
                        ('target_selection', ('selected', 'mentioned', 'all_filtered', 'unspecified'))]:
        if proposal.get(key, 'unspecified') not in values:
            raise ValueError(f'잘못된 요청 형식입니다: {key}')
    aliases = proposal.get('target_aliases', [])
    if not isinstance(aliases, list) or len(aliases) > 8 or any(not isinstance(a, str) for a in aliases):
        raise ValueError('대상 후보 형식이 올바르지 않습니다.')
    for key in ('summary', 'reason', 'criterion', 'reply'):
        value = proposal.get(key)
        if value is not None and (not isinstance(value, str) or len(value) > 2000):
            raise ValueError('요청 설명은 2,000자 이내여야 합니다.')
    for key in ('month', 'effective_from_month'):
        value = proposal.get(key)
        if value is not None and (not isinstance(value, str) or not MONTH_RE.fullmatch(value)):
            raise ValueError('월은 YYYY-MM 형식이어야 합니다.')


def _set_status(draft, status, message, field=None, question=None, choices=()):
    draft.update(status=status, state=status, message=message, execution_available=False,
                 business_saved=False, changes_applied=False, questions=[], options=[])
    if field:
        opts = [{'label': label, 'value': value} for value, label in choices]
        draft['questions'] = [{'field': field, 'question': question or message, 'options': opts}]
        choice = 'select_target' if field == 'target' else field
        draft['options'] = [{'label': o['label'], 'choice': choice, 'value': o['value']} for o in opts]
    if status not in ('cancelled', 'read_only'):
        draft['options'].append({'label': '요청 취소', 'choice': 'cancel', 'value': None})
    draft['target_count'] = len(draft.get('targets', []))
    return draft


def _advance(draft):
    if not draft.get('targets'):
        candidates = draft.get('candidates', [])
        return _set_status(draft, 'awaiting_target', '어느 청구건에 적용할지 선택해 주세요.', 'target',
                           choices=[(t['target_id'], t['label']) for t in candidates[:8]])
    if draft.get('scope') == 'once' and draft.get('application') in ('future_only', 'current_and_future'):
        draft['scope'] = 'unspecified'
        draft['application'] = 'unspecified'
    if draft.get('scope') not in ('once', 'future'):
        return _set_status(draft, 'awaiting_scope', '이번 청구에만 적용할지, 앞으로도 적용할지 선택해 주세요.', 'scope',
                           choices=[('once', '이번 청구만'), ('future', '앞으로도 적용')])
    if draft['scope'] == 'once':
        draft['application'] = 'current_case'
    elif draft.get('application') not in ('future_only', 'current_and_future'):
        return _set_status(draft, 'awaiting_application', '앞으로의 청구에만 적용할까요, 이번 청구에도 함께 적용할까요?', 'application',
                           choices=[('future_only', '앞으로만 적용'), ('current_and_future', '이번 청구와 앞으로 모두')])
    if draft['scope'] == 'future' and (not draft.get('criterion') or not draft.get('effective_from_month')):
        return _set_status(draft, 'awaiting_details', '앞으로 적용할 대상의 식별 조건과 시작 청구월을 알려주세요. 예: 출고유형이 특정 유형인 건 · 2029-08부터.',
                           'criterion', choices=[])
    if draft.get('intent') == 'set_disposition' and not draft.get('reason'):
        return _set_status(draft, 'awaiting_details', '담당자 처리 사유를 알려주세요. 원래 수량·금액 차이는 유지됩니다.', 'reason')
    if draft.get('intent') == 'upsert_rule' and not draft.get('criterion'):
        return _set_status(draft, 'awaiting_details', '어떤 비교 기준으로 변경하려는지 알려주세요.', 'criterion')
    return _set_status(draft, 'draft_ready', '요청 정리가 완료되었습니다. 예외·비교 기준은 아직 저장하거나 적용하지 않았습니다. 저장 내용 확인에서 검토 후 기록으로 저장할 수 있습니다. 저장만으로 검증 결과에 적용되거나 재대사되지 않습니다.')


def apply_proposal(root, proposal, context, pending=None):
    _proposal_check(proposal)
    if proposal.get('request_mode') != 'request':
        result = _set_status({}, 'read_only', proposal.get('reply') or '설명 요청으로 처리했습니다. 업무 기준은 변경하지 않습니다.')
        result['preserve_pending'] = True
        return result
    if proposal['intent'] == 'cancel_draft':
        return _set_status(copy.deepcopy(pending or {}), 'cancelled', '정리 중인 요청을 취소했습니다. 검증 결과는 변경되지 않았습니다.')
    if proposal['intent'] not in CHANGE_INTENTS | {'clarify'}:
        status = 'read_only' if proposal['intent'] in {'conversation', 'query_results', 'inspect_evidence', 'investigate_issue', 'run_reconciliation', 'register_materials', 'register_evidence'} else 'unsupported'
        message = '이 명령의 실행 도구는 아직 연결되지 않았습니다. 기준·예외·검증 결과를 변경하지 않았습니다.' if status == 'unsupported' else proposal.get('reply') or '조회 요청으로 처리했습니다.'
        result = _set_status({}, status, message)
        result['intent'] = proposal['intent']
        if status == 'unsupported':
            result['draft_id'] = result['pending_id'] = uuid.uuid4().hex
        return result
    # The model can take minutes. Never publish a current-looking target draft
    # from the version that existed before that wait.
    if context.get('version') and _current(root)['version'] != context['version']:
        draft = copy.deepcopy(pending or {})
        draft.setdefault('draft_id', uuid.uuid4().hex)
        draft.update(pending_id=draft['draft_id'], targets=[], clear_selection=True)
        return _set_status(draft, 'version_conflict', '응답을 기다리는 동안 자료나 검증 결과가 바뀌었습니다. 청구건을 다시 선택해 주세요.')
    previous = pending if isinstance(pending, dict) and pending.get('intent') in CHANGE_INTENTS and pending.get('status') not in ('cancelled', 'version_conflict', 'unsupported', 'read_only') else {}
    previous_intent = previous.get('intent')
    if previous and proposal['intent'] not in ('clarify', previous['intent']):
        previous = {}
    missing_target = context.get('unresolved_target') or ('target' in proposal.get('missing_fields', []) and not proposal.get('target_aliases'))
    if context.get('target_changed') or missing_target:
        previous = {}
    draft = copy.deepcopy(previous)
    draft.setdefault('draft_id', uuid.uuid4().hex)
    draft['pending_id'] = draft['draft_id']
    draft.update(version=context.get('version'), month=context.get('month'), candidates=copy.deepcopy(context.get('candidates', [])),
                 clear_selection=bool(context.get('target_changed') or missing_target))
    if proposal['intent'] != 'clarify':
        draft['intent'] = proposal['intent']
    elif (context.get('target_changed') or missing_target) and previous_intent:
        # A correction can preserve the kind of work while resetting the old
        # targets/scope/reason; it is never an approval of the new target.
        draft['intent'] = previous_intent
    if not draft.get('intent'):
        return _set_status(draft, 'awaiting_target', '먼저 확인 목록에서 청구건을 선택하고 요청 내용을 알려주세요.')
    if context.get('selection_error'):
        draft['targets'] = []
        draft['clear_selection'] = True
        return _set_status(draft, 'version_conflict', context['selection_error'])
    if not context.get('target_support'):
        return _set_status(draft, 'unsupported', '해당 월의 개별 청구건 연결은 아직 지원하지 않습니다. 월별 검증에서 자료와 결과를 먼저 확인해 주세요.')
    if proposal.get('month') and draft.get('month') and proposal['month'] != draft['month']:
        draft['targets'] = []
        return _set_status(draft, 'version_conflict', '요청한 월과 선택한 청구월이 다릅니다. 해당 월의 청구건을 다시 선택해 주세요.')
    if previous and previous.get('month') != draft.get('month'):
        draft['targets'] = []
        return _set_status(draft, 'version_conflict', '청구월이 바뀌었습니다. 대상과 적용 범위를 다시 선택해 주세요.')
    if missing_target:
        draft['targets'] = []
        draft['candidates'] = []
        return _set_status(draft, 'awaiting_target', '말씀하신 새 대상을 현재 확인 목록에서 확정하지 못했습니다. 해당 청구건을 선택하거나 주문번호·품번·원본 위치를 알려주세요.', 'target')
    for key in ('summary', 'reason', 'criterion', 'effective_from_month'):
        if proposal.get(key):
            draft[key] = proposal[key]
    for key in ('scope', 'application'):
        if proposal.get(key) not in (None, 'unspecified'):
            draft[key] = proposal[key]
    candidates = context.get('candidates', [])
    aliases = proposal.get('target_aliases', [])
    alias_map = {t.get('alias'): t for t in candidates[:8]}
    if any(alias not in alias_map for alias in aliases):
        raise ValueError('현재 근거에 없는 대상 후보를 모델이 반환했습니다.')
    explicit = context.get('selected_targets', [])
    if explicit and proposal.get('target_selection') == 'selected':
        draft['targets'] = copy.deepcopy(explicit)
    elif len(candidates) == 1 and (aliases or proposal.get('target_selection') in ('mentioned', 'selected')):
        draft['targets'] = copy.deepcopy(candidates)
    elif candidates and not draft.get('targets'):
        # A model selecting one alias among ambiguous original matches is not a
        # user target selection. Ask locally with all visible candidates.
        draft['targets'] = []
    return _advance(draft)


def choose(root, pending, choice, value=None):
    if not isinstance(pending, dict) or not pending.get('pending_id'):
        raise ValueError('진행 중인 요청이 없습니다.')
    draft = copy.deepcopy(pending)
    if choice == 'cancel':
        return _set_status(draft, 'cancelled', '정리 중인 요청을 취소했습니다. 검증 결과는 변경되지 않았습니다.')
    if draft.get('status') in ('cancelled', 'version_conflict', 'unsupported', 'read_only'):
        raise ValueError('종료되거나 변경된 요청입니다. 청구건을 다시 선택해 주세요.')
    current = _current(root)
    if draft.get('version') != current['version'] or draft.get('month') != current['month']:
        return _set_status(draft, 'version_conflict', '자료나 검증 결과가 바뀌었습니다. 대상을 다시 선택해 주세요.')
    if choice == 'select_target':
        values = value if isinstance(value, list) else [value]
        possible = {t['target_id']: t for t in draft.get('candidates', [])}
        if not values or any(v not in possible for v in values):
            raise ValueError('표시된 후보 중에서 대상을 선택해 주세요.')
        draft['targets'] = [possible[v] for v in dict.fromkeys(values)]
    elif choice == 'scope':
        if value not in ('once', 'future'):
            raise ValueError('이번 청구만 또는 앞으로도 적용을 선택해 주세요.')
        if value != draft.get('scope'):
            draft['application'] = 'unspecified'
        draft['scope'] = value
    elif choice == 'application':
        if draft.get('scope') != 'future' or value not in ('future_only', 'current_and_future'):
            raise ValueError('앞으로 적용할 범위를 다시 선택해 주세요.')
        draft['application'] = value
    elif choice == 'criterion':
        if isinstance(value, str):
            value = {'predicate': value}
        if not isinstance(value, dict) or set(value) - {'predicate', 'effective_from_month'}:
            raise ValueError('적용 조건과 시작월 형식이 올바르지 않습니다.')
        criterion = value.get('predicate', '').strip()
        month = value.get('effective_from_month')
        if not criterion or len(criterion) > 2000 or (month is not None and not MONTH_RE.fullmatch(str(month))):
            raise ValueError('적용 조건과 YYYY-MM 형식의 시작월을 입력해 주세요.')
        draft['criterion'] = criterion
        if month:
            draft['effective_from_month'] = month
    elif choice == 'reason':
        if not isinstance(value, str) or not value.strip() or len(value) > 2000:
            raise ValueError('처리 사유를 2,000자 이내로 입력해 주세요.')
        draft['reason'] = value.strip()
    else:
        raise ValueError('지원하지 않는 요청 선택입니다.')
    return _advance(draft)
