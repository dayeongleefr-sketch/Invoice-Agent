"""Local UI adapter. No calculation or business decisions live here."""
import csv
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time
import uuid
from chat_store import ChatStore
from verification_notifications import ACTIONS as VERIFICATION_ACTIONS

ROOT = Path(__file__).resolve().parents[1]
BASE = Path('data/demo_company/derived')
REPORT = Path('data/demo_company/runs/가상센터/report.md')
SESSION = REPORT.with_name('agent_state.json')
POLICY = Path('ia/config/demo_company_supporting_evidence.json')
DEFAULT_REQUEST = 'DEMO COMPANY 가상센터 2029년 7월 전체 청구 검증해줘'
STATUS_LABELS = {'idle': '대기', 'running': '실행 중', 'completed': '완료',
                 'awaiting_user_decision': '사용자 답변 대기', 'draft_ready': '요청 정리 완료', 'failed': '실패'}


def intake_module():
    import sys
    path = str(ROOT / 'ia/src')
    if path not in sys.path:
        sys.path.insert(0, path)
    import chat_intake
    return chat_intake


def conversation_status(result):
    state = result.get('intake', {}).get('state', '')
    if state.startswith('awaiting_') or state in ('invalidated', 'version_conflict'):
        return 'awaiting_user_decision'
    return 'draft_ready' if state == 'draft_ready' else 'completed'


def worker_alive(pid):
    if not pid:return False
    try:pid=int(pid)
    except (TypeError,ValueError):return False
    if pid<=0:return False
    if os.name != 'nt':
        try:os.kill(pid,0)
        except ProcessLookupError:return False
        except PermissionError:return True
        return True
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.restype = wintypes.HANDLE
    handle = kernel.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
    finally:
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle(handle)


def load(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        return default


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


def safe_file(root, relative):
    """Downloads are confined to current company data, without redirects."""
    root = Path(root).resolve()
    if not isinstance(relative, str) or '\\' in relative or ':' in relative or '..' in relative.split('/'):
        raise ValueError('허용하지 않는 파일 경로입니다.')
    p = root / relative
    if not p.resolve().is_relative_to(root / 'data/demo_company') or not p.is_file():
        raise ValueError('현재 검증 자료가 아닙니다.')
    for parent in (p, *p.parents):
        if parent == root:
            break
        if parent.is_symlink() or (hasattr(parent, 'is_junction') and parent.is_junction()):
            raise ValueError('외부 경로 연결은 허용하지 않습니다.')
    return p


class AgentAdapter:
    def __init__(self, root=ROOT):
        self.root = Path(root).resolve()
        self.runtime = self.root / 'frontend/.runtime'
        self.runtime.mkdir(parents=True, exist_ok=True)
        import sys
        self.python = Path(sys.executable)
        self._lock = threading.RLock()
        self.chat_job = {'status':'idle'}
        self.memory_job = {'status':'idle'}
        self._memory_session = uuid.uuid4().hex
        self._memory_commands = None
        self.library = ChatStore(self.runtime)
        self.job = load(self.runtime / 'job.json', {'status': 'idle'})
        self.job.setdefault('conversation_id',self.library.data.get('legacy_chat_id',''))
        self.last_verification_job=load(self.runtime/'last_verification_job.json',{})
        if self.job.get('action') in VERIFICATION_ACTIONS:
            self.last_verification_job=self._notification_job(self.job)
        current=self.library.current()
        if current.get('last_result') and self.job['status']!='running' and current['updated_at']>self.job.get('finished_at',0):
            self.chat_job={'id':'saved-conversation','conversation_id':current['id'],'action':'chat','status':conversation_status(current['last_result']),
                           'result':current['last_result'],'finished_at':current['updated_at']}
        if self.job['status'] == 'running':
            if worker_alive(self.job.get('worker_pid')):
                threading.Thread(target=self._reconnect, daemon=True).start()
            else:
                self.job.update(status='failed', error='이전 실행이 중단되었습니다. 마지막 완료 보고서를 유지합니다.')
                self._save_job()

    def _reconnect(self):
        while worker_alive(self.job.get('worker_pid')):
            time.sleep(1)
        response = load(self.runtime / 'response.json', {})
        with self._lock:
            if response.get('ok'):
                result = response['result']
                self.job.update(status='awaiting_user_decision' if result.get('waiting_for_input') else 'completed', result=result)
                self.message('assistant', '웹앱 재접속 후 기존 실행 결과를 연결했습니다. 결과 탭을 확인하세요.',self.job.get('conversation_id'))
            else:
                self.job.update(status='failed', error=response.get('error', '중단된 실행입니다. 마지막 완료 보고서를 유지합니다.'))
            self.job['finished_at'] = time.time()
            self._save_job()

    def _save_job(self):
        save(self.runtime/'job.json',self.job)
        if self.job.get('action') in VERIFICATION_ACTIONS:
            self.last_verification_job=self._notification_job(self.job)
        if self.last_verification_job:
            try:
                save(self.runtime/'last_verification_job.json',self.last_verification_job)
            except OSError:
                # Notification persistence cannot turn a published calculation
                # into a failed verification. The full job file is authoritative.
                pass

    @staticmethod
    def _notification_job(job):
        keys=('id','action','status','conversation_id','month','invoice_month','request','started_at','finished_at')
        return {k:job[k] for k in keys if k in job} | {'result':{'request':(job.get('result') or {}).get('request')}}

    @property
    def messages(self):
        return self.library.current()['messages']

    def _busy(self):
        return any(job.get('status') == 'running' for job in (self.job, self.chat_job, self.memory_job))

    def memory_commands(self):
        if self._memory_commands is None:
            intake_module()
            from chat_commands import ChatCommands
            self._memory_commands = ChatCommands(self.root, session_id=self._memory_session)
        return self._memory_commands

    def rule_commands(self):
        intake_module()
        from rule_commands import RuleCommands
        return RuleCommands(self.root, session_id=self._memory_session)

    @property
    def chat_context(self):
        return self.library.current()['context']

    def message(self, role, text, conversation_id=None):
        with self._lock:self.library.append(role,text,conversation_id)

    def manage_chat(self, action, **args):
        with self._lock:
            target=args.get('chat_id',self.library.data['active_id'])
            targets=set(args.get('chat_ids',[])) if action=='delete_many' else {target}
            busy=[j for j in (self.job,self.chat_job,self.memory_job) if j.get('status')=='running']
            if action in ('delete_messages','delete_chat','delete_all','delete_many') and any(
                    action=='delete_all' or j.get('conversation_id',target) in targets for j in busy):
                raise ValueError('이 채팅의 응답·검증이 끝난 뒤 삭제해 주세요.')
            result=self.library.mutate(action,**args)
            if action in ('delete_messages','delete_chat','delete_all','delete_many'):
                if action=='delete_all' or self.chat_job.get('conversation_id') in targets:self.chat_job={'status':'idle'}
            return result

    def snapshot(self):
        with self._lock:
            state = self.monthly_state(self.job['month']) if self.job.get('month') else load(self.root / SESSION, {})
            visible_job = self.chat_job if self.chat_job['status'] != 'idle' else self.job
            if self.memory_job['status'] == 'running':
                visible_job = self.memory_job
            current=self.library.current()
            memory_preview = copy.deepcopy(current['context'].get('memory_preview', {}))
            disposition_preview = copy.deepcopy(current['context'].get('disposition_preview', {}))
            rule_preview = copy.deepcopy(current['context'].get('rule_preview', {}))
            if rule_preview.get('state') == 'preview' and current['context'].get('rule_session') != self._memory_session:
                rule_preview.update(state='version_conflict', nonce=None,
                    message='웹앱이 재시작되어 기준 적용 확인이 만료되었습니다. 다시 계산해 주세요.')
            if disposition_preview.get('state') == 'preview' and current['context'].get('disposition_session') != self._memory_session:
                disposition_preview.update(state='version_conflict', nonce=None,
                    message='웹앱이 재시작되어 처리 확인이 만료되었습니다. 미리보기를 다시 확인해 주세요.')
            if memory_preview.get('state') == 'preview' and current['context'].get('memory_session') != self._memory_session:
                memory_preview.update(state='version_conflict', nonce=None,
                    message='웹앱이 재시작되어 저장 확인이 만료되었습니다. 저장 내용을 다시 확인해 주세요.')
            draft = current['context'].get('command_intake')
            if draft and visible_job['status'] not in ('running','failed'):
                draft_status = conversation_status({'intake':draft})
                if draft_status in ('awaiting_user_decision','draft_ready'):
                    visible_job = dict(visible_job, status=draft_status)
            return {'chat_id':current['id'],'chat_title':current['title'],'library_revision':self.library.data['revision'],
                    'memory_preview':memory_preview,
                    'disposition_preview':disposition_preview, 'disposition_revision':self.disposition_revision(),
                    'rule_preview':rule_preview, 'rule_revision':self.rule_revision(),
                    'chat_selection':copy.deepcopy(current['context'].get('command_selection', {})),
                    'command_intake':copy.deepcopy(current['context'].get('command_intake', {})),
                    'chat_result':dict(current.get('last_result',{})), 'job': dict(visible_job), 'verification_job':dict(self.job), 'messages': list(self.messages),
                    'verification_notification_job':self._notification_job(self.job) if self.job.get('action') in VERIFICATION_ACTIONS else dict(self.last_verification_job),
                    'questions': state.get('questions', []) if state.get('waiting_for_input') else [],
                    'model_calls': state.get('model_calls', 0),
                    'events': state.get('events', [])[-5:],
                    'report_exists': (self.root / REPORT).is_file()}

    def chat(self, request):
        if not request.strip() or len(request)>2000:
            raise ValueError('메시지는 2,000자 이내로 입력해 주세요.')
        with self._lock:
            if self._busy():
                raise ValueError('현재 요청이 끝난 뒤 보내주세요.')
            conversation_id=self.library.data['active_id']
            previous_month=self.chat_context.get('month')
            history=copy.deepcopy(self.messages[-16:])
            selection=copy.deepcopy(self.chat_context.get('command_selection'))
            pending=copy.deepcopy(self.chat_context.get('command_intake'))
            if selection and selection.get('conversation_id') != conversation_id:
                raise ValueError('이 채팅에서 청구건을 다시 선택해 주세요.')
            self.message('user',request)
            # Previous reply actions belong to that reply, not the new question.
            self.library.current()['last_result'] = {}
            self.library.persist()
            self.chat_job={'id':uuid.uuid4().hex,'conversation_id':conversation_id,'action':'chat','status':'running','started_at':time.time()}
            threading.Thread(target=self._run_chat,args=(request,history,conversation_id,previous_month,selection,pending),daemon=True).start()

    def _run_chat(self, request, history, conversation_id, previous_month, selection=None, pending=None):
        import tempfile
        try:
            with tempfile.TemporaryDirectory(prefix='chat_',dir=self.runtime) as folder:
                request_path=Path(folder)/'request.json';response_path=Path(folder)/'response.json'
                save(request_path,{'action':'chat','request':request,'history':history,
                                   'previous_month':previous_month,'selection':selection,'pending':pending})
                completed=subprocess.run([str(self.python),'-X','utf8',str(self.root/'frontend/worker.py'),
                        str(request_path),str(response_path)],cwd=self.root,capture_output=True,timeout=240,
                        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                response=load(response_path,{})
                if completed.returncode or not response.get('ok'):
                    raise RuntimeError(response.get('error','모델 연결에서 응답을 받지 못했습니다.'))
                result=response['result']
            with self._lock:
                self.library.set_result(conversation_id,result)
                self.message('assistant',result['explanation'],conversation_id)
                self.chat_job.update(status=conversation_status(result),result=result,finished_at=time.time())
                # This is the same user request, not a second message. Keep the
                # handoff under the lock so another submit cannot enter between
                # chat completion and the verification worker reservation.
                if not result.get('intake') and result.get('execute_requested') and result.get('proposed_verification_month'):
                    month=result['proposed_verification_month']
                    self.start('agent',request=f'DEMO COMPANY 가상센터 {month} 전체 청구 검증해줘',
                               conversation_id=conversation_id,record_request=False)
        except Exception as exc:
            with self._lock:
                self.chat_job.update(status='failed',error=str(exc),finished_at=time.time())
                self.message('assistant','응답을 받지 못했습니다. 기존 검증 결과는 유지됩니다. 잠시 후 다시 보내주세요.',conversation_id)

    def select_chat_targets(self, issue_ids, expected_result_version=None, expected_target_version=None):
        """Attach a local, version-bound selection to this conversation only."""
        with self._lock:
            if self._busy():
                raise ValueError('현재 요청이 끝난 뒤 청구건을 선택해 주세요.')
            path = self.root / BASE / '검토필요_전체목록.csv'
            observed_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            if expected_result_version is not None and expected_result_version != observed_hash:
                raise ValueError('검증 결과가 변경되었습니다. 확인 목록에서 청구건을 다시 선택해 주세요.')
            chat = self.library.current()
            selection = intake_module().select_targets(self.root, issue_ids, chat['id'])
            if expected_target_version is not None and selection['version']!=expected_target_version:
                raise ValueError('청구 항목이 변경되었습니다. 비교할 항목을 다시 선택해 주세요.')
            if hashlib.sha256(path.read_bytes()).hexdigest() != observed_hash:
                raise ValueError('대상을 선택하는 동안 결과가 바뀌었습니다. 다시 선택해 주세요.')
            lookup = {r['_id']: r for r in self.review_items()}
            selected = [lookup[key] for key in issue_ids if key in lookup]
            if selected:
                from dashboard_view import display_rows, format_number
                first = display_rows(selected[:1])[0]
                label = first.get('_display_target') or first.get('_ui_cost_label') or first['검사']
                value = format_number(first.get('청구값'))
                suffix = f" 외 {len(selected)-1}건" if len(selected)>1 else ''
                selection['label'] = f"{selection.get('month','')} · {label}{suffix} · {value} {first.get('단위','')} · {first.get('_effective_status',first['결과'])}"
            else:
                selection.setdefault('label', f"{selection.get('month', '')} · 가상센터 · 선택 {len(selection.get('targets', []))}건")
            chat['context']['command_selection'] = selection
            chat['context']['month'] = selection['month']
            chat['context'].pop('command_intake', None)
            chat['context'].pop('memory_preview', None)
            chat['context'].pop('disposition_preview', None)
            chat['context'].pop('rule_preview', None)
            chat['last_result'].pop('intake', None)
            self.chat_job = {'status': 'idle'}
            self.library.persist()
            return copy.deepcopy(selection)

    def clear_chat_targets(self):
        with self._lock:
            if self._busy():
                raise ValueError('현재 요청이 끝난 뒤 선택을 해제해 주세요.')
            chat = self.library.current()
            chat['context'].pop('command_selection', None)
            chat['context'].pop('command_intake', None)
            chat['context'].pop('memory_preview', None)
            chat['context'].pop('disposition_preview', None)
            chat['context'].pop('rule_preview', None)
            chat['last_result'].pop('intake', None)
            self.chat_job = {'status': 'idle'}
            self.library.persist()

    def command_choice(self, choice, value=None, pending_id=None):
        """Resolve C1 clarification. This never commits business rules or verdicts."""
        with self._lock:
            if self._busy():
                raise ValueError('현재 요청이 끝난 뒤 답변해 주세요.')
            chat = self.library.current()
            pending = chat['context'].get('command_intake')
            if not pending or not pending_id or pending.get('draft_id') != pending_id:
                raise ValueError('대상이나 질문이 바뀌었습니다. 현재 질문에 다시 답변해 주세요.')
            intake = intake_module().choose(self.root, copy.deepcopy(pending), choice, value)
            result = {'intake':intake, 'explanation':intake['message'], 'conversation_month':intake.get('month'),
                      'llm_used':False, 'model_calls':0, 'verification_executed':False,
                      'context_source':'대상·적용 범위 확인', 'suggestions':[]}
            label = {'scope':{'once':'이번 청구만', 'future':'앞으로 적용'},
                     'application':{'future_only':'앞으로만 적용', 'current_and_future':'이번 청구와 앞으로 모두 적용'}}
            user_text = label.get(choice, {}).get(str(value), {'select_target':'확인할 청구건 선택',
                        'criterion':'지속 적용 조건 확인', 'cancel':'요청 정리 취소'}.get(choice, '요청 내용 확인'))
            self.message('user', user_text, chat['id'])
            self.library.set_result(chat['id'], result)
            self.message('assistant', result['explanation'], chat['id'])
            self.chat_job = {'id':uuid.uuid4().hex, 'conversation_id':chat['id'], 'action':'chat',
                             'status':conversation_status(result), 'result':result, 'finished_at':time.time()}
            return copy.deepcopy(result)

    def prepare_memory_preview(self, pending_id):
        """Hashing runs off the UI event loop; it only prepares a bound preview."""
        with self._lock:
            if self._busy():
                raise ValueError('현재 요청이 끝난 뒤 저장 내용을 확인해 주세요.')
            chat = self.library.current()
            draft = copy.deepcopy(chat['context'].get('command_intake'))
            if not draft or draft.get('draft_id') != pending_id:
                raise ValueError('현재 정리한 요청의 저장 내용을 다시 확인해 주세요.')
            chat_id = chat['id']
            self.memory_job = {'status':'running','action':'memory_prepare','conversation_id':chat_id}
        try:
            preview = self.memory_commands().prepare(draft, conversation_id=chat_id)
            with self._lock:
                chat = self.library.current(chat_id)
                if chat['context'].get('command_intake') != draft:
                    raise ValueError('요청 내용이 바뀌었습니다. 저장 내용을 다시 확인해 주세요.')
                chat['context'].update(memory_preview=preview, memory_session=self._memory_session)
                self.library.persist()
                self.memory_job = {'status':'idle'}
            return copy.deepcopy(preview)
        except Exception as exc:
            with self._lock:
                self.memory_job = {'status':'idle'}
                chat = self.library.current(chat_id)
                preview = dict(draft, state='conflict' if hasattr(exc,'conflicts') else 'version_conflict',
                               message=str(exc), conflicts=getattr(exc,'conflicts', []), nonce=None)
                chat['context']['memory_preview'] = preview
                self.library.persist()
                self.memory_job = {'status':'idle'}
            raise

    def confirm_memory_preview(self, preview_id, preview_hash, nonce):
        """A UI confirmation binds the exact preview, not a model approval field."""
        with self._lock:
            if self._busy():
                raise ValueError('현재 요청이 끝난 뒤 저장해 주세요.')
            chat = self.library.current()
            preview = copy.deepcopy(chat['context'].get('memory_preview') or {})
            if (preview.get('preview_id'), preview.get('preview_hash'), preview.get('nonce')) != (preview_id,preview_hash,nonce):
                raise ValueError('다른 요청의 확인 버튼입니다. 현재 저장 내용을 확인해 주세요.')
            if chat['context'].get('memory_session') != self._memory_session:
                raise ValueError('저장 확인이 만료되었습니다. 저장 내용을 다시 확인해 주세요.')
            draft = copy.deepcopy(chat['context'].get('command_intake'))
            chat_id = chat['id']
            self.memory_job = {'status':'running','action':'memory_confirm','conversation_id':chat_id}
        receipt = None
        try:
            receipt = self.memory_commands().confirm(preview, draft, conversation_id=chat_id,
                preview_id=preview_id, preview_hash=preview_hash, nonce=nonce)
            with self._lock:
                chat = self.library.current(chat_id)
                chat['context']['memory_preview'] = receipt
                chat['context'].pop('command_intake', None)
                chat['last_result'] = {'explanation':receipt['message'],'memory_saved':True,
                    'record_id':receipt['record_id'],'verification_executed':False}
                self.library.persist()
                if not receipt.get('idempotent_replay'):
                    self.message('user','확인한 내용으로 적용 요청 기록 저장',chat_id)
                    self.message('assistant',receipt['message']+' 채팅을 삭제해도 이 기록은 보존됩니다.',chat_id)
                self.chat_job = {'status':'completed','action':'chat','conversation_id':chat_id,
                                 'result':chat['last_result'],'finished_at':time.time()}
                self.memory_job = {'status':'idle'}
            return copy.deepcopy(receipt)
        except Exception as exc:
            with self._lock:
                self.memory_job = {'status':'idle'}
                chat = self.library.current(chat_id)
                if receipt is not None:
                    # The SQLite commit is authoritative even if chat projection fails.
                    receipt['message'] = '적용 요청 기록은 저장되었습니다. 대화 화면 저장에 실패했으므로 저장한 적용 요청 목록에서 확인해 주세요. 검증 결과에는 적용하지 않았습니다.'
                    chat['context']['memory_preview'] = receipt
                    chat['context'].pop('command_intake', None)
                    return copy.deepcopy(receipt)
                chat['context']['memory_preview'] = dict(preview,state='version_conflict',nonce=None,message=str(exc))
                self.library.persist()
                self.memory_job = {'status':'idle'}
            raise

    def cancel_memory_preview(self, preview_id):
        with self._lock:
            if self._busy():
                raise ValueError('현재 요청이 끝난 뒤 취소해 주세요.')
            chat = self.library.current()
            preview = chat['context'].get('memory_preview') or {}
            if preview_id != preview.get('preview_id'):
                raise ValueError('현재 저장 미리보기가 아닙니다.')
            if preview.get('state') in ('conflict','version_conflict','expired'):
                cancelled = dict(preview,state='cancelled',nonce=None,message='저장 안내를 닫았습니다. 새 업무 기록은 저장하지 않았습니다.')
            else:
                cancelled = self.memory_commands().cancel(preview, conversation_id=chat['id'])
            chat['context']['memory_preview'] = cancelled
            self.library.persist()
            return copy.deepcopy(cancelled)

    def saved_memory(self):
        records = self.memory_commands().records()
        latest = {e['record_id']:e for e in self.rule_commands().projection()['events']}
        for record in records:
            event = latest.get(record['record_id'])
            if event:
                record['latest_rule'] = event
                if event['payload'].get('reapproval'):
                    record['targets']=event['payload']['targets']
                record['message'] = ('비교 기준 적용이 중지되어 기본 계산을 표시합니다. 이전 기록은 보존합니다.' if event.get('stopped') else '자료 또는 기능 변경으로 기준 재확인이 필요합니다. 이전 계산은 보존합니다.' if event['invalidated'] else
                    '선택 항목의 새 기준 비교가 적용되었습니다. 기존 검사·결정은 보존합니다.')
        return records

    def rule_revision(self):
        commands = self.rule_commands()
        revision = commands.memory.revision()
        if not commands.store.list_rules():
            return str(revision)
        from rule_commands import code_fingerprint
        try:
            return str(revision)+':'+intake_module()._current(self.root)['version']+':'+code_fingerprint(self.root)
        except (ValueError,OSError,TypeError):
            return str(revision)+':source-unavailable'

    def rule_options(self, record_id):
        return self.rule_commands().options(record_id)

    def rule_history(self,record_id):
        return self.rule_commands().history(record_id)

    def prepare_rule_lifecycle(self,record_id,action,selected_rule_id=None):
        return self.prepare_rule(record_id,None,None,lifecycle_action=action,selected_rule_id=selected_rule_id)

    def rate_catalog(self):
        current=intake_module()._current(self.root)
        rules=self.rule_commands().projection()
        rows=[]
        for i,row in enumerate(current['rows']):
            if row.get('_catalog_kind')!='invoice_rate':continue
            rows.append(dict(row,_id=str(i),**rules['rows'].get(str(i),{})))
        return {'rows':rows,'version':current['version'],'message':current.get('rate_catalog_error') or
            '원본 청구 항목입니다. 정상 항목도 단가 비교 대상으로 선택할 수 있습니다.'}

    def rule_results_csv(self):
        projection=self.rule_commands().projection()
        out=io.StringIO(newline='')
        fields=['청구월','대상','청구위치','기존 비교값','기존 차이','새 비교값','새 차이','단위','새 기준 결과','적용 기준','원본 근거','적용 기록']
        writer=csv.DictWriter(out,fieldnames=fields);writer.writeheader()
        safe=lambda v: ("'"+str(v) if str(v).lstrip().startswith(('=','+','-','@')) else str(v))
        labels={'order_linked':'주문 연결','shipment_linked':'송장 연결','match':'일치','difference':'불일치','not_determinable':'미검증','excluded':'비교 제외'}
        for event in projection['events']:
            payload=event['payload'];targets={t['target_id']:t for t in payload['targets']}
            for c in payload['calculation']['changes']:
                future_only=payload['application']=='future_only'
                valid=not future_only and not event.get('stopped') and not event['invalidated'] and not projection['source_error'] and event['source_fingerprint']==projection['source_fingerprint'];before,after=c['before'],c['after']
                writer.writerow({'청구월':payload['month'],'대상':safe(c['label']),'청구위치':safe(targets[c['target_id']]['locator']),
                    '기존 비교값':before['comparison'],'기존 차이':before['difference'],'새 비교값':after['comparison'] if valid else '',
                    '새 차이':after['difference'] if valid else '', '단위':before['unit'],'새 기준 결과':'기준 중지' if event.get('stopped') else '향후 기준 등록 · 이번 청구 미적용' if future_only and not event['invalidated'] else labels[after['status']] if valid else '기준 재확인',
                    '적용 기준':safe(c['basis']),'원본 근거':safe('; '.join(c['evidence_refs'])),'적용 기록':event['rule_id']})
        return out.getvalue().encode('utf-8-sig')

    def prepare_rule(self, record_id, kind, parameters, *, reapprove=False, lifecycle_action=None,selected_rule_id=None):
        with self._lock:
            if self._busy():
                raise ValueError('실행 중인 요청이 끝난 뒤 계산해 주세요.')
            chat_id = self.library.data['active_id']
            self.memory_job = {'status':'running','action':'rule_prepare','conversation_id':chat_id}
        try:
            if lifecycle_action:
                preview=self.rule_commands().prepare_lifecycle(record_id,lifecycle_action,selected_rule_id,conversation_id=chat_id)
            else:
                method=self.rule_commands().prepare_reapproval if reapprove else self.rule_commands().prepare
                preview = method(record_id,kind,parameters,conversation_id=chat_id)
            with self._lock:
                chat = self.library.current(chat_id)
                chat['context'].update(rule_preview=preview,rule_session=self._memory_session)
                self.library.persist()
            return copy.deepcopy(preview)
        finally:
            with self._lock:
                self.memory_job = {'status':'idle'}

    def confirm_rule(self, preview_id, preview_hash, nonce):
        with self._lock:
            if self._busy():
                raise ValueError('실행 중인 요청이 끝난 뒤 적용해 주세요.')
            chat = self.library.current(); chat_id = chat['id']
            preview = copy.deepcopy(chat['context'].get('rule_preview') or {})
            if (preview.get('preview_id'),preview.get('preview_hash'),preview.get('nonce')) != (preview_id,preview_hash,nonce):
                raise ValueError('현재 대화의 기준 적용 미리보기를 확인해 주세요.')
            if chat['context'].get('rule_session') != self._memory_session:
                raise ValueError('적용 확인이 만료되었습니다. 다시 계산해 주세요.')
            self.memory_job = {'status':'running','action':'rule_confirm','conversation_id':chat_id}
        receipt = None
        try:
            receipt = self.rule_commands().confirm(preview,conversation_id=chat_id,
                preview_id=preview_id,preview_hash=preview_hash,nonce=nonce)
            with self._lock:
                chat = self.library.current(chat_id)
                chat['context']['rule_preview'] = receipt
                completed = receipt.get('state') == 'applied' and not receipt.get('invalidated')
                applied = completed and not receipt.get('stopped') and receipt.get('application')!='future_only'
                chat['last_result'] = {'explanation':receipt['message'],'calculation_executed':True,'rule_applied':applied,'rule_stopped':completed and bool(receipt.get('stopped'))}
                self.library.persist()
                if not receipt.get('idempotent_replay'):
                    self.message('assistant',receipt['message'],chat_id)
                self.chat_job = {'status':'completed' if completed else 'awaiting_user_decision','action':'rule','conversation_id':chat_id}
            return copy.deepcopy(receipt)
        except Exception as exc:
            with self._lock:
                chat = self.library.current(chat_id)
                if receipt is not None:
                    receipt = dict(receipt,message='새 기준 비교는 저장됐으나 채팅 표시 저장에 실패했습니다. 저장 목록에서 확인할 수 있습니다.')
                    chat['context']['rule_preview'] = receipt
                    return copy.deepcopy(receipt)
                chat['context']['rule_preview'] = dict(preview,state='version_conflict',nonce=None,message=str(exc))
                self.library.persist()
            raise
        finally:
            with self._lock:
                self.memory_job = {'status':'idle'}

    def cancel_rule(self, preview_id):
        with self._lock:
            if self._busy():
                raise ValueError('실행 중인 요청이 끝난 뒤 처리해 주세요.')
            chat = self.library.current(); preview = chat['context'].get('rule_preview') or {}
            if preview.get('preview_id') != preview_id:
                raise ValueError('현재 대화의 기준 적용 미리보기를 확인해 주세요.')
            if preview.get('state') in ('version_conflict','expired','conflict'):
                cancelled = dict(preview,state='cancelled',nonce=None,message='적용 안내를 닫았습니다.')
            else:
                cancelled = self.rule_commands().cancel(preview,conversation_id=chat['id'])
            chat['context']['rule_preview'] = cancelled
            self.library.persist()
            return copy.deepcopy(cancelled)

    def disposition_revision(self):
        commands = self.memory_commands()
        revision = commands.store.revision()
        from checklist_review import events
        if not commands.store.list_dispositions() and not events(self.root):
            return str(revision)
        # Three UI timers share this polling hint. Coalesce their file scans;
        # actual list reads, downloads and writes still verify current bytes.
        now=time.monotonic()
        cached=getattr(self,'_review_poll_cache',None)
        if cached and cached[0]==revision and now-cached[1]<2:
            return cached[2]
        # Cheap polling trigger, not a full original-byte verification. Actual
        # view loads, confirmations and downloads rehash all evidence bytes.
        try:
            current = intake_module()._current(self.root)
            value=str(revision) + ':' + str(current['version'])
        except (OSError, ValueError):
            value=str(revision) + ':source-unavailable'
        self._review_poll_cache=(revision,now,value)
        return value

    def prepare_disposition(self, record_id, status):
        with self._lock:
            if self._busy():
                raise ValueError('실행 중인 요청이 끝난 뒤 처리해 주세요.')
            chat_id = self.library.data['active_id']
            self.memory_job = {'status':'running','action':'disposition_prepare','conversation_id':chat_id}
        try:
            preview = self.memory_commands().prepare_disposition(record_id, status, conversation_id=chat_id)
            with self._lock:
                chat = self.library.current(chat_id)
                chat['context'].update(disposition_preview=preview, disposition_session=self._memory_session)
                self.library.persist()
            return copy.deepcopy(preview)
        finally:
            with self._lock:
                self.memory_job = {'status':'idle'}

    def confirm_disposition(self, preview_id, preview_hash, nonce):
        with self._lock:
            if self._busy():
                raise ValueError('실행 중인 요청이 끝난 뒤 처리해 주세요.')
            chat = self.library.current(); chat_id = chat['id']
            preview = copy.deepcopy(chat['context'].get('disposition_preview') or {})
            if (preview.get('preview_id'), preview.get('preview_hash'), preview.get('nonce')) != (preview_id, preview_hash, nonce):
                raise ValueError('현재 대화의 처리 미리보기를 다시 확인해 주세요.')
            if chat['context'].get('disposition_session') != self._memory_session:
                raise ValueError('처리 확인이 만료되었습니다. 미리보기를 다시 확인해 주세요.')
            self.memory_job = {'status':'running','action':'disposition_confirm','conversation_id':chat_id}
        receipt = None
        try:
            receipt = self.memory_commands().confirm_disposition(preview, conversation_id=chat_id,
                preview_id=preview_id, preview_hash=preview_hash, nonce=nonce)
            with self._lock:
                chat = self.library.current(chat_id)
                chat['context']['disposition_preview'] = receipt
                chat['last_result'] = {'explanation':receipt['message'], 'disposition_applied':True,
                                      'calculation_executed':False}
                self.library.persist()
                if not receipt.get('idempotent_replay'):
                    self.message('assistant', receipt['message'], chat_id)
                self.chat_job = {'status':'completed','action':'disposition','conversation_id':chat_id}
            return copy.deepcopy(receipt)
        except Exception as exc:
            with self._lock:
                chat = self.library.current(chat_id)
                if receipt is not None:
                    receipt = dict(receipt, message='담당자 처리는 저장됐으나 채팅 표시 저장에 실패했습니다. 저장 목록에서 확인할 수 있습니다.')
                    chat['context']['disposition_preview'] = receipt
                    return copy.deepcopy(receipt)
                chat['context']['disposition_preview'] = dict(preview,state='version_conflict',nonce=None,message=str(exc))
                self.library.persist()
            raise
        finally:
            with self._lock:
                self.memory_job = {'status':'idle'}

    def cancel_disposition(self, preview_id):
        with self._lock:
            if self._busy():
                raise ValueError('실행 중인 요청이 끝난 뒤 처리해 주세요.')
            chat = self.library.current()
            preview = chat['context'].get('disposition_preview') or {}
            if preview.get('preview_id') != preview_id:
                raise ValueError('현재 대화의 처리 미리보기를 확인해 주세요.')
            if preview.get('state') in ('version_conflict','expired','conflict'):
                cancelled = dict(preview,state='cancelled',nonce=None,message='처리 안내를 닫았습니다.')
            else:
                cancelled = self.memory_commands().cancel_disposition(preview, conversation_id=chat['id'])
            chat['context']['disposition_preview'] = cancelled
            self.library.persist()
            return copy.deepcopy(cancelled)

    def disposition_summary(self):
        projection = self.memory_commands().disposition_projection()
        rows = self.review_items()
        counts = dict.fromkeys(('unreviewed','reviewed','exception_accepted','review_required'), 0)
        for row in rows:
            status=row.get('_review_status','unreviewed')
            if row.get('_saved_normal') and status=='unreviewed':continue
            counts[status] += 1
        return {'counts':counts,'unmapped_targets':projection['unmapped_targets'], 'reason':projection['source_error']}

    def review_csv(self):
        from review_catalog import display_group
        rows = self.review_items()
        fields = list(rows[0]) if rows else []
        fields = [k for k in fields if not k.startswith('_')]
        extra = ['화면 구분','화면 상태','정상 표시 근거','담당자 처리','처리 사유','처리 기록','새 기준 결과','새 기준 비교값','새 기준 차이','새 기준 근거','기준 적용 기록']
        labels = {'unreviewed':'미처리','reviewed':'검토완료','exception_accepted':'예외인정','review_required':'미처리'}
        out = io.StringIO(newline='')
        writer = csv.DictWriter(out, fieldnames=fields+extra); writer.writeheader()
        # Escape only new text fields. Original numeric/CSV cells retain bytes' values.
        safe_text = lambda v: ("'" + str(v) if str(v).lstrip().startswith(('=','+','-','@')) else str(v))
        for row in rows:
            value = {k:row.get(k,'') for k in fields}
            value.update({'담당자 처리':labels[row.get('_review_status','unreviewed')],
                          '처리 사유':safe_text(row.get('_review_reason','')), '처리 기록':row.get('_review_event',''),
                          '화면 구분':display_group(row), '화면 상태':row['_effective_status'],'정상 표시 근거':safe_text(row.get('_normal_basis',''))})
            rule_labels = {'order_linked':'주문 연결','shipment_linked':'송장 연결','match':'일치','difference':'불일치','not_determinable':'미검증','excluded':'비교 제외','review_required':'기준 재확인'}
            value.update({'새 기준 결과':rule_labels.get(row.get('_rule_status'),''),
                '새 기준 비교값':row.get('_rule_comparison',''), '새 기준 차이':row.get('_rule_difference',''),
                '새 기준 근거':safe_text(row.get('_rule_basis','')), '기준 적용 기록':row.get('_rule_event','')})
            writer.writerow(value)
        return out.getvalue().encode('utf-8-sig')

    def review_report(self, text):
        intake_module()
        from financial_overview import load_overview
        from financial_summary import replace_summary
        workflow=load(self.root/BASE/'workflow_result.json', {})
        required_summary={'full_invoice_result.json','cost_verification.json','supporting_evidence_result.json'}
        if required_summary <= set(workflow.get('result_hashes', {})):
            text = replace_summary(text, load_overview(self.root, self.review_items()))
        text = self.rule_report(text)
        from checklist_review import events
        if events(self.root):
            from review_catalog import summary
            rows=self.review_items()
            lines=['', '', '### 담당자 처리 반영 현황', '',
                   '대시보드는 담당자 처리완료를 정상에 포함합니다. 위 원본 보고서의 계산 판정·차이는 보존하며, 아래는 현재 처리 상태를 반영한 집계입니다.', '',
                   '| 항목 | 정상 | 불일치 | 미검증 | 정상 중 담당자 처리 |', '|---|---:|---:|---:|---:|']
            for r in summary(rows):
                lines.append(f"| {r['label']} | {r['정상']:,} | {r['불일치']:,} | {r['미검증']:,} | {r['manual_normal']:,} |")
            lines.extend(['','각 대상의 원래 계산값·현재 상태·처리 기록은 확인 목록과 처리 상태 포함 CSV에서 조회합니다.'])
            return text+'\n'.join(lines)+'\n'
        projection = self.memory_commands().disposition_projection()
        if not projection['events']:
            return text
        import html
        clean = lambda v: html.escape(str(v)).replace('|','&#124;').replace('\r',' ').replace('\n',' ')
        labels = {'unreviewed':'미처리','reviewed':'검토완료','exception_accepted':'예외인정','review_required':'미처리'}
        lines = ['', '', '### 담당자 처리 현황', '',
            '사용자가 확인한 처리 상태입니다. 원래 수량·금액·불일치 판정은 유지하며, 재계산 또는 지급 승인을 뜻하지 않습니다.', '',
            '| 확인 대상 | 담당자 처리 | 사유 |', '|---|---|---|']
        for event in [e for e in projection['events'] if not e.get('superseded')][:20]:
            payload=event['payload']
            status='review_required' if event.get('invalidated') or projection['source_error'] else event['disposition']
            targets=payload['targets'];target=targets[0]
            label=f"{target['label']} · {target['locator']}"+(f" 외 {len(targets)-1}개 항목" if len(targets)>1 else '')
            lines.append(f"| {clean(label)} | {labels[status]} | {clean(payload['reason'])} |")
        if projection['unmapped_targets']:
            lines.extend(['',f"현재 목록에서 다시 선택해야 할 이전 대상 {projection['unmapped_targets']}개가 있습니다. 이전 처리를 새 청구에 자동 적용하지 않았습니다."])
        lines.extend(['','전체 대상과 현재 처리 상태는 웹의 ‘처리 상태 포함 CSV’와 ‘예외·비교 기준 관리’에서 조회할 수 있습니다.'])
        return text+'\n'.join(lines)+'\n'

    def rule_report(self, text):
        projection = self.rule_commands().projection()
        if not any(e['payload']['application']!='future_only' for e in projection['events']):
            return text
        import html
        clean = lambda v: html.escape(str(v)).replace('|','&#124;').replace('\r',' ').replace('\n',' ')
        labels = {'order_linked':'주문 연결','shipment_linked':'송장 연결','match':'일치','difference':'불일치','not_determinable':'미검증','excluded':'비교 제외'}
        lines = ['', '', '### 선택 항목의 새 기준 비교', '',
            '별도 확인한 기준으로 다시 계산한 결과입니다. 위의 최초 검사값·전체 요약은 유지하며, 다른 수량·금액 검사나 지급 승인을 대신하지 않습니다.', '',
            '| 대상 | 기존 차이 | 새 차이 | 결과 | 적용 기준 |', '|---|---:|---:|---|---|']
        count = 0
        for event in projection['events']:
            if event['payload']['application']=='future_only':
                continue
            for change in event['payload']['calculation']['changes']:
                count += 1
                if count > 20:
                    continue
                valid = not event.get('stopped') and not event['invalidated'] and not projection['source_error'] and event['source_fingerprint']==projection['source_fingerprint']
                after = change['after']; before = change['before']
                state = '기준 중지' if event.get('stopped') else labels[after['status']] if valid else '기준 재확인'
                value = f"{after['difference']} {after['unit']}" if valid else '적용 해제'
                lines.append(f"| {clean(change.get('label',change['check']))} | {clean(str(before['difference'])+' '+before['unit'])} | {clean(value)} | {state} | {clean(change['basis'])} |")
        if count>20:
            lines.append(f'\n전체 {count}개 항목은 웹 확인 목록과 처리 상태 포함 CSV에서 조회할 수 있습니다.')
        if projection['unmapped_targets']:
            lines.append(f"\n현재 목록에 연결되지 않는 이전 대상 {projection['unmapped_targets']}개는 저장 기록에 보존합니다.")
        return text+'\n'.join(lines)+'\n'

    def start(self, action, request='', answers=None, registration=None, conversation_id=None, *, record_request=True):
        conversation_id=conversation_id or self.library.data['active_id']
        if action=='agent':
            if answers is not None and self.job.get('month'):
                return self.start_monthly('monthly_agent',self.job['month'],request=request,answers=answers,conversation_id=conversation_id,record_request=record_request)
            match=re.search(r'(20\d{2})\s*(?:년\s*|[-./])(1[0-2]|0?[1-9])(?:월|\b)',request)
            if match:
                month=f'{match[1]}-{int(match[2]):02}'
                if month!='2029-07':return self.start_monthly('monthly_agent',month,request=request,conversation_id=conversation_id,record_request=record_request)
        with self._lock:
            if self._busy():
                raise ValueError('이미 실행 중입니다. 완료 후 다음 요청을 보내 주세요.')
            self.chat_job={'status':'idle'}
            payload = {'action': action, 'conversation_id':conversation_id}
            if action == 'agent':
                if not request.strip() or len(request) > 2000:
                    raise ValueError('검증 요청을 2,000자 이내로 입력해 주세요.')
                if answers is not None:
                    previous = load(self.root / SESSION, {})
                    if previous.get('processing_status') != 'awaiting_user_decision':
                        raise ValueError('현재 답변을 기다리는 실행이 없습니다.')
                    ids = {q['id'] for q in previous['questions']}
                    if set(answers) != ids or not all(isinstance(v, str) and v.strip() for v in answers.values()):
                        raise ValueError('대기 중인 각 질문에 답변을 입력해 주세요.')
                    request = previous['request']
                    payload.update(answers=answers, resume=True)
                elif load(self.root / SESSION, {}).get('waiting_for_input'):
                    raise ValueError('먼저 아래 질문에 답변하고 재개해 주세요. 이전 질문을 건너뛰지 않습니다.')
                payload['request'] = request
            elif action == 'evidence':
                if load(self.root / SESSION, {}).get('waiting_for_input'):
                    raise ValueError('대기 질문에 답변한 뒤 증빙을 등록해 주세요.')
                payload['registration'] = registration
            elif action not in ('read', 'investigate'):
                raise ValueError('지원하지 않는 동작입니다.')
            self.job = {'id': uuid.uuid4().hex, 'conversation_id':conversation_id, 'action': action, 'status': 'running',
                        'started_at': time.time(),'request':request,'invoice_month':'2029-07'}
            self._save_job()
            if record_request:
                self.message('user', request if action == 'agent' else {'read': '현재 검증 결과 보여줘',
                             'investigate': '불일치 원인과 근거 보여줘', 'evidence': '추가 증빙 등록 및 재검증'}[action],conversation_id)
            if answers:
                self.message('user', '\n'.join(f'{k}: {v}' for k, v in answers.items()),conversation_id)
            threading.Thread(target=self._run, args=(payload,), daemon=True).start()

    def monthly_folder(self, month):
        if not re.fullmatch(r'20\d{2}-(0[1-9]|1[0-2])',str(month)) or month=='2029-07':
            raise ValueError('신규 청구월을 YYYY-MM으로 선택하세요. 2029-07은 기존 화면을 사용합니다.')
        return self.root/'data/demo_company/monthly'/month

    def monthly_state(self, month):
        return load(self.monthly_folder(month)/'state.json',{})

    def monthly_cases(self):
        folder=self.root/'data/demo_company/monthly'
        return [load(p) for p in sorted(folder.glob('*/case.json'))] if folder.exists() else []

    def evidence_months(self):
        months = {'2029-07'} if (self.root / POLICY).is_file() else set()
        months.update(c['month'] for c in self.monthly_cases()
                      if c and re.fullmatch(r'20\d{2}-(0[1-9]|1[0-2])', c.get('month', '')))
        return [{'month': m, 'label': m + ' · 가상센터'} for m in sorted(months)]

    def evidence_scope(self, month):
        if month == '2029-07':
            policy = load(self.root / POLICY)
            if not policy:
                raise ValueError('7월 청구 기준이 등록되어 있지 않습니다.')
            scope = {'month': month, 'period': policy['period'], 'currency': 'KRW',
                     'claim_sha256': policy['claim_sha256'], 'targets': self.claim_items(),
                     'policy_hash': hashlib.sha256((self.root / POLICY).read_bytes()).hexdigest()}
        else:
            intake_module()
            from monthly_evidence import targets
            scope = targets(self.root, month)
        scope['version_token'] = hashlib.sha256(json.dumps(scope, ensure_ascii=False,
            sort_keys=True, default=str).encode('utf-8')).hexdigest()
        return scope

    def start_evidence(self, registration, *, month):
        if month == '2029-07':
            if registration.get('invoice_month') != month:
                raise ValueError('증빙과 선택한 청구월이 다릅니다.')
            return self.start('evidence', registration=registration)
        return self.start_monthly('monthly_evidence', month, registration=registration,
                                  request=f'{month} 추가 증빙 등록 및 재검증')

    def monthly_result(self, month, pending=False):
        folder=self.monthly_folder(month)
        pointer=load(folder/('pending.json' if pending else 'latest.json'),{})
        if not pointer:return None
        for relative,h in pointer['hashes'].items():
            p=safe_file(self.root,relative)
            if not p.is_relative_to(folder) or hashlib.sha256(p.read_bytes()).hexdigest()!=h:
                raise ValueError('월별 결과 근거 파일이 변경되었습니다.')
        return load(safe_file(self.root,pointer['result']))

    def start_monthly(self, action, month, *, request='', answers=None, registration=None, conversation_id=None, record_request=True):
        conversation_id=conversation_id or self.library.data['active_id']
        with self._lock:
            if self._busy():raise ValueError('실행 중인 요청이 끝난 뒤 진행해 주세요.')
            self.chat_job={'status':'idle'}
            folder=self.monthly_folder(month)
            if action not in ('monthly_register','monthly_verify','monthly_agent','monthly_evidence'):raise ValueError('지원하지 않는 월별 동작')
            if action!='monthly_register' and not (folder/'case.json').is_file():
                raise ValueError(f'{month} 자료가 없습니다. 월별 검증 탭에서 먼저 등록해 주세요.')
            pending=self.monthly_state(month)
            if action not in ('monthly_register','monthly_evidence') and pending.get('waiting_for_input') and answers is None:
                raise ValueError('월별 검증 탭에서 대기 질문에 답하거나 등록 기준을 수정해 주세요.')
            payload={'action':action,'conversation_id':conversation_id,'month':month,'request':request,'answers':answers}
            if registration is not None:
                if registration.get('month')!=month:raise ValueError('등록 월이 다릅니다.')
                payload['registration']=registration
            self.job={'id':uuid.uuid4().hex,'conversation_id':conversation_id,'action':action,'month':month,'status':'running','started_at':time.time()}
            self._save_job()
            if record_request:
                self.message('user',request or f'{month} '+('자료 등록' if action=='monthly_register' else '청구·WMS 검증'),conversation_id)
            threading.Thread(target=self._run,args=(payload,),daemon=True).start()

    def upload_monthly(self, filename, data):
        return self._upload_bytes(filename, data, monthly=True)

    def _run(self, payload):
        request_path = self.runtime / 'request.json'
        response_path = self.runtime / 'response.json'
        try:
            save(request_path, payload)
            response_path.unlink(missing_ok=True)
            env = dict(os.environ, PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
            with (self.runtime / 'worker.log').open('w', encoding='utf-8') as log:
                process = subprocess.Popen([str(self.python), '-X', 'utf8', str(self.root / 'frontend/worker.py'),
                                            str(request_path), str(response_path)], cwd=self.root,
                                           env=env, stdout=log, stderr=log,
                                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                with self._lock:
                    self.job['worker_pid'] = process.pid
                    self._save_job()
                process.wait()  # UI remains responsive; engine controls model timeouts.
            response = load(response_path, {})
            if process.returncode or not response.get('ok'):
                raise RuntimeError(response.get('error', '실행 도구가 결과를 반환하지 않았습니다. worker.log를 확인하세요.'))
            result = response['result']
            status = 'awaiting_user_decision' if result.get('waiting_for_input') else 'completed'
            self.message('assistant', ('기준을 정해야 하는 항목이 있습니다. 아래 질문에 답변해 주세요.' if status == 'awaiting_user_decision'
                         else '추가 증빙을 등록하고 재대사했습니다. 추가 증빙 화면에서 차이를 확인하세요.' if payload['action'] in ('evidence', 'monthly_evidence')
                         else result.get('explanation') or '저장된 검증 결과와 원본 근거를 확인했습니다. 결과 탭에서 필요한 항목을 조회하세요.'),payload.get('conversation_id'))
            with self._lock:
                self.job.update(status=status, result=result, finished_at=time.time())
                self._save_job()
        except Exception as exc:
            with self._lock:
                self.job.update(status='failed', error=str(exc), finished_at=time.time())
                self._save_job()
            self.message('assistant', '실행하지 못했습니다: ' + str(exc) + '\n마지막 완료 보고서는 계속 조회할 수 있습니다.',payload.get('conversation_id'))

    def preview(self, issue_id, field, group_index=0, page=1):
        import tempfile
        with tempfile.TemporaryDirectory(prefix='preview_',dir=self.runtime) as folder:
            source=Path(folder)/'request.json';target=Path(folder)/'response.json'
            save(source,{'action':'preview','selection':{'issue_id':issue_id,'field':field,'group_index':group_index,'page':page}})
            result=subprocess.run([str(self.python),'-X','utf8',str(self.root/'frontend/worker.py'),str(source),str(target)],
                                  capture_output=True,timeout=180,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            response=load(target,{})
            if result.returncode or not response.get('ok'):raise ValueError(response.get('error','원본 근거를 읽지 못했습니다.'))
            return response['result']

    def claim_items(self):
        return load(self.root / BASE / 'full_invoice_result.json', {}).get('claim_items', [])

    def _issue_rows(self):
        path = self.root / BASE / '검토필요_전체목록.csv'
        if not path.exists():
            return []
        raw = path.read_bytes()
        version = hashlib.sha256(raw).hexdigest()
        with io.StringIO(raw.decode('utf-8-sig'), newline='') as stream:
            return [dict(row, _id=str(i), _result_version=version) for i, row in enumerate(csv.DictReader(stream))]

    def review_items(self):
        intake_module()
        from review_catalog import additional_rows,annotate,label_cost_rows
        from checklist_review import project
        rows=self.issues()+additional_rows(self.root)
        return label_cost_rows(self.root,[annotate(r) for r in project(self.root,rows)])

    def review_token(self, rows=None):
        from review_catalog import signature
        rows=self.review_items() if rows is None else rows
        return hashlib.sha256(json.dumps([(r['_id'],signature(r),r.get('_review_status')) for r in rows],ensure_ascii=False).encode()).hexdigest()

    def complete_checklist(self, ids, status, expected_token):
        """The explicit list button is authorization; no model or chat ceremony."""
        with self._lock:
            if self._busy():raise ValueError('현재 실행이 끝난 뒤 처리해 주세요.')
            self.memory_job={'status':'running','action':'checklist','conversation_id':self.library.data['active_id']}
        try:
            intake_module()
            from chat_commands import source_snapshot
            from business_memory import digest
            from checklist_review import save
            from review_catalog import signature,identity
            rows=self.review_items()
            if expected_token!=self.review_token(rows):raise ValueError('목록 또는 처리 상태가 변경되었습니다. 다시 선택해 주세요.')
            if not isinstance(ids,list) or len(ids)!=len(set(ids)):raise ValueError('중복 없이 처리 대상을 선택하세요.')
            lookup={r['_id']:r for r in rows}
            if not ids or any(i not in lookup for i in ids):raise ValueError('현재 목록에서 항목을 선택하세요.')
            revision=self.memory_commands().store.revision()
            sources=source_snapshot(self.root)
            if self.review_token()!=expected_token:raise ValueError('확인 중 결과가 바뀌었습니다. 다시 선택하세요.')
            if digest(source_snapshot(self.root))!=digest(sources):raise ValueError('확인 중 원본 자료가 변경되었습니다.')
            targets=[dict(id=i,identity=identity(lookup[i]),signature=signature(lookup[i])) for i in ids]
            return save(self.root,targets,status,digest(sources),revision)
        finally:
            with self._lock:self.memory_job={'status':'idle'}

    def issues(self):
        rows = self._issue_rows()
        projection = self.memory_commands().disposition_projection()
        rules = self.rule_commands().projection()
        for row in rows:
            row.update(projection['rows'].get(row['_id'], {'_review_status':'unreviewed','_review_reason':'','_review_event':''}))
            row.update(rules['rows'].get(row['_id'], {'_rule_status':'','_rule_comparison':'','_rule_difference':'','_rule_event':''}))
        return rows

    def upload(self, filename, data):
        return self._upload_bytes(filename, data)

    def _upload_path(self, filename, *, monthly=False):
        suffix = Path(filename).suffix.lower()
        allowed = ('.xlsb', '.xlsx', '.xlsm', '.csv', '.pdf') if monthly else ('.csv', '.xlsx', '.xlsb', '.pdf', '.png', '.jpg', '.jpeg')
        if suffix not in allowed:
            raise ValueError('XLSB/XLSX/CSV/PDF 파일을 선택해 주세요.' if monthly else 'CSV/XLSX/XLSB/PDF/이미지 파일을 선택해 주세요.')
        name = re.sub(r'[^\w.가-힣()-]', '_', Path(filename).name)[:110 if monthly else 100]
        folder = self.runtime / 'uploads'
        folder.mkdir(exist_ok=True)
        return folder / (uuid.uuid4().hex + '_' + name)

    def _upload_bytes(self, filename, data, *, monthly=False):
        if not data:
            raise ValueError('빈 파일은 등록할 수 없습니다.')
        path = self._upload_path(filename, monthly=monthly)
        temporary = path.with_name(path.name + '.part')
        try:
            temporary.write_bytes(data)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        return path.name

    async def upload_file(self, file, *, monthly=False):
        """Save NiceGUI uploads in chunks without a whole-file memory copy or size cap."""
        if not file.size():
            raise ValueError('빈 파일은 등록할 수 없습니다.')
        path = self._upload_path(file.name, monthly=monthly)
        temporary = path.with_name(path.name + '.part')
        try:
            await file.save(temporary)
            if temporary.stat().st_size != file.size():
                raise ValueError('파일 업로드가 완료되지 않았습니다. 다시 올려 주세요.')
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        return path.name

    def discard_upload(self, token):
        """Delete only the named staging upload; registered evidence/originals stay intact."""
        with self._lock:
            if self._busy():
                raise ValueError('실행이 끝난 뒤 업로드 파일을 삭제해 주세요.')
            if not isinstance(token, str) or not re.fullmatch(r'[0-9a-f]{32}_[\w.가-힣()-]+', token):
                raise ValueError('잘못된 업로드 파일입니다.')
            folder = self.runtime / 'uploads'
            path = folder / token
            boundary = self.root / 'frontend/.runtime/uploads'
            if path.resolve().parent != boundary:
                raise ValueError('임시 업로드 폴더의 파일만 삭제할 수 있습니다.')
            for parent in (path, *path.parents):
                if parent == self.root:
                    break
                if parent.is_symlink() or parent.is_junction():
                    raise ValueError('외부 경로 연결은 허용하지 않습니다.')
            path.unlink(missing_ok=True)

    def build_registration(self, token, row, axis, unit, selector, confirmed, replace=False, *, month='2029-07', version_token=None):
        """Explicit evidence mapping; never infer a price/quantity or overwrite inputs."""
        if not confirmed:
            raise ValueError('기간·단위·전체 범위·공급가액 기준을 확인해 주세요.')
        if self._busy() or (month == '2029-07' and load(self.root / SESSION, {}).get('waiting_for_input')):
            raise ValueError('현재 실행과 대기 질문을 처리한 뒤 등록해 주세요.')
        if month != '2029-07' or version_token is not None:
            scope = self.evidence_scope(month)
            if not version_token or scope['version_token'] != version_token:
                raise ValueError('청구 또는 증빙 버전이 바뀌었습니다. 청구월을 다시 선택해 주세요.')
        if month != '2029-07':
            if not isinstance(token, str) or Path(token).name != token:
                raise ValueError('잘못된 업로드 파일입니다.')
            if axis not in ('work', 'contract', 'actual_cost') or type(row) not in (int,str):
                raise ValueError('지원하는 청구 항목과 비교 종류를 선택해 주세요.')
            item = next((i for i in scope['targets'] if i.get('target_key',i['invoice_row']) == row), None)
            if item is None or unit != ('KRW' if axis == 'actual_cost' else item['unit']):
                raise ValueError('청구 항목 또는 증빙 단위가 다릅니다.')
            return {**{k: scope[k] for k in ('month', 'revision', 'case_identity', 'claim_sha256', 'period', 'currency')},
                    'amount_basis': 'supply_amount', 'expected_registry_revision': scope['registry_revision'],
                    'replace': bool(replace), 'entries': [{'invoice_row': item['invoice_row'], 'axis': axis, 'unit': unit,
                    **({'claim_path':item['claim_path']} if isinstance(row,str) else {}),
                    'scope_confirmed': True, 'coverage_complete': True, 'allocation_confirmed': True,
                    'sources': [{'token': token, 'selector': copy.deepcopy(selector)}]}]}
        if Path(token).name != token:
            raise ValueError('잘못된 업로드 파일입니다.')
        source = self.runtime / 'uploads' / token
        if not source.is_file() or source.is_symlink():
            raise ValueError('업로드 파일을 찾을 수 없습니다.')
        policy = load(self.root / POLICY)
        if any(e['invoice_row'] == row and e['axis'] == axis for e in policy['entries']) and not replace:
            raise ValueError('기존 증빙이 있습니다. 교체 확인을 선택해야 변경됩니다.')
        if axis not in ('work', 'contract', 'actual_cost') or row not in {i['row'] for i in self.claim_items()}:
            raise ValueError('지원하는 청구 항목과 비교 종류를 선택해 주세요.')
        item = next(i for i in self.claim_items() if i['row'] == row)
        if unit != ('KRW' if axis == 'actual_cost' else str(item.get('unit') or '')):
            raise ValueError('청구서 단위와 증빙 단위가 다릅니다. 단위를 확인해 주세요.')
        kind = selector.get('format')
        if kind == 'csv' and (source.suffix.lower() != '.csv' or not selector.get('column') or type(selector.get('row')) is not int or selector['row'] < 2):
            raise ValueError('CSV의 행 번호와 열 제목을 입력해 주세요.')
        if kind in ('xlsx', 'xlsb') and (source.suffix.lower() != '.' + kind or not selector.get('sheet') or not re.fullmatch('[A-Z]+[1-9][0-9]*', selector.get('cell', ''))):
            raise ValueError('엑셀 파일 형식에 맞는 시트와 셀을 입력해 주세요.')
        if kind == 'reviewed_transcription':
            from decimal import Decimal, InvalidOperation
            try:
                valid = Decimal(str(selector.get('value', '')).replace(',', '')).is_finite()
            except InvalidOperation:
                valid = False
            if not valid or selector.get('confirmed') is not True or not selector.get('reference'):
                raise ValueError('원문에서 확인한 숫자·위치와 확인 표시가 필요합니다.')
        if kind not in ('csv', 'xlsx', 'xlsb', 'reviewed_transcription'):
            raise ValueError('지원하지 않는 숫자 읽기 방식입니다.')
        dest = self.root / 'data/demo_company/source/추가증빙' / token
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.resolve().is_relative_to(self.root / 'data/demo_company/source'):
            raise ValueError('증빙 보관 경로가 회사 원본 폴더 밖입니다.')
        for parent in dest.parents:
            if parent == self.root:
                break
            if parent.is_symlink() or (hasattr(parent, 'is_junction') and parent.is_junction()):
                raise ValueError('증빙 보관 경로의 외부 연결은 허용하지 않습니다.')
        # Exclusive create: even identical filenames cannot replace originals.
        with dest.open('xb') as out:
            out.write(source.read_bytes())
        digest = hashlib.sha256(dest.read_bytes()).hexdigest().upper()
        registration = {k: policy[k] for k in ('company_id', 'warehouse_id', 'invoice_month', 'claim_sha256', 'period')}
        registration['entries'] = [{'invoice_row': row, 'axis': axis, 'scope_confirmed': True,
            'coverage_complete': True, 'currency': 'KRW', 'amount_basis': 'supply_amount',
            'unit': unit, 'period': policy['period'], 'sources': [{'path': dest.relative_to(self.root).as_posix(),
            'sha256': digest, 'selector': selector}]}]
        return registration
