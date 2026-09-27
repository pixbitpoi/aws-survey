"""`aws-survey ssh setup --print` and the install script it assembles, checked without EC2 or aws.

The generated script is bash with the gateway, the root helper and the configuration builder
embedded as heredocs. Here we check the assembly: the embedded Python still parses and equals
the sources, the user name and public key land where they should, invalid names and log globs
are refused on the host, and the words that must not reach an instance are absent. Running the
script needs a Linux root; that is tests/ec2_install_smoke.sh (Docker, run by hand).
"""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / 'bin/aws-survey'
EC2 = ROOT / 'libexec/ec2'
TOOLS = ['bash', 'sh', 'env', 'jq', 'sed', 'grep', 'awk', 'cut', 'head', 'tail', 'cat', 'wc', 'tr',
         'dirname', 'basename', 'readlink', 'mkdir', 'chmod', 'cp', 'mv', 'rm', 'stat', 'date',
         'mktemp', 'python3', 'printf', 'echo', 'test', 'touch', 'uname', 'tee']
FAKE_PUBKEY = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeFakeFakeFakeFakeFakeFakeFakeFakeFakeFak smoke-comment'


def heredoc(script, delimiter):
    """Body of the quoted heredoc that ends with `delimiter` on a line of its own."""
    match = re.search(r"<<'" + delimiter + r"'\n(.*?)\n" + delimiter + r"\n", script, re.S)
    return match.group(1) if match else None


class SshPrintCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name).resolve()
        self.home = base / 'home'
        self.target = base / 'target'
        self.bin = base / 'bin'
        for d in (self.home, self.target, self.bin):
            d.mkdir()
        for tool in TOOLS:
            found = shutil.which(tool)
            if found:
                (self.bin / tool).symlink_to(found)
        self.write_environment()
        self.keys = self.home / '.aws-survey/smoke/ssh'
        self.keys.mkdir(parents=True)
        (self.keys / 'id_ed25519').write_text('fake private key\n')
        (self.keys / 'id_ed25519.pub').write_text(FAKE_PUBKEY + '\n')

    def tearDown(self):
        self.temp.cleanup()

    def write_environment(self, extra=None):
        config = json.loads((ROOT / 'templates/environment.json').read_text())
        config.update(name='smoke', account_id='000000000000', region='test-region')
        config['auth'].update(route='own_role', source_profile='fake-src',
                              principal_arn='arn:aws:iam::000000000000:user/fake',
                              role_name='fake-role', refresh_command=None)
        if extra:
            config.update(extra)
        (self.target / 'environment.json').write_text(json.dumps(config))

    def run_cli(self, *args):
        env = {'PATH': str(self.bin), 'HOME': str(self.home), 'LANG': os.environ.get('LANG', 'C.UTF-8'),
               'LC_ALL': os.environ.get('LC_ALL', ''), 'TZ': 'UTC'}
        env = {k: v for k, v in env.items() if v}
        return subprocess.run([str(CLI), '--dir', str(self.target), *args], capture_output=True, text=True,
                              errors='replace', env=env)

    def print_script(self, *args):
        result = self.run_cli('ssh', 'setup', '--print', *args)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result


class GeneratedScript(SshPrintCase):
    def test_script_goes_to_stdout_and_guidance_to_stderr(self):
        result = self.print_script()
        self.assertTrue(result.stdout.startswith('#!/usr/bin/env bash\n'))
        self.assertNotIn('◆', result.stdout)
        self.assertIn('導入スクリプトを標準出力に出しました', result.stderr)
        self.assertIn('バイト', result.stderr)

    def test_script_is_valid_bash(self):
        result = self.print_script()
        check = subprocess.run(['bash', '-n'], input=result.stdout, capture_output=True, text=True)
        self.assertEqual(check.returncode, 0, check.stderr)

    def test_embedded_python_matches_sources_and_compiles(self):
        script = self.print_script().stdout
        for delimiter, source in (('__DIAG_GATEWAY__', 'gateway.py'), ('__DIAG_ROOT__', 'diag-root.py')):
            body = heredoc(script, delimiter)
            self.assertIsNotNone(body, delimiter)
            self.assertEqual(body + '\n', (EC2 / source).read_text())
            compile(body, source, 'exec')
        builder = heredoc(script, '__DIAG_CONF_PY__')
        self.assertIsNotNone(builder)
        compile(builder, 'diag.conf builder', 'exec')

    def test_user_and_key_are_embedded(self):
        script = self.print_script().stdout
        self.assertIn("DIAG_USER='diag'\n", script)
        # The comment on the key is replaced by a fixed one so the target's name never lands in authorized_keys.
        key = FAKE_PUBKEY.rsplit(' ', 1)[0]
        self.assertIn(f"DIAG_PUBKEY='{key} diag'\n", script)
        self.assertNotIn('smoke-comment', script)
        self.assertNotIn('fake private key', script)

    def test_default_conf_is_empty(self):
        conf = json.loads(heredoc(self.print_script().stdout, '__DIAG_CONF__'))
        self.assertEqual(conf, {'strict': False, 'logs': {}, 'deny': []})

    def test_options_land_in_conf(self):
        script = self.print_script('--log', 'app=/var/www/app/log/*.log', '--log=db=/var/lib/pg/log/postgresql-*.log',
                                   '--deny', '/var/www/app/config/*', '--deny=/srv/*.pem', '--strict').stdout
        conf = json.loads(heredoc(script, '__DIAG_CONF__'))
        self.assertEqual(conf, {'strict': True,
                                'logs': {'app': '/var/www/app/log/*.log', 'db': '/var/lib/pg/log/postgresql-*.log'},
                                'deny': ['/var/www/app/config/*', '/srv/*.pem']})

    def test_user_option(self):
        script = self.print_script('--user', 'ops_ro').stdout
        self.assertIn("DIAG_USER='ops_ro'\n", script)
        self.assertIn('Match User $DIAG_USER', script)
        # sshd closes a connection whose client has gone away (an ssh killed mid-session leaves the SSM session open)
        self.assertIn('    ClientAliveInterval 15\n    ClientAliveCountMax 3\n', script)

    def test_user_from_environment_json(self):
        self.write_environment({'ssh': {'user': 'reader', 'hosts': {}}})
        self.assertIn("DIAG_USER='reader'\n", self.print_script().stdout)

    def test_no_survey_words_reach_the_instance(self):
        script = self.print_script().stdout.lower()
        for word in ('ai ', 'agent', 'survey', 'claude', 'codex'):
            self.assertNotIn(word, script, word)

    def test_files_named_as_designed(self):
        script = self.print_script().stdout
        for path in ('/usr/local/lib/diag/gateway', '/usr/local/lib/diag/diag-root', '/etc/diag/diag.conf',
                     '/etc/ssh/sshd_config.d/diag.conf', '/etc/sudoers.d/diag', '/run/diag.lock'):
            self.assertIn(path, script)
        self.assertIn('sshd -t', script)
        self.assertIn('systemctl reload', script)
        self.assertNotIn('systemctl restart', script)
        self.assertIn('visudo -c', script)
        self.assertIn('SSH_ORIGINAL_COMMAND=uptime', script)
        self.assertIn('SSH_ORIGINAL_COMMAND=bash', script)
        self.assertIn('HOSTKEY', script)
        self.assertIn('restrict,command=', script)

    def test_script_size_is_reported(self):
        result = self.print_script()
        size = int(re.search(r'（(\d+) バイト）', result.stderr).group(1))
        self.assertEqual(size, len(result.stdout.encode()))


class RemoveScript(SshPrintCase):
    """`ssh remove --print <host>` writes the removal script (the reverse of the install) without aws."""

    def print_remove(self, user='diag'):
        config = json.loads((self.target / 'environment.json').read_text())
        config['ssh'] = {'user': 'diag', 'hosts': {'web1': {'instance_id': 'i-0123456789abcdef0', 'user': user,
                                                              'installed_at': '2026-09-10T00:00:00+09:00',
                                                              'logs': {}, 'deny': [], 'strict': False}}}
        (self.target / 'environment.json').write_text(json.dumps(config))
        result = self.run_cli('ssh', 'remove', '--print', 'web1')
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def test_script_goes_to_stdout(self):
        result = self.print_remove()
        self.assertTrue(result.stdout.startswith('#!/usr/bin/env bash\n'))
        self.assertIn('撤去スクリプトを標準出力に出しました', result.stderr)
        self.assertEqual(subprocess.run(['bash', '-n'], input=result.stdout, capture_output=True, text=True).returncode, 0)

    def test_removes_everything_the_install_creates(self):
        script = self.print_remove(user='ops').stdout
        self.assertIn("DIAG_USER='ops'", script)
        for path in ('/usr/local/lib/diag', '/etc/diag', '/etc/ssh/sshd_config.d/diag.conf', '/etc/sudoers.d/diag',
                     '/etc/tmpfiles.d/diag.conf', '/run/diag.lock'):
            self.assertIn(path, script)
        self.assertIn('userdel -r', script)
        self.assertIn('sshd -t', script)
        self.assertIn('systemctl reload', script)
        self.assertIn('visudo -c', script)
        self.assertIn('REMOVED clean', script)
        self.assertIn('REMOVED leftover', script)
        for word in ('ai', 'agent', 'survey', 'claude'):
            self.assertNotRegex(script.lower(), r'(?<![a-z])' + word + r'(?![a-z])')

    def test_marker_block_round_trip_restores_sshd_config(self):
        """Without Include, the install appends a blank line and the marked block. Installing twice and
        removing must give back the original bytes (no blank lines piling up)."""
        def strip_function(script):
            marks = re.findall(r'^MARK_(?:BEGIN|END)=.*$', script, re.M)
            body = re.search(r'^strip_marked_block\(\) \{\n.*?^\}\n', script, re.M | re.S).group(0)
            return '\n'.join(marks) + '\n' + body
        harness = '\n'.join([
            'set -eu',
            strip_function(self.print_script().stdout),
            # The append as the install script does it.
            'append() { { strip_marked_block "$1"; printf \'\\n%s\\n\' "$MARK_BEGIN"; '
            'printf \'Match User diag\\n    PermitTTY no\\n\'; printf \'%s\\n\' "$MARK_END"; } > "$1.new"; mv "$1.new" "$1"; }',
            'append "$1"; append "$1"; cp "$1" "$1.installed"',
            strip_function(self.print_remove().stdout),
            'strip_marked_block "$1" > "$1.new"; mv "$1.new" "$1"',
        ])
        base = Path(self.temp.name)
        for original in ('Port 22\nPasswordAuthentication no\n', 'Port 22\n\nUsePAM yes\n\n', '# only a comment\n\n\n'):
            with self.subTest(original=original):
                config = base / 'sshd_config'
                config.write_text(original)
                result = subprocess.run(['bash', '-c', harness, 'harness', str(config)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                installed = (base / 'sshd_config.installed').read_text()
                self.assertTrue(installed.startswith(original + '\n# diag begin'), installed)
                self.assertEqual(installed.count('# diag begin'), 1)
                self.assertEqual(config.read_text(), original)

    def test_print_touches_nothing(self):
        self.print_remove()
        after = json.loads((self.target / 'environment.json').read_text())
        self.assertIn('web1', after['ssh']['hosts'])
        self.assertFalse((self.home / '.aws-survey/smoke/ssh/config').exists())


class HostSideValidation(SshPrintCase):
    def test_bad_user_names_are_refused(self):
        for name in ('Diag', 'diag!', '1diag', 'a' * 33, '-x', 'diag user'):
            result = self.run_cli('ssh', 'setup', '--print', '--user', name)
            self.assertNotEqual(result.returncode, 0, name)
            self.assertIn('--user が不正です', result.stderr, name)
        for name in ('diag', '_svc', 'ops-ro', 'a' * 32):
            self.assertEqual(self.run_cli('ssh', 'setup', '--print', '--user', name).returncode, 0, name)

    def test_bad_ssh_user_in_environment_json_is_refused(self):
        self.write_environment({'ssh': {'user': 'Bad User', 'hosts': {}}})
        result = self.run_cli('ssh', 'setup', '--print')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('ssh.user が不正です', result.stderr)

    def test_log_glob_needs_fixed_directory(self):
        for spec in ('app=/var/*/app.log', 'app=/var/?/app.log', 'app=/var/[l]og/app.log',
                     'app=/var/log/../shadow', 'app=/var/log/./x', 'app=/var/log//x',
                     'app=relative/x.log', 'app=/var/log/', 'app=/var/log/x\tlog'):
            result = self.run_cli('ssh', 'setup', '--print', '--log', spec)
            self.assertNotEqual(result.returncode, 0, spec)
            self.assertIn('--log のパスが不正です', result.stderr, spec)
        for spec in ('app=/var/log/app.log', 'app=/var/log/app*', 'app=/var/log/app-[0-9]*.log', 'app=/var/log/nginx/*'):
            self.assertEqual(self.run_cli('ssh', 'setup', '--print', '--log', spec).returncode, 0, spec)

    def test_log_name_and_shape(self):
        for spec in ('/var/log/x', 'bad name=/var/log/x', '-x=/var/log/x', '=/var/log/x', 'a' * 41 + '=/var/log/x'):
            result = self.run_cli('ssh', 'setup', '--print', '--log', spec)
            self.assertNotEqual(result.returncode, 0, spec)
            self.assertIn('--log', result.stderr, spec)

    def test_deny_glob(self):
        for glob in ('relative/*', '/etc/../x', '/x\ny'):
            result = self.run_cli('ssh', 'setup', '--print', '--deny', glob)
            self.assertNotEqual(result.returncode, 0, glob)
            self.assertIn('--deny のパターンが不正です', result.stderr, glob)

    def test_unknown_option(self):
        result = self.run_cli('ssh', 'setup', '--print', '--bogus')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('不明なオプション', result.stderr)


class WithoutAws(SshPrintCase):
    """This case has no `aws` on PATH: --print and list still work, setup <target> stops before touching anything.
    The SSM path itself is checked with a fake aws in tests/test_ssh_setup.py."""

    def test_setup_without_target_points_to_print(self):
        result = self.run_cli('ssh', 'setup')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('--print', result.stderr)

    def test_setup_with_target_needs_aws(self):
        result = self.run_cli('ssh', 'setup', 'i-0123456789abcdef0')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('aws コマンドが見つかりません', result.stderr)
        self.assertEqual(json.loads((self.target / 'environment.json').read_text())['ssh']['hosts'], {})

    def test_list_without_aws_reads_the_file(self):
        result = self.run_cli('ssh', 'list')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('登録済みホストはありません', result.stdout)

    def test_rotate_and_remove_need_registered_hosts(self):
        result = self.run_cli('ssh', 'rotate')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('登録済みホストがありません', result.stderr)
        result = self.run_cli('ssh', 'remove', 'web1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('未登録のホスト', result.stderr)
        result = self.run_cli('ssh', 'remove')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('<host>', result.stderr)

    def test_verify_needs_a_registered_host(self):
        result = self.run_cli('ssh', 'verify', 'web1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('未登録のホスト', result.stderr)

    def test_unknown_subcommand(self):
        result = self.run_cli('ssh', 'bogus')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('不明なサブコマンド', result.stderr)

    def test_print_does_not_touch_environment_json(self):
        before = (self.target / 'environment.json').read_text()
        self.print_script('--user', 'ops')
        self.assertEqual(before, (self.target / 'environment.json').read_text())


class LoadEnv(SshPrintCase):
    def test_status_shows_hosts_only_when_registered(self):
        result = self.run_cli('status')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, '')
        self.assertIn('EC2 の中を調べる: なし', result.stdout)
        self.assertIn('ec2 で一覧から選んで登録します', result.stdout)
        self.write_environment({'ssh': {'user': 'diag', 'hosts': {'web1': {'instance_id': 'i-0'}, 'db1': {'instance_id': 'i-1'}}}})
        result = self.run_cli('status')
        self.assertIn('登録済みホスト db1 web1', result.stdout)
        self.assertIn('ログインユーザー', result.stdout)

    def test_template_has_ssh_defaults(self):
        config = json.loads((ROOT / 'templates/environment.json').read_text())
        self.assertEqual(config['ssh'], {'user': 'diag', 'hosts': {}})


if __name__ == '__main__':
    unittest.main()


class Verify(SshPrintCase):
    """`aws-survey ssh verify <host>` runs the self-test inside the survey container.

    Checked with a fake docker: the image is built from the same Dockerfile and context as
    `run`, the container gets only the read-only key mount and the region, the command is
    `ec2 --selftest <host>`, and its exit code is the result. Without aws on PATH the SSM-side
    check is skipped, not failed. An expired temporary key is reissued first; when that cannot happen, nothing reaches docker.
    """

    def setUp(self):
        super().setUp()
        self.write_environment({'ssh': {'user': 'diag', 'hosts': {'web1': {
            'instance_id': 'i-0123456789abcdef0', 'user': 'diag', 'installed_at': '2026-09-10T12:00:00+09:00',
            'logs': {}, 'deny': [], 'strict': False}}}})
        keys = self.keys.parent
        (keys / 'credentials').write_text('[claude-ro]\n')
        (self.keys / 'config').write_text('Host web1\n    HostName i-0123456789abcdef0\n    User diag\n')
        self.session(expiration='2099-01-01T00:00:00+00:00')
        self.log = self.home / 'docker.jsonl'
        docker = self.bin / 'docker'
        docker.write_text('#!/usr/bin/env python3\nimport json, os, sys\n'
                          'open(os.environ["FAKE_LOG"], "a").write(json.dumps(sys.argv[1:]) + "\\n")\n'
                          'print("sha256:fake" if sys.argv[1] == "build" else "selftest output")\n'
                          'sys.exit(int(os.environ.get("FAKE_EXIT", "0")) if sys.argv[1] == "run" else 0)\n')
        docker.chmod(0o755)

    def session(self, expiration):
        (self.keys.parent / 'session.json').write_text(json.dumps({'expiration': expiration, 'duration_seconds': 3600}))

    def run_verify(self, *args, **extra):
        env = {'FAKE_LOG': str(self.log), **extra}
        original = self.run_cli
        def run_cli(*a):
            base = {'PATH': str(self.bin), 'HOME': str(self.home), 'LANG': os.environ.get('LANG', 'C.UTF-8'), 'TZ': 'UTC'}
            base.update(env)
            return subprocess.run([str(CLI), '--dir', str(self.target), *a], capture_output=True, text=True,
                                  errors='replace', env=base)
        return run_cli('ssh', 'verify', *args)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_runs_the_selftest_in_the_container(self):
        result = self.run_verify('web1')
        self.assertEqual(result.returncode, 0, result.stderr)
        build, run = self.calls()
        self.assertEqual(build[0], 'build')
        self.assertEqual(build[-3:], ['-f', f'{ROOT}/Dockerfile', f'{ROOT}/container'])
        self.assertIn('smoke:latest', build)
        self.assertEqual(run[0], 'run')
        self.assertIn(f'{self.keys.parent}:/home/node/.aws-claude:ro', run)
        self.assertIn('AWS_DEFAULT_REGION=test-region', run)
        self.assertEqual(run[-3:], ['ec2', '--selftest', 'web1'])
        self.assertNotIn('/home/node/aws-survey/out', ' '.join(run), 'verify must not mount out/')
        self.assertIn('selftest output', result.stdout)
        self.assertIn('省きました', result.stdout)

    def test_success_records_verified_at_for_the_guide(self):
        self.run_verify('web1')
        host = json.loads((self.target / 'environment.json').read_text())['ssh']['hosts']['web1']
        self.assertRegex(host['verified_at'], r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$')
        self.assertGreater(host['verified_at'], host['installed_at'])

    def test_selftest_failure_is_the_result(self):
        result = self.run_verify('web1', FAKE_EXIT='1')
        self.assertEqual(result.returncode, 1)
        self.assertIn('失敗した項目', result.stdout)
        host = json.loads((self.target / 'environment.json').read_text())['ssh']['hosts']['web1']
        self.assertNotIn('verified_at', host)

    def test_expired_key_is_reissued_first_and_stops_before_docker_when_that_fails(self):
        # 切れていれば credentials を走らせにいく（aws の無いこの環境では発行できない）。docker には触らない
        self.session(expiration='2020-01-01T00:00:00+00:00')
        result = self.run_verify('web1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('期限切れ', result.stdout)
        self.assertIn('発行し直します', result.stdout)
        self.assertIn('発行し直せませんでした', result.stdout)
        self.assertEqual(self.calls(), [])

    def test_missing_key_stops_before_docker(self):
        (self.keys.parent / 'credentials').unlink()
        result = self.run_verify('web1')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(), [])
