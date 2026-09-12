"""`aws-survey scan` (the non-interactive inventory) and `aws-survey claude` / `codex`, with a fake `docker`.

scan launches the survey image with the same mounts as `run` but no terminal, hands the agent one
instruction - do the blank inventory, ask the user nothing - and records the result in environment.json.
The fake docker plays the parts scan relies on: `ps` for a running survey container, a read-only peek
into the agent's volume for its login, the interactive login itself, and the agent run that leaves files
in out/. Nothing here reaches AWS; the host side never calls `aws` except through `credentials`.
"""
import json
import os
from pathlib import Path
import re
import shutil
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_cli                                  # noqa: E402
from test_resources import PtyMixin              # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

FAKE_DOCKER = r'''#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps(["docker"] + argv) + "\n")
state_path = os.environ.get("FAKE_STATE")
state = json.load(open(state_path)) if state_path and os.path.exists(state_path) else {}
def save():
    json.dump(state, open(state_path, "w"))
if argv[:1] == ["ps"]:
    running = os.environ.get("FAKE_RUNNING", "")
    flt = [a for a in argv if a.startswith("name=")]
    if running and flt and flt[0] == "name=^/%s$" % running:
        print("abc123")
    sys.exit(0)
if argv[:1] != ["run"]:
    sys.exit(0)
image_i = next(i for i, a in enumerate(argv) if a.endswith(":latest"))
cmd = argv[image_i + 1 :]
mounts = {}
for i, a in enumerate(argv):
    if a == "-v":
        src, dst = argv[i + 1].split(":")[:2]; mounts[dst] = src
if cmd[:2] == ["bash", "/x/inventory.sh"]:
    # the lent listing that scan uses to size the survey (Cost Explorer plus the resource services); canned answers
    assert "-it" not in argv and not any("aws-survey/out" in a for a in argv), "the estimate must not see out/"
    if os.environ.get("FAKE_INVENTORY"):
        for line in json.loads(os.environ["FAKE_INVENTORY"]):
            print(json.dumps(line))
    sys.exit(0)
if cmd[:3] == ["claude", "auth", "status"] or cmd[:3] == ["codex", "login", "status"]:
    assert "-it" not in argv
    assert not any(":ro" in a and ".aws-claude" in a for a in argv), "the status check must not carry the key"
    sys.exit(0 if cmd[0] in state.get("auth", []) else 1)
if cmd[:3] == ["codex", "login", "--device-auth"] or cmd[:3] == ["claude", "auth", "login"]:
    state.setdefault("auth", []).append(cmd[0]); save()
    print("logged in"); sys.exit(0)
if cmd[:2] == ["sh", "-c"] and "hasTrustDialogAccepted" in cmd[2]:
    state["trusted"] = True; save(); sys.exit(0)
if (cmd[0] == "claude" and "-p" in cmd) or (cmd[0] == "codex" and "exec" in cmd):
    assert "-it" not in argv and "-t" not in argv, "scan must not allocate a terminal"
    if cmd[0] == "claude":
        assert state.get("trusted"), "claude -p needs the workspace trusted first, or the allow list is ignored"
    if os.environ.get("FAKE_SCAN_NOT_LOGGED_IN"):
        print("Not logged in. Please run /login"); sys.exit(1)
    out = mounts["/home/node/aws-survey/out"]
    phase = next(a.split("=", 1)[1] for a in argv if a.startswith("SURVEY_PHASE_DIR="))
    if os.environ.get("FAKE_SCAN_WRITES"):
        os.makedirs(os.path.join(out, "_環境"), exist_ok=True)
        os.makedirs(os.path.join(out, phase, "raw"), exist_ok=True)
        os.makedirs(os.path.join(out, phase, "log"), exist_ok=True)
        open(os.path.join(out, "_環境", "00_動作確認.md"), "w").write("確認した\n")
        for n in ("raw-a.json", "raw-b.json"):
            open(os.path.join(out, phase, "raw", n), "w").write("{}\n")
        open(os.path.join(out, phase, "log", "01_棚卸し.md"), "w").write("# 棚卸し\n\n## 見つけたもの\n\n- 2 件\n\n## 聞きたいこと\n\n- 用途\n")
    if os.environ.get("FAKE_SCAN_SLOW"):
        import time; time.sleep(float(os.environ["FAKE_SCAN_SLOW"]))
    print("inventory in progress"); sys.exit(int(os.environ.get("FAKE_SCAN_EXIT", "0")))
sys.exit(0)
'''


def iso(dt):
    return dt.strftime('%Y-%m-%dT%H:%M:%S+00:00')


class ScanCase(test_cli.CliCase, PtyMixin):
    def setUp(self):
        super().setUp()
        self.add_tool('docker', FAKE_DOCKER)
        self.state_file = self.base / 'state.json'
        self.state_file.write_text('{}')
        for tool in ('find',):                    # the progress line and the closing summary look at out/ with find
            found = shutil.which(tool)
            if found and not (self.bin / tool).exists():
                (self.bin / tool).symlink_to(found)

    def ready(self, minutes=45, setup=None, agent=None):
        # agent は古い形（文字列。名前だけ）でも {name, model, effort} でも渡せる
        self.write_environment(setup={'route_decided': '2026-09-08', 'role_created': '2026-09-08',
                                      'readonly_verified': '2026-09-08', **(setup or {})})
        if agent:
            config = json.loads((self.target / 'environment.json').read_text())
            config['agent'] = agent
            (self.target / 'environment.json').write_text(json.dumps(config))
        self.write_keys(iso(datetime.now(timezone.utc) + timedelta(minutes=minutes)))

    def authed(self, *files):
        self.state_file.write_text(json.dumps({'auth': list(files)}))

    def run_cli(self, *args, **extra):
        extra.setdefault('FAKE_STATE', str(self.state_file))
        return super().run_cli(*args, **extra)

    def docker_runs(self):
        return [c for c in self.calls() if c[:2] == ['docker', 'run']]

    def agent_run(self):
        runs = [c for c in self.docker_runs() if ('claude' in c and '-p' in c) or ('codex' in c and 'exec' in c)]
        return runs[-1] if runs else None

    def status_checks(self):
        return [c for c in self.docker_runs() if c[-3:] == ['claude', 'auth', 'status', '--json'][:3] or c[-3:] == ['codex', 'login', 'status']]

    def environment(self):
        return json.loads((self.target / 'environment.json').read_text())


class Scan(ScanCase):
    def test_runs_claude_print_mode_with_the_survey_mounts_and_no_tty(self):
        self.ready()
        self.authed('claude')
        result = self.run_cli('scan', '--agent', 'claude', FAKE_SCAN_WRITES='1')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        launch = self.agent_run()
        self.assertIn('--rm', launch)
        self.assertNotIn('-it', launch)
        self.assertNotIn('-t', launch)
        self.assertEqual(launch[launch.index('--name') + 1], 'smoke-scan')
        for mount in [f'{self.home}/.aws-survey/smoke:/home/node/.aws-claude:ro',
                      f'{ROOT}/container/instructions/survey-claude.md:/home/node/aws-survey/CLAUDE.md:ro',
                      f'{ROOT}/container/instructions/survey-agents.md:/home/node/aws-survey/AGENTS.md:ro',
                      f'{ROOT}/container/method:/home/node/aws-survey/method:ro',
                      f'{self.target}/out:/home/node/aws-survey/out',
                      f'{self.target}/code:/home/node/aws-survey/code:ro',
                      'smoke-codex:/home/node/.codex', 'smoke-claude:/home/node/.claude', 'smoke-cli:/home/node/.local']:
            self.assertIn(mount, launch)
        self.assertIn('SURVEY_PHASE_DIR=01_基礎調査', launch)
        image = launch.index('smoke:latest')
        # モデルと effort は environment.json の agent から（未定なら既定の opus / medium）
        self.assertEqual(launch[image + 1:image + 7], ['claude', '--model', 'opus', '--effort', 'medium', '-p'])
        self.assertEqual(launch[image + 8:], ['--output-format', 'text'])
        self.assertIn('inventory in progress', result.stdout)           # the agent's output is streamed
        self.assertIn('初期調査を終えました（', result.stdout)
        # what the run left in out/, by place: the raw files by count and name, the inventory note by its headings
        self.assertIn('生データ 2 件  01_基礎調査/raw/', result.stdout)
        self.assertIn('raw-a.json raw-b.json', result.stdout)
        self.assertIn('01_基礎調査/log/01_棚卸し.md', result.stdout)
        self.assertIn('・見つけたもの', result.stdout)
        self.assertIn('・聞きたいこと', result.stdout)
        self.assertIn('環境の確認の記録  _環境/00_動作確認.md', result.stdout)
        self.assertNotIn('白紙', result.stdout)
        self.assertNotIn('棚卸し中', result.stdout)
        self.assertIn('aws-survey claude', result.stdout)               # the next step is the conversation
        # off a terminal there is no progress line and no sizing run: the only container runs are the login checks and the agent
        self.assertFalse(any('/x/inventory.sh' in c for c in self.docker_runs()))

    def test_the_opening_line_says_what_is_surveyed_in_one_breath(self):
        self.ready(agent='claude')
        self.authed('claude')
        result = self.run_cli('scan')
        self.assertIn('◆ 初期調査（Claude Code）', result.stdout)
        self.assertIn('対象アカウントに何があるかを洗い出し、リソースの一覧と気づいたことを out/ に書きます。質問はしないので、待つだけです。', result.stdout)
        self.assertNotIn('見つけたものと聞きたいことを out/ に残して終わります', result.stdout)

    def test_when_nothing_was_written_the_summary_says_so(self):
        self.ready(agent='claude')
        self.authed('claude')
        result = self.run_cli('scan')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('生データ（01_基礎調査/raw/）は書かれていません', result.stdout)
        self.assertIn('01_棚卸し.md）は書かれていません', result.stdout)

    def test_the_recorded_model_and_effort_are_passed_as_flags(self):
        self.ready(agent={'name': 'claude', 'model': 'sonnet', 'effort': 'high'})
        self.authed('claude')
        result = self.run_cli('scan')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        launch = self.agent_run()
        image = launch.index('smoke:latest')
        self.assertEqual(launch[image + 1:image + 7], ['claude', '--model', 'sonnet', '--effort', 'high', '-p'])
        self.assertIn('エージェント: Claude Code（sonnet / high）', result.stdout)
        self.assertEqual(self.environment()['agent'], {'name': 'claude', 'model': 'sonnet', 'effort': 'high'})
        # 別のエージェントに切り替えると、そのエージェントの既定に戻る（前の値は渡せない）
        self.authed('claude', 'codex')
        result = self.run_cli('scan', '--agent', 'codex')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.environment()['agent'], {'name': 'codex', 'model': 'gpt-5.6-sol', 'effort': 'low'})
        self.assertIn('model_reasoning_effort=low', self.agent_run())

    def test_a_bad_effort_in_environment_is_refused_before_docker(self):
        self.ready(agent={'name': 'codex', 'model': 'gpt-5.6-sol', 'effort': 'max'})
        self.authed('codex')
        result = self.run_cli('scan')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('agent.effort', result.stderr)
        self.assertFalse(any(c[0] == 'docker' for c in self.calls()))

    def test_the_instruction_hands_over_no_survey_items(self):
        self.ready()
        self.authed('claude')
        self.run_cli('scan', '--agent', 'claude')
        launch = self.agent_run()
        prompt = launch[launch.index('-p') + 1]
        self.assertIn('棚卸し', prompt)
        self.assertIn('応答できません', prompt)
        self.assertIn('method/04', prompt)
        for word in ['EC2', 'Lambda', 'VPC', 'S3', 'RDS', 'Cost', 'CloudFront', 'aws-survey', 'libexec']:
            self.assertNotIn(word, prompt, f'{word} must not be handed to the agent from the host')

    def test_agent_codex_uses_exec_and_is_remembered(self):
        self.ready()
        self.authed('codex')
        result = self.run_cli('scan', '--agent', 'codex')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        launch = self.agent_run()
        self.assertEqual(launch[launch.index('codex'):launch.index('codex') + 9],
                         ['codex', '-m', 'gpt-5.6-sol', '-c', 'model_reasoning_effort=low', 'exec', '--skip-git-repo-check', '--color', 'never'])
        self.assertEqual(self.environment()['agent'], {'name': 'codex', 'model': 'gpt-5.6-sol', 'effort': 'low'})
        self.assertIn('aws-survey codex', result.stdout)
        self.log.unlink()
        result = self.run_cli('scan')                                   # no --agent: the remembered one
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('exec', self.agent_run())
        check = self.status_checks()[0]                                 # the CLI itself is asked, with its volumes only
        self.assertEqual(check[-3:], ['codex', 'login', 'status'])
        self.assertIn('smoke-codex:/home/node/.codex', check)
        self.assertFalse(any('aws-survey/out' in a or '.aws-claude' in a for a in check))

    def test_a_named_agent_overrides_and_replaces_the_memory(self):
        self.ready(agent='codex')
        self.authed('claude')
        result = self.run_cli('scan', '--agent', 'claude')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('-p', self.agent_run())
        self.assertEqual(self.environment()['agent']['name'], 'claude')

    def test_without_a_recorded_agent_off_a_terminal_it_stops_naming_the_flag(self):
        self.ready()
        result = self.run_cli('scan')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('aws-survey scan --agent claude', result.stdout)
        self.assertFalse(any(c[0] == 'docker' for c in self.calls()))
        self.assertIsNone(self.environment()['agent']['name'])

    def test_on_a_terminal_the_agent_is_chosen_with_arrow_keys(self):
        self.ready()
        self.authed('codex')
        code, text = self.drive(['scan'], [('エージェントを選んでください', b'\x1b[B\r')])
        self.assertEqual(code, 0, text)
        self.assertIn('エージェント: Codex（gpt-5.6-sol / low）', text)
        self.assertEqual(self.environment()['agent']['name'], 'codex')
        self.assertIn('exec', self.agent_run())

    INVENTORY = [
        {'kind': 'service', 'service': 'cost', 'region': 'global', 'status': 'ok', 'count': 4},
        {'kind': 'item', 'service': 'cost', 'region': 'global', 'id': 'Amazon Elastic Compute Cloud - Compute', 'name': '', 'state': '', 'extra': {'amount': '12.3'}},
        {'kind': 'item', 'service': 'cost', 'region': 'global', 'id': 'Amazon Relational Database Service', 'name': '', 'state': '', 'extra': {'amount': '30'}},
        {'kind': 'item', 'service': 'cost', 'region': 'global', 'id': 'AWS Lambda', 'name': '', 'state': '', 'extra': {'amount': '0.5'}},
        {'kind': 'item', 'service': 'cost', 'region': 'global', 'id': 'Tax', 'name': '', 'state': '', 'extra': {'amount': '4'}},
        {'kind': 'service', 'service': 'ec2', 'region': 'test-region', 'status': 'ok', 'count': 2},
        {'kind': 'service', 'service': 'lambda', 'region': 'test-region', 'status': 'ok', 'count': 7},
        {'kind': 'service', 'service': 'vpc', 'region': 'test-region', 'status': 'ok', 'count': 1},
        {'kind': 'service', 'service': 's3', 'region': 'global', 'status': 'denied', 'error': 'AccessDenied'},
        {'kind': 'service', 'service': 'rds', 'region': 'test-region', 'status': 'ok', 'count': 0},
    ]

    def test_on_a_terminal_the_survey_is_sized_first_and_progress_is_a_bar(self):
        self.ready(agent='claude')
        self.authed('claude')
        code, text = self.drive(['scan'], [], env_extra={'FAKE_INVENTORY': json.dumps(self.INVENTORY),
                                                         'FAKE_SCAN_WRITES': '1', 'FAKE_SCAN_SLOW': '2.5'})
        self.assertEqual(code, 0, text)
        runs = self.docker_runs()
        sizing = next(c for c in runs if '/x/inventory.sh' in c)
        self.assertEqual(sizing[-10:], ['bash', '/x/inventory.sh', '--region', 'test-region', 'cost', 'ec2', 'lambda', 'vpc', 's3', 'rds', 'ecs', 'elb', 'cloudfront'][-10:])
        self.assertLess(runs.index(sizing), runs.index(self.agent_run()))
        self.assertFalse(any('aws-survey/out' in a for a in sizing))   # the sizing run reads with the key only
        # 3 billed services (Tax is not one), 3 services with resources + 1 unreadable = 4, 10 resources → 4 + 8 + 2 = 14 files;
        # 150 s + 50 s a file, plus 120 s for the first-time environment check = 16 min 10 s
        self.assertIn('見積もり: 課金のあるサービス 3、リソース 10 件、読めないサービス 1。目安は 16 分 10 秒ほど', text)
        self.assertIn('初期調査中', text)
        self.assertRegex(text, r'[█░]{20} +\d+%  経過 \d+ 秒')
        self.assertNotIn('棚卸し中', text)
        self.assertIn('初期調査を終えました（', text)
        self.assertIn('生データ 2 件', text)

    def test_on_a_terminal_an_unreadable_account_falls_back_to_a_generic_estimate(self):
        self.ready(agent='claude')
        self.authed('claude')
        code, text = self.drive(['scan'], [], env_extra={'FAKE_SCAN_SLOW': '1.5'})
        self.assertEqual(code, 0, text)
        self.assertIn('調査の量を見積もれなかったので、一般的な目安（12 分 30 秒ほど）で進捗を出します', text)
        self.assertIn('初期調査中', text)
        self.assertIn('%', text)

    def test_unauthenticated_off_a_terminal_stops_before_the_agent(self):
        self.ready(agent='claude')
        result = self.run_cli('scan')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('aws-survey login', result.stdout + result.stderr)
        self.assertIsNone(self.agent_run())
        self.assertIsNone(self.environment()['setup']['scanned'])

    def test_unauthenticated_on_a_terminal_logs_in_first(self):
        self.ready(agent='codex')
        code, text = self.drive(['scan'], [])
        self.assertEqual(code, 0, text)
        self.assertIn('ログインがまだです', text)
        self.assertIn('ログインしました', text)
        runs = self.docker_runs()
        login = next(c for c in runs if c[-3:] == ['codex', 'login', '--device-auth'])
        self.assertIn('-it', login)
        self.assertEqual(login[login.index('--name') + 1], 'smoke-login')
        self.assertLess(runs.index(login), runs.index(self.agent_run()))
        for c in runs:
            if c is not login:
                self.assertNotIn('-it', c)
        # the second check, after the login, is what lets the scan proceed
        self.assertEqual(len(self.status_checks()), 2)

    def test_claude_login_uses_auth_login_then_trusts_the_workspace(self):
        self.ready(agent='claude')
        code, text = self.drive(['scan'], [])
        self.assertEqual(code, 0, text)
        runs = self.docker_runs()
        login = next(c for c in runs if c[-3:] == ['claude', 'auth', 'login'])
        self.assertIn('-it', login)
        self.assertEqual(login[login.index('--name') + 1], 'smoke-login')
        trust = next(c for c in runs if any('hasTrustDialogAccepted' in a for a in c))
        self.assertLess(runs.index(login), runs.index(trust))
        self.assertLess(runs.index(trust), runs.index(self.agent_run()))
        self.assertNotIn('-it', trust)

    def test_codex_needs_no_trust_record(self):
        self.ready(agent='codex')
        self.authed('codex')
        result = self.run_cli('scan')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(any(any('hasTrustDialogAccepted' in a for a in c) for c in self.docker_runs()))

    def test_a_short_key_names_credentials_off_a_terminal(self):
        self.ready(minutes=10, agent='claude')
        self.authed('claude')
        result = self.run_cli('scan')
        self.assertNotEqual(result.returncode, 0)
        self.assertRegex(result.stdout, r'残りが約 (9|10) 分')
        self.assertIn('aws-survey credentials', result.stdout)
        self.assertFalse(any(c[0] == 'docker' for c in self.calls()))

    def test_a_short_key_is_reissued_on_a_yes(self):
        self.ready(minutes=10, agent='claude')
        self.authed('claude')
        code, text = self.drive(['scan'], [('発行し直しますか', b'y\r')])
        self.assertEqual(code, 0, text)
        calls = self.calls()
        assume = next(i for i, c in enumerate(calls) if c[0] == 'aws' and 'assume-role' in c)
        first_docker = next(i for i, c in enumerate(calls) if c[0] == 'docker')
        self.assertLess(assume, first_docker)
        self.assertIsNotNone(self.agent_run())

    def test_refuses_while_the_survey_container_runs(self):
        self.ready(agent='claude')
        self.authed('claude')
        for name in ('smoke', 'smoke-scan'):
            self.log.unlink(missing_ok=True)
            result = self.run_cli('scan', FAKE_RUNNING=name)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('同じ out/ に 2 つのエージェント', result.stderr)
            self.assertIsNone(self.agent_run())

    def test_success_records_scanned_and_container_verified(self):
        self.ready(agent='claude')
        self.authed('claude')
        result = self.run_cli('scan', FAKE_SCAN_WRITES='1')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        env = self.environment()
        self.assertRegex(env['setup']['scanned'], r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}[+-]\d{4}$')
        self.assertRegex(env['setup']['container_verified'], r'^\d{4}-\d{2}-\d{2}$')
        guide = self.run_cli()
        self.assertIn('● 6 ', guide.stdout)
        self.assertIn('✔ 5 初期調査', guide.stdout)

    def test_failure_records_nothing_and_names_the_retry(self):
        self.ready(agent='claude')
        self.authed('claude')
        result = self.run_cli('scan', FAKE_SCAN_EXIT='3')
        self.assertEqual(result.returncode, 3)
        self.assertIn('途中で終わりました', result.stdout)
        self.assertIn('もう一度', result.stdout)
        self.assertIsNone(self.environment()['setup']['scanned'])

    def test_an_expired_login_is_named(self):
        self.ready(agent='claude')
        self.authed('claude')
        result = self.run_cli('scan', FAKE_SCAN_NOT_LOGGED_IN='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('ログインが切れている', result.stdout)
        self.assertIn('aws-survey claude', result.stdout)

    def test_nothing_is_written_outside_out_code_and_environment(self):
        self.ready(agent='claude')
        self.authed('claude')
        self.run_cli('scan', FAKE_SCAN_WRITES='1')
        self.assertEqual(sorted(p.name for p in self.target.iterdir()), ['code', 'environment.json', 'out'])
        self.assertTrue((self.target / 'out/01_基礎調査/raw').is_dir())
        self.assertFalse((self.target / 'out/_環境/scan.log').exists())


class Login(ScanCase):
    """`aws-survey login` settles the agent's login on its own (init calls it last), with no key and no survey mounts."""

    def test_authenticated_off_a_terminal_reports_and_needs_no_key(self):
        self.write_environment()                                        # 一時キーも到達点も無い
        config = json.loads((self.target / 'environment.json').read_text())
        config['agent'] = {'name': 'codex', 'model': 'gpt-5.6-sol', 'effort': 'low'}
        (self.target / 'environment.json').write_text(json.dumps(config))
        self.authed('codex')
        result = self.run_cli('login')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Codex の認証は済んでいます', result.stdout)
        self.assertIn('エージェント      Codex（gpt-5.6-sol / low）', result.stdout)
        for c in self.docker_runs():
            self.assertFalse(any('.aws-claude' in a or 'aws-survey/out' in a for a in c))
        self.assertIsNone(self.agent_run())

    def test_unauthenticated_off_a_terminal_names_login(self):
        self.ready(agent='claude')
        result = self.run_cli('login')
        self.assertEqual(result.returncode, 1)
        self.assertIn('aws-survey login', result.stdout)
        self.assertFalse(any(any('hasTrustDialogAccepted' in a for a in c) for c in self.docker_runs()))

    def test_unauthenticated_on_a_terminal_logs_in_and_trusts(self):
        self.ready(agent='claude')
        code, text = self.drive(['login'], [])
        self.assertEqual(code, 0, text)
        self.assertIn('Claude Code にログインしました', text)
        runs = self.docker_runs()
        login = next(c for c in runs if c[-3:] == ['claude', 'auth', 'login'])
        self.assertIn('-it', login)
        self.assertTrue(any(any('hasTrustDialogAccepted' in a for a in c) for c in runs))

    def test_a_named_agent_replaces_the_memory(self):
        self.ready(agent='claude')
        self.authed('codex')
        result = self.run_cli('login', 'codex')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.environment()['agent'], {'name': 'codex', 'model': 'gpt-5.6-sol', 'effort': 'low'})

    def test_without_an_agent_it_names_the_choice(self):
        self.ready()
        result = self.run_cli('login')
        self.assertEqual(result.returncode, 1)
        self.assertIn('aws-survey login claude', result.stdout)
        self.assertFalse(any(c[0] == 'docker' for c in self.calls()))


class InteractiveAgent(ScanCase):
    def test_claude_dispatches_to_run_with_the_agent_and_records_it(self):
        self.ready()
        code, text = self.drive(['claude', '-c'], [])
        self.assertEqual(code, 0, text)
        launch = self.docker_runs()[-1]
        self.assertIn('-it', launch)
        self.assertEqual(launch[launch.index('--name') + 1], 'smoke')
        self.assertEqual(launch[-7:], ['smoke:latest', 'claude', '--model', 'opus', '--effort', 'medium', '-c'])
        self.assertIn(f'{self.target}/out:/home/node/aws-survey/out', launch)
        self.assertEqual(self.environment()['agent'], {'name': 'claude', 'model': 'opus', 'effort': 'medium'})
        self.assertIn('◆ aws-survey claude', text)

    def test_codex_replaces_the_remembered_agent(self):
        self.ready(agent='claude')
        code, text = self.drive(['codex'], [])
        self.assertEqual(code, 0, text)
        self.assertEqual(self.docker_runs()[-1][-6:], ['smoke:latest', 'codex', '-m', 'gpt-5.6-sol', '-c', 'model_reasoning_effort=low'])
        self.assertEqual(self.environment()['agent'], {'name': 'codex', 'model': 'gpt-5.6-sol', 'effort': 'low'})

    def test_off_a_terminal_it_stops(self):
        self.ready()
        result = self.run_cli('claude')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('端末で実行してください', result.stderr)
        self.assertFalse(any(c[0] == 'docker' for c in self.calls()))

    def test_an_expired_key_names_credentials(self):
        self.ready(minutes=-5)
        code, text = self.drive(['claude'], [])
        self.assertNotEqual(code, 0)
        self.assertIn('aws-survey credentials', text)
        self.assertFalse(any(c[0] == 'docker' for c in self.calls()))


if __name__ == '__main__':
    unittest.main()
