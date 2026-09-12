"""Check the aws-survey dispatcher without Docker, AWS, or the user's real config.

Every test runs with a fake HOME and a minimal PATH built from symlinks, so real keys,
real profiles and real tools are never touched. `aws` and `docker` are stub scripts
that only log their arguments; the state table must be decided from files alone.
"""
import json
import re
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / 'bin/aws-survey'

# Tools the scripts need besides bash builtins. Anything not on this list is absent.
BASE_TOOLS = ['bash', 'sh', 'env', 'jq', 'date', 'stat', 'mktemp', 'uname', 'tr', 'sed', 'grep',
              'dirname', 'basename', 'readlink', 'cat', 'rm', 'mkdir', 'chmod', 'cp', 'mv',
              'python3', 'head', 'tail', 'tee', 'cut', 'sort', 'wc', 'ls', 'awk', 'printf', 'echo',
              'test', 'touch', 'sleep']

FAKE_AWS = '''#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps(["aws"] + argv) + "\\n")
if argv[:2] == ["configure", "list-profiles"]:
    print(os.environ.get("FAKE_PROFILES", "fake-src\\nsso-prof").replace("\\\\n", "\\n"))
elif argv[:2] == ["configure", "get"]:
    key = argv[2]; prof = argv[argv.index("--profile") + 1]
    if key == "region":
        print("ap-northeast-1")
    elif key in ("sso_session", "sso_start_url") and prof == "sso-prof":
        print("fake-sso")
    else:
        sys.exit(1)
elif "get-caller-identity" in argv:
    if os.environ.get("FAKE_CALLER_FAIL"):
        sys.stderr.write("Error when retrieving token from sso: Token has expired\\n"); sys.exit(255)
    arn = os.environ.get("FAKE_CALLER_ARN", "arn:aws:iam::000000000000:user/fake")
    print(arn if "--query" in argv else json.dumps({"Arn": arn, "Account": "000000000000"}))
elif "get-role" in argv:
    name = argv[argv.index("--role-name") + 1] if "--role-name" in argv else ""
    if name.startswith("AWSReservedSSO_"):
        if os.environ.get("FAKE_NO_GET_ROLE"):
            sys.exit(254)
        print("arn:aws:iam::000000000000:role/aws-reserved/sso.amazonaws.com/test-region/" + name)
    elif "json" in argv:
        statement = {"Effect": "Allow", "Action": "sts:AssumeRole",
                     "Principal": {"AWS": os.environ.get("FAKE_TRUST_PRINCIPAL", "arn:aws:iam::000000000000:user/fake")}}
        if not os.environ.get("FAKE_TRUST_NO_MFA"):
            statement["Condition"] = {"Bool": {"aws:MultiFactorAuthPresent": "true"}}
        print(json.dumps({"Role": {"Arn": "arn:aws:iam::000000000000:role/fake-role",
                                   "MaxSessionDuration": int(os.environ.get("FAKE_MAX_SESSION", "43200")),
                                   "AssumeRolePolicyDocument": {"Statement": [statement]}}}))
    else:
        print("arn:aws:iam::000000000000:role/fake-role")
elif argv[:2] == ["iam", "list-attached-role-policies"]:
    print("arn:aws:iam::aws:policy/ReadOnlyAccess")
elif "simulate-principal-policy" in argv:
    print("iam:CreateRole\\tallowed\\nec2:DescribeVpcs\\tallowed")
elif "assume-role" in argv:
    if os.environ.get("FAKE_ASSUME_CHAINING"):
        sys.stderr.write("An error occurred (ValidationError) when calling the AssumeRole operation: The requested DurationSeconds exceeds the 1 hour session limit for roles assumed by role chaining.\\n")
        sys.exit(254)
    print(json.dumps({"Credentials": {"AccessKeyId": "AKIAFAKE", "SecretAccessKey": "fake",
                                      "SessionToken": "fake", "Expiration": "2099-01-01T00:00:00+00:00"},
                      "AssumedRoleUser": {"Arn": "arn:aws:sts::000000000000:assumed-role/fake-role/x"},
                      "PackedPolicySize": 42}))
else:
    print("{}")
'''

FAKE_DOCKER = '''#!/usr/bin/env python3
import json, os, sys
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps(["docker"] + sys.argv[1:]) + "\\n")
'''

# Identity Center でログインしたときの身元と、その権限セットのロール（パス付き）
SSO_CALLER = 'arn:aws:sts::000000000000:assumed-role/AWSReservedSSO_AdministratorAccess_0123456789abcdef/alice'
SSO_ROLE = ('arn:aws:iam::000000000000:role/aws-reserved/sso.amazonaws.com/test-region/'
            'AWSReservedSSO_AdministratorAccess_0123456789abcdef')


def iso(dt):
    return dt.strftime('%Y-%m-%dT%H:%M:%S+00:00')


class CliCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.home = self.base / 'home'
        self.home.mkdir()
        self.target = self.base / 'target'
        self.target.mkdir()
        self.bin = self.base / 'bin'
        self.bin.mkdir()
        for tool in BASE_TOOLS:
            found = shutil.which(tool)
            if found:
                (self.bin / tool).symlink_to(found)
        self.log = self.base / 'calls.jsonl'
        self.add_tool('aws', FAKE_AWS)
        self.add_tool('docker', FAKE_DOCKER)

    def tearDown(self):
        self.temp.cleanup()

    def add_tool(self, name, body):
        path = self.bin / name
        path.write_text(body)
        path.chmod(0o755)

    def remove_tool(self, name):
        (self.bin / name).unlink()

    def write_environment(self, setup=None, name='smoke', template=False):
        config = json.loads((ROOT / 'templates/environment.json').read_text())
        if not template:
            config.update(name=name, account_id='000000000000', region='test-region')
            config['auth'].update(route='own_role', source_profile='fake-src',
                                  principal_arn='arn:aws:iam::000000000000:user/fake',
                                  role_name='fake-role', refresh_command=None, duration_seconds=3600)
        if setup:
            config['setup'].update(setup)
        (self.target / 'environment.json').write_text(json.dumps(config))

    def key_dir(self, name='smoke'):
        path = self.home / '.aws-survey' / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def write_keys(self, expiration=None, duration=3600, session=True):
        keys = self.key_dir()
        (keys / 'credentials').write_text('[claude-ro]\naws_access_key_id = AKIAFAKE\n')
        if session:
            (keys / 'session.json').write_text(json.dumps({'expiration': expiration, 'duration_seconds': duration}))
        return keys

    def run_cli(self, *args, cwd=None, cli=None, input='', **extra):
        env = {'PATH': str(self.bin), 'HOME': str(self.home), 'FAKE_LOG': str(self.log),
               'LANG': os.environ.get('LANG', 'C.UTF-8'), 'LC_ALL': os.environ.get('LC_ALL', ''), 'TZ': 'UTC'}
        env = {k: v for k, v in env.items() if v}
        env.update(extra)
        return subprocess.run([str(cli or CLI), *args], cwd=str(cwd or self.target), env=env,
                              capture_output=True, text=True, input=input)

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def assert_stage(self, result, stage, *phrases):
        self.assertEqual(result.returncode, 0 if stage else 1, result.stdout + result.stderr)
        self.assertIn(f'● {stage} ', result.stdout)
        for phrase in phrases:
            self.assertIn(phrase, result.stdout)


class StateTable(CliCase):
    def test_stage_0_missing_docker(self):
        self.remove_tool('docker')
        self.write_environment()
        self.assert_stage(self.run_cli(), 0, 'docker')
        self.assertNotIn('aws が見つかりません', self.run_cli().stdout)

    def test_stage_1_no_environment_asks_then_guides_on_no(self):
        result = self.run_cli(input='n\n')
        self.assert_stage(result, 1, 'environment.json がありません', 'init を実行しますか', 'aws-survey init')
        self.assertFalse((self.target / 'environment.json').exists())

    def test_stage_1_without_stdin_guides_only(self):
        result = self.run_cli(input='')
        self.assert_stage(result, 1, 'aws-survey init')
        self.assertFalse((self.target / 'environment.json').exists())

    def test_stage_1_yes_runs_init(self):
        # y, name, profile(1=fake-src), account, mfa, region, duration, role
        answers = ['y', 'demo', '1', '', '', '', '1800', '']
        result = self.run_cli(input='\n'.join(answers) + '\n')
        self.assert_stage(result, 1, '書き出しました')
        config = json.loads((self.target / 'environment.json').read_text())
        self.assertEqual(config['name'], 'demo')
        self.assertEqual(config['auth']['duration_seconds'], 1800)

    def test_stage_1_template_left(self):
        self.write_environment(template=True)
        self.assert_stage(self.run_cli(), 1, 'TEMPLATE-')

    def test_stage_1_broken_json(self):
        (self.target / 'environment.json').write_text('{')
        self.assert_stage(self.run_cli(), 1, '壊れています')

    def test_stage_2_route_and_role_unset(self):
        self.write_environment()
        # ロールが既にある場合にも合う案内（「作ります」と言い切らない）
        self.assert_stage(self.run_cli(), 2, 'aws-survey role', 'role --create',
                          'あれば environment.json に合わせて整えます')
        self.write_environment(setup={'route_decided': '2026-09-08'})
        self.assert_stage(self.run_cli(), 2, 'aws-survey role')

    def test_stage_3_not_verified_without_keys(self):
        self.write_environment(setup={'route_decided': '2026-09-08', 'role_created': '2026-09-08'})
        result = self.run_cli()
        self.assert_stage(result, 3, 'aws-survey credentials', 'aws-survey verify')

    def test_stage_3_not_verified_with_valid_keys(self):
        self.write_environment(setup={'route_decided': '2026-09-08', 'role_created': '2026-09-08'})
        self.write_keys(iso(datetime.now(timezone.utc) + timedelta(minutes=30)))
        result = self.run_cli()
        self.assert_stage(result, 3, 'aws-survey verify', '一時キーの残り')
        self.assertNotIn('aws-survey credentials', result.stdout)

    def verified(self):
        self.write_environment(setup={'route_decided': '2026-09-08', 'role_created': '2026-09-08',
                                      'readonly_verified': '2026-09-08'})

    def test_stage_4_no_keys(self):
        self.verified()
        result = self.run_cli()
        self.assert_stage(result, 4, '一時キーがありません', 'aws-survey credentials')
        self.assertIn('~/.aws-survey/smoke/credentials', result.stdout)      # ホームの下は ~ で省略して見せる
        self.assertNotIn(str(self.home), result.stdout)

    def test_stage_4_expired_by_session(self):
        self.verified()
        self.write_keys(iso(datetime.now(timezone.utc) - timedelta(minutes=5)))
        self.assert_stage(self.run_cli(), 4, '期限切れ', 'aws-survey credentials')

    def test_stage_4_expired_by_mtime_without_session(self):
        self.verified()
        keys = self.write_keys(session=False)
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).timestamp()
        os.utime(keys / 'credentials', (old, old))
        self.assert_stage(self.run_cli(), 4, '期限切れ', '推定', 'aws-survey credentials')

    def test_stage_5_ready(self):
        self.verified()
        self.write_keys(iso(datetime.now(timezone.utc) + timedelta(minutes=45)))
        result = self.run_cli()
        self.assert_stage(result, 5, 'aws-survey scan', '未実施', '準備完了', 'aws-survey ls',
                          'aws-survey claude', 'aws-survey codex')
        self.assertRegex(result.stdout, r'約 4[45] 分 / 60 分')
        self.assertNotIn('aws-survey run', result.stdout)        # the bare shell is not part of the guided path
        self.assertEqual(self.calls(), [])                        # nothing runs by itself past the key

    def test_stage_5_by_mtime_without_session(self):
        self.verified()
        self.write_keys(session=False)
        self.assert_stage(self.run_cli(), 5, 'aws-survey scan', '推定')

    def test_stage_5_survey_started(self):
        self.verified()
        self.write_keys(iso(datetime.now(timezone.utc) + timedelta(minutes=45)))
        (self.target / 'out').mkdir()
        (self.target / 'out/00_進捗.md').write_text('# 進捗\n')
        self.assert_stage(self.run_cli(), 6, '開始済み', '対話で開始済み')

    def test_stage_6_after_scan_names_the_remembered_agent(self):
        self.write_environment(setup={'route_decided': '2026-09-08', 'role_created': '2026-09-08',
                                      'readonly_verified': '2026-09-08', 'scanned': '2026-09-12T10:32+0900'})
        config = json.loads((self.target / 'environment.json').read_text())
        config['agent'] = 'codex'
        (self.target / 'environment.json').write_text(json.dumps(config))
        self.write_keys(iso(datetime.now(timezone.utc) + timedelta(minutes=45)))
        result = self.run_cli()
        self.assert_stage(result, 6, '済（2026-09-12T10:32+0900）', '棚卸し済み', 'aws-survey codex',
                          'aws-survey codex resume --last', 'aws-survey scan')
        self.assertNotIn('aws-survey claude', result.stdout)

    def test_stage_5_lists_the_optional_additions(self):
        self.verified()
        self.write_keys(iso(datetime.now(timezone.utc) + timedelta(minutes=45)))
        result = self.run_cli()
        self.assert_stage(result, 5, '任意の追加', 'EC2 の中を調べる: なし', 'aws-survey ec2',
                          'Lambda のコードを読む: なし', 'aws-survey lambda', 'aws-survey scan', 'aws-survey ls')
        for region, name in (('r1', 'f1'), ('r1', 'f2'), ('r2', 'f3')):
            path = self.target / 'code/lambda' / region / name
            path.mkdir(parents=True)
            (path / '_manifest.json').write_text('{}')
        (self.target / 'code/lambda-layers/r1/l1/1').mkdir(parents=True)
        result = self.run_cli()
        self.assert_stage(result, 5, '取り出してある関数 3 件', 'aws-survey scan')
        self.assertNotIn('で一覧から選んで取り出します', result.stdout)
        self.assertIn('取り出してある関数 3 件', self.run_cli('status').stdout)

    def ec2_environment(self, setup=None, hosts=None, session_hosts=None, expiration=None):
        """Stage 5 target with registered hosts. `setup` adds records, `session_hosts` is what the key was issued for."""
        self.verified()
        config = json.loads((self.target / 'environment.json').read_text())
        config['ssh']['hosts'] = hosts if hosts is not None else {
            'web1': {'instance_id': 'i-0123456789abcdef0', 'user': 'diag', 'installed_at': '2026-09-10T12:00:00+09:00'}}
        config['setup'].update(setup or {})
        (self.target / 'environment.json').write_text(json.dumps(config))
        keys = self.write_keys(expiration or iso(datetime.now(timezone.utc) + timedelta(minutes=45)))
        if session_hosts is not None:
            session = json.loads((keys / 'session.json').read_text())
            session['ssh_hosts'] = session_hosts
            (keys / 'session.json').write_text(json.dumps(session))

    def test_ec2_first_the_policy(self):
        self.ec2_environment()
        result = self.run_cli()
        self.assert_stage(result, 5, '準備が途中', 'ポリシー', '登録済みホスト web1', 'aws-survey role --create')
        self.assertIn('aws-survey claude', result.stdout)       # the survey itself is not blocked
        self.assertNotIn('aws-survey credentials', result.stdout)

    def test_ec2_then_the_key(self):
        self.ec2_environment(setup={'ssh_policy_attached': '2026-09-10'})
        result = self.run_cli()
        self.assert_stage(result, 5, '準備が途中', '権限を含んでいません', 'aws-survey credentials')
        self.assertNotIn('role --create', result.stdout)
        # a key issued for a different set of hosts is also stale
        self.ec2_environment(setup={'ssh_policy_attached': '2026-09-10'}, session_hosts='db1')
        self.assert_stage(self.run_cli(), 5, 'aws-survey credentials')

    def test_ec2_then_verify_each_host_once_per_install(self):
        self.ec2_environment(setup={'ssh_policy_attached': '2026-09-10'}, session_hosts='web1')
        result = self.run_cli()
        self.assert_stage(result, 5, '準備が途中', 'まだ調査コンテナから繋いで確かめていません', 'aws-survey ssh verify web1')
        hosts = {'web1': {'instance_id': 'i-0123456789abcdef0', 'installed_at': '2026-09-10T12:00:00+09:00',
                          'verified_at': '2026-09-10T12:30:00+09:00'},
                 'db1': {'instance_id': 'i-0123456789abcdef1', 'installed_at': '2026-09-11T12:00:00+09:00',
                         'verified_at': '2026-09-10T12:30:00+09:00'}}   # re-installed after the last check
        self.ec2_environment(setup={'ssh_policy_attached': '2026-09-10'}, hosts=hosts, session_hosts='db1 web1')
        self.assert_stage(self.run_cli(), 5, 'aws-survey ssh verify db1')
        hosts['db1']['verified_at'] = '2026-09-11T12:30:00+09:00'
        self.ec2_environment(setup={'ssh_policy_attached': '2026-09-10'}, hosts=hosts, session_hosts='db1 web1')
        result = self.run_cli()
        self.assert_stage(result, 5, '未実施', '登録済みホスト db1 web1', 'aws-survey scan')
        self.assertNotIn('準備が途中', result.stdout)

    def test_ec2_hosts_removed_but_key_still_carries_them(self):
        self.ec2_environment(hosts={}, session_hosts='web1')
        result = self.run_cli()
        self.assert_stage(result, 5, '登録を消したホスト（web1）', 'aws-survey credentials')

    def test_ec2_chain_runs_each_step_after_a_yes(self):
        self.ec2_environment(setup={'ssh_policy_attached': '2026-09-10'})
        result = self.run_cli(input='y\n')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        assume = next(call for call in self.calls() if 'assume-role' in call)
        self.assertIn('arn=arn:aws:iam::000000000000:policy/diag-ssh-smoke', assume)
        session = json.loads((self.home / '.aws-survey/smoke/session.json').read_text())
        self.assertEqual(session['ssh_hosts'], 'web1')
        # the second judgement moves on to the connection check and asks again; EOF stops there
        self.assertIn('aws-survey ssh verify web1', result.stdout)
        self.assertEqual([c for c in self.calls() if c[0] == 'docker'], [])

    def test_ec2_borrowed_role_stops_when_nothing_changed(self):
        self.ec2_environment()
        config = json.loads((self.target / 'environment.json').read_text())
        config['auth']['route'] = 'existing_role'
        (self.target / 'environment.json').write_text(json.dumps(config))
        result = self.run_cli(input='y\n')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('管理者', result.stdout)
        self.assertIn('自動では進めません', result.stdout)
        self.assertEqual([c for c in self.calls() if 'assume-role' in c], [])

    def test_guide_never_calls_aws_or_docker(self):
        self.verified()
        self.write_keys(iso(datetime.now(timezone.utc) + timedelta(minutes=45)))
        self.run_cli()
        self.run_cli('status')
        self.assertEqual(self.calls(), [])


class Dispatch(CliCase):
    def test_places_and_dir_option(self):
        elsewhere = self.base / 'elsewhere'
        elsewhere.mkdir()
        result = self.run_cli('--dir', str(self.target), cwd=elsewhere)
        self.assertIn(f'対象フォルダ: {self.target}', result.stdout)
        self.assertNotIn(str(elsewhere), result.stdout)
        self.assertNotIn('本体', result.stdout)
        self.assertIn('次に打つコマンド', result.stdout)
        self.assertIn(f'aws-survey --dir {self.target} init', result.stdout)

    def test_dir_option_after_command(self):
        elsewhere = self.base / 'elsewhere'
        elsewhere.mkdir()
        result = self.run_cli('status', '--dir', str(self.target), cwd=elsewhere)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f'対象フォルダ: {self.target}', result.stdout)
        self.assertNotIn(str(elsewhere), result.stdout)

    def test_symlink_resolves_home(self):
        link_dir = self.base / 'linkbin'
        link_dir.mkdir()
        link = link_dir / 'aws-survey'
        link.symlink_to(CLI)
        result = self.run_cli('status', cli=link)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f'本体              {ROOT}', result.stdout)

    def test_run_dispatches_to_libexec_with_target(self):
        self.write_environment()
        self.write_keys()
        elsewhere = self.base / 'elsewhere'
        elsewhere.mkdir()
        result = self.run_cli('--dir', str(self.target), 'run', 'codex', cwd=elsewhere)
        self.assertEqual(result.returncode, 0, result.stderr)
        launch = self.calls()[-1]
        self.assertEqual(launch[0], 'docker')
        self.assertIn(f'{self.target}/out:/home/node/aws-survey/out', launch)
        self.assertIn(f'{self.home}/.aws-survey/smoke:/home/node/.aws-claude:ro', launch)
        self.assertIn(f'{ROOT}/container/method:/home/node/aws-survey/method:ro', launch)
        self.assertEqual(launch[-2:], ['smoke:latest', 'codex'])
        self.assertFalse((elsewhere / 'out').exists())

    def test_run_without_keys_names_the_cli(self):
        self.write_environment()
        result = self.run_cli('run')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('aws-survey credentials', result.stderr)

    def test_credentials_writes_renew_hint_with_cli(self):
        self.write_environment(setup={'route_decided': '2026-09-08', 'role_created': '2026-09-08'})
        result = self.run_cli('credentials')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        session = json.loads((self.home / '.aws-survey/smoke/session.json').read_text())
        self.assertEqual(session['renew_hint'], f'cd {self.target} && aws-survey credentials')
        self.assertEqual(session['duration_seconds'], 3600)
        self.assertEqual(session['ssh_hosts'], '')          # the guide compares it with the registered hosts
        self.assertIn('次に打つコマンド', result.stdout)
        self.assertIn('aws-survey verify', result.stdout)
        self.assertNotIn('aws-survey run', result.stdout)
        assume = next(call for call in self.calls() if 'assume-role' in call)
        self.assertIn('arn=arn:aws:iam::aws:policy/ReadOnlyAccess', assume)
        self.assertIn('--policy', assume)

    def chained_credentials_setup(self):
        self.write_environment(setup={'route_decided': '2026-09-08', 'role_created': '2026-09-08'})
        config = json.loads((self.target / 'environment.json').read_text())
        config['auth'].update(principal_arn=SSO_CALLER, mfa_required=False, duration_seconds=10800)
        (self.target / 'environment.json').write_text(json.dumps(config))

    def test_credentials_offers_to_shorten_the_duration_for_a_chained_login(self):
        self.chained_credentials_setup()
        # 読めなければ案内だけで止まり、AWS は叩かない
        result = self.run_cli('credentials', FAKE_CALLER_ARN=SSO_CALLER)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('1 時間が上限です（environment.json は 180 分）', result.stdout)
        self.assertIn('auth.duration_seconds を 3600 にしてから', result.stdout)
        self.assertNotIn('update-role', result.stdout)
        self.assertFalse(any('assume-role' in c for c in self.calls()))
        self.assertEqual(json.loads((self.target / 'environment.json').read_text())['auth']['duration_seconds'], 10800)
        # 「はい」なら environment.json を直して、そのまま発行する
        result = self.run_cli('credentials', input='y\n', FAKE_CALLER_ARN=SSO_CALLER)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('auth.duration_seconds を 3600 にしました', result.stdout)
        self.assertIn('発行しました', result.stdout)
        assume = next(c for c in self.calls() if 'assume-role' in c)
        self.assertEqual(assume[assume.index('--duration-seconds') + 1], '3600')
        self.assertEqual(json.loads((self.target / 'environment.json').read_text())['auth']['duration_seconds'], 3600)
        self.assertEqual(json.loads((self.home / '.aws-survey/smoke/session.json').read_text())['duration_seconds'], 3600)
        # 直した後は聞かれない
        result = self.run_cli('credentials', FAKE_CALLER_ARN=SSO_CALLER)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn('上限です', result.stdout)

    def test_credentials_recovers_when_aws_reports_role_chaining(self):
        # 事前の判定をすり抜けて AWS 側で断られたときも、同じ案内で直して発行し直す（update-role の案内は出さない）
        self.chained_credentials_setup()
        result = self.run_cli('credentials', input='\n', FAKE_ASSUME_CHAINING='1')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('role chaining', result.stdout)
        self.assertIn('1 時間が上限', result.stdout)
        self.assertNotIn('update-role', result.stdout)
        self.assertEqual(json.loads((self.target / 'environment.json').read_text())['auth']['duration_seconds'], 3600)
        self.assertEqual(len([c for c in self.calls() if 'assume-role' in c]), 2)

    def test_guidance_always_uses_short_name(self):
        self.write_environment()
        result = self.run_cli()
        self.assertIn('次に打つコマンド', result.stdout)
        self.assertIn('aws-survey role ', result.stdout)
        self.assertNotIn(f'{ROOT}/bin/aws-survey', result.stdout)
        self.assertNotIn('本体', result.stdout)

    def test_unknown_command(self):
        result = self.run_cli('bogus')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('不明なコマンド', result.stderr)


class Status(CliCase):
    def test_status_without_environment(self):
        result = self.run_cli('status')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('未設定', result.stdout)
        self.assertIn(f'本体              {ROOT}', result.stdout)

    def test_status_shows_milestones_and_remaining_time(self):
        self.write_environment(setup={'route_decided': '2026-09-01', 'role_created': '2026-09-02'})
        self.write_keys(iso(datetime.now(timezone.utc) + timedelta(minutes=20)), duration=1800)
        result = self.run_cli('status')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('✔ ロールの用意のしかたを決めた  2026-09-01', result.stdout)
        self.assertIn('✔ ロールを用意した  2026-09-02', result.stdout)
        self.assertIn('－ 読み取り専用であることを確かめた  まだ', result.stdout)
        self.assertIn('－ 白紙の棚卸しを済ませた  まだ', result.stdout)
        self.assertIn('棚卸し: まだ（aws-survey scan）', result.stdout)
        self.assertIn('エージェント      Claude Code / Codex', result.stdout)
        self.assertRegex(result.stdout, r'約 (19|20) 分 / 30 分')
        self.assertIn('一時キー          ~/.aws-survey/smoke', result.stdout)
        self.assertIn(f'対象フォルダ: {self.target}', result.stdout)

    def test_status_estimates_from_mtime(self):
        self.write_environment()
        self.write_keys(session=False)
        result = self.run_cli('status')
        self.assertIn('推定', result.stdout)
        self.assertIn('/ 60 分', result.stdout)


class Doctor(CliCase):
    def test_doctor_reports_tools_then_diagnoses(self):
        self.write_environment()
        result = self.run_cli('doctor')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for tool in ['aws', 'jq', 'docker']:
            self.assertIn(f'✔ {tool}', result.stdout)
        self.assertIn('切り分け', result.stdout)
        self.assertIn('1/4', result.stdout)
        self.assertTrue(any(call[0] == 'aws' for call in self.calls()))

    def test_doctor_reports_unshared_docker_paths(self):
        self.write_environment()
        settings = self.home / 'settings-store.json'
        settings.write_text(json.dumps({'FilesharingDirectories': ['/nowhere']}))
        result = self.run_cli('doctor', DOCKER_SHARE_SETTINGS=str(settings))
        self.assertEqual(result.returncode, 1)
        self.assertIn('File Sharing', result.stdout)
        self.assertIn('切り分け', result.stdout)          # the AWS diagnosis still runs

    def test_doctor_without_docker_desktop_settings_skips_the_check(self):
        self.write_environment()
        result = self.run_cli('doctor')
        self.assertIn('確かめません', result.stdout)

    def test_doctor_stops_on_missing_tool(self):
        self.remove_tool('aws')
        self.write_environment()
        result = self.run_cli('doctor')
        self.assertEqual(result.returncode, 1)
        self.assertIn('✗ aws', result.stdout)
        self.assertNotIn('切り分け', result.stdout)

    def test_doctor_stops_when_unset(self):
        result = self.run_cli('doctor')
        self.assertEqual(result.returncode, 1)
        self.assertIn('未設定', result.stdout)
        self.assertEqual([c for c in self.calls() if c[0] == 'aws'], [])


INIT_ENV = {'AWS_SURVEY_INIT_SOURCE_PROFILE': 'fake-src',
            'AWS_SURVEY_INIT_PRINCIPAL_ARN': 'arn:aws:iam::000000000000:user/fake',
            'AWS_SURVEY_INIT_ACCOUNT_ID': '000000000000',
            'AWS_SURVEY_INIT_REGION': 'ap-northeast-1'}


class Init(CliCase):
    """`init` must fill every template item, never touch AWS in non-interactive mode,
    and leave existing files alone unless told otherwise."""

    def load_env(self):
        script = f'. "{ROOT}/libexec/load-env.sh" && echo "$SURVEY_NAME|$ACCOUNT_ID|$REGION|$PROFILE_SRC|$ROLE_NAME|$DURATION|$AWS_DIR|$MFA_REQUIRED"'
        env = {'PATH': str(self.bin), 'HOME': str(self.home), 'AWS_SURVEY_DIR': str(self.target)}
        return subprocess.run(['bash', '-c', script], env=env, capture_output=True, text=True)

    def test_noninteractive_env_fills_template_and_load_env_accepts(self):
        result = self.run_cli('init', **INIT_ENV)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        config = json.loads((self.target / 'environment.json').read_text())
        template = json.loads((ROOT / 'templates/environment.json').read_text())
        self.assertEqual(set(config), set(template))
        self.assertEqual(set(config['auth']), set(template['auth']))
        self.assertEqual(set(config['setup']), set(template['setup']))
        self.assertNotIn('TEMPLATE-', json.dumps(config, ensure_ascii=False))
        self.assertEqual(config['name'], 'target')
        self.assertEqual(config['auth']['route'], None)
        self.assertEqual(config['auth']['mfa_required'], True)
        self.assertEqual(config['auth']['refresh_command'], None)
        self.assertEqual(config['auth']['duration_seconds'], 10800)     # 既定は 3 時間
        self.assertEqual(config['auth']['role_name'], 'aws-survey-readonly')
        self.assertEqual(config['phase_dir'], '01_基礎調査')
        self.assertEqual(config['setup'], {k: None for k in template['setup']})
        loaded = self.load_env()
        self.assertEqual(loaded.returncode, 0, loaded.stderr)
        self.assertEqual(loaded.stdout.strip(),
                         f'target|000000000000|ap-northeast-1|fake-src|aws-survey-readonly|10800|{self.home}/.aws-survey/target|true')
        self.assertIn('aws-survey role', result.stdout)
        self.assertNotIn('未実装', result.stdout)

    def test_noninteractive_from_json_and_env_precedence(self):
        src = self.base / 'from.json'
        src.write_text(json.dumps({'name': 'from-json', 'account_id': '111111111111', 'region': 'us-east-1',
                                   'auth': {'source_profile': 'sso-prof', 'principal_arn': 'arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_x/u',
                                            'mfa_required': False, 'refresh_command': 'aws sso login --profile sso-prof',
                                            'duration_seconds': 3600, 'role_name': 'ro-role'}}))   # 借りたロールからは 1 時間が上限
        result = self.run_cli('init', '--from', str(src), AWS_SURVEY_INIT_NAME='from-env')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        config = json.loads((self.target / 'environment.json').read_text())
        self.assertEqual(config['name'], 'from-env')
        self.assertEqual(config['account_id'], '111111111111')
        self.assertEqual(config['auth']['source_profile'], 'sso-prof')
        self.assertEqual(config['auth']['mfa_required'], False)
        self.assertEqual(config['auth']['refresh_command'], 'aws sso login --profile sso-prof')
        self.assertEqual(config['auth']['duration_seconds'], 3600)
        self.assertEqual(config['auth']['role_name'], 'ro-role')

    def test_noninteractive_never_calls_sts(self):
        self.run_cli('init', **INIT_ENV)
        aws_calls = [c for c in self.calls() if c[0] == 'aws']
        self.assertTrue(aws_calls)
        self.assertTrue(all(c[1] == 'configure' for c in aws_calls), aws_calls)

    def test_noninteractive_rejects_missing_or_bad_values(self):
        result = self.run_cli('init', AWS_SURVEY_INIT_SOURCE_PROFILE='fake-src')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('がありません', result.stderr)
        self.assertFalse((self.target / 'environment.json').exists())
        bad = dict(INIT_ENV, AWS_SURVEY_INIT_DURATION_SECONDS='100')
        result = self.run_cli('init', **bad)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('duration_seconds', result.stderr)
        unknown = dict(INIT_ENV, AWS_SURVEY_INIT_SOURCE_PROFILE='nope')
        result = self.run_cli('init', **unknown)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('source_profile', result.stderr)
        self.assertFalse((self.target / 'environment.json').exists())

    def test_scaffold_out_and_agents(self):
        """init writes the target folder's own AGENTS.md, which stands alone.

        The host agent's job ends at launch (`aws-survey scan` / `claude` / `codex`), so this file names no rule in the
        distribution: everything after launch belongs to the survey agent in the container.
        """
        self.run_cli('init', **INIT_ENV)
        self.assertTrue((self.target / 'out/_環境').is_dir())
        agents = (self.target / 'AGENTS.md').read_text()
        self.assertNotIn('.agents/rules', agents)
        self.assertIn('対象の概要・調査項目', agents)
        self.assertIn('aws-survey scan', agents)
        self.assertNotIn('aws-survey run', agents)
        self.assertEqual((self.target / 'CLAUDE.md').read_text(), '@AGENTS.md\n')
        # アカウント ID の入るファイルは、対象フォルダを Git で管理しても載らないように init が .gitignore に入れておく
        self.assertEqual((self.target / '.gitignore').read_text(), '/environment.json\n/environment.json.bak\n/trust.json\n')
        self.assertNotIn('TEMPLATE', agents)

    def test_an_existing_gitignore_gets_only_the_missing_lines(self):
        (self.target / '.gitignore').write_text('/trust.json\nout/')          # 末尾に改行が無くても行を壊さない
        self.run_cli('init', **INIT_ENV)
        self.assertEqual((self.target / '.gitignore').read_text(), '/trust.json\nout/\n/environment.json\n/environment.json.bak\n')
        result = self.run_cli('init', '--force', **INIT_ENV)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.target / '.gitignore').read_text(), '/trust.json\nout/\n/environment.json\n/environment.json.bak\n')

    def test_existing_files_are_not_overwritten_without_force(self):
        (self.target / 'AGENTS.md').write_text('mine\n')
        self.write_environment(name='keep')
        result = self.run_cli('init', **INIT_ENV)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('--force', result.stderr)
        self.assertEqual(json.loads((self.target / 'environment.json').read_text())['name'], 'keep')
        self.assertEqual((self.target / 'AGENTS.md').read_text(), 'mine\n')
        self.assertFalse((self.target / 'environment.json.bak').exists())

    def test_force_backs_up_and_keeps_agents(self):
        (self.target / 'AGENTS.md').write_text('mine\n')
        self.write_environment(name='keep')
        result = self.run_cli('init', '--force', **INIT_ENV)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads((self.target / 'environment.json').read_text())['name'], 'target')
        self.assertEqual(json.loads((self.target / 'environment.json.bak').read_text())['name'], 'keep')
        self.assertEqual((self.target / 'AGENTS.md').read_text(), 'mine\n')

    def test_interactive_declines_replacement(self):
        self.write_environment(name='keep')
        result = self.run_cli('init', input='n\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('そのままにします', result.stdout)
        self.assertEqual(json.loads((self.target / 'environment.json').read_text())['name'], 'keep')

    def test_interactive_defaults_come_from_profile_and_sts(self):
        # IAM ユーザーのログイン: name, profile(2=sso-prof), account, mfa, region, duration, role の 7 問。
        # 貸す相手（いまのログインの自分だけ）と refresh_command（aws-login）は聞かずに決まる
        answers = ['', '2', '', '', '', '3600', '']
        result = self.run_cli('init', input='\n'.join(answers) + '\n')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        config = json.loads((self.target / 'environment.json').read_text())
        self.assertEqual(config['name'], 'target')
        self.assertEqual(config['auth']['source_profile'], 'sso-prof')
        self.assertEqual(config['auth']['principal_arn'], 'arn:aws:iam::000000000000:user/fake')
        self.assertEqual(config['account_id'], '000000000000')
        self.assertEqual(config['auth']['mfa_required'], True)
        self.assertEqual(config['auth']['refresh_command'], 'aws-login --profile sso-prof')
        self.assertEqual(config['region'], 'ap-northeast-1')
        for head in ('◆ name', '◆ source_profile', '◆ account_id', '◆ mfa_required', '◆ region', '◆ duration_seconds', '◆ role_name'):
            self.assertIn(head, result.stdout)
        self.assertIn('調査用ロールを貸す相手: 自分だけ（arn:aws:iam::000000000000:user/fake）', result.stdout)
        self.assertIn('ロールを貸す相手', result.stdout.split('◆ 対象')[1])
        self.assertIn('セッションの上限  1 時間', result.stdout.split('◆ 対象')[1])
        self.assertIn('再ログイン        aws-login --profile sso-prof', result.stdout)
        for gone in ('9 問', '準備して', '調べたい', '◆ principal_arn', '◆ refresh_command', 'Git で管理', '1/'):
            self.assertNotIn(gone, result.stdout)
        self.assertIn('aws-survey role --create', result.stdout)
        sts = [c for c in self.calls() if 'get-caller-identity' in c]
        self.assertEqual(len(sts), 1)
        self.assertIn('sso-prof', sts[0])
        self.assertFalse(any('get-role' in c for c in self.calls()))

    def init_sso(self, **extra):
        # Identity Center のログイン: name, profile(2=sso-prof), account, region, role の 5 問。
        # 貸す相手は自分（セッション ARN）、MFA は false、duration は 1 時間に固定され、どれも聞かれない
        answers = ['', '2', '', '', '']
        return self.run_cli('init', input='\n'.join(answers) + '\n', FAKE_CALLER_ARN=SSO_CALLER, **extra)

    def test_interactive_sso_asks_five_questions_and_fixes_the_rest(self):
        result = self.init_sso()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for gone in ('◆ mfa_required', 'mfa_required (y/n)', '◆ duration_seconds', 'duration_seconds を選んでください',
                     '◆ principal_arn', '権限セット', 'ARN を手で入力'):
            self.assertNotIn(gone, result.stdout)
        self.assertIn(f'調査用ロールを貸す相手: 自分だけ（{SSO_CALLER}）', result.stdout)
        # 借りたロールからのログインは 1 時間が上限（ロールチェーン）。まとめにセッションの制限時間として出す
        self.assertIn('セッションの上限  1 時間（借りたロールからのログインは、AWS の決まりで 1 時間が上限）', result.stdout)
        config = json.loads((self.target / 'environment.json').read_text())
        self.assertEqual(config['auth']['principal_arn'], SSO_CALLER)
        self.assertEqual(config['auth']['mfa_required'], False)
        self.assertEqual(config['auth']['role_name'], 'aws-survey-readonly')
        self.assertEqual(config['auth']['duration_seconds'], 3600)
        self.assertEqual(config['auth']['refresh_command'], 'aws-login --profile sso-prof')
        self.assertFalse(any('get-role' in c for c in self.calls()))

    def test_interactive_asks_the_principal_only_when_the_login_cannot_be_read(self):
        # ログインできていなければ、貸す相手と account_id は手で入れる（既定が無い）
        answers = ['', '2', 'arn:aws:iam::000000000000:user/typed', '000000000000', '', '', '3600', '']
        result = self.run_cli('init', input='\n'.join(answers) + '\n', FAKE_CALLER_FAIL='1')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('ログインできていません', result.stdout)
        config = json.loads((self.target / 'environment.json').read_text())
        self.assertEqual(config['auth']['principal_arn'], 'arn:aws:iam::000000000000:user/typed')

    def test_noninteractive_sso_refuses_mfa_required(self):
        env = {**INIT_ENV, 'AWS_SURVEY_INIT_PRINCIPAL_ARN': SSO_CALLER, 'AWS_SURVEY_INIT_MFA_REQUIRED': 'true'}
        result = self.run_cli('init', **env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('mfa_required を true にできません', result.stdout + result.stderr)
        self.assertFalse((self.target / 'environment.json').exists())

    def test_noninteractive_chained_login_caps_duration_at_one_hour(self):
        env = {k: v for k, v in INIT_ENV.items() if k != 'AWS_SURVEY_INIT_MFA_REQUIRED'}
        env['AWS_SURVEY_INIT_PRINCIPAL_ARN'] = SSO_CALLER
        result = self.run_cli('init', **env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads((self.target / 'environment.json').read_text())['auth']['duration_seconds'], 3600)
        result = self.run_cli('init', '--force', **{**env, 'AWS_SURVEY_INIT_DURATION_SECONDS': '10800'})
        self.assertEqual(result.returncode, 1)
        self.assertIn('ロールチェーン', result.stderr)
        self.assertIn('3600', result.stderr)
        # IAM ユーザーのログインなら既定の 3 時間のまま
        result = self.run_cli('init', '--force', **{**env, 'AWS_SURVEY_INIT_PRINCIPAL_ARN': 'arn:aws:iam::000000000000:user/fake'})
        self.assertEqual(json.loads((self.target / 'environment.json').read_text())['auth']['duration_seconds'], 10800)

    def test_noninteractive_sso_defaults_mfa_to_false(self):
        env = {k: v for k, v in INIT_ENV.items() if k != 'AWS_SURVEY_INIT_MFA_REQUIRED'}
        result = self.run_cli('init', **{**env, 'AWS_SURVEY_INIT_PRINCIPAL_ARN': SSO_CALLER})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        config = json.loads((self.target / 'environment.json').read_text())
        self.assertEqual(config['auth']['mfa_required'], False)

    def test_noninteractive_still_accepts_a_role_arn_as_the_principal(self):
        # 対話では自分だけに固定するが、非対話（--from / 環境変数）ではパス付きのロール ARN も渡せる
        env = {k: v for k, v in INIT_ENV.items() if k != 'AWS_SURVEY_INIT_MFA_REQUIRED'}
        result = self.run_cli('init', **{**env, 'AWS_SURVEY_INIT_PRINCIPAL_ARN': SSO_ROLE})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        config = json.loads((self.target / 'environment.json').read_text())
        self.assertEqual(config['auth']['principal_arn'], SSO_ROLE)
        self.assertEqual(config['auth']['duration_seconds'], 3600)

    def test_interactive_hides_base_profile_when_mfa_destination_exists(self):
        # 一覧に <名>-mfa があるとき、元の <名> は候補に出ず、番号は残った候補で振り直される
        profiles = 'fake-src\\nsso-prof\\nbase\\nbase-mfa'
        answers = ['', 'base', '3', '', '', '', '3600', '']
        result = self.run_cli('init', input='\n'.join(answers) + '\n', FAKE_PROFILES=profiles)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('base は <名>-mfa がある長期キー側なので省いています', result.stdout)
        self.assertIn(' 3) base-mfa', result.stdout)
        self.assertNotIn(' base\n', result.stdout.split('◆ account_id')[0])
        self.assertIn('一覧にありません', result.stdout)
        config = json.loads((self.target / 'environment.json').read_text())
        self.assertEqual(config['auth']['source_profile'], 'base-mfa')
        # 一時キーの保存先を選んでも、aws-login に渡すのは元の <名>
        self.assertEqual(config['auth']['refresh_command'], 'aws-login --profile base')

    def test_interactive_menu_uses_arrow_keys_on_a_terminal(self):
        # 端末（pty）では矢印キーで選ぶ。↓ 1 回で 2 番目の sso-prof、以降は既定を Enter
        import pty, select, time
        env = {'PATH': str(self.bin), 'HOME': str(self.home), 'FAKE_LOG': str(self.log),
               'LANG': os.environ.get('LANG', 'C.UTF-8'), 'TZ': 'UTC', 'TERM': 'xterm'}
        pid, fd = pty.fork()
        if pid == 0:
            os.chdir(str(self.target))
            os.execve(str(CLI), [str(CLI), 'init'], env)
        output = b''

        def read_until(marker, timeout=20):
            nonlocal output
            deadline = time.time() + timeout
            plain = lambda: re.sub(rb'\x1b\[[0-9;?]*[A-Za-z]', b'', output)
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

        read_until('name (target)'.encode())
        os.write(fd, b'\r')
        read_until('Enter 決定'.encode())
        os.write(fd, b'\x1b[B')
        time.sleep(0.5)
        os.write(fd, b'\r')
        read_until('account_id'.encode())
        for _ in range(5):                  # account, mfa, region, duration（メニュー）, role
            read_until(b': ')
            os.write(fd, b'\r')
            time.sleep(0.3)
        read_until('次に打つコマンド'.encode())
        read_until(b'\x00never', timeout=5)  # 終了まで読み切る（EOF で戻る）
        _, status = os.waitpid(pid, 0)
        os.close(fd)
        raw = output.decode(errors='replace')
        self.assertEqual(os.waitstatus_to_exitcode(status), 0, raw)
        self.assertIn('\x1b[K', raw)
        text = re.sub(r'\x1b\[[0-9;?]*[A-Za-z]', '', raw)
        self.assertIn('❯ sso-prof', text)
        self.assertIn('✔ source_profile: sso-prof', text)
        self.assertIn('duration_seconds を選んでください', text)     # duration も矢印キーのメニュー。Enter で既定の 3 時間
        self.assertIn('✔ duration_seconds: 10800（3 時間）', text)
        self.assertIn('✔ name: target', text)
        self.assertIn('◆ role_name', text)
        self.assertNotIn('name [', text)
        config = json.loads((self.target / 'environment.json').read_text())
        self.assertEqual(config['auth']['source_profile'], 'sso-prof')

    def test_duration_is_chosen_by_number_or_typed_in_seconds(self):
        # name, profile(2=sso-prof), account, mfa, region, duration, role
        result = self.run_cli('init', input='\n'.join(['', '2', '', '', '', '4', '']) + '\n')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('1) 1 時間（3600 秒）', result.stdout)
        self.assertIn('4) 8 時間（28800 秒）', result.stdout)
        self.assertIn('duration_seconds: 28800（8 時間）', result.stdout)
        self.assertEqual(json.loads((self.target / 'environment.json').read_text())['auth']['duration_seconds'], 28800)
        (self.target / 'environment.json').unlink()
        result = self.run_cli('init', input='\n'.join(['', '2', '', '', '', '5400', '']) + '\n')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('duration_seconds: 5400（90 分）', result.stdout)
        self.assertEqual(json.loads((self.target / 'environment.json').read_text())['auth']['duration_seconds'], 5400)
        # 5 は番号でも秒でもない
        (self.target / 'environment.json').unlink()
        result = self.run_cli('init', input='\n'.join(['', '2', '', '', '', '5', '', '']) + '\n')
        self.assertIn('受け付けられない値', result.stdout)
        self.assertEqual(json.loads((self.target / 'environment.json').read_text())['auth']['duration_seconds'], 10800)

    def test_interactive_retries_invalid_and_fails_on_eof(self):
        answers = ['bad name!', 'ok-name', 'fake-src', '', '']          # region の前で入力が尽きる
        result = self.run_cli('init', input='\n'.join(answers) + '\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('受け付けられない値', result.stdout)
        self.assertIn('入力が尽きました', result.stderr)
        self.assertIn('--from', result.stderr)
        self.assertFalse((self.target / 'environment.json').exists())

    def test_init_in_home_itself_does_not_touch_home_agents(self):
        # A stand-in for the installed body: libexec/ is the real one, AGENTS.md is the body's own.
        fake_home = self.base / 'fake-home'
        fake_home.mkdir()
        (fake_home / 'libexec').symlink_to(ROOT / 'libexec')
        (fake_home / 'AGENTS.md').write_text('body\n')
        result = self.run_cli('init', AWS_SURVEY_HOME=str(fake_home), AWS_SURVEY_DIR=str(fake_home), **INIT_ENV)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('置きません', result.stdout)
        self.assertEqual((fake_home / 'AGENTS.md').read_text(), 'body\n')
        self.assertFalse((fake_home / 'CLAUDE.md').exists())
        self.assertTrue((fake_home / 'environment.json').exists())
        self.assertTrue((fake_home / 'out/_環境').is_dir())

    def test_role_create_records_own_role_when_unset(self):
        self.run_cli('init', **INIT_ENV)
        result = self.run_cli('role', '--create')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        config = json.loads((self.target / 'environment.json').read_text())
        self.assertEqual(config['auth']['route'], 'own_role')
        self.assertTrue(config['setup']['route_decided'])
        self.assertTrue(config['setup']['role_created'])


class Role(CliCase):
    """`role` must not tell the user to create a role that already exists and is complete."""

    def test_existing_complete_role_is_recorded_not_recreated(self):
        self.run_cli('init', **INIT_ENV)          # auth.route null, setup.* null
        result = self.run_cli('role')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('ロールは用意できています', result.stdout)
        self.assertIn('記録されていません', result.stdout)
        self.assertIn('aws-survey role --create', result.stdout)
        self.assertNotIn('読み取り専用ロールを作ります', result.stdout)
        self.assertEqual(self.run_cli('role', '--create').returncode, 0)
        result = self.run_cli('role')
        self.assertIn('ロールは用意できています', result.stdout)
        self.assertIn('aws-survey credentials', result.stdout)
        self.assertNotIn('role --create', result.stdout)

    def use_principal(self, principal, mfa=None):
        # Identity Center のログインは MFA の条件を満たせないので、既定では mfa_required を false にする
        self.write_environment()
        config = json.loads((self.target / 'environment.json').read_text())
        config['auth']['principal_arn'] = principal
        config['auth']['mfa_required'] = ('AWSReservedSSO_' not in principal) if mfa is None else mfa
        (self.target / 'environment.json').write_text(json.dumps(config))

    def run_sso(self, *args, **extra):
        # Identity Center でログインしていて、信頼ポリシーに MFA の条件が無い状態（FAKE_TRUST_NO_MFA を消せば条件あり）
        env = {'FAKE_CALLER_ARN': SSO_CALLER, 'FAKE_TRUST_NO_MFA': '1', **extra}
        return self.run_cli(*args, **{k: v for k, v in env.items() if v})

    def test_existing_role_with_wrong_principal_lists_shortfall(self):
        self.use_principal('arn:aws:iam::000000000000:user/someone-else')
        result = self.run_cli('role')
        self.assertIn('信頼ポリシーの貸す相手に、あなた', result.stdout)
        self.assertIn('このままでは借りられません', result.stdout)
        self.assertIn('合っていないところがあります', result.stdout)
        self.assertIn('aws-survey role --create', result.stdout)

    def test_duration_above_the_role_ceiling_is_a_shortfall(self):
        # 借りるロール: 上限は変えられないので environment.json を下げる案内。--create は勧めない
        self.write_environment(setup={'route_decided': '2026-09-08', 'role_created': '2026-09-08'})
        config = json.loads((self.target / 'environment.json').read_text())
        config['auth'].update(route='existing_role', duration_seconds=10800)
        (self.target / 'environment.json').write_text(json.dumps(config))
        result = self.run_cli('role', FAKE_MAX_SESSION='3600')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('セッション上限（3600 秒）が environment.json の duration_seconds（10800 秒）より短い', result.stdout)
        self.assertIn('auth.duration_seconds を 3600 以下に', result.stdout)
        self.assertNotIn('次に打つコマンド', result.stdout)
        # 上限に収まっていれば不足にしない
        result = self.run_cli('role', FAKE_MAX_SESSION='14400')
        self.assertNotIn('より短い', result.stdout)
        # 自分のロール: --create が上限を合わせるので、⚠ にはするが environment.json を直せとは言わない
        config['auth'].update(route='own_role')
        (self.target / 'environment.json').write_text(json.dumps(config))
        result = self.run_cli('role', FAKE_MAX_SESSION='3600')
        self.assertIn('より短い', result.stdout)
        self.assertNotIn('以下にしてください', result.stdout)
        self.assertIn('aws-survey role --create', result.stdout)
        result = self.run_cli('role', '--create', FAKE_MAX_SESSION='3600')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('セッション上限を 10800 秒に合わせました', result.stdout)

    def test_chained_login_warns_about_the_one_hour_limit(self):
        # ロール側の上限が 3 時間でも、借りたロールからは 1 時間まで。environment.json を直す案内で、--create は勧めない
        self.write_environment(setup={'route_decided': '2026-09-08', 'role_created': '2026-09-08'})
        config = json.loads((self.target / 'environment.json').read_text())
        config['auth'].update(principal_arn=SSO_CALLER, mfa_required=False, duration_seconds=10800)
        (self.target / 'environment.json').write_text(json.dumps(config))
        result = self.run_sso('role', FAKE_MAX_SESSION='10800')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('1 時間が上限です（environment.json は 180 分）', result.stdout)
        self.assertIn('auth.duration_seconds を 3600 に', result.stdout)
        self.assertNotIn('より短い', result.stdout)

    def test_role_lent_to_the_permission_set_covers_my_session(self):
        # 信頼ポリシーはロールの形、environment.json はセッションの形。書き方は違うが借りられるので不足にしない
        self.use_principal(SSO_CALLER)
        result = self.run_sso('role', FAKE_TRUST_PRINCIPAL=SSO_ROLE)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('あなたはこのロールを借りられます', result.stdout)
        self.assertIn('権限セット AdministratorAccess でログインした人なら誰でも', result.stdout)
        self.assertIn('ロールはもうあるので、この確認は要りません', result.stdout)
        self.assertIn('ロールは用意できています', result.stdout)
        self.assertNotIn('⚠', result.stdout)

    def test_role_lent_to_another_permission_set_does_not_cover_me(self):
        self.use_principal(SSO_CALLER)
        other = SSO_ROLE.replace('AdministratorAccess', 'ViewOnlyAccess')
        result = self.run_sso('role', FAKE_TRUST_PRINCIPAL=other)
        self.assertIn('このままでは借りられません', result.stdout)

    def test_role_lent_only_to_me_is_narrower_but_still_mine(self):
        # environment.json は「権限セットの人なら誰でも」、信頼ポリシーは「自分だけ」。あなたは借りられるので不足にしない
        self.use_principal(SSO_ROLE)
        result = self.run_sso('role', FAKE_TRUST_PRINCIPAL=SSO_CALLER)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('あなたはこのロールを借りられます', result.stdout)
        self.assertIn('より狭い範囲です。ほかの人は借りられません', result.stdout)
        self.assertIn('ロールは用意できています', result.stdout)
        self.assertNotIn('⚠', result.stdout)

    def test_role_lent_to_someone_else_in_my_permission_set_does_not_cover_me(self):
        self.use_principal(SSO_ROLE)
        result = self.run_sso('role', FAKE_TRUST_PRINCIPAL=SSO_CALLER.replace('/alice', '/bob'))
        self.assertIn('このままでは借りられません', result.stdout)

    def test_sso_with_mfa_required_is_told_to_turn_it_off(self):
        self.use_principal(SSO_CALLER, mfa=True)
        result = self.run_sso('role', FAKE_TRUST_PRINCIPAL=SSO_CALLER)
        self.assertIn('Identity Center のログインではロールの側で MFA を確かめられません。false にしてください', result.stdout)
        self.assertIn('auth.mfa_required を false に書き換えてください', result.stdout)
        self.assertNotIn('という条件がありません', result.stdout)

    def test_sso_trust_with_mfa_condition_cannot_be_borrowed(self):
        # 今回実際に起きた状態: 信頼ポリシーに MFA の条件があり、Identity Center のログインでは AccessDenied になる
        self.use_principal(SSO_CALLER)
        result = self.run_sso('role', FAKE_TRUST_PRINCIPAL=SSO_CALLER, FAKE_TRUST_NO_MFA='')
        self.assertIn('今は借りられません', result.stdout)
        self.assertNotIn('あなたはこのロールを借りられます', result.stdout)
        self.assertIn('条件があるため、Identity Center のログインでは借りられません', result.stdout)
        self.assertIn('aws-survey role --create', result.stdout)

    def test_role_create_refuses_the_mfa_condition_for_sso(self):
        self.use_principal(SSO_CALLER, mfa=True)
        result = self.run_sso('role', '--create', FAKE_TRUST_PRINCIPAL=SSO_CALLER)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('借りられなくなります', result.stdout)
        written = [c for c in self.calls() if 'update-assume-role-policy' in c or 'create-role' in c]
        self.assertEqual(written, [])
        self.assertFalse((self.target / 'trust.json').exists())
        # 直さずに止まるので「このあと直します」とは言わない
        self.assertNotIn('このあと「ロールを整えます」で直します', result.stdout)
        self.assertNotIn('は直しました', result.stdout)

    def test_role_create_says_the_warnings_were_before_and_are_fixed(self):
        # --create は直す前の判定を先に出す。⚠ が直す前の状態だと分かり、最後に直したと分かること
        self.use_principal('arn:aws:iam::000000000000:user/someone-else')
        result = self.run_cli('role', '--create')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        out = result.stdout
        self.assertIn('まず、いまの状態（直す前）を確かめます', out)
        self.assertIn('ここまでは直す前のいまの状態です', out)
        self.assertLess(out.index('ここまでは直す前'), out.index('ロールを整えます'))
        self.assertIn('直す前にあった ⚠ の点（1 件）は直しました', out)
        self.assertLess(out.index('ロールを整えます'), out.index('は直しました'))

    def test_role_create_without_warnings_does_not_claim_a_fix(self):
        self.write_environment()
        result = self.run_cli('role', '--create')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn('ここまでは直す前', result.stdout)
        self.assertNotIn('は直しました', result.stdout)

    def test_role_lent_to_the_account_covers_me(self):
        self.write_environment()
        result = self.run_cli('role', FAKE_TRUST_PRINCIPAL='arn:aws:iam::000000000000:root')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('アカウント全体に貸す書き方', result.stdout)
        self.assertNotIn('⚠', result.stdout)

    def test_missing_mfa_condition_is_still_a_shortfall_for_an_iam_user(self):
        self.write_environment()                  # IAM ユーザー・mfa_required true
        result = self.run_cli('role', FAKE_TRUST_NO_MFA='1')
        self.assertIn('あなたはこのロールを借りられます', result.stdout)
        self.assertIn('「MFA 済みの人だけ」という条件がありません', result.stdout)
        self.assertIn('合っていないところがあります', result.stdout)


class Chain(CliCase):
    """No-arg `aws-survey` off a terminal asks before each step and keeps going: guidance, confirm, run, judge again.
    Declining, a step that fails, or no terminal all stop the chain with the command still named.
    On a terminal (pty) it runs through without asking anything (`role --create` included: every step is
    required), and each step is folded into one line (a spinner while it runs, then ✔ with the command's last ✔ text)."""

    def drive(self, keys, stage_env):
        import pty, select, time
        env = {'PATH': str(self.bin), 'HOME': str(self.home), 'FAKE_LOG': str(self.log),
               'LANG': os.environ.get('LANG', 'C.UTF-8'), 'TZ': 'UTC', 'TERM': 'xterm', **stage_env}
        pid, fd = pty.fork()
        if pid == 0:
            os.chdir(str(self.target))
            os.execve(str(CLI), [str(CLI)], env)
        output = b''
        plain = lambda: re.sub(rb'\x1b\[[0-9;?]*[A-Za-z]', b'', output)

        def read_until(marker, limit=30):
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
            read_until(marker.encode())
            time.sleep(0.3)
            os.write(fd, key)
        read_until(b'\x00never', limit=10)
        _, status = os.waitpid(pid, 0)
        os.close(fd)
        return os.waitstatus_to_exitcode(status), plain().decode(errors='replace')

    def test_on_a_terminal_the_chain_runs_through_to_ready_without_asking(self):
        self.verified()                                   # 段階 4: 一時キーが無い → credentials → 準備完了で止まる
        code, text = self.drive([], {})
        self.assertEqual(code, 0, text)
        self.assertNotIn('実行しますか', text)
        lines = [l for l in text.replace('\r', '\n').splitlines() if l.strip()]
        # 段階の一覧は最初の 1 回だけ。credentials は 1 行に畳まれ、その中身（見出し・表）は画面に出ない
        self.assertEqual(sum('● 4 一時キー' in l for l in lines), 1)
        self.assertIn('✔ 一時キーを発行しました（60 分）', text)
        self.assertNotIn('◆ aws-survey credentials', text)
        self.assertNotIn('1/3 ホストのプロファイル', text)
        self.assertIn('⠋', text)                          # 回転する印
        # 準備完了で止まる。scan は時間がかかり、claude / codex は端末を渡すので、ここから先は自動で実行しない
        self.assertIn('準備完了', text)
        self.assertIn('aws-survey scan', text)
        self.assertNotIn('aws-survey run', text)
        self.assertTrue(any('assume-role' in c for c in self.calls()))
        self.assertFalse(any(c[0] == 'docker' for c in self.calls()), text)

    def test_on_a_terminal_role_create_runs_without_asking(self):
        # 段階 2: role --create も聞かずに走り、credentials → verify まで続く（verify は実 AWS が要るので偽物では落ちて止まる）
        self.write_environment()
        code, text = self.drive([], {})
        self.assertNotEqual(code, 0, text)
        self.assertNotIn('実行しますか', text)
        self.assertIn('✔ ロールの準備ができました', text)
        self.assertNotIn('1/3 ホストのプロファイル', text)
        self.assertIn('✔ 一時キーを発行しました（60 分）', text)
        self.assertIn('✗ aws-survey verify が失敗しました', text)
        ops = [c for c in self.calls() if c[0] == 'aws']      # 偽の aws ではロールが既にあるので、整えて（attach）から発行する
        self.assertTrue(any('attach-role-policy' in c for c in ops))
        self.assertLess(next(i for i, c in enumerate(ops) if 'attach-role-policy' in c),
                        next(i for i, c in enumerate(ops) if 'assume-role' in c))

    def test_on_a_terminal_a_failing_step_shows_its_output_and_stops(self):
        self.verified()
        # 偽 aws が assume-role を断る → credentials が復旧を聞く（回転を止めて端末に直接）→ n で止まる
        code, text = self.drive([('そのまま発行しますか', b'n\r')], {'FAKE_ASSUME_CHAINING': '1'})
        self.assertNotEqual(code, 0, text)
        self.assertIn('✗ aws-survey credentials が失敗しました', text)
        self.assertIn('そのときの出力', text)
        self.assertIn('role chaining', text)
        self.assertIn('もう一度 aws-survey を実行すれば', text)

    def verified(self):
        self.write_environment(setup={'route_decided': '2026-09-08', 'role_created': '2026-09-08',
                                      'readonly_verified': '2026-09-08'})

    def test_declining_leaves_the_command_named_and_touches_nothing(self):
        self.write_environment()
        result = self.run_cli(input='n\n')
        self.assert_stage(result, 2, '次に打つコマンド', 'aws-survey role --create を実行しますか')
        self.assertEqual(self.calls(), [])

    def test_create_advances_the_stage_and_the_chain_offers_credentials(self):
        self.write_environment()
        result = self.run_cli(input='y\nn\n')
        self.assertTrue(any('simulate-principal-policy' in call for call in self.calls()))   # 作る前に判定も通す
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        config = json.loads((self.target / 'environment.json').read_text())
        self.assertTrue(config['setup']['role_created'])
        self.assertIn('● 3 ', result.stdout)
        self.assertIn('aws-survey credentials を実行しますか', result.stdout)

    def test_credentials_then_ready(self):
        self.verified()
        result = self.run_cli(input='y\ny\n')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.home / '.aws-survey/smoke/session.json').exists())
        self.assertIn('準備完了', result.stdout)
        self.assertIn('aws-survey scan', result.stdout)
        self.assertNotIn('を実行しますか', result.stdout.split('準備完了')[-1])
        self.assertFalse(any(c[0] == 'docker' for c in self.calls()))

    def test_a_failed_step_stops_the_chain(self):
        self.verified()
        self.add_tool('aws', '#!/bin/sh\nexit 1\n')
        result = self.run_cli(input='y\ny\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('aws-survey credentials が失敗しました', result.stdout)
        self.assertNotIn('aws-survey scan を実行しますか', result.stdout)

    def test_chained_commands_do_not_print_their_own_next_step(self):
        self.write_environment(setup={'route_decided': '2026-09-08', 'role_created': '2026-09-08'})
        result = self.run_cli('credentials', AWS_SURVEY_CHAIN='1')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn('次に打つコマンド', result.stdout)


class EmptyFolderToRun(CliCase):
    """S4 gate, as far as fakes allow: an empty folder goes guide → init → role --create →
    credentials → ready (scan / claude / codex named), with each guide step naming the command that was run next.
    `run bash` still works as the bare shell, off the guided path.
    `verify` needs real AWS (dry-run denials), so readonly_verified is stamped by hand."""

    def test_walkthrough(self):
        self.assert_stage(self.run_cli(input=''), 1, 'aws-survey init')
        self.assertEqual(self.run_cli('init', **INIT_ENV).returncode, 0)
        self.assert_stage(self.run_cli(), 2, 'aws-survey role')
        result = self.run_cli('role', '--create')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_stage(self.run_cli(), 3, 'aws-survey credentials', 'aws-survey verify')
        result = self.run_cli('credentials')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('aws-survey verify', result.stdout)
        self.assert_stage(self.run_cli(), 3, 'aws-survey verify')
        config = json.loads((self.target / 'environment.json').read_text())
        config['setup']['readonly_verified'] = '2026-09-08'
        (self.target / 'environment.json').write_text(json.dumps(config))
        self.assert_stage(self.run_cli(), 5, 'aws-survey scan', 'aws-survey claude')
        result = self.run_cli('run', 'bash')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        launch = self.calls()[-1]
        self.assertEqual(launch[0], 'docker')
        self.assertIn(f'{self.target}/out:/home/node/aws-survey/out', launch)
        self.assertIn(f'{self.home}/.aws-survey/target:/home/node/.aws-claude:ro', launch)
        self.assertTrue((self.target / 'out/01_基礎調査/raw').is_dir())
        # No wrapper scripts at the repository root: the only entry point is bin/aws-survey.
        self.assertFalse((ROOT / 'run.sh').exists())
        self.assertFalse((ROOT / 'aws-survey').exists())
        self.assertFalse((ROOT / 'out/.gitkeep').exists())


if __name__ == '__main__':
    unittest.main()
