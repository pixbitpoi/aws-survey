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
              'test', 'touch']

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
    print("arn:aws:iam::000000000000:user/fake" if "--query" in argv
          else json.dumps({"Arn": "arn:aws:iam::000000000000:user/fake", "Account": "000000000000"}))
elif "get-role" in argv:
    if "json" in argv:
        print(json.dumps({"Role": {"Arn": "arn:aws:iam::000000000000:role/fake-role", "MaxSessionDuration": 3600,
                                   "AssumeRolePolicyDocument": {"Statement": [{"Effect": "Allow",
                                       "Principal": {"AWS": "arn:aws:iam::000000000000:user/fake"}, "Action": "sts:AssumeRole",
                                       "Condition": {"Bool": {"aws:MultiFactorAuthPresent": "true"}}}]}}}))
    else:
        print("arn:aws:iam::000000000000:role/fake-role")
elif argv[:2] == ["iam", "list-attached-role-policies"]:
    print("arn:aws:iam::aws:policy/ReadOnlyAccess")
elif "simulate-principal-policy" in argv:
    print("iam:CreateRole\\tallowed\\nec2:DescribeVpcs\\tallowed")
elif "assume-role" in argv:
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
                                  role_name='fake-role', refresh_command=None)
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
        answers = ['y', 'demo', '1', '', '', '', '', '', '1800', '']
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
        self.assert_stage(self.run_cli(), 2, 'aws-survey role', 'role --create')
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
        self.assertIn(str(self.home / '.aws-survey/smoke/credentials'), result.stdout)

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
        self.assert_stage(result, 5, 'aws-survey run', '調査: 未開始')
        self.assertRegex(result.stdout, r'約 4[45] 分 / 60 分')

    def test_stage_5_by_mtime_without_session(self):
        self.verified()
        self.write_keys(session=False)
        self.assert_stage(self.run_cli(), 5, 'aws-survey run', '推定')

    def test_stage_5_survey_started(self):
        self.verified()
        self.write_keys(iso(datetime.now(timezone.utc) + timedelta(minutes=45)))
        (self.target / 'out').mkdir()
        (self.target / 'out/00_進捗.md').write_text('# 進捗\n')
        self.assert_stage(self.run_cli(), 5, '開始済み')

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
        self.assertIn('次に打つコマンド', result.stdout)
        self.assertIn('aws-survey verify', result.stdout)
        self.assertNotIn('aws-survey run', result.stdout)
        assume = next(call for call in self.calls() if 'assume-role' in call)
        self.assertIn('arn=arn:aws:iam::aws:policy/ReadOnlyAccess', assume)
        self.assertIn('--policy', assume)

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
        self.assertRegex(result.stdout, r'約 (19|20) 分 / 30 分')
        self.assertIn(f'一時キー          {self.home}/.aws-survey/smoke', result.stdout)
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
        self.assertEqual(config['auth']['duration_seconds'], 3600)
        self.assertEqual(config['auth']['role_name'], 'aws-survey-readonly')
        self.assertEqual(config['phase_dir'], '01_基礎調査')
        self.assertEqual(config['setup'], {k: None for k in template['setup']})
        loaded = self.load_env()
        self.assertEqual(loaded.returncode, 0, loaded.stderr)
        self.assertEqual(loaded.stdout.strip(),
                         f'target|000000000000|ap-northeast-1|fake-src|aws-survey-readonly|3600|{self.home}/.aws-survey/target|true')
        self.assertIn('aws-survey role', result.stdout)
        self.assertNotIn('未実装', result.stdout)

    def test_noninteractive_from_json_and_env_precedence(self):
        src = self.base / 'from.json'
        src.write_text(json.dumps({'name': 'from-json', 'account_id': '111111111111', 'region': 'us-east-1',
                                   'auth': {'source_profile': 'sso-prof', 'principal_arn': 'arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_x/u',
                                            'mfa_required': False, 'refresh_command': 'aws sso login --profile sso-prof',
                                            'duration_seconds': 7200, 'role_name': 'ro-role'}}))
        result = self.run_cli('init', '--from', str(src), AWS_SURVEY_INIT_NAME='from-env')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        config = json.loads((self.target / 'environment.json').read_text())
        self.assertEqual(config['name'], 'from-env')
        self.assertEqual(config['account_id'], '111111111111')
        self.assertEqual(config['auth']['source_profile'], 'sso-prof')
        self.assertEqual(config['auth']['mfa_required'], False)
        self.assertEqual(config['auth']['refresh_command'], 'aws sso login --profile sso-prof')
        self.assertEqual(config['auth']['duration_seconds'], 7200)
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

        The host agent's job ends at `aws-survey run`, so this file names no rule in the
        distribution: everything after launch belongs to the survey agent in the container.
        """
        self.run_cli('init', **INIT_ENV)
        self.assertTrue((self.target / 'out/_環境').is_dir())
        agents = (self.target / 'AGENTS.md').read_text()
        self.assertNotIn('.agents/rules', agents)
        self.assertIn('対象の概要・調査項目', agents)
        self.assertEqual((self.target / 'CLAUDE.md').read_text(), '@AGENTS.md\n')
        self.assertFalse((self.target / '.gitignore').exists())
        self.assertNotIn('TEMPLATE', agents)

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
        # name, profile(2=sso-prof), arn, account, mfa, refresh, region, duration, role
        answers = ['', '2', '', '', '', '', '', '3600', '']
        result = self.run_cli('init', input='\n'.join(answers) + '\n')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        config = json.loads((self.target / 'environment.json').read_text())
        self.assertEqual(config['name'], 'target')
        self.assertEqual(config['auth']['source_profile'], 'sso-prof')
        self.assertEqual(config['auth']['principal_arn'], 'arn:aws:iam::000000000000:user/fake')
        self.assertEqual(config['account_id'], '000000000000')
        self.assertEqual(config['auth']['mfa_required'], True)
        self.assertEqual(config['auth']['refresh_command'], 'aws sso login --profile sso-prof')
        self.assertEqual(config['region'], 'ap-northeast-1')
        self.assertIn('1/9', result.stdout)
        self.assertIn('9/9', result.stdout)
        self.assertNotIn('調べたい', result.stdout.split('9 問')[0])
        sts = [c for c in self.calls() if 'get-caller-identity' in c]
        self.assertEqual(len(sts), 1)
        self.assertIn('sso-prof', sts[0])

    def test_interactive_hides_base_profile_when_mfa_destination_exists(self):
        # 一覧に <名>-mfa があるとき、元の <名> は候補に出ず、番号は残った候補で振り直される
        profiles = 'fake-src\\nsso-prof\\nbase\\nbase-mfa'
        answers = ['', 'base', '3', '', '', '', '', '', '3600', '']
        result = self.run_cli('init', input='\n'.join(answers) + '\n', FAKE_PROFILES=profiles)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('base は <名>-mfa がある長期キー側なので省いています', result.stdout)
        self.assertIn(' 3) base-mfa', result.stdout)
        self.assertNotIn(' base\n', result.stdout.split('3/9')[0])
        self.assertIn('一覧にありません', result.stdout)
        config = json.loads((self.target / 'environment.json').read_text())
        self.assertEqual(config['auth']['source_profile'], 'base-mfa')

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
        read_until('3/9'.encode())
        for _ in range(7):
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
        self.assertIn('✔ name: target', text)
        self.assertIn('◆ 9/9 role_name', text)
        self.assertNotIn('name [', text)
        config = json.loads((self.target / 'environment.json').read_text())
        self.assertEqual(config['auth']['source_profile'], 'sso-prof')

    def test_interactive_retries_invalid_and_fails_on_eof(self):
        answers = ['bad name!', 'ok-name', 'fake-src', '', '', '', '-', '', '']
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

    def test_existing_role_with_wrong_principal_lists_shortfall(self):
        self.write_environment()
        config = json.loads((self.target / 'environment.json').read_text())
        config['auth']['principal_arn'] = 'arn:aws:iam::000000000000:user/someone-else'
        (self.target / 'environment.json').write_text(json.dumps(config))
        result = self.run_cli('role')
        self.assertIn('信頼ポリシーに自分', result.stdout)
        self.assertIn('不足があります', result.stdout)
        self.assertIn('aws-survey role --create', result.stdout)


class Chain(CliCase):
    """No-arg `aws-survey` asks before each step and keeps going: guidance, confirm, run, judge again.
    Declining, a step that fails, or no terminal all stop the chain with the command still named."""

    def verified(self):
        self.write_environment(setup={'route_decided': '2026-09-08', 'role_created': '2026-09-08',
                                      'readonly_verified': '2026-09-08'})

    def test_declining_leaves_the_command_named_and_touches_nothing(self):
        self.write_environment()
        result = self.run_cli(input='n\n')
        self.assert_stage(result, 2, '次に打つコマンド', 'aws-survey role を実行しますか')
        self.assertEqual(self.calls(), [])

    def test_role_runs_then_the_chain_offers_create(self):
        self.write_environment()
        result = self.run_cli(input='y\nn\n')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(any('simulate-principal-policy' in call for call in self.calls()))
        self.assertIn('aws-survey role --create を実行しますか', result.stdout)
        self.assertNotIn('aws iam create-role', result.stdout)

    def test_create_advances_the_stage_and_the_chain_offers_credentials(self):
        self.write_environment()
        result = self.run_cli(input='y\ny\nn\n')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        config = json.loads((self.target / 'environment.json').read_text())
        self.assertTrue(config['setup']['role_created'])
        self.assertIn('● 3 ', result.stdout)
        self.assertIn('aws-survey credentials を実行しますか', result.stdout)

    def test_credentials_then_run(self):
        self.verified()
        result = self.run_cli(input='y\ny\n')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.home / '.aws-survey/smoke/session.json').exists())
        launch = self.calls()[-1]
        self.assertEqual(launch[0], 'docker')
        self.assertIn('run', launch)

    def test_a_failed_step_stops_the_chain(self):
        self.verified()
        self.add_tool('aws', '#!/bin/sh\nexit 1\n')
        result = self.run_cli(input='y\ny\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('aws-survey credentials が失敗しました', result.stdout)
        self.assertNotIn('aws-survey run を実行しますか', result.stdout)

    def test_chained_commands_do_not_print_their_own_next_step(self):
        self.write_environment(setup={'route_decided': '2026-09-08', 'role_created': '2026-09-08'})
        result = self.run_cli('credentials', AWS_SURVEY_CHAIN='1')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn('次に打つコマンド', result.stdout)


class EmptyFolderToRun(CliCase):
    """S4 gate, as far as fakes allow: an empty folder goes guide → init → role --create →
    credentials → run, with each guide step naming the command that was run next.
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
        self.assert_stage(self.run_cli(), 5, 'aws-survey run')
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
