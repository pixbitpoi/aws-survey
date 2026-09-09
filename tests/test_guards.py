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
        ]
        for command, allowed in cases:
            with self.subTest(command=command):
                payload = json.dumps({'tool_name': 'Bash', 'tool_input': {'command': command}})
                result = subprocess.run(['bash', str(HOOKS / 'aws-readonly-guard.sh')], input=payload, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0 if allowed else 2, result.stderr)
                self.check_codex('Bash', command, allowed)
        self.assertIn('ALLOW', (self.root / 'audit.tsv').read_text())
        self.assertIn('DENY', (self.root / 'audit.tsv').read_text())

    def test_codex_shell(self):
        for command in ['ls out/', "jq '.Vpcs | length' out/raw.json", 'survey-status', 'mkdir -p out/01_基礎調査/raw']:
            with self.subTest(command=command):
                self.check_codex('Bash', command, True)
        for command in ['bash -c "aws sts get-caller-identity"', 'python3 -c "print(1)"', 'cat /home/node/.aws-claude/credentials', 'rm out/_環境/aws-audit.log', 'cat out/x; node script.js', 'rg --pre=sh text out/', 'mkdir -p /tmp/new', 'ls $(pwd)', 'cat out/x > out/_環境/aws-audit.log']:
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
