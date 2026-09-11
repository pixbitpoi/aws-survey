import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / 'container/hooks'
spec = importlib.util.spec_from_file_location('guard', HOOKS / 'codex-guard.py')
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class Guards(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        (self.root / 'out/_環境').mkdir(parents=True)
        self.env = patch.dict(os.environ, {'SURVEY_AUDIT_LOG': str(self.root / 'audit.tsv')})
        self.env.start()
        self.root_patch = patch.object(guard, 'ROOT', self.root)
        self.root_patch.start()

    def tearDown(self):
        self.root_patch.stop()
        self.env.stop()
        self.temp.cleanup()

    def check_codex(self, name, command, allowed):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            try:
                guard.check({'tool_name': name, 'tool_input': {'command': command}, 'cwd': str(self.root)})
            except SystemExit:
                pass
        if allowed:
            self.assertEqual(output.getvalue(), '', output.getvalue())
        else:
            self.assertEqual(json.loads(output.getvalue())['hookSpecificOutput']['permissionDecision'], 'deny')

    def test_aws_policy_for_both_agents(self):
        cases = [
            ('aws ec2 describe-vpcs', True),
            ('aws ec2 describe-vpcs > out/01_基礎調査/raw/vpcs.json', True),
            ('aws resource-explorer-2 search --query-string "*" > out/01_基礎調査/raw/rex.json', True),
            ('aws ec2 terminate-instances --instance-ids i-0', False),
            ('aws sqs receive-message --queue-url example', False),
            ('aws ssm get-parameter --name example', False),
            ('aws sts assume-role --role-arn example', False),
            ('aws ec2 describe-vpcs --profile other', False),
            ('aws ec2 describe-vpcs | head -3', False),
            ('aws ec2 describe-vpcs > out/../escape.json', False),
            ('aws ec2 describe-vpcs > out//escape.json', False),
            ('aws ec2 describe-vpcs > /tmp/escape.json', False),
            ('aws ec2 describe-vpcs > out/report.md', False),
            # a second line is a second command: the guard must not read it as more arguments
            ('aws ec2 describe-vpcs\naws ec2 terminate-instances --instance-ids i-0', False),
            ('aws ec2 describe-vpcs\raws ec2 terminate-instances --instance-ids i-0', False),
            ('aws ec2 describe-vpcs --max-items 1\naws lambda get-function --function-name f', False),
            ('aws ec2 describe-vpcs \\\n  --max-items 5', False),
            ('aws s3 ls x\\\\\naws ec2 terminate-instances --instance-ids i-0', False),
            ('ec2 web1 uptime\nid', False),
            # EC2 の中を調べる経路: ec2 ラッパーだけを、aws と同じ規則で通す
            ('ec2 web1 uptime', True),
            ('ec2 web1 help', True),
            ('ec2 --list', True),
            ('ec2 --selftest web1', True),
            ('ec2 web1 tail /var/log/messages --lines 200 > out/01_基礎調査/raw/web1-messages.txt', True),
            ('ec2 web1 grep /var/log/secure --pattern ssh --ignore-case', True),
            ('ec2 web1 service sshd', True),
            ('ec2 web1', False),
            ('ec2 -F /home/node/.aws-claude/ssh/config web1 uptime', False),
            ('ec2 web1 uptime | head -3', False),
            ('ec2 web1 uptime; id', False),
            ('ec2 web1 uptime > out/web1.md', False),
            ('ec2 web1 uptime > out/../escape.txt', False),
            ('ssh web1 bash', False),
            ('ssh -F /home/node/.aws-claude/ssh/config web1 uptime', False),
            ('scp web1:/etc/passwd out/', False),
            ('sftp web1', False),
            ('session-manager-plugin', False),
            ('aws ssm start-session --target i-0 --document-name AWS-StartSSHSession', False),
            ('cat /home/node/.aws-claude/ssh/id_ed25519', False),
            # Lambda: code URLs are refused by service and verb together; cloudfront get-function is a different API
            ('aws lambda get-function-configuration --function-name f', True),
            ('aws lambda list-functions --max-items 10', True),
            ('aws cloudfront get-function --name f out/01_基礎調査/raw/cf-f.txt', True),
            ('aws lambda get-function --function-name f', False),
            ('aws lambda get-function --function-name f > out/01_基礎調査/raw/f.json', False),
            ('aws lambda get-layer-version --layer-name l --version-number 1', False),
            ('aws lambda get-layer-version-by-arn --arn arn:aws:lambda:r:0:layer:l:1', False),
            # searching code and saved output may mention aws / boto3 when the reading command runs alone
            ('grep -rn boto3 code/', True),
            ("grep -rn 'aws.config' code/", True),
            ("rg -n '@aws-sdk/client-s3' code/", True),
            ('head -20 code/lambda/r/f/src/aws_client.py', True),
            ('wc -l out/01_基礎調査/raw/aws-lambda.json', True),
            ('grep boto3 code/ | aws s3 ls', False),
            ('grep $(aws sts get-caller-identity) code/', False),
            ('grep -rn boto3 code/ > out/hits.txt', False),
            ('grep -rn boto3 code/; aws s3 ls', False),
            ('grep -rn boto3 code/\naws ec2 terminate-instances --instance-ids i-0', False),
            ('rg --pre=sh boto3 out/', False),
            ('rg --pre sh boto3 out/', False),
            ('rg --hostname-bin=sh boto3 out/', False),
        ]
        for command, allowed in cases:
            with self.subTest(command=command):
                payload = json.dumps({'tool_name': 'Bash', 'tool_input': {'command': command}})
                result = subprocess.run(['bash', str(HOOKS / 'aws-readonly-guard.sh')], input=payload, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0 if allowed else 2, result.stderr)
                self.check_codex('Bash', command, allowed)
        self.assertIn('ALLOW', (self.root / 'audit.tsv').read_text())
        self.assertIn('DENY', (self.root / 'audit.tsv').read_text())

    def test_bash_guard_only_cases(self):
        """Forms the Bash guard must judge on its own (Codex never runs them: interpreters and
        pipelines are refused before the AWS policy is consulted)."""
        cases = [
            ('bash -c "ec2 web1 uptime"', False),
            ('cd out && ec2 web1 uptime', False),
            ('echo hi | ssh web1 bash', False),
            ('xargs ssh', False),
            ('env ssh web1 uptime', False),
            ("python3 -c \"import os; os.system('ssh web1 bash')\"", False),
            ('$(ec2 web1 uptime)', False),
            # words inside arguments are not commands: reading saved output that mentions ssh is fine
            ('grep ssh out/01_基礎調査/raw/web1-secure.txt', True),
            ("grep -c 'ssh' out/01_基礎調査/raw/web1-secure.txt", True),
            ('jq . out/01_基礎調査/raw/raw-ec2.json', True),
            ('cat out/ec2/notes.txt', True),
            # the search exception is for the reading command alone, not as a prefix to something else
            ('grep -rn boto3 code/ && aws s3 ls', False),
            ('grep -rn boto3 `aws sts get-caller-identity`', False),
            ('ls code/\rpython3 -c "import boto3"', False),
            ('sed -n /boto3/p code/app.py', False),
        ]
        for command, allowed in cases:
            with self.subTest(command=command):
                payload = json.dumps({'tool_name': 'Bash', 'tool_input': {'command': command}})
                result = subprocess.run(['bash', str(HOOKS / 'aws-readonly-guard.sh')], input=payload, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0 if allowed else 2, result.stderr)

    def test_codex_shell(self):
        for command in ['ls out/', "jq '.Vpcs | length' out/raw.json", 'survey-status', 'mkdir -p out/01_基礎調査/raw']:
            with self.subTest(command=command):
                self.check_codex('Bash', command, True)
        for command in ['bash -c "aws sts get-caller-identity"', 'python3 -c "print(1)"', 'cat /home/node/.aws-claude/credentials', 'rm out/_環境/aws-audit.log', 'cat out/x; node script.js', 'rg --pre=sh text out/', 'mkdir -p /tmp/new', 'ls $(pwd)', 'cat out/x > out/_環境/aws-audit.log', 'ssh web1 uptime', 'cat /home/node/.aws-claude/ssh/config']:
            with self.subTest(command=command):
                self.check_codex('Bash', command, False)

    def test_patch_paths_and_moves(self):
        for path, allowed in [('out/report.md', True), ('AGENTS.md', False), ('CLAUDE.md', False), ('method/x.md', False), ('out/../AGENTS.md', False), ('out/_環境/aws-audit.log', False)]:
            with self.subTest(path=path):
                self.check_codex('apply_patch', f'*** Begin Patch\n*** Add File: {path}\n+text\n*** End Patch', allowed)
        self.check_codex('apply_patch', '*** Begin Patch\n*** Update File: out/x.md\n*** Move to: method/x.md\n@@\n-a\n+b\n*** End Patch', False)
        (self.root / 'out/alias').symlink_to(self.root / 'out/_環境')
        self.check_codex('apply_patch', '*** Begin Patch\n*** Add File: out/alias/aws-audit.log\n+x\n*** End Patch', False)

    def test_unknown_and_malformed_tools(self):
        self.check_codex('mcp__fs__write_file', 'x', False)
        self.check_codex('spawn_agent', 'x', False)
        self.check_codex('apply_patch', 'invalid patch', False)
        result = subprocess.run(['python3', str(HOOKS / 'codex-guard.py')], input='invalid json', text=True, capture_output=True)
        self.assertEqual(json.loads(result.stdout)['hookSpecificOutput']['permissionDecision'], 'deny')


if __name__ == '__main__':
    unittest.main()


class Ec2Wrapper(unittest.TestCase):
    """container/ec2 hands the agent's words to ssh in fixed positions, and nothing else.

    The host name must be one of the Host entries in the mounted config, options are never
    accepted, and the verb and arguments travel as one %q-quoted string after `--`, so nothing
    the agent types can become an ssh option. Checked with a fake ssh that records its argv.

    The wrapper also tidies SSM sessions in two layers, both through a fake aws here: before
    every connection it terminates the Active sessions its own key left on that instance
    (Terminating ones excluded), and after an abnormal exit (255) it terminates what that
    connection left behind. Only aws's arguments are recorded; nothing is contacted.
    """

    OWNER = 'arn:aws:sts::000000000000:assumed-role/fake-role/claude-survey-20260910T000000'

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        ssh_dir = self.root / 'home/.aws-claude/ssh'
        ssh_dir.mkdir(parents=True)
        (ssh_dir / 'config').write_text(
            '# generated\n\nHost web1\n    HostName i-0123456789abcdef0\n    User diag\n'
            '\nHost db-1\n    HostName i-0fedcba9876543210\n    User ops\n')
        (self.root / 'bin').mkdir()
        fake = self.root / 'bin/ssh'
        fake.write_text('#!/usr/bin/env python3\nimport json, os, sys\n'
                        'open(os.environ["FAKE_LOG"], "a").write(json.dumps(["ssh"] + sys.argv[1:]) + "\\n")\n'
                        'sys.exit(int(os.environ.get("FAKE_EXIT", "0")))\n')
        fake.chmod(0o755)
        # aws: get-caller-identity answers the owner, describe-sessions answers FAKE_SESSIONS
        # (lines of "<id> <start> <status>"; the first call only when FAKE_SESSIONS_ONCE=1), the rest is silent
        aws = self.root / 'bin/aws'
        aws.write_text('#!/usr/bin/env python3\nimport json, os, sys\n'
                       'argv = sys.argv[1:]\n'
                       'open(os.environ["FAKE_LOG"], "a").write(json.dumps(["aws"] + argv) + "\\n")\n'
                       'if "get-caller-identity" in argv:\n'
                       f'    print("{self.OWNER}")\n'
                       'elif "describe-sessions" in argv:\n'
                       '    n = sum(1 for l in open(os.environ["FAKE_LOG"]) if "describe-sessions" in l)\n'
                       '    if n == 1 or os.environ.get("FAKE_SESSIONS_ONCE") != "1":\n'
                       '        sys.stdout.write(os.environ.get("FAKE_SESSIONS", "").replace(" ", "\\t"))\n'
                       'sys.exit(0)\n')
        aws.chmod(0o755)
        self.log = self.root / 'calls.jsonl'

    def tearDown(self):
        self.temp.cleanup()

    def run_ec2(self, *args, **extra):
        env = dict(os.environ, HOME=str(self.root / 'home'), FAKE_LOG=str(self.log),
                   PATH=f'{self.root / "bin"}:{os.environ["PATH"]}')
        env.update(extra)
        return subprocess.run(['bash', str(ROOT / 'container/ec2'), *args], env=env, capture_output=True, text=True)

    def all_calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def calls(self):
        return [c[1:] for c in self.all_calls() if c[0] == 'ssh']

    def aws_ops(self):
        return [c[2] for c in self.all_calls() if c[0] == 'aws']

    def test_verb_and_arguments_are_one_quoted_string_after_the_host(self):
        result = self.run_ec2('web1', 'grep', '/var/log/my app.log', '--pattern', 'a|b; id $(x)')
        self.assertEqual(result.returncode, 0, result.stderr)
        [argv] = self.calls()
        config = str(self.root / 'home/.aws-claude/ssh/config')
        self.assertEqual(argv[:4], ['-F', config, '--', 'web1'])
        self.assertEqual(len(argv), 5)
        self.assertEqual(argv[4], "grep /var/log/my\\ app.log --pattern a\\|b\\;\\ id\\ \\$\\(x\\)")

    def test_exit_code_of_ssh_is_returned(self):
        result = self.run_ec2('web1', 'bash', FAKE_EXIT='2')
        self.assertEqual(result.returncode, 2)

    def test_unknown_host_and_options_never_reach_ssh(self):
        for args in [['web9', 'uptime'], ['-F', 'x', 'web1', 'uptime'], ['--foo'], ['web1'], ['../web1', 'uptime'], ['web1 x', 'uptime']]:
            with self.subTest(args=args):
                result = self.run_ec2(*args)
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertIn('ec2:', result.stderr)
        self.assertEqual(self.calls(), [])

    def test_list_and_help_read_the_config_only(self):
        self.assertEqual(self.run_ec2('--list').stdout.split(), ['web1', 'db-1'])
        self.assertIn('ec2 <host>', self.run_ec2('--help').stdout)
        self.assertEqual(self.calls(), [])

    def test_missing_config_is_a_plain_message(self):
        (self.root / 'home/.aws-claude/ssh/config').unlink()
        result = self.run_ec2('web1', 'uptime')
        self.assertEqual(result.returncode, 2)
        self.assertIn('接続設定がありません', result.stderr)

    SESSIONS = ('claude-survey-20260910T000000-aaaa 2026-09-10T00:00:01+00:00 Connected\n'
                'claude-survey-20260910T000000-bbbb 2026-09-10T00:00:02+00:00 Terminating\n'
                'claude-survey-20260910T000000-cccc 2026-09-10T00:00:03+00:00 Connecting\n')

    def test_leftover_sessions_are_terminated_before_connecting(self):
        result = self.run_ec2('web1', 'uptime', FAKE_SESSIONS=self.SESSIONS, FAKE_SESSIONS_ONCE='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.all_calls()
        kinds = [(c[0], c[2] if c[0] == 'aws' else 'ssh') for c in calls]
        self.assertEqual(kinds, [('aws', 'get-caller-identity'), ('aws', 'describe-sessions'),
                                 ('aws', 'terminate-session'), ('aws', 'terminate-session'), ('ssh', 'ssh')])
        describe = calls[1]
        self.assertIn('key=Target,value=i-0123456789abcdef0', describe)
        self.assertIn(f'key=Owner,value={self.OWNER}', describe)
        self.assertIn('Active', describe)
        terminated = [c[c.index('--session-id') + 1] for c in calls if c[0] == 'aws' and 'terminate-session' in c]
        self.assertEqual(terminated, ['claude-survey-20260910T000000-aaaa', 'claude-survey-20260910T000000-cccc'])
        self.assertIn('残っていた SSM セッション 2 件を終了しました', result.stderr)
        self.assertEqual(len(self.calls()), 1)

    def test_nothing_left_means_no_terminate_and_no_message(self):
        result = self.run_ec2('web1', 'uptime')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.aws_ops(), ['get-caller-identity', 'describe-sessions'])
        self.assertEqual(result.stderr, '')

    def test_abnormal_exit_looks_again_after_ssh(self):
        result = self.run_ec2('web1', 'uptime', FAKE_EXIT='255')
        self.assertEqual(result.returncode, 255)
        kinds = [(c[0], c[2] if c[0] == 'aws' else 'ssh') for c in self.all_calls()]
        self.assertEqual(kinds, [('aws', 'get-caller-identity'), ('aws', 'describe-sessions'), ('ssh', 'ssh'),
                                 ('aws', 'describe-sessions')])

    def test_without_aws_the_connection_still_goes_through(self):
        (self.root / 'bin/aws').write_text('#!/bin/sh\nexit 253\n')       # aws that cannot answer (no key, no network)
        result = self.run_ec2('web1', 'uptime')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(result.stderr, '')


class AuditLogPermissions(unittest.TestCase):
    """The audit log is protected by Edit(...) in settings.json, not Write(...).

    Claude's permission rules key on the tool actually used to change an existing file,
    which is Edit. A Write(...) entry looks like protection and denies nothing, so adding
    one and dropping Edit would leave the log editable while the settings file still reads
    as guarded. The Bash side is covered separately by the guard's has 'aws-audit' check,
    and both are required: neither one alone closes the other's path.
    """

    LOG = '//home/node/aws-survey/out/_環境/aws-audit.log'

    def setUp(self):
        self.deny = json.loads((ROOT / 'container/settings.json').read_text())['permissions']['deny']

    def test_the_log_is_denied_to_edit(self):
        self.assertIn(f'Edit({self.LOG})', self.deny,
                      'the audit log must be denied to Edit(...)')

    def test_write_is_not_used_instead_of_edit(self):
        self.assertNotIn(f'Write({self.LOG})', self.deny,
                         'Write(...) does not cover edits to an existing file; use Edit(...)')

    def test_the_bash_path_is_closed_too(self):
        guard = (ROOT / 'container/hooks/aws-readonly-guard.sh').read_text()
        self.assertIn("has 'aws-audit'", guard,
                      'the Bash guard must reject commands touching the audit log')
