"""Recalculate before stopping/restoring a selected rule; never edit old versions."""
import copy
import uuid

from business_memory import digest
from chat_commands import source_snapshot
import rule_adapters


def history(commands, record_id):
    record=commands.memory.get_record(record_id)
    if not record or record['payload']['intent']!='upsert_rule':
        raise ValueError('저장된 비교 기준 요청을 선택해 주세요.')
    return [event for event in commands.store.list_rules(latest=False) if event['record_id']==record_id]


def prepare(commands, record_id, action, selected_rule_id, *, conversation_id):
    from rule_commands import code_fingerprint
    if action not in ('stop','restore','compare','exact_replay'):
        raise ValueError('중지·과거 기준 비교·복원 중 지원하는 작업을 선택해 주세요.')
    events=history(commands,record_id)
    if not events:raise ValueError('아직 적용한 비교 기준이 없습니다.')
    latest=events[0]
    if action=='stop':
        selected=latest
        if latest['stopped']:raise ValueError('이미 중지된 기준입니다.')
        if selected_rule_id not in (None,latest['rule_id']):raise ValueError('현재 적용 버전만 중지할 수 있습니다.')
    else:
        selected=next((e for e in events if e['rule_id']==selected_rule_id),None)
        if not selected or selected['stopped']:raise ValueError('비교할 이전 적용 버전을 선택해 주세요. 중지 기록은 계산 기준이 아닙니다.')
    if action=='exact_replay':
        from rule_replay import replay
        archived=replay(commands,selected)
        if archived is not None:return archived
    sources=source_snapshot(commands.root);fingerprint=digest(sources);code=code_fingerprint(commands.root)
    commands.store.invalidate_rules(fingerprint,code)
    if action=='exact_replay' and (selected['source_fingerprint']!=fingerprint or selected['code_fingerprint']!=code):
        return {'state':'unavailable','lifecycle_action':action,'record_id':record_id,'selected_rule_id':selected['rule_id'],
                'month':selected['payload']['month'],'scope':selected['payload']['scope'],'application':selected['payload']['application'],
                'target_count':len(selected['payload']['targets']),
                'calculation_executed':False,'changes_applied':False,'message':'당시 자료 또는 계산 코드가 현재와 다르고 보관된 실행 묶음이 없습니다. 당시 그대로 재현할 수 없습니다. 현재 자료로 과거 기준을 비교할 수 있습니다.'}
    record,context,_=commands._reapproval(record_id,sources)
    revision=commands.memory.revision()
    spec=copy.deepcopy(selected['payload']['spec'])
    # Stopping the override returns to the registered baseline, recomputed now.
    calculation_spec=spec if action!='stop' else (
        {'kind':'quantity_components','parameters':{'consumables':'include'}} if spec['kind'] in ('quantity_components','recurring_quantity') else
        {'kind':'rate_selection','parameters':{'rate_source':'claim'}})
    if action=='stop' and spec['kind']=='quantity_rematch':
        from quantity_rematch import baseline_parameters
        calculation_spec={'kind':'quantity_rematch','parameters':baseline_parameters(commands.root)}
    if action=='stop' and spec['kind']=='b2c_link':
        from b2c_link_rules import baseline_parameters
        calculation_spec={'kind':'b2c_link','parameters':baseline_parameters(commands.root)}
    if action=='stop' and spec['kind']=='pack_conversion':
        from pack_conversion_rules import baseline_parameters
        calculation_spec={'kind':'pack_conversion','parameters':baseline_parameters(commands.root)}
    if action=='stop' and spec['kind']=='formula':
        from formula_rules import calculate
        calculation=calculate(commands.root,record,spec['parameters'],baseline=True)
    else:
        calculation=rule_adapters.calculate(commands.root,record,calculation_spec['kind'],calculation_spec['parameters'])
    if digest(source_snapshot(commands.root))!=fingerprint or code_fingerprint(commands.root)!=code:
        raise ValueError('계산 중 자료나 코드가 바뀌었습니다. 다시 확인해 주세요.')
    if commands.memory.revision()!=revision:
        raise ValueError('계산 중 다른 처리가 저장되었습니다. 현재 상태로 다시 확인해 주세요.')
    if action=='exact_replay' and calculation!=selected['payload']['calculation']:
        raise ValueError('동일 자료·코드 재계산값이 저장값과 다릅니다. 당시 그대로 재현 완료로 표시하지 않습니다.')
    previous={c['target_id']:c for c in latest['payload']['calculation']['changes']}
    transitions=[]
    current_changes={change['target_id']:change for change in calculation['changes']}
    for old,current in zip(latest['payload']['targets'],record['payload']['targets']):
        change=current_changes[current['target_id']]
        prior=previous[old['target_id']]['after']
        transitions.append({'label':current['label'],'previous_comparison':prior['comparison'],
            'previous_difference':prior['difference'],'new_comparison':change['after']['comparison'],
            'new_difference':change['after']['difference'],'unit':change['after']['unit'],
            'previous_state':'중지' if latest['stopped'] else '자료·코드 변경 전 기록' if latest['invalidated'] else '현재 적용'})
    shared=dict(record_id=record_id,lifecycle_action=action,selected_rule_id=selected['rule_id'],
        selected_rule_version=selected['rule_version'],previous_rule_id=latest['rule_id'],
        spec=spec,changes=calculation['changes'],transitions=transitions,limitations=calculation['limitations'],
        month=record['payload']['month'],scope=record['payload']['scope'],application=record['payload']['application'],
        target_count=len(record['payload']['targets']),calculation_executed=True,base_calculation_unchanged=True,
        future_rule_applied=False,source_fingerprint=fingerprint,code_fingerprint=code)
    if action in ('compare','exact_replay'):
        return dict(shared,state='comparison',changes_applied=False,
            message='현재 적용 기준을 바꾸지 않고 비교 결과를 계산했습니다.' if action=='compare' else
            '자료·계산 코드의 해시와 재계산 결과가 당시 기록과 같습니다. 모델 설명 문구의 재현이나 현재 기준 변경은 수행하지 않았습니다.')
    lifecycle={'action':action,'previous_rule_id':latest['rule_id'],'selected_rule_id':selected['rule_id']}
    preview=commands.store.create_rule_preview(record_id,spec,calculation,actor_id=commands.actor_id,
        session_id=commands.session_id,conversation_id=conversation_id,source_fingerprint=fingerprint,code_fingerprint=code,
        expected_revision=revision,idempotency_key=uuid.uuid4().hex,reapproval=context,lifecycle=lifecycle)
    return dict(shared,state='preview',preview_id=preview['proposal_id'],preview_hash=preview['proposal_hash'],
        nonce=preview['approval_nonce'],expires_at=preview['expires_at'],reapproval_required=True,reapproval=context,
        expected_revision=revision,changes_applied=False,
        message='추가 적용 기준을 중지하고 현재 원본의 기본 비교로 돌아갑니다. 확인 후 중지 기록을 저장합니다.' if action=='stop' else
        '선택한 이전 기준을 현재 자료에 재계산했습니다. 확인하면 새 적용 버전으로 복원합니다. 이전 기록은 수정하지 않습니다.')
