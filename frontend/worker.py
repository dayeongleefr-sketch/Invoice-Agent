"""Subprocess boundary: run only with the existing calculation interpreter."""
import contextlib
import json
from pathlib import Path
import sys
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ia/src'))
sys.path.insert(0, str(ROOT / 'frontend'))


def execute(payload, root=ROOT):
    import demo_company_workflow as workflow
    action = payload['action']
    if action not in ('preview','read','investigate','public_calculate'):
        raise ValueError('공개판은 가상 자료 계산·조회만 지원합니다.')
    if action == 'public_calculate':
        result=workflow.calculate(root)
        return {'processing_status':result['processing_status'],'waiting_for_input':False,
                'unresolved_count':len(result['questions']),'model_calls':0,'llm_used':False,
                'explanation':'가상 청구·WMS를 Python으로 계산했습니다. 미확정 항목은 결과에 유지했습니다.'}

    if action == 'preview':
        from source_preview import preview
        return preview(root,**payload['selection'])
    if action == 'chat':
        from conversation import respond
        return respond(root,payload['request'],payload.get('history',[]),payload.get('previous_month'),
                       selection=payload.get('selection'),pending=payload.get('pending'))
    if action == 'monthly_register':
        from monthly_cases import register
        return register(root, payload['registration'])
    if action in ('monthly_verify', 'monthly_agent'):
        from monthly_workflow import execute as execute_month
        return execute_month(root,payload['month'],request=payload.get('request',''),answers=payload.get('answers'),
                             use_model=action=='monthly_agent')
    if action == 'monthly_evidence':
        from monthly_evidence import register_and_execute
        return register_and_execute(root, payload['month'], payload['registration'])
    if action == 'agent':
        return workflow.agent(root, payload['request'], answers=payload.get('answers'),
                              resume=bool(payload.get('resume')))
    if action in ('read', 'investigate'):
        result = workflow.load(root / workflow.BASE / 'workflow_result.json')
        workflow.verify_artifacts(root, result)
        return (workflow.investigate(root, result) if action == 'investigate'
                else workflow.observe(result))
    if action == 'evidence':
        from supporting_evidence import register_and_refresh
        result = register_and_refresh(root, payload['registration'])
        return {k: v for k, v in result.items() if k not in ('rows', 'changes')}
    raise ValueError('unsupported_frontend_action')


if __name__ == '__main__':
    request_path, response_path = map(Path, sys.argv[1:3])
    try:
        with contextlib.redirect_stdout(sys.stderr):
            result = execute(json.loads(request_path.read_text(encoding='utf-8')))
        response = {'ok': True, 'result': result}
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        response = {'ok': False, 'error': str(exc)}
    temp = response_path.with_suffix('.tmp')
    temp.write_text(json.dumps(response, ensure_ascii=False, default=str), encoding='utf-8')
    temp.replace(response_path)
    sys.exit(0 if response['ok'] else 1)
