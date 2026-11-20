"""Durable C2 intentions and separately approved C3 handler dispositions.

This is the first runtime migration, not the complete 47-table design.  C2 keeps
immutable proposals, approvals, source/baseline snapshots, and saved intentions.
C3 appends handler states without changing results, rules, or calculations.
The caller is the trusted local service, not an LLM or a browser JSON dispatcher.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3
import uuid


DB_PATH = Path('data/demo_company/memory/registry.sqlite')
SCHEMA_VERSION = 'c2-intent-storage-v1'
DISPOSITION_SCHEMA_VERSION = 'c3-disposition-storage-v1'
DISPOSITIONS = {'reviewed', 'exception_accepted', 'review_required'}
MONTH = re.compile(r'20\d{2}-(?:0[1-9]|1[0-2])\Z')
HASH = re.compile(r'[a-f0-9]{64}\Z')
PAYLOAD_FIELDS = {'schema_version', 'company_id', 'warehouse_id', 'case_id', 'month',
                  'period', 'draft_id', 'draft_hash', 'intent', 'summary', 'reason', 'criterion',
                  'scope', 'application', 'effective_from_month', 'targets',
                  'source_snapshot', 'support_state', 'calculation_unchanged'}


class MemoryError(ValueError):
    """A safe, user-visible persistence rejection."""


class VersionConflict(MemoryError):
    pass


class BindingError(MemoryError):
    pass


class ExpiredProposal(MemoryError):
    pass


class IdempotencyConflict(MemoryError):
    pass


class ScopeConflict(MemoryError):
    def __init__(self, conflicts):
        self.conflicts = conflicts
        super().__init__('저장된 적용 요청과 범위가 겹칩니다. 기존 요청을 자동 대체하지 않았습니다. 겹치는 대상·조건을 확인해 주세요.')


def canonical(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise MemoryError('저장할 요청이 올바른 JSON 형식이 아닙니다.') from exc


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def _text(value, label, maximum=2000, minimum=1):
    if not isinstance(value, str) or not minimum <= len(value.strip()) <= maximum:
        raise MemoryError(f'{label} 값이 없거나 허용 길이를 벗어났습니다.')
    return value


def _time(value=None):
    if value is None:
        return datetime.now(timezone.utc)
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise MemoryError('저장 시각에는 시간대가 필요합니다.')
    return value.astimezone(timezone.utc)


def _stamp(value):
    return value.isoformat(timespec='microseconds').replace('+00:00', 'Z')


def _parse_stamp(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def _validate_payload(payload):
    if not isinstance(payload, dict) or set(payload) != PAYLOAD_FIELDS:
        raise MemoryError('저장 요청의 필드가 현재 지원 형식과 다릅니다.')
    if payload['schema_version'] != 'c2.1' or payload['company_id'] != 'demo_company' or payload['warehouse_id'] != '가상센터':
        raise MemoryError('현재 지원하는 회사·센터의 요청이 아닙니다.')
    month = payload['month']
    if not isinstance(month, str) or not MONTH.fullmatch(month):
        raise MemoryError('청구월은 YYYY-MM 형식이어야 합니다.')
    if payload['case_id'] != f"demo_company:가상센터:{month}":
        raise MemoryError('청구월과 사례 식별자가 일치하지 않습니다.')
    if payload['support_state'] != 'pending_implementation' or payload['calculation_unchanged'] is not True:
        raise MemoryError('이 단계에서는 계산에 적용하지 않는 요청만 저장합니다.')
    if payload['intent'] not in ('set_disposition', 'upsert_rule'):
        raise MemoryError('지원하지 않는 저장 작업입니다.')
    for field in ('draft_id', 'summary'):
        _text(payload[field], field)
    if not isinstance(payload['draft_hash'], str) or not HASH.fullmatch(payload['draft_hash']):
        raise MemoryError('확인한 요청 초안의 해시가 필요합니다.')
    for field in ('reason', 'criterion'):
        if payload[field] is not None:
            _text(payload[field], field)
    if payload['intent'] == 'set_disposition' and not payload['reason']:
        raise MemoryError('담당자 처리 사유가 필요합니다.')
    if payload['intent'] == 'upsert_rule' and not payload['criterion']:
        raise MemoryError('저장할 비교 기준이 필요합니다.')
    period = payload['period']
    if not isinstance(period, dict) or set(period) != {'start', 'end'}:
        raise MemoryError('청구기간 시작일과 종료일이 필요합니다.')
    try:
        dates = [datetime.strptime(period[k], '%Y-%m-%d').date() for k in ('start', 'end')]
    except (TypeError, ValueError) as exc:
        raise MemoryError('청구기간 날짜 형식이 올바르지 않습니다.') from exc
    if dates[0] > dates[1] or any(period[k] != dates[i].isoformat() for i, k in enumerate(('start', 'end'))):
        raise MemoryError('청구기간 시작일·종료일을 확인해 주세요.')
    start = payload['effective_from_month']
    if not isinstance(start, str) or not MONTH.fullmatch(start):
        raise MemoryError('적용 시작 청구월이 필요합니다.')
    if payload['scope'] == 'once':
        if payload['application'] != 'current_case' or start != month:
            raise MemoryError('이번 청구만 저장하는 요청의 범위가 일치하지 않습니다.')
    elif payload['scope'] == 'future':
        if payload['application'] not in ('future_only', 'current_and_future') or not payload['criterion']:
            raise MemoryError('앞으로 적용할 범위와 조건이 필요합니다.')
    else:
        raise MemoryError('적용 범위를 먼저 확인해 주세요.')
    targets = payload['targets']
    if not isinstance(targets, list) or not 1 <= len(targets) <= 1000:
        raise MemoryError('명시적으로 선택한 청구 대상이 필요합니다.')
    ids = []
    for target in targets:
        if not isinstance(target, dict):
            raise MemoryError('청구 대상 형식이 올바르지 않습니다.')
        ids.append(_text(target.get('target_id'), '대상 ID', maximum=256))
        _text(target.get('check'), '검사 종류', maximum=256)
        for field in ('source', 'locator'):
            if field in target and (not isinstance(target[field], str) or len(target[field]) > 10000):
                raise MemoryError('청구 대상의 원본 위치 형식이 올바르지 않습니다.')
    if len(ids) != len(set(ids)):
        raise MemoryError('같은 청구 대상이 반복되었습니다.')
    if not isinstance(payload['source_snapshot'], dict) or not payload['source_snapshot']:
        raise MemoryError('원본·기존 결정·결과의 기준판이 필요합니다.')
    serialized = canonical(payload)
    if len(serialized.encode('utf-8')) > 16 * 1024 * 1024:
        raise MemoryError('저장 요청의 기준판이 너무 큽니다.')
    return json.loads(serialized)


def _identity(actor_id, session_id, conversation_id):
    _text(actor_id, '로컬 사용자', 256)
    _text(session_id, '서버 세션', 512, 16)
    _text(conversation_id, '대화', 256)
    return hashlib.sha256(session_id.encode('utf-8')).hexdigest()


def _nonce(session_id, proposal):
    fields = {key: proposal[key] for key in ('proposal_id', 'payload_hash', 'source_fingerprint', 'expected_revision', 'expires_at')}
    return hmac.new(session_id.encode('utf-8'), canonical(fields).encode('utf-8'), hashlib.sha256).hexdigest()


def _scopes_overlap(a, b):
    if (a['company_id'], a['warehouse_id']) != (b['company_id'], b['warehouse_id']):
        return False
    if not {t['check'] for t in a['targets']} & {t['check'] for t in b['targets']}:
        return False
    def intervals(value):
        if value['scope'] == 'once':
            return [(value['month'], value['month'])]
        found = [(value['effective_from_month'], '9999-12')]
        if value['application'] == 'current_and_future':
            found.append((value['month'], value['month']))
        return found
    if not any(a0 <= b1 and b0 <= a1 for a0, a1 in intervals(a) for b0, b1 in intervals(b)):
        return False
    if a['scope'] == 'future' or b['scope'] == 'future':
        # Cost rules are bounded by the selected category/item/unit, unlike free
        # quantity conditions. Different exact selectors cannot overlap.
        if all(t.get('check')=='선택 단가 금액 비교' and isinstance(t.get('cost_selector'),list)
               and len(t['cost_selector'])==3 and all(type(v)is str and v for v in t['cost_selector'])
               for p in (a,b) for t in p['targets']):
            if not {tuple(t['cost_selector']) for t in a['targets']} & {tuple(t['cost_selector']) for t in b['targets']}:
                return False
        # Free text cannot establish that conditions are logically disjoint.
        return True
    if {t['target_id'] for t in a['targets']} & {t['target_id'] for t in b['targets']}:
        return True
    def occurrences(value):
        return {(t['check'], t.get('source', ''), t.get('locator', '')) for t in value['targets'] if t.get('locator')}
    # Same source location after a material-version change needs review too.
    return bool(occurrences(a) & occurrences(b))


_SCHEMA = """
CREATE TABLE memory_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT;
CREATE TABLE memory_head (singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision INTEGER NOT NULL CHECK(revision>=0)) STRICT;
CREATE TABLE memory_proposals (
 proposal_id TEXT PRIMARY KEY, actor_id TEXT NOT NULL, session_hash TEXT NOT NULL,
 conversation_id TEXT NOT NULL, idempotency_key TEXT NOT NULL, payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
 payload_hash TEXT NOT NULL, proposal_hash TEXT NOT NULL, nonce_hash TEXT NOT NULL,
 source_fingerprint TEXT NOT NULL, expected_revision INTEGER NOT NULL CHECK(expected_revision>=0),
 created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
 UNIQUE(actor_id,idempotency_key)
) STRICT;
CREATE TABLE memory_proposal_events (
 event_id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL REFERENCES memory_proposals(proposal_id),
 event_type TEXT NOT NULL CHECK(event_type IN ('cancelled')), created_at TEXT NOT NULL,
 UNIQUE(proposal_id,event_type)
) STRICT;
CREATE TABLE memory_approvals (
 approval_id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL UNIQUE REFERENCES memory_proposals(proposal_id),
 proposal_hash TEXT NOT NULL, actor_id TEXT NOT NULL, session_hash TEXT NOT NULL,
 conversation_id TEXT NOT NULL, decision TEXT NOT NULL CHECK(decision='accepted'), approved_at TEXT NOT NULL
) STRICT;
CREATE TABLE memory_intents (
 record_id TEXT PRIMARY KEY, record_version INTEGER NOT NULL UNIQUE CHECK(record_version>0),
 proposal_id TEXT NOT NULL UNIQUE REFERENCES memory_proposals(proposal_id),
 approval_id TEXT NOT NULL UNIQUE REFERENCES memory_approvals(approval_id),
 payload_json TEXT NOT NULL CHECK(json_valid(payload_json)), payload_hash TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status='saved_pending_application'), saved_at TEXT NOT NULL
) STRICT;
CREATE TABLE memory_audit (
 event_id TEXT PRIMARY KEY, event_type TEXT NOT NULL CHECK(event_type IN ('preview_created','preview_cancelled','intent_saved')),
 proposal_id TEXT NOT NULL REFERENCES memory_proposals(proposal_id), record_id TEXT REFERENCES memory_intents(record_id),
 revision INTEGER NOT NULL CHECK(revision>=0), event_json TEXT NOT NULL CHECK(json_valid(event_json)), created_at TEXT NOT NULL
) STRICT;
INSERT INTO memory_head VALUES(1,0);
"""

_DISPOSITION_SCHEMA = """
CREATE TABLE memory_disposition_proposals (
 proposal_id TEXT PRIMARY KEY, record_id TEXT NOT NULL REFERENCES memory_intents(record_id),
 actor_id TEXT NOT NULL, session_hash TEXT NOT NULL, conversation_id TEXT NOT NULL,
 idempotency_key TEXT NOT NULL, payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
 payload_hash TEXT NOT NULL, proposal_hash TEXT NOT NULL, nonce_hash TEXT NOT NULL,
 source_fingerprint TEXT NOT NULL, expected_revision INTEGER NOT NULL CHECK(expected_revision>=0),
 created_at TEXT NOT NULL, expires_at TEXT NOT NULL, UNIQUE(actor_id,idempotency_key)
) STRICT;
CREATE TABLE memory_disposition_cancellations (
 proposal_id TEXT PRIMARY KEY REFERENCES memory_disposition_proposals(proposal_id), cancelled_at TEXT NOT NULL
) STRICT;
CREATE TABLE memory_disposition_approvals (
 approval_id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL UNIQUE REFERENCES memory_disposition_proposals(proposal_id),
 proposal_hash TEXT NOT NULL, actor_id TEXT NOT NULL, session_hash TEXT NOT NULL,
 conversation_id TEXT NOT NULL, decision TEXT NOT NULL CHECK(decision='accepted'), approved_at TEXT NOT NULL
) STRICT;
CREATE TABLE memory_dispositions (
 disposition_id TEXT PRIMARY KEY, disposition_version INTEGER NOT NULL UNIQUE CHECK(disposition_version>0),
 record_id TEXT NOT NULL REFERENCES memory_intents(record_id),
 proposal_id TEXT NOT NULL UNIQUE REFERENCES memory_disposition_proposals(proposal_id),
 approval_id TEXT NOT NULL UNIQUE REFERENCES memory_disposition_approvals(approval_id),
 disposition TEXT NOT NULL CHECK(disposition IN ('reviewed','exception_accepted','review_required')),
 payload_json TEXT NOT NULL CHECK(json_valid(payload_json)), payload_hash TEXT NOT NULL,
 source_fingerprint TEXT NOT NULL, applied_at TEXT NOT NULL
) STRICT;
CREATE INDEX memory_dispositions_record_version ON memory_dispositions(record_id,disposition_version);
CREATE TABLE memory_disposition_audit (
 event_id TEXT PRIMARY KEY, event_type TEXT NOT NULL CHECK(event_type IN ('preview_created','preview_cancelled','disposition_applied')),
 proposal_id TEXT NOT NULL REFERENCES memory_disposition_proposals(proposal_id),
 disposition_id TEXT REFERENCES memory_dispositions(disposition_id),
 revision INTEGER NOT NULL CHECK(revision>=0), event_json TEXT NOT NULL CHECK(json_valid(event_json)), created_at TEXT NOT NULL
) STRICT;
CREATE TABLE memory_disposition_invalidations (
 invalidation_id TEXT PRIMARY KEY, disposition_id TEXT NOT NULL UNIQUE REFERENCES memory_dispositions(disposition_id),
 revision INTEGER NOT NULL CHECK(revision>0), actor_id TEXT NOT NULL CHECK(actor_id='system'),
 previous_fingerprint TEXT NOT NULL, current_fingerprint TEXT NOT NULL, created_at TEXT NOT NULL
) STRICT;
"""
_DISPOSITION_TABLES = ('memory_disposition_proposals', 'memory_disposition_cancellations',
                       'memory_disposition_approvals', 'memory_dispositions', 'memory_disposition_audit',
                       'memory_disposition_invalidations')


def _resolved_local_path(path):
    for _ in range(3):
        resolved=path.resolve()
        text=str(resolved)
        # SQLite can unlink WAL/SHM during Windows realpath. Retry the requested
        # name if Windows returned its transient deleted-file location. Never
        # accept that location or skip the workspace/symlink boundary checks.
        if os.name=='nt' and re.match(r'^\\\\\?\\[A-Za-z]:\\',text):
            resolved=Path(text[4:])
            text=str(resolved)
        if not (os.name=='nt' and re.match(r'^[A-Za-z]:\\\$Extend\\\$Deleted\\',text,re.I)):
            return resolved
    return resolved


class BusinessMemory:
    """Bound preview/confirmation storage owned by a trusted local service."""

    def __init__(self, root):
        self.root = _resolved_local_path(Path(root))
        self.path = self.root / DB_PATH
        self._boundary()

    def _boundary(self):
        for path in (self.path, *(self.path.with_name(self.path.name + suffix) for suffix in ('-wal', '-shm', '-journal'))):
            if not _resolved_local_path(path).is_relative_to(self.root):
                raise MemoryError('업무 기록 저장소는 현재 프로젝트 안에 있어야 합니다.')
            for item in (path, *path.parents):
                if item == self.root:
                    break
                if item.is_symlink() or (hasattr(item, 'is_junction') and item.is_junction()):
                    raise MemoryError('외부 경로가 연결된 업무 기록 저장소는 사용할 수 없습니다.')

    @contextmanager
    def _connect(self, write=False):
        self._boundary()
        if not write and not self.path.is_file():
            yield None
            return
        if write:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._boundary()
        connection = sqlite3.connect(str(self.path) if write else self.path.as_uri() + '?mode=ro', uri=not write, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute('PRAGMA foreign_keys=ON')
            connection.execute('PRAGMA busy_timeout=10000')
            if not write and not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_metadata'").fetchone():
                if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table'").fetchone():
                    raise MemoryError('기존 업무 저장소의 스키마가 다릅니다.')
                # A rejected first transaction may leave an empty SQLite file.
                # It is still an empty store, not a partially accepted request.
                yield None
                return
            if write:
                # Do not even change journal mode on an unrelated database.
                metadata = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_metadata'").fetchone()
                if not metadata and connection.execute("SELECT 1 FROM sqlite_master WHERE type='table'").fetchone():
                    raise MemoryError('기존 업무 저장소의 스키마가 다릅니다. 자동 덮어쓰기는 하지 않습니다.')
                if metadata:
                    version = connection.execute("SELECT value FROM memory_metadata WHERE key='schema_version'").fetchone()
                    if not version or version[0] != SCHEMA_VERSION:
                        raise MemoryError('지원하지 않는 업무 기록 버전입니다.')
                connection.execute('PRAGMA journal_mode=WAL')
                connection.execute('PRAGMA synchronous=FULL')
                connection.execute('BEGIN IMMEDIATE')
                exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_metadata'").fetchone()
                if not exists:
                    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table'").fetchone():
                        raise MemoryError('기존 업무 저장소의 스키마가 다릅니다. 자동 덮어쓰기는 하지 않습니다.')
                    # executescript commits implicitly; execute single statements
                    # instead so initialization is atomic under the writer lock.
                    for statement in _SCHEMA.split(';'):
                        if statement.strip():
                            connection.execute(statement)
                    connection.execute('INSERT INTO memory_metadata VALUES(?,?)', ('schema_version', SCHEMA_VERSION))
                    for table in ('memory_metadata', 'memory_proposals', 'memory_proposal_events', 'memory_approvals', 'memory_intents', 'memory_audit'):
                        for op in ('UPDATE', 'DELETE'):
                            connection.execute(f"CREATE TRIGGER {table}_{op.lower()}_blocked BEFORE {op} ON {table} BEGIN SELECT RAISE(ABORT,'immutable_business_memory'); END")
                    connection.execute("CREATE TRIGGER memory_head_delete_blocked BEFORE DELETE ON memory_head BEGIN SELECT RAISE(ABORT,'immutable_memory_head'); END")
                    connection.execute("CREATE TRIGGER memory_head_monotonic BEFORE UPDATE ON memory_head WHEN NEW.singleton != OLD.singleton OR NEW.revision != OLD.revision + 1 BEGIN SELECT RAISE(ABORT,'invalid_memory_revision'); END")
            version = connection.execute("SELECT value FROM memory_metadata WHERE key='schema_version'").fetchone()
            if not version or version[0] != SCHEMA_VERSION:
                raise MemoryError('지원하지 않는 업무 기록 버전입니다.')
            yield connection
            if write:
                connection.commit()
        except Exception:
            if write:
                connection.rollback()
            raise
        finally:
            connection.close()

    def revision(self):
        with self._connect() as db:
            return int(db.execute('SELECT revision FROM memory_head WHERE singleton=1').fetchone()[0]) if db else 0

    @staticmethod
    def _record(row, replay=False):
        if row is None:
            return None
        payload = json.loads(row['payload_json'])
        if digest(payload) != row['payload_hash']:
            raise MemoryError('저장된 업무 기록의 내용 해시가 일치하지 않습니다.')
        return {key: row[key] for key in ('record_id', 'record_version', 'approval_id', 'proposal_id', 'status', 'saved_at')} | {
            'payload': payload, 'business_saved': True, 'changes_applied': False,
            'execution_enabled': False, 'idempotent_replay': replay}

    def list_records(self, limit=100):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise MemoryError('조회 개수는 1~1000 사이여야 합니다.')
        with self._connect() as db:
            if db is None:
                return []
            return [self._record(row) for row in db.execute('SELECT * FROM memory_intents ORDER BY record_version DESC LIMIT ?', (limit,))]

    def get_record(self, record_id):
        _text(record_id, '기록 ID', 256)
        with self._connect() as db:
            return self._record(db.execute('SELECT * FROM memory_intents WHERE record_id=?', (record_id,)).fetchone()) if db else None

    @staticmethod
    def _conflicts(db, payload):
        result = []
        for row in db.execute('SELECT * FROM memory_intents ORDER BY record_version DESC'):
            record = BusinessMemory._record(row)
            existing = record['payload']
            if _scopes_overlap(payload, existing):
                # A genuinely new source version may be saved for re-review
                # after the old handler disposition was invalidated. This only
                # saves an intention; C3 must explicitly name the invalidated
                # dispositions it replaces before any current handling changes.
                # Pending requests, future conditions, and active handling stay
                # conflicting; no timestamp-based automatic supersession.
                if (payload['scope'] == existing['scope'] == 'once'
                        and payload['application'] == existing['application'] == 'current_case'
                        and payload['intent'] == existing['intent'] == 'set_disposition'
                        and digest(payload['source_snapshot']) != digest(existing['source_snapshot'])
                        and not {t['target_id'] for t in payload['targets']}.intersection(t['target_id'] for t in existing['targets'])
                        and BusinessMemory._disposition_schema(db)):
                    latest = db.execute('SELECT d.disposition_id FROM memory_dispositions d '
                        'WHERE d.record_id=? ORDER BY d.disposition_version DESC LIMIT 1', (record['record_id'],)).fetchone()
                    if latest and db.execute('SELECT 1 FROM memory_disposition_invalidations WHERE disposition_id=?', (latest[0],)).fetchone():
                        continue
                result.append({'record_id': record['record_id'], 'record_version': record['record_version'],
                               'summary': existing['summary'], 'scope': existing['scope'], 'month': existing['month'],
                               'reason': '같은 검사·대상 또는 앞으로 적용할 월 범위가 겹쳐 조건 검토가 필요합니다.'})
        return result

    @staticmethod
    def _view(row, session_id):
        payload = json.loads(row['payload_json'])
        if digest(payload) != row['payload_hash']:
            raise MemoryError('저장 미리보기의 내용 해시가 일치하지 않습니다.')
        return {key: row[key] for key in ('proposal_id', 'proposal_hash', 'expected_revision', 'source_fingerprint', 'expires_at')} | {
            'approval_nonce': _nonce(session_id, row), 'status': 'preview', 'payload': payload, 'conflicts': [],
            'business_saved': False, 'changes_applied': False, 'execution_enabled': False}

    @staticmethod
    def _audit(db, event, proposal_id, revision, now, record_id=None, detail=None):
        db.execute('INSERT INTO memory_audit VALUES(?,?,?,?,?,?,?)', (
            uuid.uuid4().hex, event, proposal_id, record_id, revision, canonical(detail or {}), _stamp(now)))

    def create_proposal(self, payload, *, actor_id, session_id, conversation_id, source_fingerprint,
                        expected_revision, idempotency_key, ttl_seconds=900, now=None):
        payload = _validate_payload(payload)
        session_hash = _identity(actor_id, session_id, conversation_id)
        _text(idempotency_key, '요청 재시도 키', 512)
        if not isinstance(source_fingerprint, str) or not HASH.fullmatch(source_fingerprint):
            raise MemoryError('자료 기준판의 해시가 필요합니다.')
        if source_fingerprint != digest(payload['source_snapshot']):
            raise BindingError('자료 기준판과 저장할 기준판 해시가 일치하지 않습니다.')
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
            raise MemoryError('저장소 버전 형식이 올바르지 않습니다.')
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or not 30 <= ttl_seconds <= 3600:
            raise MemoryError('저장 확인 유효시간은 30~3600초여야 합니다.')
        instant = _time(now)
        payload_hash = digest(payload)
        with self._connect(write=True) as db:
            old = db.execute('SELECT * FROM memory_proposals WHERE actor_id=? AND idempotency_key=?', (actor_id, idempotency_key)).fetchone()
            if old:
                if old['session_hash'] != session_hash or old['conversation_id'] != conversation_id:
                    raise BindingError('이 대화·세션에서 만든 저장 미리보기가 아닙니다.')
                if old['payload_hash'] != payload_hash or old['source_fingerprint'] != source_fingerprint or old['expected_revision'] != expected_revision:
                    raise IdempotencyConflict('같은 요청 키로 다른 내용을 저장할 수 없습니다. 미리보기를 다시 생성해 주세요.')
                if db.execute('SELECT 1 FROM memory_proposal_events WHERE proposal_id=?', (old['proposal_id'],)).fetchone():
                    raise MemoryError('취소된 저장 미리보기입니다. 다시 생성해 주세요.')
                if instant >= _parse_stamp(old['expires_at']):
                    raise ExpiredProposal('저장 미리보기의 유효시간이 지났습니다. 다시 확인해 주세요.')
                return self._view(old, session_id)
            revision = db.execute('SELECT revision FROM memory_head WHERE singleton=1').fetchone()[0]
            if revision != expected_revision:
                raise VersionConflict('다른 업무 요청이 저장되었습니다. 현재 기록으로 미리보기를 다시 확인해 주세요.')
            conflicts = self._conflicts(db, payload)
            if conflicts:
                raise ScopeConflict(conflicts)
            proposal = {'proposal_id': uuid.uuid4().hex, 'actor_id': actor_id, 'session_hash': session_hash,
                        'conversation_id': conversation_id, 'idempotency_key': idempotency_key,
                        'payload_json': canonical(payload), 'payload_hash': payload_hash, 'source_fingerprint': source_fingerprint,
                        'expected_revision': expected_revision, 'created_at': _stamp(instant),
                        'expires_at': _stamp(instant + timedelta(seconds=ttl_seconds))}
            proposal['nonce_hash'] = hashlib.sha256(_nonce(session_id, proposal).encode()).hexdigest()
            proposal['proposal_hash'] = digest(proposal)
            columns = ','.join(proposal)
            db.execute(f'INSERT INTO memory_proposals ({columns}) VALUES ({",".join("?" for _ in proposal)})', tuple(proposal.values()))
            self._audit(db, 'preview_created', proposal['proposal_id'], revision, instant)
            return self._view(proposal, session_id)

    @staticmethod
    def _bound(db, proposal_id, proposal_hash, approval_nonce, actor_id, session_id, conversation_id):
        session_hash = _identity(actor_id, session_id, conversation_id)
        for value in (proposal_id, proposal_hash, approval_nonce):
            _text(value, '저장 확인 참조', 512)
        if not HASH.fullmatch(proposal_hash) or not HASH.fullmatch(approval_nonce):
            raise BindingError('저장 확인 참조가 올바르지 않습니다. 현재 미리보기를 다시 확인해 주세요.')
        row = db.execute('SELECT * FROM memory_proposals WHERE proposal_id=?', (proposal_id,)).fetchone()
        if not row or row['actor_id'] != actor_id or row['session_hash'] != session_hash or row['conversation_id'] != conversation_id:
            raise BindingError('이 대화·세션에서 만든 저장 미리보기가 아닙니다.')
        if not hmac.compare_digest(row['proposal_hash'], proposal_hash) or not hmac.compare_digest(row['nonce_hash'], hashlib.sha256(approval_nonce.encode()).hexdigest()):
            raise BindingError('저장 확인 대상이 달라졌습니다. 현재 미리보기를 다시 확인해 주세요.')
        actual = dict(row); actual.pop('proposal_hash')
        if digest(actual) != row['proposal_hash']:
            raise MemoryError('저장 미리보기의 내용이 변경되었습니다.')
        BusinessMemory._view(row, session_id)
        return row

    def confirm(self, proposal_id, *, proposal_hash, approval_nonce, actor_id, session_id,
                conversation_id, current_fingerprint, now=None):
        instant = _time(now)
        if not self.path.is_file():
            raise MemoryError('저장할 미리보기가 없습니다.')
        with self._connect(write=True) as db:
            proposal = self._bound(db, proposal_id, proposal_hash, approval_nonce, actor_id, session_id, conversation_id)
            saved = db.execute('SELECT * FROM memory_intents WHERE proposal_id=?', (proposal_id,)).fetchone()
            if saved:
                return self._record(saved, replay=True)
            if db.execute('SELECT 1 FROM memory_proposal_events WHERE proposal_id=?', (proposal_id,)).fetchone():
                raise MemoryError('취소한 요청은 저장할 수 없습니다.')
            if instant >= _parse_stamp(proposal['expires_at']):
                raise ExpiredProposal('저장 미리보기의 유효시간이 지났습니다. 다시 확인해 주세요.')
            if current_fingerprint != proposal['source_fingerprint']:
                raise VersionConflict('자료·기존 결정·검증 결과가 바뀌었습니다. 저장 미리보기를 다시 확인해 주세요.')
            revision = db.execute('SELECT revision FROM memory_head WHERE singleton=1').fetchone()[0]
            if revision != proposal['expected_revision']:
                raise VersionConflict('다른 업무 요청이 저장되었습니다. 현재 기록으로 미리보기를 다시 확인해 주세요.')
            payload = _validate_payload(json.loads(proposal['payload_json']))
            conflicts = self._conflicts(db, payload)
            if conflicts:
                raise ScopeConflict(conflicts)
            approval_id, record_id = uuid.uuid4().hex, uuid.uuid4().hex
            stamp = _stamp(instant)
            db.execute('INSERT INTO memory_approvals VALUES(?,?,?,?,?,?,?,?)', (
                approval_id, proposal_id, proposal_hash, actor_id, proposal['session_hash'], conversation_id, 'accepted', stamp))
            db.execute('INSERT INTO memory_intents VALUES(?,?,?,?,?,?,?,?)', (
                record_id, revision + 1, proposal_id, approval_id, proposal['payload_json'], proposal['payload_hash'], 'saved_pending_application', stamp))
            changed = db.execute('UPDATE memory_head SET revision=? WHERE singleton=1 AND revision=?', (revision + 1, revision)).rowcount
            if changed != 1:
                raise VersionConflict('다른 요청과 저장이 겹쳤습니다. 미리보기를 다시 확인해 주세요.')
            self._audit(db, 'intent_saved', proposal_id, revision + 1, instant, record_id,
                        {'proposal_hash': proposal_hash, 'source_fingerprint': current_fingerprint, 'applied': False})
            return self._record(db.execute('SELECT * FROM memory_intents WHERE record_id=?', (record_id,)).fetchone())

    def cancel_proposal(self, proposal_id, *, proposal_hash, approval_nonce, actor_id, session_id,
                        conversation_id, now=None):
        instant = _time(now)
        if not self.path.is_file():
            raise MemoryError('취소할 미리보기가 없습니다.')
        with self._connect(write=True) as db:
            self._bound(db, proposal_id, proposal_hash, approval_nonce, actor_id, session_id, conversation_id)
            if db.execute('SELECT 1 FROM memory_intents WHERE proposal_id=?', (proposal_id,)).fetchone():
                raise MemoryError('이미 저장한 기록입니다. 저장 취소·중지 기능은 아직 연결되지 않았습니다.')
            event = db.execute('SELECT 1 FROM memory_proposal_events WHERE proposal_id=?', (proposal_id,)).fetchone()
            if not event:
                db.execute('INSERT INTO memory_proposal_events VALUES(?,?,?,?)', (uuid.uuid4().hex, proposal_id, 'cancelled', _stamp(instant)))
                revision = db.execute('SELECT revision FROM memory_head WHERE singleton=1').fetchone()[0]
                self._audit(db, 'preview_cancelled', proposal_id, revision, instant)
            return {'proposal_id': proposal_id, 'status': 'cancelled', 'business_saved': False,
                    'changes_applied': False, 'execution_enabled': False, 'idempotent_replay': bool(event)}

    @staticmethod
    def _disposition_schema(db, create=False):
        if db is None:
            return False
        version = db.execute("SELECT value FROM memory_metadata WHERE key='disposition_schema_version'").fetchone()
        present = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        found = present.intersection(_DISPOSITION_TABLES)
        if version:
            if version[0] != DISPOSITION_SCHEMA_VERSION or found != set(_DISPOSITION_TABLES):
                raise MemoryError('담당자 처리 저장소의 버전 또는 구성이 다릅니다.')
            return True
        if found:
            raise MemoryError('완성되지 않은 담당자 처리 저장소를 자동으로 덮어쓰지 않습니다.')
        if not create:
            return False
        # Additive migration under the existing BEGIN IMMEDIATE transaction.
        # C2 rows and its immutable schema-version entry remain untouched.
        for statement in _DISPOSITION_SCHEMA.split(';'):
            if statement.strip():
                db.execute(statement)
        for table in _DISPOSITION_TABLES:
            for operation in ('UPDATE', 'DELETE'):
                db.execute(f"CREATE TRIGGER {table}_{operation.lower()}_blocked BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'immutable_disposition_memory'); END")
        db.execute('INSERT INTO memory_metadata VALUES(?,?)', ('disposition_schema_version', DISPOSITION_SCHEMA_VERSION))
        return True

    @staticmethod
    def _disposition_record(row, replay=False):
        if row is None:
            return None
        payload = json.loads(row['payload_json'])
        if (digest(payload) != row['payload_hash'] or payload['record_id'] != row['record_id']
                or payload['disposition'] != row['disposition']
                or payload['source_fingerprint'] != row['source_fingerprint']):
            raise MemoryError('저장된 담당자 처리의 내용 해시가 일치하지 않습니다.')
        keys = ('disposition_id', 'disposition_version', 'record_id', 'proposal_id', 'approval_id',
                'disposition', 'source_fingerprint', 'applied_at')
        return {key: row[key] for key in keys} | {
            'payload': payload, 'status': 'disposition_applied', 'changes_applied': True,
            'calculation_unchanged': True, 'calculation_executed': False,
            'future_rule_applied': False, 'idempotent_replay': replay}

    def list_dispositions(self, limit=1000, latest=True):
        if (limit is not None and (type(limit) is not int or not 1 <= limit <= 10000)) or type(latest) is not bool:
            raise MemoryError('처리 기록 조회 조건이 올바르지 않습니다.')
        with self._connect() as db:
            if not self._disposition_schema(db):
                return []
            query = ('SELECT d.* FROM memory_dispositions d WHERE NOT EXISTS '
                     '(SELECT 1 FROM memory_dispositions n WHERE n.record_id=d.record_id AND n.disposition_version>d.disposition_version) '
                     if latest else 'SELECT d.* FROM memory_dispositions d ')
            query += 'ORDER BY d.disposition_version DESC'
            parameters = ()
            if limit is not None:
                query += ' LIMIT ?'
                parameters = (limit,)
            return [self._disposition_with_invalidation(db, row) for row in db.execute(query, parameters)]

    @staticmethod
    def _disposition_with_invalidation(db, row, replay=False):
        result = BusinessMemory._disposition_record(row, replay)
        if result is None:
            return None
        invalidation = db.execute('SELECT * FROM memory_disposition_invalidations WHERE disposition_id=?', (result['disposition_id'],)).fetchone()
        result.update(invalidated=bool(invalidation), invalidation=dict(invalidation) if invalidation else None)
        return result

    def get_disposition(self, disposition_id):
        _text(disposition_id, '담당자 처리 ID', 256)
        with self._connect() as db:
            if not self._disposition_schema(db):
                return None
            return self._disposition_with_invalidation(db, db.execute('SELECT * FROM memory_dispositions WHERE disposition_id=?', (disposition_id,)).fetchone())

    def get_disposition_for_proposal(self, proposal_id):
        _text(proposal_id, '담당자 처리 확인 ID', 256)
        with self._connect() as db:
            if not self._disposition_schema(db):
                return None
            return self._disposition_with_invalidation(db, db.execute('SELECT * FROM memory_dispositions WHERE proposal_id=?', (proposal_id,)).fetchone())

    def invalidate_dispositions(self, current_fingerprint, *, actor_id='system', now=None):
        if actor_id != 'system' or not isinstance(current_fingerprint, str) or not HASH.fullmatch(current_fingerprint):
            raise MemoryError('자동 재검토에는 시스템의 현재 근거 해시가 필요합니다.')
        if not self.path.is_file():
            return []
        instant = _time(now)
        with self._connect(write=True) as db:
            if not self._disposition_schema(db):
                return []
            query = ('SELECT d.* FROM memory_dispositions d WHERE NOT EXISTS '
                     '(SELECT 1 FROM memory_dispositions n WHERE n.record_id=d.record_id AND n.disposition_version>d.disposition_version) '
                     'AND NOT EXISTS (SELECT 1 FROM memory_disposition_invalidations i WHERE i.disposition_id=d.disposition_id)')
            changed = [self._disposition_record(row) for row in db.execute(query) if row['source_fingerprint'] != current_fingerprint]
            if not changed:
                return []
            revision = db.execute('SELECT revision FROM memory_head WHERE singleton=1').fetchone()[0]
            events = []
            for receipt in changed:
                event = {'invalidation_id': uuid.uuid4().hex, 'disposition_id': receipt['disposition_id'],
                         'revision': revision + 1, 'actor_id': actor_id, 'previous_fingerprint': receipt['source_fingerprint'],
                         'current_fingerprint': current_fingerprint, 'created_at': _stamp(instant)}
                db.execute('INSERT INTO memory_disposition_invalidations VALUES(?,?,?,?,?,?,?)', tuple(event.values()))
                events.append(event)
            count = db.execute('UPDATE memory_head SET revision=? WHERE singleton=1 AND revision=?', (revision + 1, revision)).rowcount
            if count != 1:
                raise VersionConflict('근거 변경 확인 중 다른 처리가 저장되었습니다.')
            return events

    @staticmethod
    def _eligible_disposition(db, record_id, disposition, actor_id, current_fingerprint):
        row = db.execute('SELECT i.*,a.actor_id AS saved_actor_id FROM memory_intents i '
                         'JOIN memory_approvals a ON a.approval_id=i.approval_id WHERE i.record_id=?', (record_id,)).fetchone()
        if row is None:
            raise MemoryError('먼저 저장한 담당자 처리 요청을 선택해 주세요.')
        if row['saved_actor_id'] != actor_id:
            raise BindingError('현재 로컬 사용자에게 저장된 요청이 아닙니다.')
        record = BusinessMemory._record(row)
        original = _validate_payload(record['payload'])
        if original['intent'] != 'set_disposition' or original['application'] not in ('current_case', 'current_and_future'):
            raise MemoryError('이번 청구의 담당자 처리 요청만 적용할 수 있습니다. 미래 기준이나 비교 규칙은 아직 적용하지 않습니다.')
        if current_fingerprint != digest(original['source_snapshot']):
            raise VersionConflict('요청 저장 이후 원본·기존 기준·결과가 바뀌었습니다. 현재 대상과 근거로 다시 확인해 주세요.')
        result = {'schema_version': 'c3.1', 'record_id': record_id, 'record_version': record['record_version'],
                'record_payload_hash': row['payload_hash'], 'case_id': original['case_id'], 'month': original['month'],
                'period': original['period'], 'scope': original['scope'], 'application': original['application'],
                'disposition': disposition, 'reason': original['reason'], 'targets': original['targets'],
                'source_fingerprint': current_fingerprint, 'future_rule_applied': False, 'calculation_unchanged': True}
        result['replaces_invalidated_ids'] = BusinessMemory._invalidated_disposition_ids(db, result)
        return result

    @staticmethod
    def _overlapping_disposition_targets(a, b):
        if a['case_id'] != b['case_id']:
            return False
        if {t['target_id'] for t in a['targets']}.intersection(t['target_id'] for t in b['targets']):
            return True
        def occurrences(value):
            return {(target['check'], target.get('source', ''), target.get('locator', ''))
                    for target in value['targets'] if target.get('locator')}
        return bool(occurrences(a).intersection(occurrences(b)))

    @staticmethod
    def _invalidated_disposition_ids(db, payload):
        if not BusinessMemory._disposition_schema(db):
            return []
        query = ('SELECT d.* FROM memory_dispositions d JOIN memory_disposition_invalidations i '
                 'ON i.disposition_id=d.disposition_id WHERE NOT EXISTS '
                 '(SELECT 1 FROM memory_dispositions n WHERE n.record_id=d.record_id AND n.disposition_version>d.disposition_version)')
        return sorted(row['disposition_id'] for row in db.execute(query)
                      if BusinessMemory._overlapping_disposition_targets(
                          payload, BusinessMemory._disposition_record(row)['payload']))

    @staticmethod
    def _disposition_conflicts(db, payload):
        conflicts = []
        query = ('SELECT d.* FROM memory_dispositions d WHERE NOT EXISTS '
                 '(SELECT 1 FROM memory_dispositions n WHERE n.record_id=d.record_id AND n.disposition_version>d.disposition_version) '
                 'AND NOT EXISTS (SELECT 1 FROM memory_disposition_invalidations i WHERE i.disposition_id=d.disposition_id)')
        for row in db.execute(query):
            existing = BusinessMemory._disposition_record(row)['payload']
            if existing['record_id'] == payload['record_id'] or existing['case_id'] != payload['case_id']:
                continue
            if BusinessMemory._overlapping_disposition_targets(payload, existing):
                conflicts.append({'record_id': existing['record_id'], 'disposition_id': row['disposition_id'],
                                  'reason': '동일 청구 대상에 다른 저장 요청의 담당자 처리가 있습니다.'})
        return conflicts

    @staticmethod
    def _disposition_view(row, session_id):
        payload = json.loads(row['payload_json'])
        if digest(payload) != row['payload_hash'] or payload['record_id'] != row['record_id']:
            raise MemoryError('담당자 처리 미리보기의 내용이 변경되었습니다.')
        return {key: row[key] for key in ('proposal_id', 'proposal_hash', 'expected_revision', 'source_fingerprint', 'expires_at')} | {
            'approval_nonce': _nonce(session_id, row), 'status': 'preview', 'payload': payload,
            'changes_applied': False, 'calculation_unchanged': True, 'calculation_executed': False,
            'future_rule_applied': False, 'conflicts': []}

    @staticmethod
    def _disposition_audit(db, event, proposal_id, revision, instant, disposition_id=None, detail=None):
        db.execute('INSERT INTO memory_disposition_audit VALUES(?,?,?,?,?,?,?)', (
            uuid.uuid4().hex, event, proposal_id, disposition_id, revision, canonical(detail or {}), _stamp(instant)))

    def propose_disposition(self, record_id, disposition, *, actor_id, session_id, conversation_id,
                            current_fingerprint, expected_revision, idempotency_key, ttl_seconds=900, now=None):
        _text(record_id, '저장 요청 ID', 256)
        if not isinstance(disposition, str) or disposition not in DISPOSITIONS:
            raise MemoryError('담당자 처리는 검토완료·예외인정·재검토 중에서 선택해 주세요.')
        session_hash = _identity(actor_id, session_id, conversation_id)
        _text(idempotency_key, '처리 재시도 키', 512)
        if not isinstance(current_fingerprint, str) or not HASH.fullmatch(current_fingerprint):
            raise MemoryError('현재 원본·기준·결과의 해시가 필요합니다.')
        if type(expected_revision) is not int or expected_revision < 0:
            raise MemoryError('업무 저장소의 현재 버전이 필요합니다.')
        if type(ttl_seconds) is not int or not 30 <= ttl_seconds <= 3600:
            raise MemoryError('담당자 처리 확인 유효시간은 30~3600초여야 합니다.')
        if not self.path.is_file():
            raise MemoryError('저장된 담당자 처리 요청이 없습니다.')
        instant = _time(now)
        with self._connect(write=True) as db:
            payload = self._eligible_disposition(db, record_id, disposition, actor_id, current_fingerprint)
            self._disposition_schema(db, create=True)
            payload_hash = digest(payload)
            old = db.execute('SELECT * FROM memory_disposition_proposals WHERE actor_id=? AND idempotency_key=?', (actor_id, idempotency_key)).fetchone()
            if old:
                if old['session_hash'] != session_hash or old['conversation_id'] != conversation_id:
                    raise BindingError('이 대화·세션에서 만든 처리 미리보기가 아닙니다.')
                if old['payload_hash'] != payload_hash or old['expected_revision'] != expected_revision:
                    raise IdempotencyConflict('같은 요청 키로 다른 담당자 처리를 적용할 수 없습니다.')
                if db.execute('SELECT 1 FROM memory_disposition_cancellations WHERE proposal_id=?', (old['proposal_id'],)).fetchone():
                    raise MemoryError('취소된 처리 미리보기입니다. 다시 확인해 주세요.')
                if instant >= _parse_stamp(old['expires_at']):
                    raise ExpiredProposal('담당자 처리 확인 시간이 지났습니다. 다시 확인해 주세요.')
                return self._disposition_view(old, session_id)
            revision = db.execute('SELECT revision FROM memory_head WHERE singleton=1').fetchone()[0]
            if revision != expected_revision:
                raise VersionConflict('다른 업무 요청이 먼저 저장되었습니다. 담당자 처리 내용을 다시 확인해 주세요.')
            conflicts = self._disposition_conflicts(db, payload)
            if conflicts:
                raise ScopeConflict(conflicts)
            proposal = {'proposal_id': uuid.uuid4().hex, 'record_id': record_id, 'actor_id': actor_id,
                        'session_hash': session_hash, 'conversation_id': conversation_id, 'idempotency_key': idempotency_key,
                        'payload_json': canonical(payload), 'payload_hash': payload_hash,
                        'source_fingerprint': current_fingerprint, 'expected_revision': expected_revision,
                        'created_at': _stamp(instant), 'expires_at': _stamp(instant + timedelta(seconds=ttl_seconds))}
            proposal['nonce_hash'] = hashlib.sha256(_nonce(session_id, proposal).encode()).hexdigest()
            proposal['proposal_hash'] = digest(proposal)
            db.execute(f'INSERT INTO memory_disposition_proposals ({",".join(proposal)}) VALUES ({",".join("?" for _ in proposal)})', tuple(proposal.values()))
            self._disposition_audit(db, 'preview_created', proposal['proposal_id'], revision, instant)
            return self._disposition_view(proposal, session_id)

    @staticmethod
    def _disposition_bound(db, proposal_id, proposal_hash, approval_nonce, actor_id, session_id, conversation_id):
        session_hash = _identity(actor_id, session_id, conversation_id)
        for value in (proposal_id, proposal_hash, approval_nonce):
            _text(value, '담당자 처리 확인 참조', 512)
        if not HASH.fullmatch(proposal_hash) or not HASH.fullmatch(approval_nonce):
            raise BindingError('담당자 처리 확인 참조가 올바르지 않습니다.')
        row = db.execute('SELECT * FROM memory_disposition_proposals WHERE proposal_id=?', (proposal_id,)).fetchone()
        if not row or row['actor_id'] != actor_id or row['session_hash'] != session_hash or row['conversation_id'] != conversation_id:
            raise BindingError('이 대화·세션에서 만든 처리 미리보기가 아닙니다.')
        if not hmac.compare_digest(row['proposal_hash'], proposal_hash) or not hmac.compare_digest(row['nonce_hash'], hashlib.sha256(approval_nonce.encode()).hexdigest()):
            raise BindingError('담당자 처리 확인 대상이 달라졌습니다.')
        actual = dict(row); actual.pop('proposal_hash')
        if digest(actual) != row['proposal_hash']:
            raise MemoryError('담당자 처리 미리보기의 내용이 변경되었습니다.')
        BusinessMemory._disposition_view(row, session_id)
        return row

    def confirm_disposition(self, proposal_id, *, proposal_hash, approval_nonce, actor_id, session_id,
                            conversation_id, current_fingerprint, now=None):
        instant = _time(now)
        if not self.path.is_file():
            raise MemoryError('적용할 담당자 처리 미리보기가 없습니다.')
        with self._connect(write=True) as db:
            if not self._disposition_schema(db):
                raise MemoryError('적용할 담당자 처리 미리보기가 없습니다.')
            proposal = self._disposition_bound(db, proposal_id, proposal_hash, approval_nonce, actor_id, session_id, conversation_id)
            applied = db.execute('SELECT * FROM memory_dispositions WHERE proposal_id=?', (proposal_id,)).fetchone()
            if applied:
                return self._disposition_with_invalidation(db, applied, replay=True)
            if db.execute('SELECT 1 FROM memory_disposition_cancellations WHERE proposal_id=?', (proposal_id,)).fetchone():
                raise MemoryError('취소한 담당자 처리는 적용할 수 없습니다.')
            if instant >= _parse_stamp(proposal['expires_at']):
                raise ExpiredProposal('담당자 처리 확인 시간이 지났습니다. 다시 확인해 주세요.')
            if current_fingerprint != proposal['source_fingerprint']:
                raise VersionConflict('원본·기존 기준·결과가 바뀌었습니다. 담당자 처리 내용을 다시 확인해 주세요.')
            expected_payload = json.loads(proposal['payload_json'])
            payload = self._eligible_disposition(db, proposal['record_id'], expected_payload['disposition'], actor_id, current_fingerprint)
            if digest(payload) != proposal['payload_hash']:
                raise VersionConflict('저장한 요청의 대상·사유·범위가 달라졌습니다.')
            revision = db.execute('SELECT revision FROM memory_head WHERE singleton=1').fetchone()[0]
            if revision != proposal['expected_revision']:
                raise VersionConflict('다른 업무 처리가 먼저 반영되었습니다. 현재 상태로 다시 확인해 주세요.')
            conflicts = self._disposition_conflicts(db, payload)
            if conflicts:
                raise ScopeConflict(conflicts)
            approval_id, disposition_id = uuid.uuid4().hex, uuid.uuid4().hex
            stamp = _stamp(instant)
            db.execute('INSERT INTO memory_disposition_approvals VALUES(?,?,?,?,?,?,?,?)', (
                approval_id, proposal_id, proposal_hash, actor_id, proposal['session_hash'], conversation_id, 'accepted', stamp))
            db.execute('INSERT INTO memory_dispositions VALUES(?,?,?,?,?,?,?,?,?,?)', (
                disposition_id, revision + 1, proposal['record_id'], proposal_id, approval_id, payload['disposition'],
                canonical(payload), digest(payload), current_fingerprint, stamp))
            changed = db.execute('UPDATE memory_head SET revision=? WHERE singleton=1 AND revision=?', (revision + 1, revision)).rowcount
            if changed != 1:
                raise VersionConflict('담당자 처리가 다른 요청과 겹쳤습니다. 다시 확인해 주세요.')
            self._disposition_audit(db, 'disposition_applied', proposal_id, revision + 1, instant, disposition_id,
                                    {'source_fingerprint': current_fingerprint, 'calculation_unchanged': True, 'future_rule_applied': False})
            return self._disposition_with_invalidation(db, db.execute('SELECT * FROM memory_dispositions WHERE disposition_id=?', (disposition_id,)).fetchone())

    def cancel_disposition(self, proposal_id, *, proposal_hash, approval_nonce, actor_id, session_id,
                           conversation_id, now=None):
        instant = _time(now)
        if not self.path.is_file():
            raise MemoryError('취소할 담당자 처리 미리보기가 없습니다.')
        with self._connect(write=True) as db:
            if not self._disposition_schema(db):
                raise MemoryError('취소할 담당자 처리 미리보기가 없습니다.')
            self._disposition_bound(db, proposal_id, proposal_hash, approval_nonce, actor_id, session_id, conversation_id)
            if db.execute('SELECT 1 FROM memory_dispositions WHERE proposal_id=?', (proposal_id,)).fetchone():
                raise MemoryError('이미 반영된 담당자 처리입니다. 새 처리 상태를 확인한 뒤 변경해 주세요.')
            previous = db.execute('SELECT 1 FROM memory_disposition_cancellations WHERE proposal_id=?', (proposal_id,)).fetchone()
            if not previous:
                db.execute('INSERT INTO memory_disposition_cancellations VALUES(?,?)', (proposal_id, _stamp(instant)))
                revision = db.execute('SELECT revision FROM memory_head WHERE singleton=1').fetchone()[0]
                self._disposition_audit(db, 'preview_cancelled', proposal_id, revision, instant)
            return {'proposal_id': proposal_id, 'status': 'cancelled', 'changes_applied': False,
                    'calculation_unchanged': True, 'future_rule_applied': False, 'idempotent_replay': bool(previous)}
