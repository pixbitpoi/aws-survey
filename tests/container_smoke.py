"""Run inside the test image only. Local fake Responses API; no real auth/AWS.

Mount this file at /tmp/container_smoke.py and the distribution instructions at
/home/node/aws-survey/{AGENTS,CLAUDE}.md. Container filesystem is disposable.
"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import threading

requests = []
errors = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        requests.append(body)
        names = [t.get('name') for t in body.get('tools', [])]
        shell = next((n for n in names if n in {'shell_command', 'shell', 'exec_command'}), None)
        if shell is None:
            errors.append(f'Missing shell tool: {names}')
        index = len(requests)
        commands = {
            1: (shell, {'command': 'aws ec2 terminate-instances --instance-ids i-0'}),
            2: ('apply_patch', '*** Begin Patch\n*** Add File: out/smoke.md\n+smoke passed\n*** End Patch'),
            3: ('apply_patch', '*** Begin Patch\n*** Add File: out/_環境/aws-audit.log\n+tampered\n*** End Patch'),
            4: (shell, {'command': 'ls out/'}),
        }
        if index in commands:
            name, args = commands[index]
            if name == 'apply_patch' and any(t.get('name') == name and t.get('type') == 'custom' for t in body.get('tools', [])):
                output = {'id': f'item_{index}', 'type': 'custom_tool_call', 'call_id': f'call_{index}', 'name': name, 'input': args}
            else:
                output = {'id': f'item_{index}', 'type': 'function_call', 'call_id': f'call_{index}', 'name': name, 'arguments': json.dumps({'cmd': args['command']} if name == 'exec_command' else {'input': args} if name == 'apply_patch' else args)}
        else:
            output = {'id': 'msg_final', 'type': 'message', 'role': 'assistant', 'status': 'completed', 'content': [{'type': 'output_text', 'text': 'Smoke complete.'}]}
        response = {'id': f'resp_{index}', 'object': 'response', 'status': 'completed', 'output': [output], 'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}}
        events = [
            {'type': 'response.created', 'response': {'id': f'resp_{index}', 'status': 'in_progress', 'output': []}},
            {'type': 'response.output_item.added', 'output_index': 0, 'item': output},
            {'type': 'response.output_item.done', 'output_index': 0, 'item': output},
            {'type': 'response.completed', 'response': response},
        ]
        data = ''.join('event: '+e['type']+'\ndata: '+json.dumps(e)+'\n\n' for e in events).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
root = Path('/home/node/aws-survey')
(root / 'out/_環境').mkdir(parents=True, exist_ok=True)
cmd = ['codex', '--strict-config', '-c', 'model_provider="smoke"', '-c', 'model="gpt-5.4"',
       '-c', 'model_providers.smoke.name="Smoke"', '-c', f'model_providers.smoke.base_url="http://127.0.0.1:{server.server_port}/v1"',
       '-c', 'model_providers.smoke.wire_api="responses"', '-c', 'model_providers.smoke.requires_openai_auth=false',
       '-c', 'model_providers.smoke.supports_websockets=false',
       'exec', '--skip-git-repo-check', 'Run the local smoke scenario.']
try:
    result = subprocess.run(cmd, stdin=subprocess.DEVNULL, text=True, capture_output=True, timeout=45)
finally:
    server.shutdown()
if result.returncode != 0 or errors:
    print(result.stdout)
    print(result.stderr)
assert result.returncode == 0, result.returncode
assert not errors, errors
assert 'smoke.md' in json.dumps(requests[-1]), 'allowed shell did not return the output listing'
for path in ['/etc/codex/config.toml', '/etc/codex/requirements.toml', '/etc/codex/hooks/codex-guard.py', '/home/node/aws-survey/.claude/settings.json']:
    assert Path(path).stat().st_uid == 0, path
    assert not Path(path).stat().st_mode & 0o022, path
for path in ['AGENTS.md', 'CLAUDE.md']:
    try:
        with (root / path).open('a') as f:
            pass
    except PermissionError:
        pass
    except OSError as error:
        assert error.errno == 30, error
    else:
        raise AssertionError(f'{path} is writable')
assert len(requests) == 5, len(requests)
context = json.dumps(requests[0], ensure_ascii=False)
assert 'AWS インフラの調査' in context, 'distribution AGENTS.md was not loaded'
assert 'ホストと調査コンテナ' not in context, 'development instructions leaked'
assert (root / 'out/smoke.md').read_text().strip() == 'smoke passed', 'allowed patch did not run'
audit = (root / 'out/_環境/aws-audit.log').read_text()
assert 'DENY' in audit and 'terminate-instances' in audit, audit
assert 'tampered' not in audit, 'audit patch was not blocked'
all_requests = json.dumps(requests, ensure_ascii=False)
assert '監査ログ' in all_requests, 'patch denial was not returned to model'
print('PASS: distribution instructions, managed shell hook, permitted patch, denied audit patch')
