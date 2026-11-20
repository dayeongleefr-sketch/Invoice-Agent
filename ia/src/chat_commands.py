"""C2: confirmed, source-bound intent records; no calculation/rule activation.

All actor/session values originate on the server. Full file hashes bind a
preview to its source state; retained manifests are not exact-replay assets.
"""
from __future__ import annotations

import copy
import base64
import hashlib
import json
from pathlib import Path
import re
import uuid

import chat_intake
from business_memory import BusinessMemory

BASE = Path('data/demo_company')
REPORT = BASE / 'runs/가상센터/report.md'
ISSUES = BASE / 'derived/검토필요_전체목록.csv'
POLICY = Path('ia/config/demo_company_supporting_evidence.json')
WORKFLOW = BASE / 'derived/workflow_result.json'


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode('utf-8')).hexdigest()


def _safe(root, relative):
    return chat_intake._safe_file(root, relative)


def _read_json(path, default=None):
    return json.loads(path.read_text('utf-8-sig')) if path.is_file() else default


def draft_hash(draft):
    return digest(draft)


def source_snapshot(root):
    """Read every registered source byte, configs, report, list and result files.

    No snapshot/aggregate/cache-seeding call can write business artifacts here.
    The source bytes themselves are NOT copied into a replay archive in C2.
    """
    root = Path(root).resolve()
    source = _safe(root, BASE / 'source')
    paths = {POLICY, WORKFLOW, ISSUES, REPORT}
    source_files = []
    def relevant_source(path):
        # C2/C3 currently binds July targets only. Other registered months have
        # their own versioned inputs and cannot invalidate a July review.
        parts = path.relative_to(source).parts
        return not parts or parts[0] not in ('월별등록', '월별증빙')
    if source.exists():
        for path in sorted(source.rglob('*')):
            if not relevant_source(path):
                continue
            _safe(root, path.relative_to(root))
            if path.is_file() and not path.name.startswith('~$'):
                source_files.append(path.relative_to(root))
    if not source_files:
        raise ValueError('저장 근거가 될 원본 자료가 없습니다.')
    paths.update(source_files)
    config_dir = _safe(root, Path('ia/config'))
    config_files = set(p.relative_to(root) for p in config_dir.glob('*.json'))
    paths.update(config_files)
    workflow = _read_json(_safe(root, WORKFLOW), {})
    result_hashes = workflow.get('result_hashes', {})
    if not isinstance(result_hashes, dict):
        raise ValueError('현재 결과의 근거 목록 형식이 올바르지 않습니다.')
    for name in result_hashes:
        if not isinstance(name, str) or Path(name).name != name or '/' in name or '\\' in name or ':' in name:
            raise ValueError('허용하지 않는 결과 근거 경로입니다.')
        paths.add(BASE / 'derived' / name)
    manifest, configs, stamps = {}, {}, {}
    for relative in sorted(paths, key=str):
        path = _safe(root, relative)
        if not path.is_file():
            raise ValueError(f'저장 근거 파일을 찾을 수 없습니다: {relative.name}')
        before = path.stat()
        with path.open('rb') as stream:
            sha = hashlib.file_digest(stream, 'sha256').hexdigest()
        after = path.stat()
        stamp = (after.st_size, after.st_mtime_ns)
        if (before.st_size, before.st_mtime_ns) != stamp:
            raise ValueError('원본을 확인하는 동안 자료가 변경되었습니다. 다시 확인해 주세요.')
        manifest[relative.as_posix()] = {'sha256': sha, 'bytes': after.st_size}
        stamps[relative] = stamp
        if relative.parts[:2] == ('ia', 'config'):
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != sha:
                raise ValueError('기존 결정 파일이 변경되었습니다. 다시 확인해 주세요.')
            # Retain scoped decisions independently of deletable conversation history.
            configs[relative.as_posix()] = {'content_base64': base64.b64encode(raw).decode('ascii'), 'sha256': sha}
        expected = result_hashes.get(relative.name) if relative.parent == BASE / 'derived' else None
        if expected and sha.lower() != str(expected).lower():
            raise ValueError('계산 결과와 근거 파일의 버전이 다릅니다. 완료 결과를 먼저 확인해 주세요.')
    for relative, stamp in stamps.items():
        stat = _safe(root, relative).stat()
        if (stat.st_size, stat.st_mtime_ns) != stamp:
            raise ValueError('저장 미리보기를 준비하는 동안 자료가 바뀌었습니다.')
    current_sources = {p.relative_to(root) for p in source.rglob('*') if relevant_source(p) and p.is_file() and not p.name.startswith('~$')}
    if current_sources != set(source_files):
        raise ValueError('원본 목록이 바뀌었습니다. 다시 확인해 주세요.')
    if {p.relative_to(root) for p in config_dir.glob('*.json')} != config_files:
        raise ValueError('기존 기준 목록이 바뀌었습니다. 다시 확인해 주세요.')
    return {'files': manifest, 'baseline_configs': configs, 'legacy_case_id':workflow.get('case_id'),
            'source_count': len(source_files), 'source_bytes_retained': False,
            'exact_replay_available': False}


def _validated_draft(root, draft, conversation_id):
    if not isinstance(draft, dict) or draft.get('state') != 'draft_ready':
        raise ValueError('청구건과 적용 범위를 먼저 정리해 주세요.')
    if draft.get('intent') not in ('set_disposition', 'upsert_rule'):
        raise ValueError('현재 저장할 수 있는 적용 요청이 아닙니다.')
    targets = draft.get('targets')
    if not isinstance(targets, list) or not targets or len(targets) > 100:
        raise ValueError('저장할 청구건을 다시 선택해 주세요.')
    if draft.get('intent') == 'set_disposition' and any(t.get('check') == '선택 단가 금액 비교' for t in targets):
        raise ValueError('단가 비교용 청구 항목은 비교 기준 요청으로 저장해 주세요. 담당자 처리는 확인 목록에서 선택합니다.')
    selection = chat_intake.select_targets(root, [t.get('issue_id') for t in targets], conversation_id)
    if selection['version'] != draft.get('version') or selection['month'] != draft.get('month'):
        raise ValueError('자료·결정·검증 결과가 바뀌었습니다. 대상을 다시 선택해 주세요.')
    if selection['targets'] != targets:
        # C1 adds only presentation aliases. They are not source identity.
        stripped = [{k:v for k,v in t.items() if k != 'alias'} for t in targets]
        if selection['targets'] != stripped:
            raise ValueError('선택한 청구건의 내용이 달라졌습니다. 다시 선택해 주세요.')
    candidate = chat_intake._advance(copy.deepcopy(draft))
    if candidate['state'] != 'draft_ready':
        raise ValueError(candidate['message'])
    if not draft.get('summary') or not isinstance(draft.get('summary'), str):
        raise ValueError('저장할 요청 내용을 먼저 확인해 주세요.')
    return selection


def _flat_record(record):
    payload = copy.deepcopy(record.get('payload') or {})
    payload.pop('source_snapshot', None)
    payload.update({k:v for k,v in record.items() if k != 'payload'})
    payload['target_count'] = len(payload.get('targets', []))
    payload['execution_applied'] = False
    payload['message'] = '기록 저장 완료 · 검증 결과에는 아직 적용하지 않았습니다.'
    return payload


class ChatCommands:
    def __init__(self, root, *, session_id, actor_id='local-pc-owner'):
        self.root = Path(root).resolve()
        self.session_id = session_id
        self.actor_id = actor_id
        self.store = BusinessMemory(self.root)

    def prepare(self, draft, *, conversation_id):
        selection = _validated_draft(self.root, draft, conversation_id)
        revision = self.store.revision()
        sources = source_snapshot(self.root)
        _validated_draft(self.root, draft, conversation_id)
        policy = json.loads(base64.b64decode(sources['baseline_configs'][POLICY.as_posix()]['content_base64']).decode('utf-8-sig'))
        month = selection['month']
        period = policy.get('period', {})
        if not isinstance(period, dict) or not all(period.get(k) for k in ('start','end')):
            raise ValueError('청구기간 시작일과 종료일이 필요합니다.')
        payload = {'schema_version':'c2.1', 'company_id':'demo_company', 'warehouse_id':'가상센터',
                   'case_id':f'demo_company:가상센터:{month}',
                   'month':month, 'period':{'start':period['start'],'end':period['end']},
                   'draft_id':draft['draft_id'], 'draft_hash':draft_hash(draft),
                   'intent':draft['intent'], 'summary':draft['summary'], 'reason':draft.get('reason'),
                   'criterion':draft.get('criterion'), 'scope':draft['scope'], 'application':draft['application'],
                   'effective_from_month':draft.get('effective_from_month') if draft['scope']=='future' else month,
                   'targets':selection['targets'], 'source_snapshot':sources,
                   'support_state':'pending_implementation', 'calculation_unchanged':True}
        prepared = self.store.create_proposal(payload, actor_id=self.actor_id, session_id=self.session_id,
            conversation_id=conversation_id, source_fingerprint=digest(sources), expected_revision=revision,
            idempotency_key=digest({'conversation':conversation_id,'draft':draft_hash(draft),'revision':revision,'attempt':uuid.uuid4().hex}))
        flat = {k:v for k,v in payload.items() if k != 'source_snapshot'}
        flat.update(state='preview', preview_id=prepared['proposal_id'], preview_hash=prepared['proposal_hash'],
                    nonce=prepared['approval_nonce'], expires_at=prepared['expires_at'], target_count=len(selection['targets']),
                    expected_revision=revision, conflicts=prepared.get('conflicts', []))
        flat['message'] = '이 내용을 기록으로 저장합니다. 계산 결과와 기존 기준은 유지하며, 새 기준의 자동 적용·재대사는 아직 실행하지 않습니다.'
        return flat

    def confirm(self, preview, draft, *, conversation_id, preview_id, preview_hash, nonce):
        if not isinstance(preview, dict) or preview.get('state') not in ('preview','saved','saved_pending_application'):
            raise ValueError('저장 내용을 먼저 확인해 주세요.')
        if (preview.get('preview_id'),preview.get('preview_hash'),preview.get('nonce')) != (preview_id,preview_hash,nonce):
            raise ValueError('확인 내용이 바뀌었습니다. 현재 저장 미리보기를 다시 확인해 주세요.')
        if preview.get('state') == 'preview':
            if draft_hash(draft) != preview.get('draft_hash'):
                raise ValueError('요청 내용이 변경되었습니다. 저장 미리보기를 다시 만들어 주세요.')
            _validated_draft(self.root, draft, conversation_id)
        snapshot = source_snapshot(self.root)
        record = self.store.confirm(preview_id, proposal_hash=preview_hash, approval_nonce=nonce,
            actor_id=self.actor_id, session_id=self.session_id, conversation_id=conversation_id,
            current_fingerprint=digest(snapshot))
        flat = _flat_record(record)
        flat.update(state='saved_pending_application', preview_id=preview_id, preview_hash=preview_hash, nonce=nonce)
        return flat

    def cancel(self, preview, *, conversation_id):
        if not preview or preview.get('state') != 'preview':
            raise ValueError('취소할 저장 미리보기가 없습니다.')
        self.store.cancel_proposal(preview['preview_id'], proposal_hash=preview['preview_hash'],
            approval_nonce=preview['nonce'], actor_id=self.actor_id, session_id=self.session_id,
            conversation_id=conversation_id)
        return dict(preview, state='cancelled', nonce=None, message='저장을 취소했습니다. 검증 결과는 변경되지 않았습니다.')

    def records(self):
        records = [_flat_record(r) for r in self.store.list_records()]
        if not hasattr(self.store, 'list_dispositions'):
            return records
        projection = self.disposition_projection()
        latest = {e['record_id']: e for e in projection['events']}
        for record in records:
            event = latest.get(record['record_id'])
            if event:
                record['latest_disposition'] = event
                record['review_status'] = ('review_required' if event.get('invalidated') or projection['source_error']
                                           else event['disposition'])
                record['execution_applied'] = True
                record['message'] = '담당자 처리 기록이 있습니다. 계산값·판정은 유지합니다.'
                if event.get('superseded'):
                    record['message'] = '이전 자료의 처리 기록입니다. 새 확인으로 대체했으며 이력은 보관합니다.'
        return records

    def _disposition_record(self, record_id, *, snapshot=None):
        record = self.store.get_record(record_id)
        if not record or record['payload']['intent'] != 'set_disposition':
            raise ValueError('담당자 처리로 저장한 요청을 선택해 주세요. 비교 기준의 계산 적용은 다음 단계입니다.')
        payload = record['payload']
        if payload['application'] not in ('current_case', 'current_and_future'):
            raise ValueError('앞으로만 적용하는 요청은 이번 청구의 처리 상태를 바꾸지 않습니다.')
        sources = snapshot if snapshot is not None else source_snapshot(self.root)
        if digest(sources) != digest(payload['source_snapshot']):
            raise ValueError('저장 당시 자료·결정·결과와 달라졌습니다. 현재 청구건을 다시 선택해 주세요.')
        current = chat_intake.select_targets(self.root, [t['issue_id'] for t in payload['targets']], 'disposition-validation')
        clean = lambda t: {k:v for k,v in t.items() if k not in ('target_id', 'issue_id', 'alias')}
        if [clean(t) for t in current['targets']] != [clean(t) for t in payload['targets']]:
            raise ValueError('저장한 대상의 원본 위치나 비교값이 바뀌었습니다. 대상을 다시 선택해 주세요.')
        return record, sources

    def prepare_disposition(self, record_id, status, *, conversation_id):
        if status not in ('reviewed', 'exception_accepted', 'review_required'):
            raise ValueError('검토완료·예외인정·재검토 중 처리 방식을 선택해 주세요.')
        record, sources = self._disposition_record(record_id)
        self.store.invalidate_dispositions(digest(sources))
        revision = self.store.revision()
        previous = next((e for e in self.store.list_dispositions(limit=None) if e['record_id'] == record_id), None)
        prepared = self.store.propose_disposition(record_id, status, actor_id=self.actor_id,
            session_id=self.session_id, conversation_id=conversation_id,
            current_fingerprint=digest(sources), expected_revision=revision,
            idempotency_key=digest({'record':record_id,'status':status,'revision':revision,'chat':conversation_id,'attempt':uuid.uuid4().hex}))
        flat = _flat_record(record)
        flat.update(state='preview', preview_id=prepared['proposal_id'], preview_hash=prepared['proposal_hash'],
            nonce=prepared['approval_nonce'], requested_status=status, expires_at=prepared['expires_at'],
            before_status=('review_required' if previous and previous.get('invalidated') else
                           previous['disposition'] if previous else 'unreviewed'),
            expected_revision=revision, calculation_executed=False, calculation_unchanged=True,
            replaces_invalidated_ids=prepared.get('payload', {}).get('replaces_invalidated_ids', []),
            future_rule_applied=False,
            message='선택한 검사항목의 담당자 처리 상태만 반영합니다. 수량·금액·불일치 판정은 그대로 유지합니다.')
        return flat

    def confirm_disposition(self, preview, *, conversation_id, preview_id, preview_hash, nonce):
        if not isinstance(preview, dict) or preview.get('state') not in ('preview','applied'):
            raise ValueError('담당자 처리 미리보기를 먼저 확인해 주세요.')
        if (preview.get('preview_id'), preview.get('preview_hash'), preview.get('nonce')) != (preview_id, preview_hash, nonce):
            raise ValueError('확인 내용이 바뀌었습니다. 현재 처리 미리보기를 다시 확인해 주세요.')
        sources = source_snapshot(self.root)
        self._disposition_record(preview['record_id'], snapshot=sources)
        receipt = self.store.confirm_disposition(preview_id, proposal_hash=preview_hash,
            approval_nonce=nonce, actor_id=self.actor_id, session_id=self.session_id,
            conversation_id=conversation_id, current_fingerprint=digest(sources))
        flat = dict(preview, **{k:v for k,v in receipt.items() if k != 'payload'})
        flat.update(state='applied', requested_status=receipt['disposition'],
                    message='담당자 처리 상태를 반영했습니다. 원래 계산값과 차이는 유지했으며 재대사를 실행한 것은 아닙니다.')
        if receipt.get('invalidated'):
            flat.update(requested_status='review_required',
                        message='이전 처리 기록은 저장되어 있으나 근거가 변경되어 현재는 재검토 상태입니다.')
        return flat

    def cancel_disposition(self, preview, *, conversation_id):
        if not preview or preview.get('state') != 'preview':
            raise ValueError('취소할 처리 미리보기가 없습니다.')
        self.store.cancel_disposition(preview['preview_id'], proposal_hash=preview['preview_hash'],
            approval_nonce=preview['nonce'], actor_id=self.actor_id, session_id=self.session_id,
            conversation_id=conversation_id)
        return dict(preview, state='cancelled', nonce=None, message='처리 반영을 취소했습니다. 기존 기록과 계산 결과는 유지합니다.')

    def disposition_projection(self):
        """One full source read per view/export; never rewrites calculation artifacts.

        A changed source can only produce review_required, even if it later returns
        to its former bytes. An absent/ambiguous row remains in the saved ledger.
        """
        events = self.store.list_dispositions(limit=None)
        if not events:
            return {'events':[], 'rows':{}, 'unmapped_targets':0, 'source_error':None}
        error = None
        try:
            sources = source_snapshot(self.root)
            self.store.invalidate_dispositions(digest(sources))
            events = self.store.list_dispositions(limit=None)
            current = chat_intake._current(self.root)
        except (ValueError, OSError) as exc:
            error = str(exc)
            # Missing/unreadable evidence is not permission to reactivate an
            # earlier completion when the file later comes back.
            self.store.invalidate_dispositions(digest({'source_unavailable':error}))
            events = self.store.list_dispositions(limit=None)
            try:
                current = chat_intake._current(self.root)
            except (ValueError, OSError):
                current = {'rows':[], 'version':None}
        # Include the particular check, locator and identifiers; an order alone
        # cannot mark its other quantity/price/amount checks as reviewed.
        identity_fields = ('check','source','locator','label','date','order_id','inbound_id',
                           'item_id','tracking_id','unit','applied_criterion')
        key = lambda t: digest({k:t.get(k,'') for k in identity_fields})
        by_key = {}
        for i, row in enumerate(current['rows']):
            target = chat_intake._target(row, i, current['version'])
            by_key.setdefault(key(target), []).append((str(i), target))
        superseded = {ref for event in events for ref in event['payload'].get('replaces_invalidated_ids', [])}
        for event in events:
            event['superseded'] = event['disposition_id'] in superseded
        projected, unmapped = {}, 0
        for event in events:
            if event.get('superseded'):
                continue
            payload = event['payload']
            status = 'review_required' if event.get('invalidated') or error else event['disposition']
            for target in payload['targets']:
                matches = by_key.get(key(target), [])
                # An unchanged full-file fingerprint authenticates the original
                # row position. Once evidence changes, that shortcut is disabled.
                if len(matches) > 1 and not event.get('invalidated') and not error:
                    matches = [m for m in matches if m[0] == str(target['issue_id'])]
                if len(matches) != 1:
                    unmapped += 1
                    continue
                index, actual = matches[0]
                if status != 'review_required' and any(actual.get(k) != target.get(k)
                    for k in ('claim_value','comparison_value','difference','status','reason')):
                    status_for_row = 'review_required'
                else:
                    status_for_row = status
                annotation = {'_review_status':status_for_row, '_review_reason':payload['reason'],
                    '_review_event':event['disposition_id'], '_review_record':event['record_id'],
                    '_review_invalidated':bool(event.get('invalidated') or error)}
                if index in projected:
                    # Do not choose a newer overlapping disposition silently.
                    annotation.update(_review_status='review_required', _review_reason='여러 처리 기록이 연결되어 재검토가 필요합니다.')
                projected[index] = annotation
        return {'events':events, 'rows':projected, 'unmapped_targets':unmapped, 'source_error':error}
