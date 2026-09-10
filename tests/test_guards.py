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
    """

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
                        'open(os.environ["FAKE_LOG"], "a").write(json.dumps(sys.argv[1:]) + "\\n")\n'
                        'sys.exit(int(os.environ.get("FAKE_EXIT", "0")))\n')
        fake.chmod(0o755)
        self.log = self.root / 'ssh.jsonl'

    def tearDown(self):
        self.temp.cleanup()

    def run_ec2(self, *args, **extra):
        env = dict(os.environ, HOME=str(self.root / 'home'), FAKE_LOG=str(self.log),
                   PATH=f'{self.root / "bin"}:{os.environ["PATH"]}')
        env.update(extra)
        return subprocess.run(['bash', str(ROOT / 'container/ec2'), *args], env=env, capture_output=True, text=True)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

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
