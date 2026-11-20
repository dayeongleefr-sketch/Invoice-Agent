"""Codex transport for Windows/macOS; business tool execution stays in the host."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

SCHEMAS = {'agent_action.schema.json', 'demo_company_action.schema.json',
           'demo_company_chat.schema.json', 'demo_company_command_intake.schema.json',
           'demo_company_conversation.schema.json'}


def confined(root, path):
    root = Path(root).resolve()
    path = Path(path).absolute()
    if not path.is_relative_to(root) or not path.resolve().is_relative_to(root):
        raise ValueError('Model transport path outside runtime')
    for part in (path, *path.parents):
        if part == root:break
        if part.is_symlink() or part.is_junction():
            raise ValueError('Model transport link rejected')
    return path


def invoke_model(root, prompt, folder, turn, schema_file='demo_company_action.schema.json', timeout=180):
    raise ValueError('이 공개 버전은 AI 호출을 지원하지 않습니다.')
    root = Path(root).resolve()
    if schema_file not in SCHEMAS:raise ValueError('Unsupported model schema')
    if not isinstance(turn, int) or turn < 1:raise ValueError('Invalid model turn')
    runtime = confined(root, root/'ia/.runtime')
    folder = confined(runtime, folder)
    folder.mkdir(parents=True, exist_ok=True)
    output = confined(runtime, folder/f'model_response_{turn}.json')
    if output.exists():raise ValueError('Model response already exists')
    schema = confined(root, root/'ia/config'/schema_file)
    schema_hash = hashlib.sha256(schema.read_bytes()).hexdigest()
    command = shutil.which('codex.exe' if os.name == 'nt' else 'codex')
    if not command:raise ValueError('Codex CLI를 설치하고 03_AI_LOGIN으로 로그인하세요.')
    args = [command, 'exec', '--ignore-user-config', '--skip-git-repo-check', '--ephemeral',
            '--sandbox', 'read-only', '-C', str(folder), '--config', 'features.shell_tool=false',
            '--config', 'web_search="disabled"', '--config', 'project_doc_max_bytes=0',
            '--output-schema', str(schema), '--output-last-message', str(output), '--json', '-']
    # Inherit the account environment, without opening or copying credential files.
    try:
        proc = subprocess.run(args, input=prompt, text=True, encoding='utf-8', errors='replace',
                              capture_output=True, timeout=timeout,
                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except subprocess.TimeoutExpired:
        raise ValueError('AI model response timed out; no model result was fabricated') from None
    events = []
    for line in proc.stdout.splitlines():
        try:event = json.loads(line)
        except ValueError:continue
        if not isinstance(event, dict):continue
        if event.get('type') == 'turn.completed':
            events.append({'type': 'turn.completed', 'usage': event.get('usage')})
        if event.get('type') == 'item.completed' and event.get('item', {}).get('type') != 'agent_message':
            events.append({'type': 'item.completed', 'item_type': event.get('item', {}).get('type')})
    (folder/f'model_events_{turn}.json').write_text(json.dumps(events, ensure_ascii=False), encoding='utf-8')
    if proc.returncode or not output.is_file():
        reason = 'provider_error'
        for pattern, label in [(r'401|unauthorized|not logged in','authentication_required'),
                               (r'unexpected argument|unrecognized|invalid value','invalid_cli_argument'),
                               (r'connect|resolve host|network|stream disconnected','connection_failure')]:
            if re.search(pattern, proc.stderr, re.I):reason = label
        raise ValueError(f'AI provider execution failed ({reason}); no model result was fabricated')
    if sum(e['type']=='turn.completed' for e in events) != 1:
        raise ValueError('AI provider did not report a completed turn')
    if any('item_type' in e and e['item_type'] != 'reasoning' for e in events):
        raise ValueError('Unexpected model-side tool use')
    if hashlib.sha256(schema.read_bytes()).hexdigest() != schema_hash:
        raise ValueError('AI response schema changed')
    confined(runtime, output)
    return json.loads(output.read_text('utf-8'))
