"""Registered-case preflight and isolated, evidence-preserving invoice revisions."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile

CONTEXT = 'ia/config/demo_company_case_context.json'
CASE_DECISIONS = 'ia/config/demo_company_case_decisions.json'


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,default=str).encode()).hexdigest()


def normalized_case(case):
    m=case['metadata']
    return {'company_id':m['company_id'],'warehouse_id':m['warehouse_scope'],
            'invoice_month':m['invoice_month'],'currency':m['currency'],
            'service_period':{k:m['service_period'][k] for k in ('start','end')},
            'sources':{role:{'path':r['source_path'],'sha256':r['sha256']}
                       for role,r in case['sources'].items() if role!='reference_optional'}}


def extended_policy_bindings(policies):
    names={'U36':'b2c_charges','U40':'storage_area_unit','U41':'olive_extra_work',
           'U42':'independent_work_evidence','U43':'independent_storage_evidence'}
    return {decision:policies[name] for decision,name in names.items() if policies.get(name)}


def preflight(root, case_id):
    from demo_company_material_registry import inspect_case, get_case, current_adapter_sources
    from demo_company_revision_impact import assess_revision, assess_claim_structure
    root=Path(root).resolve()
    status=inspect_case(root,case_id)
    case=get_case(root,case_id)
    status['case_registration_sha256']=canonical_hash(case)
    if status['executable']:
        status['execution_mode']='current_registered_sources'
        return status
    # A new month never falls back to July. Missing input is kept separate from adapter support.
    if any(x['reason']!='revision_adapter_and_decision_review_required' for x in status['limitations']):
        return status
    # The registration can itself be revised. Approved rules belong to the active
    # adapter's original sources, never to a mutable registry alias.
    previous={'company_id':'demo_company','warehouse_id':'demo_center',
              'invoice_month':'2029-07','currency':'KRW',
              'service_period':{'start':'2029-06-26','end':'2029-07-25'},
              'sources':current_adapter_sources(root)}
    assessment=assess_revision(root,previous,normalized_case(case))
    status['decision_impact']=assessment
    changed=set(status['source_roles_changed_from_active_adapter'])
    if 'claim' in changed:
        structure=assess_claim_structure(root,previous['sources']['claim'],normalized_case(case)['sources']['claim'])
        status['claim_structure']=structure
        if not structure['same_structure']:
            status.update(executable=False,calculation_status='not_implemented',
                          limitations=[{'status':'not_implemented','reason':'changed_invoice_layout'}])
            return status
    if 'claim_summary' in changed:
        structure=assess_claim_structure(root,previous['sources']['claim_summary'],
                                        normalized_case(case)['sources']['claim_summary'])
        status['claim_summary_structure']=structure
        if not structure['same_structure']:
            status.update(executable=False,calculation_status='not_implemented',
                          limitations=[{'status':'not_implemented','reason':'changed_invoice_summary_layout'}])
            return status
        # The summary can contain actual/negotiated charges absent from the
        # detail claim. Reuse U26 only when its original values are unchanged.
        unchanged_values=any(
            evidence.get('comparison') in ('identical_verified_bytes','identical_zip_member_payloads')
            or (evidence.get('comparison')=='all_sheets_rows_cells_text_and_formula_tokens'
                and evidence.get('numeric_value_changes')==0)
            for evidence in structure.get('evidence',[]))
        if not unchanged_values:
            assessment['status']='review_required'
            assessment['verified_reusable_decisions']=[
                item for item in assessment['verified_reusable_decisions'] if item!='U26']
            if not any(item['decision_id']=='U26' for item in assessment['review_required']):
                assessment['review_required'].append({
                    'decision_id':'U26','reason':'claim_summary_values_changed_or_unverified',
                    'changed_or_missing_roles':['claim_summary']})
            assessment.setdefault('evidence',[]).append({
                'role':'claim_summary','unchanged_original_values_verified':False})
    # Newer source-bound decisions also need an explicit reuse proof. Equal ZIP
    # payloads (e.g. a file comment change) or identical business cells suffice;
    # same layout alone is not approval to reuse changed quantities/rates.
    policy_path=root/'ia/config/demo_company_cost_decisions.json'
    policies=json.loads(policy_path.read_text('utf-8-sig')) if policy_path.is_file() else {}
    bindings=extended_policy_bindings(policies)
    business_unchanged=changed <= {'claim','claim_summary'} and all(
        any(e.get('comparison') in ('identical_verified_bytes','identical_zip_member_payloads') or
            (e.get('comparison')=='all_sheets_rows_cells_text_and_formula_tokens' and e.get('numeric_value_changes')==0)
            for e in status.get('claim_structure' if role=='claim' else 'claim_summary_structure',{}).get('evidence',[]))
        for role in changed)
    for decision in bindings:
        if business_unchanged:
            if decision not in assessment['verified_reusable_decisions']:assessment['verified_reusable_decisions'].append(decision)
        else:
            assessment['status']='review_required'
            assessment['review_required'].append({'decision_id':decision,'reason':'new_cost_policy_business_evidence_changed_or_unverified'})
    # This adapter only transfers a claim/document revision whose relevant business facts
    # have been proved unchanged. Changed criteria must not inherit source-bound answers.
    if changed <= {'claim','claim_summary'} and not assessment['review_required'] and not assessment['scope_changed']:
        if not baseline_decisions_valid(root):
            status.update(calculation_status='revision_review_required',executable=False,
                          limitations=[{'status':'not_implemented','reason':'baseline_decision_evidence_unverified'}])
            return status
        status.update(executable=True,calculation_status='ready_for_verified_invoice_revision',
                      execution_mode='isolated_verified_invoice_revision',limitations=[])
    else:
        status['calculation_status']='revision_review_required'
        status['limitations']=[{'status':'not_implemented',
            'reason':'changed_business_basis_requires_rule_review_before_execution',
            'decision_ids':[r['decision_id'] for r in assessment['review_required']]}]
    return status


def verified_reuse(root, decision_id, decision):
    """Only a host-produced, source-bound proof can carry an existing scoped decision."""
    root=Path(root)
    path=root/CONTEXT
    if not path.is_file():return False
    context=json.loads(path.read_text('utf-8-sig'))
    if decision_id not in context.get('verified_reusable_decisions',[]):return False
    if context.get('decision_hashes',{}).get(decision_id)!=canonical_hash(decision):return False
    cfg=json.loads((root/'ia/config/demo_company_direct_wms.json').read_text('utf-8-sig'))
    if cfg['sources']!=context.get('active_sources'):raise ValueError('revision_context_source_changed')
    proof=context.get('assessment')
    if not proof:return False
    if proof['scope_changed'] or proof['review_required']:raise ValueError('unverified_revision_decision_transfer')
    return True


def baseline_decisions_valid(root):
    import demo_company_workflow as w
    from demo_company_material_registry import current_adapter_sources
    try:
        result=w.load(root/w.BASE/'workflow_result.json')
        cost_path=root/w.BASE/'cost_verification.json'
        if w.sha(cost_path)!=result['result_hashes']['cost_verification.json']:return False
        cost=w.load(cost_path)
        expected=current_adapter_sources(root)
        policy=w.load(root/'ia/config/demo_company_cost_decisions.json')
        if cost['sources']!=expected:
            context_path=root/w.BASE/'case_execution_context.json'
            if not context_path.is_file() or w.sha(context_path)!=result.get('case_execution_context_sha256'):return False
            context=w.load(context_path)
            return (context.get('baseline_approval_verified') is True
                    and context.get('baseline_source_versions')==expected
                    and context.get('baseline_policy_hash')==canonical_hash(policy))
        billing=cost['billing_quantity'];causes=billing['cause_analysis']
        return (policy['unresolved_billing_quantity_basis']['scope_fingerprint']==billing['scope_fingerprints']['U32']
                and policy['inbound_work_pcs_unit']['scope_fingerprint']==causes['scope_fingerprint']
                and policy['outbound_repeated_box_work']['scope_fingerprint']==causes['box_scope_fingerprint'])
    except (OSError,KeyError,ValueError):return False


def prepare_stage(root, stage, case, assessment):
    """Copy execution settings, link registered originals read-only, retain actual paths."""
    import demo_company_workflow as w
    sources=normalized_case(case)['sources']
    for folder in ('ia/src','ia/config'):
        for p in (root/folder).iterdir():
            if p.is_file():
                dest=stage/folder/p.name;dest.parent.mkdir(parents=True,exist_ok=True)
                shutil.copyfile(p,dest)
    shutil.copyfile(root/'ia/IA.md',stage/'ia/IA.md')
    (stage/'ia/.runtime').mkdir()
    for record in sources.values():
        dest=stage/record['path'];dest.parent.mkdir(parents=True,exist_ok=True)
        os.link(root/record['path'],dest)
    cfg=w.load(stage/'ia/config/demo_company_direct_wms.json')
    previous=deepcopy(cfg['sources'])
    cfg['sources']={role:sources[role] for role in cfg['sources']}
    w.save(stage/'ia/config/demo_company_direct_wms.json',cfg)
    coverage=w.load(stage/'ia/config/coverage_demo.json')
    for entry in coverage['cases']:
        for role in ('claim','claim_summary'):
            if entry['source_path']==previous[role]['path']:
                entry.update(source_path=sources[role]['path'],source_sha256=sources[role]['sha256'])
    w.save(stage/'ia/config/coverage_demo.json',coverage)
    matching=w.load(stage/'ia/config/matching_review_decisions.json')
    matching['source_hashes']={role:sources['outbound_h1' if role=='outbound_q1' else role]['sha256']
                               for role in matching['source_hashes']}
    matching['revision_reuse_evidence']='demo_company_case_context.json'
    w.save(stage/'ia/config/matching_review_decisions.json',matching)
    policies=w.load(stage/'ia/config/demo_company_cost_decisions.json')
    w.save(stage/CONTEXT,{'case_id':case['case_id'],'revision_id':case['revision_id'],
        'active_sources':cfg['sources'],'assessment':assessment,
        'verified_reusable_decisions':assessment['verified_reusable_decisions'],
        'decision_hashes':{**{v['id']:canonical_hash(v) for v in policies.values() if isinstance(v,dict) and 'id' in v},
                           **{key:canonical_hash(value) for key,value in extended_policy_bindings(policies).items()}}})
    store=w.load(root/CASE_DECISIONS) if (root/CASE_DECISIONS).is_file() else {}
    record=store.get(case['case_id'],{})
    if record.get('revision_id')==case['revision_id']:
        for name,value in record.get('decisions',{}).items():
            if name not in ('demo_company_cost_decisions.json','demo_company_scoped_decisions.json'):
                raise ValueError('invalid_case_decision_file')
            w.save(stage/'ia/config'/name,value)
    copy_comparison_baseline(root,stage)


def copy_comparison_baseline(root,stage):
    """Let model tools observe the comparison before final publication."""
    import demo_company_workflow as w
    from issue_revision import SNAPSHOT
    path=root/w.BASE/SNAPSHOT
    if not path.is_file():return
    previous=w.load(root/w.BASE/'workflow_result.json')
    w.verify_artifacts(root,previous)
    if w.sha(path)!=previous.get('revision_comparison',{}).get('snapshot_sha256'):
        raise ValueError('revision_baseline_snapshot_unverified')
    (stage/w.BASE).mkdir(parents=True,exist_ok=True)
    for name in (SNAPSHOT,'workflow_result.json'):
        shutil.copyfile(root/w.BASE/name,stage/w.BASE/name)


def decision_payload(root, stage, case):
    import demo_company_workflow as w
    store=w.load(root/CASE_DECISIONS) if (root/CASE_DECISIONS).is_file() else {}
    decisions={name:w.load(stage/'ia/config'/name)
               for name in ('demo_company_cost_decisions.json','demo_company_scoped_decisions.json')
               if (stage/'ia/config'/name).is_file()}
    store[case['case_id']]={'revision_id':case['revision_id'],'decisions':decisions}
    return (json.dumps(store,ensure_ascii=False,indent=2)+'\n').encode('utf-8')


def execute_case(root, case_id, request=None, model=None, answers=None, resume=False):
    """Run a selected registered case; no unsupported month or criterion substitution."""
    import demo_company_workflow as w
    from demo_company_material_registry import get_case
    from artifact_transaction import publish_files, recover_pending
    root=Path(root).resolve()
    recover_pending(root)
    status=preflight(root,case_id)
    if not status['executable']:
        return {**status,'processing_status':status['calculation_status'],
                'calculation_executed':False,'report_replaced':False,'llm_used':False}
    if status['execution_mode']=='current_registered_sources':
        if w.active_requires_isolation(root):
            raise ValueError('explicit_active_case_reset_required')
        return w.agent(root,request,answers=answers,resume=resume,model=model) if request else w.calculate(root)
    case=get_case(root,case_id)
    baseline=w.snapshot(root)
    published_before=w.publication_baseline(root)
    for name in (w.ACTIVE_CASE,CASE_DECISIONS,w.SESSION.as_posix()):
        published_before[name]=w.sha(root/name) if (root/name).is_file() else None
    with tempfile.TemporaryDirectory(prefix='s4_revision_') as td:
        stage=Path(td).resolve()
        prepare_stage(root,stage,case,status['decision_impact'])
        if resume:
            previous=w.load(root/w.SESSION)
            if (previous.get('registered_case_id')!=case_id or previous.get('registered_revision_id')!=case['revision_id']):
                raise ValueError('revision_resume_case_changed')
            (stage/w.SESSION).parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(root/w.SESSION,stage/w.SESSION)
        state=w.agent(stage,request,answers=answers,resume=resume,model=model) if request else w.calculate(stage)
        state['registered_case_id']=case_id;state['registered_revision_id']=case['revision_id']
        if state.get('waiting_for_input'):
            # Keep a resumable session; no partial calculation replaces the published report.
            publish_files(root,{w.SESSION.as_posix():(json.dumps(state,ensure_ascii=False,indent=2)+'\n').encode('utf-8'),
                                CASE_DECISIONS:decision_payload(root,stage,case)},expected_files=published_before)
            return {**status,'processing_status':'awaiting_user_decision',
                    'questions':state['questions'],'calculation_executed':True,'report_replaced':False}
        if state.get('processing_status') not in ('completed','completed_with_limitations'):
            publish_files(root,{w.SESSION.as_posix():(json.dumps(state,ensure_ascii=False,indent=2)+'\n').encode('utf-8'),
                                CASE_DECISIONS:decision_payload(root,stage,case)},expected_files=published_before)
            return {**status,'processing_status':state.get('processing_status'),
                    'calculation_executed':True,'report_replaced':False}
        result=w.load(stage/w.BASE/'workflow_result.json')
        w.verify_artifacts(stage,result)
        if w.snapshot(root)!=baseline or preflight(root,case_id)['case_registration_sha256']!=status['case_registration_sha256']:
            raise ValueError('revision_inputs_changed_during_execution')
        result.update(case_id=case_id,material_revision_id=case['revision_id'],
                      registration_fingerprint=status['case_registration_sha256'])
        from demo_company_material_registry import current_adapter_sources
        context={'case_id':case_id,'revision_id':case['revision_id'],
                 'assessment':status['decision_impact'],'baseline_approval_verified':True,
                 'baseline_source_versions':current_adapter_sources(root),
                 'baseline_policy_hash':canonical_hash(w.load(root/'ia/config/demo_company_cost_decisions.json')),
                 'matching_decisions':w.load(stage/'ia/config/matching_review_decisions.json'),
                 'matching_decisions_sha256':w.sha(stage/'ia/config/matching_review_decisions.json')}
        w.save(stage/w.BASE/'case_execution_context.json',context)
        result['case_execution_context_sha256']=w.sha(stage/w.BASE/'case_execution_context.json')
        w.save(stage/w.BASE/'workflow_result.json',result)
        from issue_revision import prepare
        result=prepare(stage,root)
        w.update_report(stage)
        w.verify_artifacts(stage,result)
        payload={p.relative_to(stage).as_posix():p.read_bytes() for p in (stage/w.BASE).iterdir()
                 if p.suffix in ('.json','.csv') and p.name!='workflow_cache.json'}
        payload[result['report']]=(stage/result['report']).read_bytes()
        payload[w.ACTIVE_CASE]=(json.dumps({'case_id':case_id,'revision_id':case['revision_id'],
                                           'isolated_revision':True},ensure_ascii=False)+'\n').encode('utf-8')
        payload[w.SESSION.as_posix()]=(json.dumps(state,ensure_ascii=False,indent=2)+'\n').encode('utf-8')
        payload[CASE_DECISIONS]=decision_payload(root,stage,case)
        # Store minimum revision provenance; never mutate the active original-source configuration.
        publish_files(root,payload,expected_files={name:published_before.get(name) for name in set(published_before)|set(payload)})
        return {**result,'calculation_executed':True,'report_replaced':True,
                'agent_execution':state if request else None}
