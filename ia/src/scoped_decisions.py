"""Server-scoped user decisions; no calculations, business-rule inference or model calls.

Callers construct pending questions from tool evidence, persist this store separately
from legacy decisions, and apply returned modes only in implemented calculation tools.
An implemented choice is a server declaration, never a field accepted from an answer.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile


SCHEMA_VERSION = '1.0'
UNRESOLVABLE = '확정할 수 없음'


def _text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('required_text:' + field)
    return value.strip()


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_canonical(value).encode('utf-8')).hexdigest()


def build_scope_fingerprint(scope):
    """Bind a question to company, warehouse, period, sources, target and rule version.

    source_hashes is the *relevant* evidence subset, not an unrelated whole-project
    fingerprint. The caller owns its selection and the target's business identity.
    """
    if not isinstance(scope, dict):
        raise ValueError('invalid_scope')
    required = ('company_id', 'warehouse_id', 'period', 'source_hashes', 'target', 'rule_version')
    if any(key not in scope or scope[key] in (None, '', {}, []) for key in required):
        raise ValueError('incomplete_scope')
    for key in ('company_id', 'warehouse_id', 'rule_version'):
        _text(scope[key], key)
    if not isinstance(scope['source_hashes'], dict) or not all(
            isinstance(k, str) and k and isinstance(v, str) and re.fullmatch(r'[a-fA-F0-9]{64}', v)
            for k, v in scope['source_hashes'].items()):
        raise ValueError('invalid_source_hashes')
    normalized = deepcopy(scope)
    normalized['source_hashes'] = {k:v.lower() for k,v in scope['source_hashes'].items()}
    return _hash(normalized)


def validate_question(question):
    if not isinstance(question, dict):
        raise ValueError('invalid_question')
    q = deepcopy(question)
    for field in ('id', 'category', 'text', 'scope_fingerprint'):
        q[field] = _text(q.get(field), field)
    if not re.fullmatch(r'[a-fA-F0-9]{64}', q['scope_fingerprint']):
        raise ValueError('invalid_scope_fingerprint')
    q['scope_fingerprint'] = q['scope_fingerprint'].lower()
    checks = q.get('check_ids')
    if not isinstance(checks, list) or not checks:
        raise ValueError('check_ids_required')
    checks = [_text(value, 'check_id') for value in checks]
    if len(checks) != len(set(checks)):
        raise ValueError('duplicate_check_id')
    q['check_ids'] = sorted(checks)
    choices = q.get('choices')
    if not isinstance(choices, list):
        raise ValueError('choices_required')
    ids = set()
    for choice in choices:
        if not isinstance(choice, dict):
            raise ValueError('invalid_choice')
        for field in ('id', 'label', 'mode'):
            choice[field] = _text(choice.get(field), 'choice.' + field)
        if choice['id'] in ids:
            raise ValueError('duplicate_choice_id')
        ids.add(choice['id'])
        if not isinstance(choice.get('implemented'), bool):
            raise ValueError('choice_implementation_required')
        if choice['label'] == UNRESOLVABLE:
            raise ValueError('unresolvable_is_reserved')
        if 'parameters' in choice and not isinstance(choice['parameters'], dict):
            raise ValueError('invalid_choice_parameters')
    _canonical(q)  # Reject non-JSON and non-finite evidence before any write.
    return q


def _questions(pending):
    if not isinstance(pending, list):
        raise ValueError('pending_questions_required')
    result = {}
    for value in pending:
        q = validate_question(value)
        if q['id'] in result:
            raise ValueError('duplicate_question_id')
        result[q['id']] = q
    return result


def _question_fingerprint(q):
    # Wording/evidence-link edits do not invalidate a decision with the same scope.
    # Availability is a code property, not a new user choice. Changed modes,
    # parameters or check scope are different decisions.
    choices = [{k:c[k] for k in ('id','mode','parameters') if k in c} for c in q['choices']]
    return _hash({'id':q['id'], 'scope_fingerprint':q['scope_fingerprint'],
                  'check_ids':q['check_ids'], 'choices':sorted(choices, key=lambda c:c['id'])})


def _store(existing):
    value = deepcopy(existing) if existing is not None else {'schema_version':SCHEMA_VERSION,'decisions':{}}
    if (not isinstance(value, dict) or value.get('schema_version') != SCHEMA_VERSION
            or not isinstance(value.get('decisions'), dict)):
        raise ValueError('invalid_decision_store')
    _canonical(value)
    return value


def _answer(q, value):
    if isinstance(value, str):
        answer = value.strip()
        choice_id = None
    elif isinstance(value, dict) and set(value) == {'choice_id','answer'}:
        choice_id = _text(value['choice_id'], 'choice_id')
        answer = _text(value['answer'], 'answer')
    else:
        raise ValueError('invalid_answer_shape')
    if (not answer or answer in ('모름','모르겠음','모르겠어요','확인 필요','미정','알 수 없음')
            or (UNRESOLVABLE in answer and answer != UNRESOLVABLE)):
        raise ValueError('explicit_item_decision_required')
    if answer == UNRESOLVABLE:
        if choice_id is not None:
            raise ValueError('unresolvable_answer_must_be_explicit_string')
        return {'answer':answer, 'decision_status':'confirmed_unresolvable_by_user',
                'choice_id':None, 'mode':None, 'parameters':{}, 'skip_check_ids':list(q['check_ids'])}
    choices = [c for c in q['choices'] if c['id'] == choice_id] if choice_id is not None else [
        c for c in q['choices'] if c['label'] == answer]
    if choice_id is not None and not choices:
        raise ValueError('unknown_choice_id')
    if len(choices) > 1:
        raise ValueError('ambiguous_choice_label')
    if choices:
        choice = choices[0]
        if choice_id is not None and answer != choice['label']:
            raise ValueError('choice_answer_mismatch')
        return {'answer':answer, 'choice_id':choice['id'], 'mode':choice['mode'],
                'parameters':deepcopy(choice.get('parameters', {})), 'skip_check_ids':[],
                'decision_status':'confirmed_by_user' if choice['implemented'] else 'rule_implementation_required'}
    # Free text is retained as a development requirement, not made executable.
    return {'answer':answer, 'choice_id':None, 'mode':None, 'parameters':{}, 'skip_check_ids':[],
            'decision_status':'rule_implementation_required'}


def prepare_answers(pending, answers, existing=None):
    """Validate the whole batch and return a new store; never mutate the input store."""
    questions = _questions(pending)
    if not isinstance(answers, dict) or not answers:
        raise ValueError('explicit_answers_required')
    if any(key not in questions for key in answers):
        raise ValueError('unasked_question')
    value = _store(existing)
    prepared = {}
    for key, answer in answers.items():
        q = questions[key]
        parsed = _answer(q, answer)
        fingerprint = _question_fingerprint(q)
        record = {'question_id':key, 'scope_fingerprint':q['scope_fingerprint'],
                  'question_fingerprint':fingerprint, 'check_ids':q['check_ids'],
                  'category':q['category'], 'question':q['text'],
                  'evidence_refs':deepcopy(q.get('evidence_refs', [])), **parsed}
        old = value['decisions'].get(fingerprint)
        if old:
            relevant = ('question_id','scope_fingerprint','question_fingerprint','check_ids',
                        'choice_id','mode','parameters','skip_check_ids')
            if any(old.get(k) != record.get(k) for k in relevant):
                raise ValueError('conflicting_existing_decision')
            if record['choice_id'] is None and old.get('answer') != record['answer']:
                raise ValueError('conflicting_existing_decision')
            prepared[fingerprint] = old  # Idempotent replay preserves time/evidence.
        else:
            record['recorded_at'] = datetime.now(timezone.utc).isoformat()
            prepared[fingerprint] = record
    value['decisions'].update(prepared)
    return value


def resolve_questions(pending, records=None):
    """Return scoped ready decisions and blocks; never change calculation findings."""
    questions = _questions(pending)
    store = _store(records)
    applied = {}; unanswered = []; needs_implementation = []; approved_skip = set(); blocked = set()
    for key, q in questions.items():
        record = store['decisions'].get(_question_fingerprint(q))
        if not record:
            unanswered.append(q);blocked.update(q['check_ids']);continue
        # Detect edited store data rather than applying a record under the wrong key.
        if (record.get('question_id') != key or record.get('scope_fingerprint') != q['scope_fingerprint']
                or record.get('question_fingerprint') != _question_fingerprint(q)
                or record.get('check_ids') != q['check_ids']):
            raise ValueError('decision_binding_mismatch')
        current_choice = next((c for c in q['choices'] if c['id'] == record.get('choice_id')), None)
        replay_answer = ({'choice_id':record['choice_id'],'answer':current_choice['label'] if current_choice else record['answer']}
                         if record.get('choice_id') else record.get('answer'))
        parsed = _answer(q, replay_answer)
        availability_states = {'confirmed_by_user','rule_implementation_required'}
        checked = ('choice_id','mode','parameters','skip_check_ids')
        if (any(record.get(k) != parsed[k] for k in checked)
                or (record.get('choice_id') and record.get('decision_status') not in availability_states)
                or (not record.get('choice_id') and record.get('decision_status') != parsed['decision_status'])):
            raise ValueError('decision_content_mismatch')
        effective = deepcopy(record)
        effective['recorded_decision_status'] = record['decision_status']
        effective['decision_status'] = parsed['decision_status']
        if parsed['decision_status'] == 'rule_implementation_required':
            needs_implementation.append({'question':q, 'decision':effective})
            blocked.update(q['check_ids'])
        else:
            applied[key] = effective
            approved_skip.update(record['skip_check_ids'])
    return {'applied_decisions':applied, 'pending_questions':unanswered,
            'rule_implementation_required':needs_implementation,
            'skip_check_ids':sorted(approved_skip - blocked), 'blocked_check_ids':sorted(blocked),
            'waiting_for_input':bool(unanswered), 'ready_for_execution':not unanswered and not needs_implementation,
            'processing_status':'awaiting_user_decision' if unanswered else 'rule_implementation_required' if needs_implementation else 'decisions_ready'}


def _load(path):
    def unique_pairs(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('duplicate_json_key')
            value[key] = item
        return value
    return json.loads(path.read_text('utf-8-sig'), object_pairs_hook=unique_pairs)


def save_answers(path, pending, answers):
    """Atomically save one validated batch to the caller-selected decision store."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name(path.name + '.lock')
    try:
        handle = lock.open('x', encoding='utf-8')
    except FileExistsError as exc:
        raise ValueError('decision_store_busy') from exc
    temporary = None
    try:
        with handle:
            handle.write('scoped_decisions\n')
        value = prepare_answers(pending, answers, _load(path) if path.is_file() else None)
        encoded = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
        if path.is_file() and _load(path) == value:
            return value
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix=path.name + '.', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(encoded);stream.flush();os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
        return value
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        lock.unlink(missing_ok=True)
