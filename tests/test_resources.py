"""`aws-survey ls` (and, later, the interactive `ec2` / `lambda`), checked with a fake `docker` and a fake `aws`.

The listing runs inside the survey image with only the read-only key mounted: no out/, no code/, no
instructions, no terminal. The fake docker plays the container by running the lent inventory script on
the host with the fake `aws`, so what the tests see on screen is what the real script prints. Nothing on
the host side calls `aws`, and nothing is written under the target folder.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_cli                                  # noqa: E402
import test_inventory                            # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

# `run` executes whatever was lent as /x/inventory.sh with the region the container was given; `build` is a no-op.
FAKE_DOCKER = r'''#!/usr/bin/env python3
import json, os, subprocess, sys
argv = sys.argv[1:]
entry = {"tool": "docker", "argv": argv}
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps(entry) + "\n")
if argv[:1] == ["build"]:
    print("sha256:fake"); sys.exit(0)
if argv[:1] != ["run"]:
    sys.exit(0)
if os.environ.get("FAKE_DOCKER_FAIL") == "1":
    sys.stderr.write("docker: Error response from daemon: boom\n"); sys.exit(125)
mounts, env = {}, dict(os.environ)
for i, a in enumerate(argv):
    if a == "-v":
        parts = argv[i + 1].split(":"); mounts[parts[1]] = parts[0]
    if a == "-e" and "=" in argv[i + 1]:
        k, v = argv[i + 1].split("=", 1); env[k] = v
env["FAKE_IN_CONTAINER"] = "1"
cmd = argv[argv.index("bash"):]
cmd[1] = mounts[cmd[1]]
sys.exit(subprocess.run(cmd, env=env).returncode)
'''


class PtyMixin:
    """Drive the CLI on a pseudo-terminal: the menus only appear when stdin and stdout are terminals."""

    def drive(self, args, keys, env_extra=None, timeout=30):
        import pty, re, select, time
        env = {'PATH': str(self.bin), 'HOME': str(self.home), 'FAKE_LOG': str(self.log), 'FAKE_STATE': str(self.state_file),
               'LANG': os.environ.get('LANG', 'C.UTF-8'), 'TZ': 'UTC', 'TERM': 'xterm', 'AWS_SURVEY_SSM_POLL': '0'}
        env.update(env_extra or {})
        pid, fd = pty.fork()
        if pid == 0:
            os.chdir(str(self.target))
            os.execve(str(test_cli.CLI), [str(test_cli.CLI), *args], env)
        output = b''
        plain = lambda: re.sub(rb'\x1b\[[0-9;?]*[A-Za-z]', b'', output)

        def read_until(marker, limit=timeout):
            nonlocal output
            deadline = time.time() + limit
            while marker not in plain() and time.time() < deadline:
                ready, _, _ = select.select([fd], [], [], 0.5)
                if ready:
                    try:
                        chunk = os.read(fd, 4096)
                    except OSError:
                        return
                    if not chunk:
                        return
                    output += chunk
            if marker != b'\x00never':
                self.assertIn(marker, plain(), output.decode(errors='replace'))

        for marker, key in keys:
            # '\x00never' は「画面の更新が落ち着くまで少し待つ」の意味（次のキーを送る前）
            read_until(marker.encode(), limit=1 if marker == '\x00never' else timeout)
            if key is None:
                continue
            time.sleep(0.3)
            # 1 キーずつ送る（矢印は 3 バイトで 1 キー）。まとめて送るとメニューの read が取りこぼす
            for one in re.findall(rb'\x1b\[[A-Z]|.', key, re.S):
                try:
                    os.write(fd, one)
                except OSError:
                    self.fail('the CLI exited before the key ' + repr(one) + ' could be sent:\n' + plain().decode(errors='replace'))
                time.sleep(0.2)
        read_until(b'\x00never', limit=10)
        _, status = os.waitpid(pid, 0)
        os.close(fd)
        return os.waitstatus_to_exitcode(status), plain().decode(errors='replace')


class ResourcesCase(test_cli.CliCase):
    def setUp(self):
        super().setUp()
        self.add_tool('docker', FAKE_DOCKER)
        self.add_tool('aws', test_inventory.FAKE_AWS)
        self.state_file = self.base / 'state.json'
        self.set_state(test_inventory.default_state())
        for tool in ('find', 'sleep'):
            found = subprocess.run(['sh', '-c', f'command -v {tool}'], capture_output=True, text=True).stdout.strip()
            if found and not (self.bin / tool).exists():
                (self.bin / tool).symlink_to(found)

    def set_state(self, state):
        self.state_file.write_text(json.dumps(state))

    def ready(self, expiration='2099-01-01T00:00:00+00:00'):
        """Stage 5: environment verified and a valid key on disk."""
        self.write_environment(setup={'route_decided': '2026-09-08', 'role_created': '2026-09-08',
                                      'readonly_verified': '2026-09-08'})
        self.write_keys(expiration=expiration)

    def run_cli(self, *args, **extra):
        extra.setdefault('FAKE_STATE', str(self.state_file))
        return super().run_cli(*args, **extra)

    def docker_runs(self):
        return [c['argv'] for c in self.calls() if c.get('tool') == 'docker' and c['argv'][:1] == ['run']]

    def aws_calls(self):
        return [c for c in self.calls() if 'service' in c]


class Ls(ResourcesCase, PtyMixin):
    def test_lists_from_inside_the_container_with_only_the_key(self):
        self.ready()
        result = self.run_cli('ls')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        (run,) = self.docker_runs()
        self.assertIn('--rm', run)
        self.assertNotIn('-it', run)
        self.assertNotIn('-t', run)
        self.assertIn(f'{self.key_dir()}:/home/node/.aws-claude:ro', run)
        self.assertIn(f'{ROOT}/libexec/inventory.sh:/x/inventory.sh:ro', run)
        mounts = [run[i + 1] for i, a in enumerate(run) if a == '-v']
        self.assertEqual(len(mounts), 2, mounts)
        self.assertNotIn('/home/node/aws-survey/out', ' '.join(run))
        self.assertIn('AWS_DEFAULT_REGION=test-region', run)
        self.assertEqual(run[-4:], ['bash', '/x/inventory.sh', '--region', 'test-region'])
        self.assertIn('EC2 インスタンス（2）', result.stdout)
        self.assertIn('i-0aaaaaaaaaaaaaaaa  web1', result.stdout)
        self.assertIn('ssm Online', result.stdout)
        self.assertIn('Lambda 関数（2）', result.stdout)
        self.assertIn('fn-py', result.stdout)
        self.assertIn('S3 バケット（1）', result.stdout)
        self.assertIn('ロードバランサー（0）', result.stdout)
        self.assertIn('aws-survey ec2', result.stdout)
        self.assertIn('aws-survey lambda', result.stdout)
        self.assertIn('aws-survey scan', result.stdout)          # 棚卸しがまだなら scan、済んでいれば claude / codex
        self.assertNotIn('aws-survey run', result.stdout)

    def test_after_the_scan_the_next_step_is_the_agent(self):
        self.ready()
        config = json.loads((self.target / 'environment.json').read_text())
        config['setup']['scanned'] = '2026-09-12T10:32+0900'; config['agent'] = 'codex'
        (self.target / 'environment.json').write_text(json.dumps(config))
        result = self.run_cli('ls')
        self.assertIn('aws-survey codex', result.stdout)
        self.assertNotIn('aws-survey scan', result.stdout)

    def test_every_aws_call_happens_inside_the_container_and_nothing_is_written(self):
        self.ready()
        self.run_cli('ls')
        calls = self.aws_calls()
        self.assertTrue(calls)
        self.assertTrue(all(c['in_container'] == '1' for c in calls), calls)
        self.assertEqual(sorted(p.name for p in self.target.iterdir()), ['environment.json'])

    def test_a_service_the_key_cannot_read_is_marked_not_dropped(self):
        self.ready()
        result = self.run_cli('ls', FAKE_DENY='rds,ssm')
        self.assertEqual(result.returncode, 1)
        self.assertIn('RDS', result.stdout)
        self.assertIn('読めません', result.stdout)
        self.assertIn('SSM の管理下かどうかは読めませんでした', result.stdout)
        self.assertIn('ECS クラスター（1）', result.stdout)

    def test_service_filter_and_json_passthrough(self):
        self.ready()
        result = self.run_cli('ls', 'ec2', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        (run,) = self.docker_runs()
        self.assertEqual(run[-5:], ['bash', '/x/inventory.sh', '--region', 'test-region', 'ec2'])
        lines = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
        self.assertEqual([line['service'] for line in lines if line['kind'] == 'service'], ['ec2'])
        self.assertEqual([line['id'] for line in lines if line['kind'] == 'item'], ['i-0aaaaaaaaaaaaaaaa', 'i-0bbbbbbbbbbbbbbbb'])

    def test_a_missing_or_expired_key_is_reissued_before_docker(self):
        # 利用者に credentials を打たせない。無ければ発行し、切れていれば発行し直してから、コンテナで読む
        self.write_environment(setup={'route_decided': '2026-09-08', 'role_created': '2026-09-08',
                                      'readonly_verified': '2026-09-08'})
        result = self.run_cli('ls')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('一時キーがありません。一時キーを発行し直します', result.stdout)
        self.assertIn('一時キーを発行しました', result.stdout)
        self.assertNotIn('次に打つコマンド: aws-survey credentials', result.stdout)
        host = [c for c in self.aws_calls() if not c['in_container']]
        self.assertEqual([c['op'] for c in host], ['get-caller-identity', 'get-role', 'assume-role'])
        self.assertTrue((self.key_dir() / 'session.json').exists())
        self.assertEqual(len(self.docker_runs()), 1)
        self.write_keys(expiration='2020-01-01T00:00:00+00:00')
        result = self.run_cli('ls')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('期限切れ', result.stdout)
        self.assertIn('一時キーを発行しました', result.stdout)
        self.assertEqual(len(self.docker_runs()), 2)

    def test_a_short_key_is_reissued_but_a_long_one_is_kept(self):
        from datetime import datetime, timedelta, timezone
        soon = (datetime.now(timezone.utc) + timedelta(minutes=3)).strftime('%Y-%m-%dT%H:%M:%S+00:00')
        self.ready(expiration=soon)
        result = self.run_cli('ls')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r'残りが約 [23] 分です（5 分は要ります）')
        self.assertTrue(any(c['op'] == 'assume-role' for c in self.aws_calls()))
        self.log.unlink()
        self.ready(expiration=(datetime.now(timezone.utc) + timedelta(minutes=7)).strftime('%Y-%m-%dT%H:%M:%S+00:00'))
        result = self.run_cli('ls')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn('発行し直します', result.stdout)
        self.assertFalse(any(c['op'] == 'assume-role' for c in self.aws_calls()))

    def test_unverified_stops_before_aws_and_docker(self):
        self.write_environment(setup={'route_decided': '2026-09-08', 'role_created': '2026-09-08'})
        self.write_keys(expiration='2099-01-01T00:00:00+00:00')
        result = self.run_cli('ls')
        self.assertEqual(result.returncode, 1)
        self.assertIn('まだ確かめていません', result.stdout)
        self.assertIn('次に打つコマンド', result.stdout)
        self.assertEqual(self.docker_runs(), [])
        self.assertEqual(self.aws_calls(), [])

    def test_on_a_terminal_a_spinner_runs_while_listing(self):
        self.ready()
        code, text = self.drive(['ls'], [])
        self.assertEqual(code, 0, text)
        self.assertIn('アカウントのリソースを読んでいます', text)
        self.assertIn('⠋', text)
        tail = text.split('\r')[-1]                       # 回転の行が消えたあとの画面
        self.assertIn('EC2 インスタンス（2）', text)
        self.assertNotIn('読んでいます', tail)

    def test_a_container_that_fails_to_start_is_reported(self):
        self.ready()
        result = self.run_cli('ls', FAKE_DOCKER_FAIL='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('列挙できませんでした', result.stdout)


if __name__ == '__main__':
    unittest.main()


def running_everywhere():
    state = test_inventory.default_state()
    for instance in state['instances']:
        instance['state'] = 'running'
    state['ssm'] = [{'id': i['id'], 'ping': 'Online'} for i in state['instances']]
    return state


class Ec2(ResourcesCase, PtyMixin):
    def registered(self):
        config = json.loads((self.target / 'environment.json').read_text())
        config['ssh'] = {'user': 'diag', 'hosts': {'web1': {'instance_id': 'i-0aaaaaaaaaaaaaaaa', 'user': 'diag',
                                                              'installed_at': '2026-09-10T12:00:00+09:00',
                                                              'logs': {}, 'deny': [], 'strict': False}}}
        (self.target / 'environment.json').write_text(json.dumps(config))

    def test_lists_with_registration_and_ssm_marks_and_guides_when_not_a_terminal(self):
        self.ready()
        self.registered()
        result = self.run_cli('ec2')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        (run,) = self.docker_runs()
        self.assertEqual(run[-5:], ['bash', '/x/inventory.sh', '--region', 'test-region', 'ec2'])
        self.assertIn('EC2 インスタンス（2）', result.stdout)
        self.assertIn('✔ i-0aaaaaaaaaaaaaaaa  web1  running  SSM Online  t3.small  登録済み: web1', result.stdout)
        self.assertIn('－ i-0bbbbbbbbbbbbbbbb  web2  stopped  SSM なし  t3.micro', result.stdout)
        self.assertIn('次に打つコマンド', result.stdout)
        self.assertIn('aws-survey ssh setup <instance-id | Name タグ>', result.stdout)
        self.assertIn('aws-survey ssh verify <host>', result.stdout)
        self.assertIn('aws-survey ssh remove <host>', result.stdout)
        self.assertTrue(all(c['in_container'] == '1' for c in self.aws_calls()))
        self.assertNotIn('send-command', json.dumps(self.calls()))

    def test_without_registration_only_setup_is_offered(self):
        self.ready()
        result = self.run_cli('ec2')
        self.assertNotIn('登録済み', result.stdout)
        self.assertNotIn('ssh remove', result.stdout)
        self.assertIn('aws-survey ssh setup', result.stdout)

    def test_rejects_arguments_and_stops_without_a_key(self):
        self.ready()
        result = self.run_cli('ec2', 'i-0aaaaaaaaaaaaaaaa')
        self.assertEqual(result.returncode, 1)
        self.assertIn('ssh setup', result.stderr)
        self.write_environment(setup={'route_decided': '2026-09-08', 'role_created': '2026-09-08'})
        result = self.run_cli('ec2')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.docker_runs(), [])

    def test_picking_an_unregistered_instance_hands_it_to_ssh_setup(self):
        self.ready()
        self.set_state(running_everywhere())
        code, text = self.drive(['ec2'], [
            ('Enter 決定', b'\x1b[B'),          # ↓ で 2 台目（i-0bbbbbbbbbbbbbbbb）
            ('\x00never', b'\r'),
            ('に何をしますか', b'\r'),           # 「診断ゲートウェイを導入して…」
        ])
        self.assertIn('✔ i-0bbbbbbbbbbbbbbbb  web2  running  SSM Online', text)
        self.assertIn('✔ 診断ゲートウェイを導入して', text)
        self.assertIn('◆ aws-survey ssh setup', text)
        host_side = [c for c in self.aws_calls() if c['in_container'] != '1']
        self.assertEqual(host_side[0]['op'], 'get-caller-identity')
        describe = [c for c in host_side if c['op'] == 'describe-instances']
        self.assertTrue(describe, text)
        self.assertEqual(describe[0]['argv'][describe[0]['argv'].index('--instance-ids') + 1], 'i-0bbbbbbbbbbbbbbbb')
        self.assertNotEqual(code, 0)      # the fake stops ssh setup at the instance lookup; the handoff is what is checked

    def test_picking_a_registered_instance_offers_remove_and_verify(self):
        self.ready()
        self.registered()
        self.set_state(running_everywhere())
        code, text = self.drive(['ec2'], [
            ('Enter 決定', b'\r'),              # 1 台目（web1、登録済み）
            ('に何をしますか', b'\x1b[B\x1b[B'),  # ↓↓ で「設定を外す」
            ('\x00never', b'\r'),
        ])
        self.assertIn('✔ 設定を外す', text)
        self.assertIn('◆ aws-survey ssh remove', text)
        self.assertIn('web1', text)

    def test_a_stopped_instance_cannot_be_set_up(self):
        self.ready()
        code, text = self.drive(['ec2'], [
            ('Enter 決定', b'\x1b[B'),          # i-0bbbbbbbbbbbbbbbb は stopped
            ('\x00never', b'\r'),
        ])
        self.assertEqual(code, 1, text)
        self.assertIn('running で SSM の管理下', text)
        self.assertEqual([c for c in self.aws_calls() if c['in_container'] != '1'], [])


class Lambda(ResourcesCase, PtyMixin):
    def pulled(self, name, at='2026-09-01T00:00:00+09:00'):
        d = self.target / 'code/lambda/test-region' / name
        d.mkdir(parents=True)
        (d / '_manifest.json').write_text(json.dumps({'pulled_at': at, 'CodeSha256': 'x'}))

    def test_lists_with_pulled_marks_and_guides_when_not_a_terminal(self):
        self.ready()
        self.pulled('fn-py')
        result = self.run_cli('lambda')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        (run,) = self.docker_runs()
        self.assertEqual(run[-5:], ['bash', '/x/inventory.sh', '--region', 'test-region', 'lambda'])
        self.assertIn('Lambda 関数（2）', result.stdout)
        self.assertIn('－ fn-node  nodejs20.x  更新 2026-01-01T00:00:00', result.stdout)
        self.assertIn('✔ fn-py  python3.12  更新 2026-01-02T00:00:00  取り出し済み 2026-09-01T00:00:00+09:00', result.stdout)
        self.assertIn('aws-survey lambda pull <関数名>', result.stdout)
        self.assertIn('aws-survey lambda list', result.stdout)
        self.assertIn('aws-survey lambda remove <関数名>', result.stdout)
        self.assertTrue(all(c['in_container'] == '1' for c in self.aws_calls()))

    def test_old_subcommands_still_dispatch(self):
        self.ready()
        result = self.run_cli('lambda', 'list')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('aws-survey lambda list', result.stdout)
        self.assertEqual(self.docker_runs(), [])
        result = self.run_cli('lambda', 'bogus')
        self.assertEqual(result.returncode, 1)
        self.assertIn('不明なサブコマンド', result.stderr)

    def test_picking_a_function_hands_it_to_pull(self):
        self.ready()
        self.add_tool('curl', '#!/bin/sh\nexit 1\n')
        code, text = self.drive(['lambda'], [
            ('Enter 決定', b'\x1b[B'),          # ↓ で fn-py
            ('\x00never', b'\r'),
            ('に何をしますか', b'\r'),           # 「デプロイされたコードを取り出す」
        ])
        self.assertIn('✔ fn-py  python3.12', text)
        self.assertIn('✔ デプロイされたコードを取り出す', text)
        self.assertIn('◆ aws-survey lambda pull', text)
        self.assertIn('関数              fn-py', text)
        host_side = [c for c in self.aws_calls() if c['in_container'] != '1']
        self.assertEqual(host_side[0]['op'], 'get-caller-identity')
        self.assertIn('assume-role', [c['op'] for c in host_side])

    def test_several_functions_are_picked_with_space_and_pulled_together(self):
        self.ready()
        self.add_tool('curl', '#!/bin/sh\nexit 1\n')
        code, text = self.drive(['lambda'], [
            ('Space 選ぶ', b' \x1b[B '),          # fn-node に印、↓、fn-py に印
            ('\x00never', b'\r'),
            ('選んだ 2 関数 に何をしますか', b'\r'),
        ])
        self.assertIn('✔ fn-node  nodejs20.x', text)
        self.assertIn('✔ fn-py  python3.12', text)
        self.assertIn('◆ aws-survey lambda pull', text)
        self.assertIn('関数              fn-node fn-py', text)

    def test_picking_a_pulled_function_can_remove_it(self):
        self.ready()
        self.pulled('fn-py')
        code, text = self.drive(['lambda'], [
            ('Enter 決定', b'\x1b[B'),
            ('\x00never', b'\r'),
            ('に何をしますか', b'\x1b[B'),       # ↓ で「取り出したコードを消す」
            ('\x00never', b'\r'),
        ])
        self.assertEqual(code, 0, text)
        self.assertIn('✔ 取り出したコードを消す', text)
        self.assertIn('消しました: lambda/test-region/fn-py', text)
        self.assertFalse((self.target / 'code/lambda/test-region/fn-py').exists())
