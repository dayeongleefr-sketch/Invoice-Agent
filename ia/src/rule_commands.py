"""Source-bound C4 calculations; approved results are separate from the baseline."""
import hashlib
import copy
from pathlib import Path
import uuid

import chat_intake
from chat_commands import digest, source_snapshot
from business_memory import BusinessMemory
from rule_memory import RuleMemory
import rule_adapters
from recurring_rules import KINDS as RECURRING_KINDS


def code_fingerprint(root):
    files = ('rule_commands.py', 'rule_memory.py', 'rule_reapproval.py', 'rule_lifecycle.py', 'rule_replay.py', 'portable_replay_runtime.py', 'chat_commands.py', 'chat_intake.py', 'business_memory.py', 'recurring_rate_rules.py', 'recurring_quantity_rules.py', 'recurring_rules.py', 'formula_rules.py', 'calculation_expression.py', 'expression_functions.py', 'formula_dependencies.py', 'cost_references.py', 'document_layouts.py')
    here = Path(__file__).resolve().parent
    return digest({'adapter':rule_adapters.code_fingerprint(root), 'service':{
        name:hashlib.sha256((here/name).read_bytes()).hexdigest() for name in files}})


class RuleCommands:
    def __init__(self, root, *, session_id, actor_id='local-pc-owner'):
        self.root = Path(root).resolve()
        self.session_id = session_id
        self.actor_id = actor_id
        self.store = RuleMemory(self.root)
        self.memory = BusinessMemory(self.root)

    def history(self,record_id):
        from rule_lifecycle import history
        return history(self,record_id)

    def prepare_lifecycle(self,record_id,action,selected_rule_id=None,*,conversation_id):
        from rule_lifecycle import prepare
        return prepare(self,record_id,action,selected_rule_id,conversation_id=conversation_id)

    def _record(self, record_id, sources=None):
        record = self.memory.get_record(record_id)
        if not record or record['payload']['intent'] != 'upsert_rule':
            raise ValueError('먼저 비교 기준 요청을 저장해 주세요.')
        record = copy.deepcopy(record)
        payload = record['payload']
        if payload['application'] not in ('current_case','current_and_future','future_only'):
            raise ValueError('앞으로만 적용하는 요청입니다. 이번 청구 계산에는 적용하지 않습니다.')
        sources = source_snapshot(self.root) if sources is None else sources
        latest = next((r for r in self.store.list_rules() if r['record_id']==record_id),None)
        if latest and latest['payload'].get('reapproval'):
            if latest.get('invalidated') or latest['payload']['code_fingerprint'] != code_fingerprint(self.root):
                raise ValueError('이전에 재승인한 자료 또는 계산 기능이 바뀌었습니다. 현재 자료로 다시 확인해 주세요.')
            renewal=latest['payload']['reapproval']
            payload.update(targets=renewal['targets'],source_snapshot=renewal['source_snapshot'])
        if digest(sources) != digest(payload['source_snapshot']):
            raise ValueError('저장 당시 자료·결정·결과와 달라 이 기준을 적용할 수 없습니다. 기존 기준 이력은 보존합니다.')
        current = chat_intake.select_targets(self.root, [t['issue_id'] for t in payload['targets']], 'rule-validation')
        clean = lambda t: {k:v for k,v in t.items() if k not in ('target_id','issue_id','alias')}
        if [clean(t) for t in current['targets']] != [clean(t) for t in payload['targets']]:
            raise ValueError('저장된 대상의 위치나 비교값이 달라졌습니다. 이 기준을 적용하지 않았습니다.')
        return record, sources

    def options(self, record_id):
        try:
            record, _ = self._record(record_id)
            return rule_adapters.options(self.root, record)
        except (ValueError, OSError, KeyError, TypeError) as exc:
            try:
                record,context,previous=self._reapproval(record_id)
                options=rule_adapters.options(self.root,record)
                return dict(options,reapproval_required=True,previous_spec=previous['payload']['spec'] if previous else None,
                    message='자료가 변경되었습니다. 현재 대상과 값을 다시 계산한 뒤 확인하면 같은 기준을 이번 청구에 재적용합니다.')
            except (ValueError,OSError,KeyError,TypeError) as current_error:
                return {'supported':False,'choices':[],'message':str(current_error)}

    def _reapproval(self,record_id,sources=None):
        from rule_reapproval import build
        record=self.memory.get_record(record_id)
        if not record or record['payload']['intent']!='upsert_rule':raise ValueError('저장된 비교 기준 요청을 선택해 주세요.')
        if record['payload']['application'] not in ('current_case','current_and_future','future_only'):
            raise ValueError('적용 범위를 확인할 수 없습니다.')
        sources=source_snapshot(self.root) if sources is None else sources
        previous=next((r for r in self.store.list_rules() if r['record_id']==record_id),None)
        renewed,context=build(self.root,record,previous,sources)
        return renewed,context,previous

    def prepare_reapproval(self,record_id,kind,parameters,*,conversation_id):
        from rule_reapproval import changes
        saved=self.memory.get_record(record_id)
        if saved and saved['payload']['application']=='future_only':
            raise ValueError('앞으로만 적용하는 요청은 이번 청구에 재적용할 수 없습니다.')
        sources=source_snapshot(self.root);code=code_fingerprint(self.root)
        self.store.invalidate_rules(digest(sources),code)
        record,context,previous=self._reapproval(record_id,sources)
        spec={'kind':kind,'parameters':parameters}
        if previous and previous['payload']['spec']!=spec:
            raise ValueError('자료 재승인에서는 마지막으로 적용한 기준을 유지합니다. 기준 자체를 바꾸려면 별도 변경 요청이 필요합니다.')
        revision=self.memory.revision()
        calculation=rule_adapters.calculate(self.root,record,kind,parameters)
        if digest(source_snapshot(self.root))!=digest(sources) or code_fingerprint(self.root)!=code:
            raise ValueError('재계산 중 자료나 코드가 바뀌었습니다. 다시 확인해 주세요.')
        prepared=self.store.create_rule_preview(record_id,spec,calculation,
            actor_id=self.actor_id,session_id=self.session_id,conversation_id=conversation_id,
            source_fingerprint=digest(sources),code_fingerprint=code,expected_revision=revision,
            idempotency_key=uuid.uuid4().hex,reapproval=context)
        original=self.memory.get_record(record_id)['payload']
        baseline=previous['payload'].get('reapproval',{}).get('source_snapshot',original['source_snapshot']) if previous else original['source_snapshot']
        old_targets=previous['payload']['targets'] if previous else original['targets']
        rebound=[{'label':new['label'],'previous_locator':old['locator'],'current_locator':new['locator'],
                  'previous_claim':old.get('claim_value'),'current_claim':new.get('claim_value'),
                  'previous_comparison':old.get('comparison_value'),'current_comparison':new.get('comparison_value')}
                 for old,new in zip(old_targets,context['targets'])]
        payload=record['payload']
        return dict(state='preview',record_id=record_id,preview_id=prepared['proposal_id'],
            preview_hash=prepared['proposal_hash'],nonce=prepared['approval_nonce'],expires_at=prepared['expires_at'],
            spec=spec,changes=calculation['changes'],limitations=calculation['limitations'],
            target_count=len(payload['targets']),scope=payload['scope'],month=payload['month'],
            criterion=payload['criterion'],application=payload['application'],expected_revision=revision,
            calculation_executed=True,future_rule_applied=False,base_calculation_unchanged=True,
            reapproval_required=True,reapproval=context,rebound_targets=rebound,
            changed_files=changes(baseline,sources),previous_rule_id=context['previous_rule_id'],
            message='현재 자료로 다시 계산했습니다. 대상·원본 위치·변경값을 확인하면 새 적용 버전을 저장합니다. 이전 기준과 결과는 보존합니다.')

    def prepare(self, record_id, kind, parameters, *, conversation_id):
        record, sources = self._record(record_id)
        if record['payload']['application']=='future_only' and kind not in RECURRING_KINDS:
            raise ValueError('앞으로만 적용하는 요청에는 미래 적용을 지원하는 비교 방식을 선택해 주세요.')
        code = code_fingerprint(self.root)
        self.store.invalidate_rules(digest(sources), code)
        revision = self.memory.revision()
        calculation = rule_adapters.calculate(self.root, record, kind, parameters)
        # No preview can combine reads from different source/code versions.
        after = source_snapshot(self.root)
        if digest(after) != digest(sources) or code_fingerprint(self.root) != code:
            raise ValueError('계산 중 자료 또는 계산 기능이 변경되어 결과를 적용하지 않았습니다.')
        prepared = self.store.create_rule_preview(record_id, {'kind':kind,'parameters':parameters}, calculation,
            actor_id=self.actor_id, session_id=self.session_id, conversation_id=conversation_id,
            source_fingerprint=digest(sources), code_fingerprint=code, expected_revision=revision,
            idempotency_key=digest({'record':record_id,'revision':revision,'attempt':uuid.uuid4().hex}))
        payload = record['payload']
        return dict(state='preview', record_id=record_id, preview_id=prepared['proposal_id'],
            preview_hash=prepared['proposal_hash'], nonce=prepared['approval_nonce'],
            expires_at=prepared['expires_at'], spec={'kind':kind,'parameters':parameters},
            changes=calculation['changes'], limitations=calculation['limitations'],
            target_count=len(payload['targets']), scope=payload['scope'], month=payload['month'],
            criterion=payload['criterion'], application=payload['application'], expected_revision=revision,
            calculation_executed=True, future_rule_applied=False, base_calculation_unchanged=True,
            message='선택 항목을 새 기준으로 계산했습니다. 변경 전후를 확인한 뒤 적용해 주세요. 기존 전체 검증 결과는 유지합니다.')

    def confirm(self, preview, *, conversation_id, preview_id, preview_hash, nonce):
        if not preview or preview.get('state') not in ('preview','applied'):
            raise ValueError('기준 적용 전 계산 결과를 먼저 확인해 주세요.')
        if (preview.get('preview_id'),preview.get('preview_hash'),preview.get('nonce')) != (preview_id,preview_hash,nonce):
            raise ValueError('현재 계산 미리보기를 다시 확인해 주세요.')
        sources = source_snapshot(self.root)
        if preview.get('reapproval_required'):
            _,context,_=self._reapproval(preview['record_id'],sources)
            # An already committed preview is handled idempotently by the ledger.
            committed=self.store.get_rule_for_proposal(preview_id)
            if not committed and digest(context)!=digest(preview.get('reapproval')):
                raise ValueError('현재 자료나 대상이 재승인 미리보기와 다릅니다. 다시 계산해 주세요.')
        else:
            self._record(preview['record_id'], sources)
        receipt = self.store.confirm_rule(preview_id, proposal_hash=preview_hash, approval_nonce=nonce,
            actor_id=self.actor_id, session_id=self.session_id, conversation_id=conversation_id,
            current_fingerprint=digest(sources), current_code_fingerprint=code_fingerprint(self.root))
        result = dict(preview, **{k:v for k,v in receipt.items() if k != 'payload'})
        payload = receipt['payload']
        result.update(spec=payload['spec'],changes=payload['calculation']['changes'],
            limitations=payload['calculation']['limitations'],target_count=len(payload['targets']),
            scope=payload['scope'],month=payload['month'],criterion=payload['criterion'],application=payload['application'])
        result.update(state='applied', message=('변경된 자료의 기준 재승인과 재대사 결과를 새 버전으로 저장했습니다. 이전 기록은 보존합니다. ' if payload.get('reapproval') else '')+'선택 항목의 새 기준 비교 결과를 저장했습니다. 확인 목록·보고서·다운로드에 별도로 표시합니다. 다른 검사와 기존 결정은 유지합니다.')
        if payload['spec']['kind']=='recurring_rate':
            p=payload['spec']['parameters']
            result.update(recurring_rule_registered=not receipt.get('stopped'),
                message=f"{p['start_month']}부터 {p['category']} / {p['item']} / {p['unit']} 항목에 단가 {p['rate']}원을 적용하도록 저장했습니다. 해당 월 검증 때 자동 비교하며 이미 완료된 월은 재검증해야 합니다."+(' 이번 청구에는 적용하지 않았습니다.' if payload['application']=='future_only' else ' 이번 선택 항목의 비교 결과도 별도 반영했습니다.'))
        action=payload.get('lifecycle',{}).get('action')
        if payload['spec']['kind']=='formula':
            result.update(message='조건·계산식을 저장했습니다. 원래 대사 결과는 유지하며 새 기준 비교를 별도로 표시합니다.'+(' 이번 청구에는 적용하지 않았습니다.' if payload['application']=='future_only' else ''))
        if payload['spec']['kind']=='recurring_quantity':
            from recurring_quantity_rules import description
            result.update(recurring_rule_registered=not receipt.get('stopped'),
                message=description(payload['spec']['parameters'])+' 기준을 저장했습니다. 해당 월 검증 때 별도 수량 비교를 수행합니다. 완료한 월은 재검증해야 바뀝니다.'+(' 이번 청구에는 적용하지 않았습니다.' if payload['application']=='future_only' else ' 이번 선택 건의 수량 비교도 별도 반영했습니다.'))
        if action:
            result.update(lifecycle_action=action,message=(
                '선택한 비교 기준을 중지했습니다. 현재 원본의 기본 비교를 재계산했으며 추가 기준 결과의 적용을 해제했습니다. 이전 기준과 결과는 보존합니다.' if action=='stop' else
                '선택한 이전 기준으로 현재 자료를 재대사하고 새 버전으로 복원했습니다. 이전 기록·원본 계산·기존 결정은 보존합니다.'))
            if payload['spec']['kind'] in RECURRING_KINDS and payload['scope']=='future':
                result['message']=('계속 적용하던 비교 기준을 중지했습니다.' if action=='stop' else '선택한 이전 비교 기준을 새 버전으로 복원했습니다.')+' 다음 월 검증부터 반영합니다. 이미 완료된 월은 재검증해야 바뀌며 이전 기록은 보존합니다.'
        if receipt.get('invalidated'):
            result.update(state='version_conflict', changes_applied=False,
                message='이전 계산 기록은 보존되지만 근거가 바뀌어 적용 상태는 해제되었습니다.')
        elif not receipt.get('stopped'):
            try:
                from rule_replay import capture
                result['exact_replay_archived']=capture(self,receipt,sources)
            except (ValueError,OSError,KeyError) as exc:
                result['exact_replay_archived']=False
                result['replay_archive_error']=str(exc)
                result['message']+=' 적용 결과는 저장했지만 당시 환경 보관은 완료하지 못했습니다: '+str(exc)
        return result

    def cancel(self, preview, *, conversation_id):
        if not preview or preview.get('state') != 'preview':
            raise ValueError('취소할 기준 적용 미리보기가 없습니다.')
        self.store.cancel_rule(preview['preview_id'], proposal_hash=preview['preview_hash'],
            approval_nonce=preview['nonce'], actor_id=self.actor_id, session_id=self.session_id,
            conversation_id=conversation_id)
        return dict(preview,state='cancelled',nonce=None,message='기준 적용을 취소했습니다. 기존 결과를 유지합니다.')

    def projection(self):
        events = self.store.list_rules()
        if not events:
            return {'events':[],'rows':{},'unmapped_targets':0,'source_error':None}
        error = None
        try:
            fingerprint = digest(source_snapshot(self.root))
            current = chat_intake._current(self.root)
        except (ValueError,OSError) as exc:
            error = str(exc)
            fingerprint = digest({'source_unavailable':error})
            try:
                current = chat_intake._current(self.root)
            except (ValueError,OSError):
                current = {'rows':[],'version':None}
        self.store.invalidate_rules(fingerprint, code_fingerprint(self.root))
        events = self.store.list_rules()
        fields = ('check','source','locator','label','date','order_id','inbound_id','item_id','tracking_id','unit','applied_criterion')
        key = lambda t: digest({k:t.get(k,'') for k in fields})
        lookup = {}
        for i,row in enumerate(current['rows']):
            target = chat_intake._target(row,i,current['version'])
            lookup.setdefault(key(target),[]).append((str(i),target))
        rows, unmapped = {},0
        for event in events:
            if event.get('stopped') or event['payload']['application']=='future_only':
                continue
            payload = event['payload']
            changes = {c['target_id']:c for c in payload['calculation']['changes']}
            dependency_error = None
            if payload['spec']['kind']=='formula' and payload['spec']['parameters'].get('result_references') and not event['invalidated'] and not error:
                try:
                    revision=self.memory.revision()
                    record,_=self._record(event['record_id'])
                    fresh=rule_adapters.calculate(self.root,record,'formula',payload['spec']['parameters'])
                    if self.memory.revision()!=revision:raise ValueError('참조 기준이 변경되어 다시 계산해야 합니다.')
                    changes={c['target_id']:c for c in fresh['changes']}
                except (ValueError,OSError,KeyError) as exc:
                    dependency_error=str(exc)
            for target in payload['targets']:
                candidates = lookup.get(key(target),[])
                if len(candidates)>1 and not event['invalidated'] and not error:
                    candidates = [c for c in candidates if c[0]==target['issue_id']]
                if len(candidates)!=1:
                    unmapped += 1
                    continue
                index,_ = candidates[0]; change=changes[target['target_id']]
                valid = not event['invalidated'] and not error and not dependency_error and event['source_fingerprint']==fingerprint
                item = {'_rule_status':change['after']['status'] if valid else 'review_required',
                    '_rule_comparison':change['after']['comparison'] if valid else '',
                    '_rule_difference':change['after']['difference'] if valid else '',
                    '_rule_event':event['rule_id'], '_rule_record':event['record_id'],
                    '_rule_basis':dependency_error or change['basis'], '_rule_evidence':change['evidence_refs'],
                    '_rule_before':change['before'], '_rule_after':change['after'] if valid else {},
                    '_rule_invalidated':not valid}
                if index in rows:
                    item.update(_rule_status='review_required',_rule_comparison='',_rule_difference='',_rule_after={},_rule_invalidated=True)
                rows[index] = item
        return {'events':events,'rows':rows,'unmapped_targets':unmapped,'source_error':error,'source_fingerprint':fingerprint}
