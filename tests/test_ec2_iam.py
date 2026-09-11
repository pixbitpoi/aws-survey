"""IAM side of the EC2 inspection feature, checked with a fake `aws` (no AWS).

`aws-survey role --create` must create the customer-managed policy diag-ssh-<name> and attach it
to the survey role when environment.json has ssh.hosts, do nothing of the kind when it has none,
be idempotent (a second run creates nothing, a drifted document gets a new default version), and
only print the document when the role is borrowed. `aws-survey credentials` must pass that policy
in --policy-arns next to ReadOnlyAccess only when hosts are registered. `aws-survey verify` must
try start-session against an instance without the tag and against the non-SSH documents.

The fake keeps the policy in a state directory so a second invocation sees what the first created.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / 'bin/aws-survey'
TOOLS = ['bash', 'sh', 'env', 'jq', 'sed', 'grep', 'awk', 'cut', 'head', 'tail', 'cat', 'wc', 'tr',
         'dirname', 'basename', 'readlink', 'mkdir', 'chmod', 'cp', 'mv', 'rm', 'stat', 'date',
         'mktemp', 'python3', 'printf', 'echo', 'test', 'touch', 'uname', 'sort', 'ls']
ACCOUNT = '000000000000'
DIAG_ARN = f'arn:aws:iam::{ACCOUNT}:policy/diag-ssh-smoke'
RO_ARN = 'arn:aws:iam::aws:policy/ReadOnlyAccess'
WEB1 = 'i-0123456789abcdef0'
OTHER = 'i-0fedcba9876543210'

FAKE_AWS = r'''#!/usr/bin/env python3
"""Fake aws for the IAM side. State lives in FAKE_STATE (policy.json = current document,
attached.txt = policies attached to the role). Knobs:
  FAKE_ROLE          exists: get-role succeeds (default: missing until create-role)
  FAKE_OLD_VERSIONS  space-separated non-default version ids answered by list-policy-versions
  FAKE_ASSUME_FAIL   1: assume-role with --policy-arns fails because the diag policy is missing
  FAKE_SESSION_ALLOWED  1: start-session is allowed (the CLI then fails to find the plugin)
  FAKE_LAMBDA_ALLOWED   1: get-function is allowed (the canary name then answers ResourceNotFoundException)
"""
import json, os, sys
argv = sys.argv[1:]
state = os.environ["FAKE_STATE"]
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps(["aws"] + argv) + "\n")
def opt(name, default=None):
    return argv[argv.index(name) + 1] if name in argv else default
def read_doc(arg):
    return json.load(open(arg[len("file://"):])) if arg.startswith("file://") else json.loads(arg)
def fail(msg, code=254):
    sys.stderr.write("An error occurred (%s)\n" % msg); sys.exit(code)
policy_file = os.path.join(state, "policy.json")
attached_file = os.path.join(state, "attached.txt")
role_file = os.path.join(state, "role")
def attached():
    return open(attached_file).read().split() if os.path.exists(attached_file) else []
svc, op = (argv[0], argv[1]) if len(argv) > 1 else ("", "")
if op == "get-caller-identity":
    if os.environ.get("AWS_PROFILE") == "claude-ro":       # verify runs with the issued key
        print("arn:aws:sts::000000000000:assumed-role/fake-role/claude-survey-x"); sys.exit(0)
    print("arn:aws:iam::000000000000:user/fake"); sys.exit(0)
if op == "get-role":
    if os.environ.get("FAKE_ROLE") != "exists" and not os.path.exists(role_file):
        fail("NoSuchEntity")
    if "json" in argv:
        print(json.dumps({"Role": {"Arn": "arn:aws:iam::000000000000:role/fake-role", "MaxSessionDuration": 3600,
                                   "AssumeRolePolicyDocument": {"Statement": [{"Effect": "Allow",
                                       "Principal": {"AWS": "arn:aws:iam::000000000000:user/fake"}, "Action": "sts:AssumeRole",
                                       "Condition": {"Bool": {"aws:MultiFactorAuthPresent": "true"}}}]}}}))
    else:
        print("arn:aws:iam::000000000000:role/fake-role")
    sys.exit(0)
if op == "list-attached-role-policies":
    print("\t".join(attached())); sys.exit(0)
if op == "simulate-principal-policy":
    print("iam:CreateRole\tallowed\nec2:DescribeVpcs\tallowed"); sys.exit(0)
if op == "create-role":
    open(role_file, "w").close(); print("arn:aws:iam::000000000000:role/fake-role"); sys.exit(0)
if op in ("update-assume-role-policy", "update-role"):
    print("{}"); sys.exit(0)
if op == "attach-role-policy":
    with open(attached_file, "a") as f:
        f.write(opt("--policy-arn") + "\n")
    sys.exit(0)
if op == "get-policy":
    if not os.path.exists(policy_file):
        fail("NoSuchEntity")
    print("v1"); sys.exit(0)
if op == "get-policy-version":
    print(json.dumps(json.load(open(policy_file)))); sys.exit(0)
if op == "list-policy-versions":
    print(os.environ.get("FAKE_OLD_VERSIONS", "")); sys.exit(0)
if op == "delete-policy-version":
    print("{}"); sys.exit(0)
if op in ("create-policy", "create-policy-version"):
    if op == "create-policy" and os.path.exists(policy_file):
        fail("EntityAlreadyExists")
    json.dump(read_doc(opt("--policy-document")), open(policy_file, "w"))
    print("arn:aws:iam::000000000000:policy/" + opt("--policy-name", "diag-ssh-smoke")); sys.exit(0)
if op == "assume-role":
    if "--policy-arns" not in argv:
        fail("AccessDenied when calling the AssumeRole operation")
    if os.environ.get("FAKE_ASSUME_FAIL") == "1":
        fail("AccessDenied: policy arn:aws:iam::000000000000:policy/diag-ssh-smoke does not exist or is not attachable")
    print(json.dumps({"Credentials": {"AccessKeyId": "AKIAFAKE", "SecretAccessKey": "fake", "SessionToken": "fake",
                                      "Expiration": "2099-01-01T00:00:00+00:00"},
                      "AssumedRoleUser": {"Arn": "arn:aws:sts::000000000000:assumed-role/fake-role/x"},
                      "PackedPolicySize": 42}))
    sys.exit(0)
# ---- verify ----
if op == "describe-vpcs":
    print("vpc-0fake"); sys.exit(0)
if op == "create-tags":
    fail("UnauthorizedOperation")
if op in ("describe-parameters", "list-queues"):
    print("None"); sys.exit(0)
if op == "get-function":
    if os.environ.get("FAKE_LAMBDA_ALLOWED") == "1":
        fail("ResourceNotFoundException) when calling the GetFunction operation: Function not found")
    fail("AccessDeniedException) when calling the GetFunction operation: not authorized to perform: lambda:GetFunction")
if op == "describe-instances":
    print(json.dumps([{"id": "__WEB1__", "tag": "smoke"}, {"id": "__OTHER__", "tag": None}])); sys.exit(0)
if op == "start-session":
    if os.environ.get("FAKE_SESSION_ALLOWED") == "1":
        sys.stderr.write("SessionManagerPlugin is not found.\n"); sys.exit(255)
    fail("AccessDeniedException when calling the StartSession operation: not authorized to perform: ssm:StartSession")
print("{}")
'''.replace('__WEB1__', WEB1).replace('__OTHER__', OTHER)


class IamCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name).resolve()
        self.home = base / 'home'
        self.target = base / 'target'
        self.bin = base / 'bin'
        self.state = base / 'state'
        for d in (self.home, self.target, self.bin, self.state):
            d.mkdir()
        for tool in TOOLS:
            found = shutil.which(tool)
            if found:
                (self.bin / tool).symlink_to(found)
        (self.bin / 'aws').write_text(FAKE_AWS)
        (self.bin / 'aws').chmod(0o755)
        self.log = base / 'calls.jsonl'

    def tearDown(self):
        self.temp.cleanup()

    def write_environment(self, hosts=True, route='own_role', setup=None):
        config = json.loads((ROOT / 'templates/environment.json').read_text())
        config.update(name='smoke', account_id=ACCOUNT, region='test-region')
        config['auth'].update(route=route, source_profile='fake-src', mfa_required=False,
                              principal_arn='arn:aws:iam::000000000000:user/fake',
                              role_name='fake-role', refresh_command=None)
        if hosts:
            config['ssh']['hosts'] = {'web1': {'instance_id': WEB1, 'user': 'diag', 'installed_at': '2026-09-10T00:00:00+00:00',
                                               'logs': {}, 'deny': [], 'strict': False}}
        if setup:
            config['setup'].update(setup)
        (self.target / 'environment.json').write_text(json.dumps(config))

    def run_cli(self, *args, **knobs):
        env = {'PATH': str(self.bin), 'HOME': str(self.home), 'LANG': os.environ.get('LANG', 'C.UTF-8'),
               'LC_ALL': os.environ.get('LC_ALL', ''), 'TZ': 'UTC', 'FAKE_LOG': str(self.log),
               'FAKE_STATE': str(self.state)}
        env = {k: v for k, v in env.items() if v}
        env.update(knobs)
        return subprocess.run([str(CLI), '--dir', str(self.target), *args], capture_output=True, text=True,
                              errors='replace', env=env)

    def calls(self, op=None):
        if not self.log.exists():
            return []
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        return [c for c in calls if op is None or (len(c) > 2 and c[2] == op)]

    def clear_log(self):
        if self.log.exists():
            self.log.unlink()

    def policy_document(self):
        return json.loads((self.state / 'policy.json').read_text())


class RoleCreate(IamCase):
    def test_creates_policy_and_attaches_it_after_readonly(self):
        self.write_environment()
        result = self.run_cli('role', '--create')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        create = self.calls('create-policy')
        self.assertEqual(len(create), 1)
        self.assertIn('fake-src', create[0])
        self.assertEqual(create[0][create[0].index('--policy-name') + 1], 'diag-ssh-smoke')
        description = create[0][create[0].index('--description') + 1]
        self.assertTrue(description.isascii(), description)
        for word in ('ai', 'agent', 'survey', 'claude'):
            self.assertNotIn(word, description.lower())
        attached = [c[c.index('--policy-arn') + 1] for c in self.calls('attach-role-policy')]
        self.assertEqual(attached, [RO_ARN, DIAG_ARN])
        self.assertIn('diag-ssh-smoke を作りました', result.stdout)
        setup = json.loads((self.target / 'environment.json').read_text())['setup']
        self.assertRegex(setup['ssh_policy_attached'], r'^\d{4}-\d{2}-\d{2}$')

    def test_policy_document_limits_start_session_to_tag_and_ssh_document(self):
        self.write_environment()
        self.assertEqual(self.run_cli('role', '--create').returncode, 0)
        doc = self.policy_document()
        self.assertEqual(doc['Version'], '2012-10-17')
        text = json.dumps(doc)
        self.assertNotIn('SendCommand', text)
        self.assertNotIn('"*"', text.replace('instance/*', '').replace('session/claude-survey-*', ''))
        by_action = {}
        for st in doc['Statement']:
            self.assertEqual(st['Effect'], 'Allow')
            actions = st['Action'] if isinstance(st['Action'], list) else [st['Action']]
            for a in actions:
                by_action.setdefault(a, []).append(st)
        self.assertEqual(sorted(by_action), ['ssm:ResumeSession', 'ssm:StartSession', 'ssm:TerminateSession'])
        instance = [s for s in by_action['ssm:StartSession'] if 'instance/' in s['Resource']]
        document = [s for s in by_action['ssm:StartSession'] if 'document/' in s['Resource']]
        self.assertEqual(len(instance), 1)
        self.assertEqual(instance[0]['Resource'], f'arn:aws:ec2:*:{ACCOUNT}:instance/*')
        self.assertEqual(instance[0]['Condition'], {'StringEquals': {'ssm:resourceTag/diag:ssh': 'smoke'}})
        self.assertEqual(len(document), 1)
        self.assertEqual(document[0]['Resource'], 'arn:aws:ssm:*:*:document/AWS-StartSSHSession')
        self.assertNotIn('Condition', document[0])
        # Terminate / Resume only for sessions the survey key started (<role session name>-<random>)
        self.assertEqual(by_action['ssm:TerminateSession'][0]['Resource'], 'arn:aws:ssm:*:*:session/claude-survey-*')
        self.assertNotIn('aws:userid', text)

    def test_second_run_creates_nothing_but_still_attaches(self):
        self.write_environment()
        self.assertEqual(self.run_cli('role', '--create').returncode, 0)
        self.clear_log()
        result = self.run_cli('role', '--create')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.calls('create-policy'), [])
        self.assertEqual(self.calls('create-policy-version'), [])
        self.assertIn(DIAG_ARN, [c[c.index('--policy-arn') + 1] for c in self.calls('attach-role-policy')])
        self.assertIn('内容も同じ', result.stdout)

    def test_drifted_document_gets_a_new_default_version(self):
        self.write_environment()
        self.assertEqual(self.run_cli('role', '--create').returncode, 0)
        good = self.policy_document()
        (self.state / 'policy.json').write_text(json.dumps({'Version': '2012-10-17', 'Statement': [
            {'Effect': 'Allow', 'Action': 'ssm:StartSession', 'Resource': '*'}]}))
        self.clear_log()
        result = self.run_cli('role', '--create', FAKE_OLD_VERSIONS='v0 v2')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.calls('create-policy'), [])
        new_version = self.calls('create-policy-version')
        self.assertEqual(len(new_version), 1)
        self.assertIn('--set-as-default', new_version[0])
        self.assertEqual(sorted(c[c.index('--version-id') + 1] for c in self.calls('delete-policy-version')), ['v0', 'v2'])
        self.assertEqual(self.policy_document(), good)
        self.assertIn('内容を合わせました', result.stdout)

    def test_without_registered_hosts_no_policy_is_touched(self):
        self.write_environment(hosts=False)
        result = self.run_cli('role', '--create')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for op in ('get-policy', 'create-policy', 'create-policy-version'):
            self.assertEqual(self.calls(op), [], op)
        attached = [c[c.index('--policy-arn') + 1] for c in self.calls('attach-role-policy')]
        self.assertEqual(attached, [RO_ARN])
        self.assertFalse((self.state / 'policy.json').exists())
        self.assertIn('ssh setup', result.stdout)

    def test_borrowed_role_prints_the_document_for_an_admin(self):
        self.write_environment(route='existing_role', setup={'route_decided': '2026-09-10', 'role_created': '2026-09-10'},)
        result = self.run_cli('role', '--create', FAKE_ROLE='exists')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for op in ('create-policy', 'create-policy-version', 'attach-role-policy', 'create-role'):
            self.assertEqual(self.calls(op), [], op)
        self.assertIn('"ssm:resourceTag/diag:ssh": "smoke"', result.stdout)
        self.assertIn('AWS-StartSSHSession', result.stdout)
        self.assertIn('aws iam create-policy --policy-name diag-ssh-smoke', result.stdout)
        self.assertIn('attach-role-policy --role-name fake-role --policy-arn ' + DIAG_ARN, result.stdout)

    def test_borrowed_role_records_the_policy_once_an_admin_attached_it(self):
        self.write_environment(route='existing_role', setup={'route_decided': '2026-09-10', 'role_created': '2026-09-10'},)
        (self.state / 'attached.txt').write_text(RO_ARN + '\n' + DIAG_ARN + '\n')
        result = self.run_cli('role', '--create', FAKE_ROLE='exists')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn('aws iam create-policy', result.stdout)
        self.assertIn('aws-survey credentials', result.stdout)
        setup = json.loads((self.target / 'environment.json').read_text())['setup']
        self.assertRegex(setup['ssh_policy_attached'], r'^\d{4}-\d{2}-\d{2}$')

    def test_borrowed_role_without_hosts_has_nothing_to_show(self):
        self.write_environment(hosts=False, route='existing_role')
        result = self.run_cli('role', '--create', FAKE_ROLE='exists')
        self.assertEqual(result.returncode, 1)
        self.assertNotIn('diag:ssh', result.stdout)

    def test_check_lists_the_missing_policy_as_a_shortfall_only_with_hosts(self):
        self.write_environment()
        (self.state / 'attached.txt').write_text(RO_ARN + '\n')
        result = self.run_cli('role', FAKE_ROLE='exists')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('diag-ssh-smoke）が付いていません', result.stdout)
        self.assertIn('aws-survey role --create', result.stdout)
        self.write_environment(hosts=False)
        result = self.run_cli('role', FAKE_ROLE='exists')
        self.assertNotIn('diag-ssh-smoke', result.stdout)
        self.assertIn('ロールは用意できています', result.stdout)


class Credentials(IamCase):
    def assume_call(self):
        calls = self.calls('assume-role')
        self.assertEqual(len(calls), 1)
        return calls[0]

    def policy_arns(self, call):
        i = call.index('--policy-arns') + 1
        arns = []
        while i < len(call) and call[i].startswith('arn='):
            arns.append(call[i]); i += 1
        return arns

    def test_diag_policy_is_listed_after_readonly_when_hosts_are_registered(self):
        self.write_environment(setup={'route_decided': '2026-09-10', 'role_created': '2026-09-10'})
        result = self.run_cli('credentials', FAKE_ROLE='exists')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        call = self.assume_call()
        self.assertEqual(self.policy_arns(call), ['arn=' + RO_ARN, 'arn=' + DIAG_ARN])
        guard = json.loads(call[call.index('--policy') + 1])
        self.assertTrue(all(st['Effect'] == 'Deny' for st in guard['Statement']))
        self.assertTrue(call[call.index('--role-session-name') + 1].startswith('claude-survey-'))
        self.assertIn('diag-ssh-smoke も付けます', result.stdout)
        self.assertTrue((self.home / '.aws-survey/smoke/credentials').exists())

    def test_only_readonly_without_hosts(self):
        self.write_environment(hosts=False, setup={'route_decided': '2026-09-10', 'role_created': '2026-09-10'})
        result = self.run_cli('credentials', FAKE_ROLE='exists')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.policy_arns(self.assume_call()), ['arn=' + RO_ARN])
        self.assertNotIn('diag-ssh', result.stdout)

    def test_missing_policy_points_at_role_create_and_writes_nothing(self):
        self.write_environment(setup={'route_decided': '2026-09-10', 'role_created': '2026-09-10'})
        result = self.run_cli('credentials', FAKE_ROLE='exists', FAKE_ASSUME_FAIL='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('aws-survey role --create', result.stdout)
        self.assertFalse((self.home / '.aws-survey/smoke/credentials').exists())


class Verify(IamCase):
    def prepare(self, hosts=True):
        self.write_environment(hosts=hosts, setup={'route_decided': '2026-09-10', 'role_created': '2026-09-10'})
        self.assertEqual(self.run_cli('credentials', FAKE_ROLE='exists').returncode, 0)
        self.clear_log()

    def sessions(self):
        return [(c[c.index('--target') + 1], c[c.index('--document-name') + 1], c[c.index('--parameters') + 1])
                for c in self.calls('start-session')]

    def test_tries_an_untagged_instance_and_the_non_ssh_documents(self):
        self.prepare()
        result = self.run_cli('verify')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.sessions(), [
            (OTHER, 'AWS-StartSSHSession', 'portNumber=22'),
            (WEB1, 'AWS-StartInteractiveCommand', 'command=uptime'),
            (WEB1, 'AWS-StartPortForwardingSession', 'portNumber=22,localPortNumber=0')])
        self.assertIn('タグの無いインスタンスへの接続は拒否されました', result.stdout)
        self.assertIn('ポート転送（AWS-StartPortForwardingSession）は拒否されました', result.stdout)
        self.assertIn('問題あり          0 件', result.stdout)
        self.assertTrue(json.loads((self.target / 'environment.json').read_text())['setup']['readonly_verified'])

    def test_an_allowed_session_is_a_failure(self):
        self.prepare()
        result = self.run_cli('verify', FAKE_SESSION_ALLOWED='1')
        self.assertEqual(result.returncode, 1)
        self.assertIn('接続できてしまいます', result.stdout)
        self.assertIn('diag-ssh-smoke', result.stdout)
        self.assertIsNone(json.loads((self.target / 'environment.json').read_text())['setup']['readonly_verified'])

    def test_the_code_url_is_asked_for_a_function_that_does_not_exist(self):
        self.prepare(hosts=False)
        result = self.run_cli('verify')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        [call] = self.calls('get-function')
        self.assertEqual(call[call.index('--function-name') + 1], 'verify-canary-does-not-exist')
        self.assertIn('関数のコードの URL の取得は拒否されました', result.stdout)

    def test_a_reachable_code_url_is_a_failure(self):
        self.prepare(hosts=False)
        result = self.run_cli('verify', FAKE_LAMBDA_ALLOWED='1')
        self.assertEqual(result.returncode, 1)
        self.assertIn('関数のコードの URL を取得できてしまいます', result.stdout)
        self.assertIsNone(json.loads((self.target / 'environment.json').read_text())['setup']['readonly_verified'])

    def test_skipped_without_hosts(self):
        self.prepare(hosts=False)
        result = self.run_cli('verify')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.sessions(), [])
        self.assertIn('登録済みホストが無いため飛ばしました', result.stdout)


if __name__ == '__main__':
    unittest.main()
