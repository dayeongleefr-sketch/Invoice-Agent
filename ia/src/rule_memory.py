"""C4 immutable, source-bound selected-check calculations and rule approvals.

Only the trusted calculation adapter calls this ledger. It never evaluates user
text, opens source files, changes base results, or applies a future-month rule.
"""
from datetime import timedelta
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import hmac
import json
import re
import uuid

from business_memory import (BusinessMemory, MemoryError, BindingError,
                             VersionConflict, ExpiredProposal, IdempotencyConflict,
                             ScopeConflict, HASH, canonical, digest, _identity,
                             _nonce, _text, _time, _stamp, _parse_stamp,
                             _validate_payload)


RULE_SCHEMA_VERSION = 'c4-rule-storage-v1'
from recurring_rules import KINDS as RECURRING_KINDS
RULE_KINDS = {'quantity_components', 'quantity_rematch', 'b2c_link', 'pack_conversion', 'rate_selection', 'custom_rate'} | RECURRING_KINDS
STATUSES = {'match', 'difference', 'not_determinable', 'excluded', 'order_linked', 'shipment_linked'}
_TABLES = ('memory_rule_proposals', 'memory_rule_cancellations',
           'memory_rule_approvals', 'memory_rules', 'memory_rule_invalidations', 'memory_rule_audit')
_SCHEMA = """
CREATE TABLE memory_rule_proposals (
 proposal_id TEXT PRIMARY KEY, record_id TEXT NOT NULL REFERENCES memory_intents(record_id),
 actor_id TEXT NOT NULL, session_hash TEXT NOT NULL, conversation_id TEXT NOT NULL,
 idempotency_key TEXT NOT NULL, payload_json TEXT NOT NULL, payload_hash TEXT NOT NULL,
 source_fingerprint TEXT NOT NULL, code_fingerprint TEXT NOT NULL, expected_revision INTEGER NOT NULL,
 created_at TEXT NOT NULL, expires_at TEXT NOT NULL, nonce_hash TEXT NOT NULL, proposal_hash TEXT NOT NULL,
 UNIQUE(actor_id,idempotency_key)) STRICT;
CREATE TABLE memory_rule_cancellations (
 proposal_id TEXT PRIMARY KEY REFERENCES memory_rule_proposals(proposal_id), created_at TEXT NOT NULL) STRICT;
CREATE TABLE memory_rule_approvals (
 approval_id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL UNIQUE REFERENCES memory_rule_proposals(proposal_id),
 proposal_hash TEXT NOT NULL, actor_id TEXT NOT NULL, session_hash TEXT NOT NULL,
 conversation_id TEXT NOT NULL, decision TEXT NOT NULL CHECK(decision='accepted'), approved_at TEXT NOT NULL) STRICT;
CREATE TABLE memory_rules (
 rule_id TEXT PRIMARY KEY, rule_version INTEGER NOT NULL UNIQUE,
 record_id TEXT NOT NULL REFERENCES memory_intents(record_id),
 proposal_id TEXT NOT NULL UNIQUE REFERENCES memory_rule_proposals(proposal_id),
 approval_id TEXT NOT NULL UNIQUE REFERENCES memory_rule_approvals(approval_id),
 payload_json TEXT NOT NULL, payload_hash TEXT NOT NULL,
 source_fingerprint TEXT NOT NULL, code_fingerprint TEXT NOT NULL, applied_at TEXT NOT NULL) STRICT;
CREATE TABLE memory_rule_invalidations (
 invalidation_id TEXT PRIMARY KEY, rule_id TEXT NOT NULL UNIQUE REFERENCES memory_rules(rule_id),
 revision INTEGER NOT NULL, actor_id TEXT NOT NULL,
 previous_fingerprint TEXT NOT NULL, current_fingerprint TEXT NOT NULL,
 previous_code_fingerprint TEXT NOT NULL, current_code_fingerprint TEXT NOT NULL, created_at TEXT NOT NULL) STRICT;
CREATE TABLE memory_rule_audit (
 event_id TEXT PRIMARY KEY, event TEXT NOT NULL, proposal_id TEXT,
 rule_id TEXT, revision INTEGER NOT NULL, detail_json TEXT NOT NULL, created_at TEXT NOT NULL) STRICT;
"""


def _fields(value, fields, label):
    if type(value) is not dict or set(value) != set(fields):
        raise MemoryError(f'{label} 필드 구성이 올바르지 않습니다.')


def _bounded_json(value, depth=0):
    if depth > 5:
        raise MemoryError('비교 기준의 중첩 깊이가 너무 큽니다.')
    if value is None or type(value) is bool:
        return
    if type(value) is str:
        if len(value) > 4000:
            raise MemoryError('비교 기준 값이 너무 깁니다.')
        return
    if type(value) is int and abs(value) <= 10 ** 15:
        return
    if type(value) is list and len(value) <= 1000:
        for item in value:
            _bounded_json(item, depth + 1)
        return
    if type(value) is dict and len(value) <= 100:
        for key, item in value.items():
            if type(key) is not str or not re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_]{0,63}', key):
                raise MemoryError('비교 기준 매개변수 이름이 올바르지 않습니다.')
            _bounded_json(item, depth + 1)
        return
    raise MemoryError('비교 기준은 제한된 JSON 값만 허용합니다. 소수는 문자열로 전달해 주세요.')


def _number(value):
    if value is None:
        return None
    if type(value) is not str or len(value) > 80 or not re.fullmatch(r'-?\d+(?:\.\d+)?', value):
        raise MemoryError('계산값은 유한한 십진수 문자열 또는 빈값이어야 합니다.')
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise MemoryError('계산값을 읽을 수 없습니다.') from exc
    if not number.is_finite() or abs(number) > Decimal('1e40'):
        raise MemoryError('계산값의 범위가 너무 큽니다.')
    return number


def _validate(spec, calculation, targets):
    _fields(spec, ('kind', 'parameters'), '비교 기준')
    if type(spec['kind']) is not str or spec['kind'] not in RULE_KINDS or type(spec['parameters']) is not dict:
        raise MemoryError('지원되는 비교 기준 종류와 매개변수가 필요합니다.')
    _bounded_json(spec['parameters'])
    key,allowed = ('consumables',('include','exclude')) if spec['kind']=='quantity_components' else ('rate_source',('claim','contract'))
    if spec['kind']=='formula':
        from formula_rules import validate
        validate(spec['parameters'])
    elif spec['kind']=='pack_conversion':
        from pack_conversion_rules import validate
        validate(spec['parameters'])
    elif spec['kind']=='b2c_link':
        from b2c_link_rules import validate
        validate(spec['parameters'])
    elif spec['kind']=='quantity_rematch':
        from quantity_rematch import validate
        validate(spec['parameters'])
    elif spec['kind']=='recurring_quantity':
        from recurring_quantity_rules import validate
        validate(spec['parameters'])
    elif spec['kind']=='recurring_rate':
        from recurring_rate_rules import validate
        validate(spec['parameters'])
    elif spec['kind']=='custom_rate':
        from identifier_tools import stored_number
        p=spec['parameters']
        if set(p)!={'rate','unit'} or type(p.get('rate')) is not str or stored_number(p['rate']) is None or abs(stored_number(p['rate']))>Decimal('1e15') or not isinstance(p.get('unit'),str) or not p['unit'].strip():
            raise MemoryError('지정 단가와 원본 과금 단위를 확인해 주세요.')
    elif set(spec['parameters'])!={key} or spec['parameters'][key] not in allowed:
        raise MemoryError('지원되는 비교 기준의 매개변수를 선택해 주세요.')
    _fields(calculation, ('adapter_version', 'changes', 'limitations', 'calculation_executed'), '계산 결과')
    _text(calculation['adapter_version'], '계산 도구 버전', 128)
    if calculation['calculation_executed'] is not True:
        raise MemoryError('실제 실행한 계산 결과만 저장할 수 있습니다.')
    changes, limitations = calculation['changes'], calculation['limitations']
    if type(changes) is not list or not 1 <= len(changes) <= 1000:
        raise MemoryError('선택 대상의 실제 비교 결과가 필요합니다.')
    if type(limitations) is not list or len(limitations) > 100:
        raise MemoryError('비교 한계의 형식이 올바르지 않습니다.')
    for item in limitations:
        _text(item, '비교 한계', 4000)
    by_id = {target['target_id']: target for target in targets}
    ids = []
    for change in changes:
        _fields(change, ('target_id', 'label', 'check', 'before', 'after', 'basis', 'evidence_refs'), '대상 계산')
        target_id = _text(change['target_id'], '계산 대상', 256)
        ids.append(target_id)
        if target_id not in by_id or change['check'] != by_id[target_id]['check']:
            raise BindingError('계산 대상과 저장 요청의 검사 항목이 다릅니다.')
        _text(change['label'], '대상 표시 이름', 2000)
        if change['label'] != by_id[target_id].get('label'):
            raise BindingError('계산 대상의 표시 이름과 저장 요청이 다릅니다.')
        _text(change['basis'], '적용 근거', 4000)
        refs = change['evidence_refs']
        if type(refs) is not list or not 1 <= len(refs) <= 10000:
            raise MemoryError('계산 대상의 원본 근거가 필요합니다.')
        for ref in refs:
            _text(ref, '원본 근거', 2000)
        for side in ('before', 'after'):
            values = change[side]
            _fields(values, ('claim', 'comparison', 'difference', 'status', 'unit'), '비교값')
            _text(values['unit'], '수량·금액 단위', 128)
            if type(values['status']) is not str or values['status'] not in STATUSES:
                raise MemoryError('계산 판정 상태가 올바르지 않습니다.')
            claim, comparison, difference = (_number(values[k]) for k in ('claim', 'comparison', 'difference'))
            if values['status'] in ('order_linked','shipment_linked') and spec['kind']!='b2c_link':
                raise MemoryError('연결 판정은 B2C 연결 검사에만 사용할 수 있습니다.')
            if spec['kind']=='b2c_link' and (comparison is not None or difference is not None or values['status'] not in ('order_linked','shipment_linked','not_determinable')):
                raise MemoryError('B2C 연결 여부를 금액 비교나 정상 금액으로 바꿀 수 없습니다.')
            if side == 'after' and values['status'] in ('match', 'difference'):
                with localcontext() as context:
                    context.prec = 100
                    if None in (claim, comparison, difference) or claim - comparison != difference:
                        raise MemoryError('판정에 필요한 차이 계산이 일치하지 않습니다.')
                if (values['status'] == 'match') != (difference == 0):
                    raise MemoryError('계산 차이와 판정 상태가 일치하지 않습니다.')
        before, after = change['before'], change['after']
        if before['claim'] != after['claim'] or before['unit'] != after['unit']:
            raise BindingError('새 기준으로 청구 원문값이나 단위를 바꿀 수 없습니다.')
        target = by_id[target_id]
        if spec['kind']=='formula':
            operation=spec['parameters']['operation']
            expected={'cost':('선택 단가 금액 비교','KRW'),'inbound':('입고 상품 수량','EA'),'outbound':('B2B 상품 수량','EA')}[operation]
            if (target.get('check'),target.get('unit'))!=expected:raise BindingError('수식 적용 대상·단위가 다릅니다.')
        if spec['kind']=='pack_conversion' and (target.get('check') not in ('입고 BOX·PCS 잠정 환산','B2B BOX·PCS 잠정 환산') or target.get('unit') not in ('BOX','PCS')):
            raise BindingError('박스·낱개 참고 비교 대상만 환산할 수 있습니다.')
        if spec['kind']=='b2c_link' and (target.get('check')!='B2C 주문 연결' or target.get('unit')!='기재 운임 원'):
            raise BindingError('B2C 연결 내역만 다시 찾을 수 있습니다.')
        if spec['kind']=='quantity_rematch' and (target.get('check') not in ('입고 상품 수량','B2B 상품 수량') or target.get('unit')!='EA'):
            raise BindingError('기간·연결키 수량 비교는 입고·B2B 상품 EA에만 적용할 수 있습니다.')
        if spec['kind']=='recurring_quantity':
            operation=spec['parameters']['operation']
            if target.get('check')!=('입고 상품 수량' if operation=='inbound' else 'B2B 상품 수량') or target.get('unit')!='EA':
                raise BindingError('저장 수량 기준과 선택한 업무·단위가 다릅니다.')
        if spec['kind'] in ('rate_selection','custom_rate','recurring_rate') and (target.get('check')!='선택 단가 금액 비교' or target.get('unit')!='KRW'):
            raise BindingError('단가 금액 비교는 해당 청구 금액 항목에만 적용할 수 있습니다.')
        for target_key, value_key in (('claim_value', 'claim'), ('comparison_value', 'comparison'), ('difference', 'difference')):
            raw = target.get(target_key)
            original_number = None if raw in (None, '') else _number(str(raw))
            if original_number != _number(before[value_key]):
                raise BindingError('계산 전 수치가 저장된 청구 대상의 수치와 다릅니다.')
        if target.get('unit') and target['unit'] != before['unit']:
            raise BindingError('계산 단위가 저장된 청구 대상의 단위와 다릅니다.')
    if len(ids) != len(set(ids)) or set(ids) != set(by_id):
        raise BindingError('저장 요청의 전체 선택 대상을 빠짐없이 한 번씩 계산해야 합니다.')
    if len(canonical({'spec': spec, 'calculation': calculation}).encode('utf-8')) > 4 * 1024 * 1024:
        raise MemoryError('비교 결과가 허용 크기를 넘습니다.')


class RuleMemory:
    def __init__(self, root):
        self.memory = BusinessMemory(root)
        self.path = self.memory.path

    @staticmethod
    def _schema(db, create=False):
        if db is None:
            return False
        version = db.execute("SELECT value FROM memory_metadata WHERE key='rule_schema_version'").fetchone()
        found = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}.intersection(_TABLES)
        if version:
            if version[0] != RULE_SCHEMA_VERSION or found != set(_TABLES):
                raise MemoryError('비교 기준 저장소의 버전 또는 구성이 다릅니다.')
            return True
        if found:
            raise MemoryError('완성되지 않은 비교 기준 저장소는 자동으로 덮어쓰지 않습니다.')
        if not create:
            return False
        for statement in _SCHEMA.split(';'):
            if statement.strip():
                db.execute(statement)
        for table in _TABLES:
            for operation in ('UPDATE', 'DELETE'):
                db.execute(f"CREATE TRIGGER {table}_{operation.lower()}_blocked BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'immutable_rule_memory'); END")
        db.execute('INSERT INTO memory_metadata VALUES(?,?)', ('rule_schema_version', RULE_SCHEMA_VERSION))
        return True

    @staticmethod
    def _fingerprints(source, code):
        if any(type(value) is not str or not HASH.fullmatch(value) for value in (source, code)):
            raise MemoryError('현재 자료와 계산 코드의 해시가 필요합니다.')

    @staticmethod
    def _payload(db, record_id, actor_id, source, code, spec, calculation, reapproval=None, lifecycle=None):
        row = db.execute('SELECT i.*,a.actor_id AS saved_actor_id FROM memory_intents i '
                         'JOIN memory_approvals a ON a.approval_id=i.approval_id WHERE i.record_id=?', (record_id,)).fetchone()
        if row is None:
            raise MemoryError('먼저 저장한 비교 기준 요청을 선택해 주세요.')
        if row['saved_actor_id'] != actor_id:
            raise BindingError('현재 로컬 사용자의 저장 요청이 아닙니다.')
        record = BusinessMemory._record(row)
        original = _validate_payload(record['payload'])
        if spec.get('kind') in ('quantity_rematch','b2c_link','pack_conversion') and (original['scope']!='once' or original['application']!='current_case'):
            raise BindingError('기간·연결키·대상 조건 변경은 이번 청구에만 저장할 수 있습니다.')
        if original['intent'] != 'upsert_rule' or (original['application'] not in ('current_case', 'current_and_future') and spec['kind'] not in RECURRING_KINDS):
            raise MemoryError('이번 청구의 비교 기준 요청만 계산할 수 있습니다. 미래 월 자동 적용은 지원하지 않습니다.')
        if spec['kind']=='recurring_rate':
            from recurring_rate_rules import binding
            binding(original,spec['parameters'])
        if spec['kind']=='formula':
            from formula_rules import binding
            binding(original,spec['parameters'])
        if spec['kind']=='recurring_quantity':
            from recurring_quantity_rules import binding
            binding(original,spec['parameters'])
        if lifecycle is not None:
            _fields(lifecycle, ('action','previous_rule_id','selected_rule_id'), '기준 중지·복원')
            if lifecycle['action'] not in ('stop','restore') or reapproval is None:
                raise BindingError('현재 자료의 중지 또는 복원 확인이 필요합니다.')
            latest = db.execute('SELECT * FROM memory_rules WHERE record_id=? ORDER BY rule_version DESC LIMIT 1',(record_id,)).fetchone()
            selected = db.execute('SELECT * FROM memory_rules WHERE rule_id=?',(lifecycle['selected_rule_id'],)).fetchone()
            if not latest or latest['rule_id'] != lifecycle['previous_rule_id']:
                raise VersionConflict('현재 적용 버전이 바뀌었습니다. 다시 확인해 주세요.')
            if not selected or selected['record_id'] != record_id:
                raise BindingError('이 요청에 속한 이전 기준을 선택해 주세요.')
            chosen = RuleMemory._record(db,selected)
            if chosen['stopped'] or chosen['payload']['spec'] != spec:
                raise BindingError('선택 버전의 비교 방식과 현재 요청이 다릅니다.')
            if lifecycle['action']=='stop' and selected['rule_id']!=latest['rule_id']:
                raise BindingError('현재 적용된 버전만 중지할 수 있습니다.')
        inherited_reapproval=None
        if reapproval is None and source != digest(original['source_snapshot']):
            latest=db.execute('SELECT * FROM memory_rules WHERE record_id=? ORDER BY rule_version DESC LIMIT 1',(record_id,)).fetchone()
            if latest:
                last=RuleMemory._record(db,latest)
                context=last['payload'].get('reapproval')
                if context and not last['invalidated'] and last['source_fingerprint']==source and last['code_fingerprint']==code:
                    from rule_reapproval import validate
                    original=validate(original,context,source)
                    inherited_reapproval=context
        if reapproval is not None:
            from rule_reapproval import validate
            if type(reapproval) is not dict:raise BindingError('재승인 형식이 올바르지 않습니다.')
            latest = db.execute('SELECT * FROM memory_rules WHERE record_id=? ORDER BY rule_version DESC LIMIT 1',(record_id,)).fetchone()
            if reapproval.get('previous_rule_id') != (latest['rule_id'] if latest else None):
                raise VersionConflict('이전 기준 버전이 변경되었습니다. 다시 계산해 주세요.')
            if latest and lifecycle is None and spec != json.loads(latest['payload_json'])['spec']:
                raise BindingError('재승인은 마지막으로 적용한 비교 방식과 매개변수를 유지해야 합니다.')
            original = validate(original,reapproval,source)
        elif source != digest(original['source_snapshot']):
            raise VersionConflict('요청 저장 이후 자료·기준·결과가 바뀌었습니다. 현재 근거로 다시 확인해 주세요.')
        _validate(spec, calculation, original['targets'])
        if lifecycle is not None and lifecycle['action']=='stop':
            for change in calculation['changes']:
                if any(_number(change['before'][key])!=_number(change['after'][key]) for key in ('claim','comparison','difference')):
                    raise BindingError('기준 중지는 현재 등록 원본의 기본 비교값으로 돌아가야 합니다.')
        payload = {'schema_version': 'c4.1', 'record_id': record_id, 'record_version': record['record_version'],
                'record_payload_hash': row['payload_hash'],
                **{k: original[k] for k in ('company_id', 'warehouse_id', 'case_id', 'month', 'period', 'scope',
                                          'application', 'effective_from_month', 'reason', 'criterion', 'targets')},
                'source_fingerprint': source, 'code_fingerprint': code, 'spec': spec, 'calculation': calculation,
                'future_rule_applied': False, 'base_calculation_unchanged': True}
        if reapproval is not None or inherited_reapproval is not None:
            payload['reapproval']=reapproval if reapproval is not None else inherited_reapproval
            payload['reapproval_requested']=reapproval is not None
        if lifecycle is not None:
            payload['lifecycle']=lifecycle
        if spec['kind'] in RECURRING_KINDS and original['scope']=='future':
            payload['recurring_rule_registered']=not (lifecycle and lifecycle['action']=='stop')
        return payload

    @staticmethod
    def _record(db, row, replay=False):
        if row is None:
            return None
        payload = json.loads(row['payload_json'])
        if digest(payload) != row['payload_hash'] or any(payload[k] != row[k] for k in ('record_id', 'source_fingerprint', 'code_fingerprint')):
            raise MemoryError('저장된 비교 기준의 내용 해시가 일치하지 않습니다.')
        invalidation = db.execute('SELECT * FROM memory_rule_invalidations WHERE rule_id=?', (row['rule_id'],)).fetchone()
        stopped = payload.get('lifecycle',{}).get('action') == 'stop'
        return {key: row[key] for key in ('rule_id', 'rule_version', 'record_id', 'proposal_id', 'approval_id',
                                         'source_fingerprint', 'code_fingerprint', 'applied_at')} | {
            'payload': payload, 'status': 'rule_stopped' if stopped else 'rule_applied', 'stopped':stopped, 'changes_applied': True,
            'calculation_executed': True, 'base_calculation_unchanged': True, 'future_rule_applied': False,
            'idempotent_replay': replay, 'invalidated': bool(invalidation),
            'invalidation': dict(invalidation) if invalidation else None}

    @staticmethod
    def _active_query():
        return ('SELECT r.* FROM memory_rules r WHERE NOT EXISTS '
                '(SELECT 1 FROM memory_rules n WHERE n.record_id=r.record_id AND n.rule_version>r.rule_version) '
                "AND COALESCE(json_extract(r.payload_json,'$.lifecycle.action'),'')!='stop' "
                'AND NOT EXISTS (SELECT 1 FROM memory_rule_invalidations i WHERE i.rule_id=r.rule_id)')

    @staticmethod
    def _conflicts(db, payload):
        if payload.get('lifecycle',{}).get('action')=='stop':
            return []
        conflicts = []
        ids = {t['target_id'] for t in payload['targets']}
        locations = {(t['check'], t.get('source'), t.get('locator')) for t in payload['targets'] if t.get('locator')}
        for row in db.execute(RuleMemory._active_query()):
            old = RuleMemory._record(db, row)
            other = old['payload']
            if other['record_id'] == payload['record_id']:
                continue
            if payload['spec']['kind']==other['spec']['kind']=='recurring_rate':
                from recurring_rate_rules import selector
                if (payload['company_id'],payload['warehouse_id'],selector(payload['spec']['parameters']))==(other['company_id'],other['warehouse_id'],selector(other['spec']['parameters'])):
                    conflicts.append({'record_id':old['record_id'],'rule_id':old['rule_id'],'rule_version':old['rule_version']})
                    continue
            if payload['spec']['kind']==other['spec']['kind']=='recurring_quantity':
                from recurring_quantity_rules import overlap
                if (payload['company_id'],payload['warehouse_id'])==(other['company_id'],other['warehouse_id']) and overlap(payload['spec']['parameters'],other['spec']['parameters']):
                    conflicts.append({'record_id':old['record_id'],'rule_id':old['rule_id'],'rule_version':old['rule_version']})
                    continue
            if other['case_id'] != payload['case_id']:continue
            if ids.intersection(t['target_id'] for t in other['targets']) or locations.intersection(
                    (t['check'], t.get('source'), t.get('locator')) for t in other['targets'] if t.get('locator')):
                conflicts.append({'record_id': old['record_id'], 'rule_id': old['rule_id'], 'rule_version': old['rule_version']})
        return conflicts

    @staticmethod
    def _audit(db, event, proposal_id, revision, instant, rule_id=None, detail=None):
        db.execute('INSERT INTO memory_rule_audit VALUES(?,?,?,?,?,?,?)',
                   (uuid.uuid4().hex, event, proposal_id, rule_id, revision, canonical(detail or {}), _stamp(instant)))

    @staticmethod
    def _view(row, session_id):
        payload = json.loads(row['payload_json'])
        if digest(payload) != row['payload_hash']:
            raise MemoryError('비교 기준 미리보기의 내용이 변경되었습니다.')
        return {key: row[key] for key in ('proposal_id', 'proposal_hash', 'expected_revision', 'source_fingerprint', 'code_fingerprint', 'expires_at')} | {
            'approval_nonce': _nonce(session_id, row), 'status': 'preview', 'payload': payload,
            'changes_applied': False, 'calculation_executed': True,
            'base_calculation_unchanged': True, 'future_rule_applied': False, 'conflicts': []}

    def create_rule_preview(self, record_id, spec, calculation, *, actor_id, session_id, conversation_id,
                            source_fingerprint, code_fingerprint, expected_revision, idempotency_key,
                            ttl_seconds=900, now=None, reapproval=None, lifecycle=None):
        _text(record_id, '저장 요청 ID', 256)
        session_hash = _identity(actor_id, session_id, conversation_id)
        _text(idempotency_key, '계산 재시도 키', 512)
        self._fingerprints(source_fingerprint, code_fingerprint)
        if type(expected_revision) is not int or expected_revision < 0:
            raise MemoryError('업무 저장소의 현재 버전이 필요합니다.')
        if type(ttl_seconds) is not int or not 30 <= ttl_seconds <= 3600:
            raise MemoryError('확인 유효시간은 30~3600초여야 합니다.')
        if not self.path.is_file():
            raise MemoryError('저장된 비교 기준 요청이 없습니다.')
        instant = _time(now)
        with self.memory._connect(write=True) as db:
            self._schema(db, create=True)
            payload = self._payload(db, record_id, actor_id, source_fingerprint, code_fingerprint, spec, calculation,reapproval,lifecycle)
            old = db.execute('SELECT * FROM memory_rule_proposals WHERE actor_id=? AND idempotency_key=?', (actor_id, idempotency_key)).fetchone()
            if old:
                if old['session_hash'] != session_hash or old['conversation_id'] != conversation_id:
                    raise BindingError('이 대화·세션의 비교 미리보기가 아닙니다.')
                if old['payload_hash'] != digest(payload) or old['expected_revision'] != expected_revision:
                    raise IdempotencyConflict('같은 요청 키로 다른 비교 결과를 저장할 수 없습니다.')
                if db.execute('SELECT 1 FROM memory_rule_cancellations WHERE proposal_id=?', (old['proposal_id'],)).fetchone():
                    raise MemoryError('취소된 비교 미리보기입니다.')
                if instant >= _parse_stamp(old['expires_at']):
                    raise ExpiredProposal('비교 기준 확인 시간이 지났습니다.')
                return self._view(old, session_id)
            revision = db.execute('SELECT revision FROM memory_head WHERE singleton=1').fetchone()[0]
            if revision != expected_revision:
                raise VersionConflict('다른 업무 요청이 먼저 저장되었습니다. 비교 내용을 다시 확인해 주세요.')
            conflicts = self._conflicts(db, payload)
            if conflicts:
                raise ScopeConflict(conflicts)
            proposal = {'proposal_id': uuid.uuid4().hex, 'record_id': record_id, 'actor_id': actor_id,
                        'session_hash': session_hash, 'conversation_id': conversation_id, 'idempotency_key': idempotency_key,
                        'payload_json': canonical(payload), 'payload_hash': digest(payload),
                        'source_fingerprint': source_fingerprint, 'code_fingerprint': code_fingerprint,
                        'expected_revision': revision, 'created_at': _stamp(instant),
                        'expires_at': _stamp(instant + timedelta(seconds=ttl_seconds))}
            proposal['nonce_hash'] = hashlib.sha256(_nonce(session_id, proposal).encode()).hexdigest()
            proposal['proposal_hash'] = digest(proposal)
            db.execute(f'INSERT INTO memory_rule_proposals ({",".join(proposal)}) VALUES ({",".join("?" for _ in proposal)})', tuple(proposal.values()))
            self._audit(db, 'preview_created', proposal['proposal_id'], revision, instant)
            return self._view(proposal, session_id)

    @staticmethod
    def _bound(db, proposal_id, proposal_hash, approval_nonce, actor_id, session_id, conversation_id):
        session_hash = _identity(actor_id, session_id, conversation_id)
        for value in (proposal_id, proposal_hash, approval_nonce):
            _text(value, '비교 기준 확인 참조', 512)
        if not HASH.fullmatch(proposal_hash) or not HASH.fullmatch(approval_nonce):
            raise BindingError('비교 기준 확인 참조가 올바르지 않습니다.')
        row = db.execute('SELECT * FROM memory_rule_proposals WHERE proposal_id=?', (proposal_id,)).fetchone()
        if not row or row['actor_id'] != actor_id or row['session_hash'] != session_hash or row['conversation_id'] != conversation_id:
            raise BindingError('이 대화·세션의 비교 미리보기가 아닙니다.')
        if not hmac.compare_digest(row['proposal_hash'], proposal_hash) or not hmac.compare_digest(row['nonce_hash'], hashlib.sha256(approval_nonce.encode()).hexdigest()):
            raise BindingError('비교 기준 확인 대상이 달라졌습니다.')
        actual = dict(row); actual.pop('proposal_hash')
        if digest(actual) != row['proposal_hash']:
            raise MemoryError('비교 기준 미리보기의 내용이 변경되었습니다.')
        RuleMemory._view(row, session_id)
        return row

    def confirm_rule(self, proposal_id, *, proposal_hash, approval_nonce, actor_id, session_id,
                     conversation_id, current_fingerprint, current_code_fingerprint, now=None):
        self._fingerprints(current_fingerprint, current_code_fingerprint)
        instant = _time(now)
        if not self.path.is_file():
            raise MemoryError('적용할 비교 기준 미리보기가 없습니다.')
        with self.memory._connect(write=True) as db:
            if not self._schema(db):
                raise MemoryError('적용할 비교 기준 미리보기가 없습니다.')
            proposal = self._bound(db, proposal_id, proposal_hash, approval_nonce, actor_id, session_id, conversation_id)
            if current_fingerprint != proposal['source_fingerprint'] or current_code_fingerprint != proposal['code_fingerprint']:
                raise VersionConflict('자료 또는 계산 코드가 바뀌었습니다. 현재 반영 기록은 재검토가 필요합니다.')
            applied = db.execute('SELECT * FROM memory_rules WHERE proposal_id=?', (proposal_id,)).fetchone()
            if applied:
                return self._record(db, applied, replay=True)
            if db.execute('SELECT 1 FROM memory_rule_cancellations WHERE proposal_id=?', (proposal_id,)).fetchone():
                raise MemoryError('취소한 비교 기준은 적용할 수 없습니다.')
            if instant >= _parse_stamp(proposal['expires_at']):
                raise ExpiredProposal('비교 기준 확인 시간이 지났습니다.')
            expected = json.loads(proposal['payload_json'])
            payload = self._payload(db, proposal['record_id'], actor_id, current_fingerprint, current_code_fingerprint, expected['spec'], expected['calculation'],expected.get('reapproval') if expected.get('reapproval_requested') else None,expected.get('lifecycle'))
            if digest(payload) != proposal['payload_hash']:
                raise VersionConflict('저장한 기준의 대상·계산·범위가 달라졌습니다.')
            revision = db.execute('SELECT revision FROM memory_head WHERE singleton=1').fetchone()[0]
            if revision != proposal['expected_revision']:
                raise VersionConflict('다른 업무 처리가 먼저 반영되었습니다. 현재 상태로 다시 확인해 주세요.')
            conflicts = self._conflicts(db, payload)
            if conflicts:
                raise ScopeConflict(conflicts)
            approval_id, rule_id, stamp = uuid.uuid4().hex, uuid.uuid4().hex, _stamp(instant)
            db.execute('INSERT INTO memory_rule_approvals VALUES(?,?,?,?,?,?,?,?)',
                       (approval_id, proposal_id, proposal_hash, actor_id, proposal['session_hash'], conversation_id, 'accepted', stamp))
            db.execute('INSERT INTO memory_rules VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (rule_id, revision + 1, proposal['record_id'], proposal_id, approval_id, canonical(payload), digest(payload), current_fingerprint, current_code_fingerprint, stamp))
            if db.execute('UPDATE memory_head SET revision=? WHERE singleton=1 AND revision=?', (revision + 1, revision)).rowcount != 1:
                raise VersionConflict('다른 기준 반영과 겹쳤습니다. 다시 확인해 주세요.')
            self._audit(db, 'rule_'+payload.get('lifecycle',{}).get('action','applied'), proposal_id, revision + 1, instant, rule_id,
                        {'source_fingerprint': current_fingerprint, 'code_fingerprint': current_code_fingerprint,
                         'calculation_executed': True, 'base_calculation_unchanged': True})
            return self._record(db, db.execute('SELECT * FROM memory_rules WHERE rule_id=?', (rule_id,)).fetchone())

    def cancel_rule(self, proposal_id, *, proposal_hash, approval_nonce, actor_id, session_id, conversation_id, now=None):
        if not self.path.is_file():
            raise MemoryError('취소할 비교 기준 미리보기가 없습니다.')
        with self.memory._connect(write=True) as db:
            if not self._schema(db):
                raise MemoryError('취소할 비교 기준 미리보기가 없습니다.')
            self._bound(db, proposal_id, proposal_hash, approval_nonce, actor_id, session_id, conversation_id)
            if db.execute('SELECT 1 FROM memory_rules WHERE proposal_id=?', (proposal_id,)).fetchone():
                raise MemoryError('이미 반영된 비교 기준입니다. 취소 대신 새 변경 내용을 확인해 주세요.')
            old = db.execute('SELECT 1 FROM memory_rule_cancellations WHERE proposal_id=?', (proposal_id,)).fetchone()
            if not old:
                instant = _time(now)
                db.execute('INSERT INTO memory_rule_cancellations VALUES(?,?)', (proposal_id, _stamp(instant)))
                revision = db.execute('SELECT revision FROM memory_head WHERE singleton=1').fetchone()[0]
                self._audit(db, 'preview_cancelled', proposal_id, revision, instant)
            return {'proposal_id': proposal_id, 'status': 'cancelled', 'changes_applied': False, 'idempotent_replay': bool(old)}

    def list_rules(self, limit=None, latest=True):
        if (limit is not None and (type(limit) is not int or not 1 <= limit <= 10000)) or type(latest) is not bool:
            raise MemoryError('비교 기준 조회 조건이 올바르지 않습니다.')
        with self.memory._connect() as db:
            if not self._schema(db):
                return []
            query = ('SELECT r.* FROM memory_rules r WHERE NOT EXISTS '
                     '(SELECT 1 FROM memory_rules n WHERE n.record_id=r.record_id AND n.rule_version>r.rule_version) '
                     if latest else 'SELECT r.* FROM memory_rules r ')
            query += 'ORDER BY r.rule_version DESC'
            args = ()
            if limit is not None:
                query += ' LIMIT ?'; args = (limit,)
            return [self._record(db, row) for row in db.execute(query, args)]

    def get_rule(self, rule_id):
        return self._get('rule_id', rule_id)

    def get_rule_for_proposal(self, proposal_id):
        return self._get('proposal_id', proposal_id)

    def _get(self, column, value):
        _text(value, '비교 기준 기록 ID', 256)
        with self.memory._connect() as db:
            if not self._schema(db):
                return None
            return self._record(db, db.execute(f'SELECT * FROM memory_rules WHERE {column}=?', (value,)).fetchone())

    def invalidate_rules(self, current_fingerprint, current_code_fingerprint, *, actor_id='system', now=None):
        self._fingerprints(current_fingerprint, current_code_fingerprint)
        if actor_id != 'system':
            raise BindingError('자료·코드 변경 확인은 시스템만 수행합니다.')
        if not self.path.is_file():
            return []
        with self.memory._connect(write=True) as db:
            if not self._schema(db):
                return []
            changed = [self._record(db, row) for row in db.execute(self._active_query())
                       if row['code_fingerprint'] != current_code_fingerprint or (row['source_fingerprint'] != current_fingerprint and (json.loads(row['payload_json'])['spec']['kind'] not in RECURRING_KINDS or json.loads(row['payload_json']).get('scope')!='future'))]
            if not changed:
                return []
            revision = db.execute('SELECT revision FROM memory_head WHERE singleton=1').fetchone()[0]
            instant, events = _time(now), []
            for receipt in changed:
                event = {'invalidation_id': uuid.uuid4().hex, 'rule_id': receipt['rule_id'], 'revision': revision + 1,
                         'actor_id': actor_id, 'previous_fingerprint': receipt['source_fingerprint'],
                         'current_fingerprint': current_fingerprint, 'previous_code_fingerprint': receipt['code_fingerprint'],
                         'current_code_fingerprint': current_code_fingerprint, 'created_at': _stamp(instant)}
                db.execute('INSERT INTO memory_rule_invalidations VALUES(?,?,?,?,?,?,?,?,?)', tuple(event.values()))
                events.append(event)
            if db.execute('UPDATE memory_head SET revision=? WHERE singleton=1 AND revision=?', (revision + 1, revision)).rowcount != 1:
                raise VersionConflict('근거 변경 확인 중 다른 처리가 저장되었습니다.')
            return events
