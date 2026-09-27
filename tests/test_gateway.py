"""The diagnostic gateway and its root helper, checked without an EC2 instance.

Both scripts are plain Python on the standard library, so parsing, the deny list, masking and
the argv they would run can be verified here. Commands that only exist on Linux (ls
--time-style, journalctl, sudo diag-root) are checked as argv, not executed.
"""
import importlib.util
import io
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
EC2 = ROOT / 'libexec/ec2'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gateway = load('gateway', EC2 / 'gateway.py')
diag_root = load('diag_root', EC2 / 'diag-root.py')
CONF = {'strict': False, 'logs': {}, 'deny': []}


def shared_block(path):
    text = path.read_text()
    match = re.search(r'# ---- shared begin ----.*?# ---- shared end ----', text, re.S)
    return match.group(0) if match else None


class SharedValidation(unittest.TestCase):
    """diag-root re-validates with the gateway's code, so the two copies must not drift."""

    def test_shared_block_identical(self):
        block = shared_block(EC2 / 'gateway.py')
        self.assertIsNotNone(block)
        self.assertEqual(block, shared_block(EC2 / 'diag-root.py'))

    def test_no_survey_names_on_ec2(self):
        for path in (EC2 / 'gateway.py', EC2 / 'diag-root.py', EC2 / 'diag.conf'):
            text = path.read_text().lower()
            for word in ('ai ', 'agent', 'survey', 'claude', 'codex'):
                self.assertNotIn(word, text, f'{path.name} mentions {word!r}')

    def test_template_is_json_and_loads(self):
        conf = gateway.load_conf(str(EC2 / 'diag.conf'))
        self.assertEqual(conf, {'strict': False, 'logs': {}, 'deny': []})

    def test_bad_conf_is_denied(self):
        with tempfile.NamedTemporaryFile('w', suffix='.conf', delete=False) as handle:
            handle.write('{"logs": ["not", "a", "map"]}')
        try:
            with self.assertRaises(gateway.Denied):
                gateway.load_conf(handle.name)
        finally:
            os.unlink(handle.name)
        with self.assertRaises(gateway.Denied):
            gateway.load_conf('/nonexistent/diag.conf')


class Tokenize(unittest.TestCase):
    def build(self, command, conf=CONF):
        return gateway.build(gateway.tokenize(command), conf)

    def test_shell_bypass_attempts_are_refused(self):
        for command in ['uptime; id', "'uptime; id'", '$(id)', 'uptime | id', 'uptime && id',
                        'bash', 'sh -c id', '/bin/bash', 'uptime\nid', "'up\ntime'", '`id`',
                        'uptime --help', 'cat --recursive /etc/hosts', '']:
            with self.assertRaises(gateway.Denied, msg=command):
                self.build(command)

    def test_missing_and_oversized_command(self):
        for value in (None, '', '   '):
            with self.assertRaises(gateway.Denied):
                gateway.tokenize(value)
        with self.assertRaises(gateway.Denied):
            gateway.tokenize('uptime ' + 'x' * gateway.MAX_COMMAND)

    def test_quoted_arguments_survive(self):
        argv = gateway.tokenize(r'grep /var/log/x --pattern foo\ bar')
        self.assertEqual(argv, ['grep', '/var/log/x', '--pattern', 'foo bar'])


class Verbs(unittest.TestCase):
    def build(self, command, conf=CONF):
        return gateway.build(gateway.tokenize(command), conf)

    def commands(self, command, conf=CONF):
        return self.build(command, conf).commands()

    def deny(self, command, conf=CONF):
        with self.assertRaises(gateway.Denied, msg=command):
            self.build(command, conf)

    def test_fixed_diagnostics_argv(self):
        self.assertEqual(self.commands('uptime'), [['uptime']])
        self.assertEqual(self.commands('free'), [['free', '-m']])
        self.assertEqual(self.commands('df'), [['df', '-hT'], ['df', '-ih']])
        self.assertEqual(self.commands('net'), [['ip', '-br', 'addr'], ['ip', 'route'], ['ss', '-tuln'],
                                                ['ss', '-tn', 'state', 'established']])
        self.assertEqual(self.commands('ps --top 5 --sort mem'),
                         [['ps', '-eo', 'pid,ppid,user,%cpu,%mem,rss,etime,stat,args', '--sort=-%mem']])
        self.assertEqual(self.commands('vmstat --count 3'), [['vmstat', '1', '3']])
        self.assertEqual(self.commands('service nginx.service'),
                         [['systemctl', 'status', 'nginx.service', '--no-pager', '-l', '-n', '50']])
        self.assertEqual(self.commands('unit sshd'), [['systemctl', 'cat', 'sshd', '--no-pager']])

    def test_fixed_diagnostics_limits(self):
        for command in ['uptime extra', 'ps --top 501', 'ps --top 0', 'ps --sort io', 'vmstat --count 11',
                        'service "nginx; id"', 'service ../x', 'service ' + 'a' * 81, 'unit -f',
                        'proc abc', 'proc 1/environ', 'proc 1 environ', 'sar disk', 'sar cpu --day 2024-01-01']:
            self.deny(command)

    def test_no_environ_and_no_extra_verbs(self):
        self.assertNotIn('environ', gateway.HELP)
        for verb in ('top', 'strace', 'tcpdump', 'curl', 'env', 'bash', 'sh', 'sudo', 'ssh', 'scp'):
            self.deny(verb)

    def test_general_read_tier_argv(self):
        self.assertEqual(self.commands('ls /var/log'), [['ls', '-la', '--time-style=long-iso', '--', '/var/log']])
        self.assertEqual(self.commands('ls /var/log --recursive'), [['find', '/var/log', '-maxdepth', '3', '-ls']])
        self.assertEqual(self.commands('stat /etc/hosts'), [['stat', '--', '/etc/hosts']])
        self.assertEqual(self.commands('du /var --depth 2'),
                         [['du', '-sh', '--', '/var'], ['du', '-d', '2', '-h', '--', '/var'], ['sort', '-hr']])
        self.assertEqual(self.commands('find /var/log --name "*.log" --depth 2 --newer "2026-01-01 00:00" --larger 10M'),
                         [['find', '/var/log', '-maxdepth', '2', '-name', '*.log', '-newermt', '2026-01-01 00:00',
                           '-size', '+10M']])

    def test_general_read_tier_limits(self):
        for command in ['ls relative/path', 'ls', 'ls /a /b', 'find /var --depth 6', 'find /var --name ' + 'x' * 81,
                        'find /var --name a/b', 'find /var --newer yesterday', 'find /var --larger big',
                        'find /var --exec id', 'du /var --depth 3', 'head /etc/hosts --lines 5001',
                        'tail /etc/hosts --lines 0', 'grep /etc/hosts', 'grep /etc/hosts --pattern ' + 'x' * 201,
                        'grep /etc/hosts --pattern x --context 6', 'grep /etc/hosts --pattern x --max 2001',
                        'cat /etc/hosts /etc/passwd']:
            self.deny(command)

    def test_strict_disables_general_tier_only(self):
        strict = dict(CONF, strict=True)
        for command in ['cat /etc/hosts', 'ls /', 'find /var', 'grep /etc/hosts --pattern x', 'du /', 'stat /']:
            self.deny(command, strict)
        self.assertEqual(self.commands('uptime', strict), [['uptime']])
        self.assertEqual(self.commands('listeners', strict)[0][:4], ['sudo', '-n', '--', gateway.DIAG_ROOT])

    def test_journal_argv_and_limits(self):
        with patch.object(gateway, 'journal_readable', return_value=True):
            self.assertEqual(self.commands('journal nginx --since 2h --lines 10 --grep err --priority err'),
                             [['journalctl', '--no-pager', '-o', 'short-iso', '-q', '--since=-2h', '-n', '10',
                               '-u', 'nginx', '--grep=err', '-p', 'err']])
            for command in ['journal --since 8d', 'journal --since 2w', 'journal --since -1h', 'journal --lines 5001',
                            'journal --priority loud', 'journal "a b"', 'journal a b']:
                self.deny(command)
        with patch.object(gateway, 'journal_readable', return_value=False):
            self.assertEqual(self.commands('journal nginx --since 2h'),
                             [['sudo', '-n', '--', gateway.DIAG_ROOT, 'journal', 'nginx', '--since', '2h',
                               '--lines', '500']])

    def test_root_tier_goes_through_sudo_with_fixed_verbs(self):
        prefix = ['sudo', '-n', '--', gateway.DIAG_ROOT]
        conf = dict(CONF, logs={'app': '/var/log/app/app.log*'})
        self.assertEqual(self.commands('listeners'), [prefix + ['listeners']])
        self.assertEqual(self.commands('cron'), [prefix + ['cron']])
        self.assertEqual(self.commands('dmesg --lines 50'), [prefix + ['dmesg', '--lines', '50']])
        self.assertEqual(self.commands('log app --tail 3 --grep "x|y" --context 1 --file /var/log/app/app.log.1', conf),
                         [prefix + ['log', 'app', '--tail', '3', '--grep', 'x|y', '--context', '1',
                                    '--file', '/var/log/app/app.log.1']])
        for command in ['dmesg --lines 2001', 'log', 'log secure --tail 3', 'log "app; id"',
                        'log app --file relative', 'listeners --all', 'cron /etc/crontab']:
            self.deny(command, conf)
        # Nothing in the root tier takes an arbitrary path to read.
        self.assertEqual(set(gateway.ROOT_VERBS), set(diag_root.VERBS))
        self.assertEqual(diag_root.VERBS, ('listeners', 'dmesg', 'cron', 'journal', 'log'))


class DenyPatterns(unittest.TestCase):
    def test_names_directories_and_extensions(self):
        blocked = ['/var/www/app/.env', '/var/www/app/.env.production', '/etc/pki/tls/private/server.key',
                   '/home/ubuntu/cert.pem', '/opt/app/Secrets.yml', '/opt/app/credentials.json',
                   '/var/www/wp-config.php', '/var/www/app/config/database.yml', '/home/ubuntu/.netrc',
                   '/root/notes.txt', '/home/ubuntu/.ssh/authorized_keys', '/home/ubuntu/.aws/credentials',
                   '/etc/ssh/sshd_config', '/etc/diag/diag.conf', '/etc/sudoers.d/diag', '/etc/shadow',
                   '/etc/shadow-', '/etc/letsencrypt/live/example/privkey.pem', '/var/lib/docker/x',
                   '/srv/repo/.git/config', '/proc/1/environ', '/proc/123/mem', '/dev/mem',
                   '/sys/firmware/efi/x', '/var/lib/app/data.sqlite3', '/var/lib/app/app.db',
                   '/home/ubuntu/.ssh/id_ed25519', '/opt/app/id_rsa.bak']
        for path in blocked:
            self.assertTrue(gateway.denied(path, CONF), path)
        allowed = ['/var/log/syslog', '/etc/hosts', '/proc/1/status', '/var/www/app/log/app.log',
                   '/var/log/nginx/access.log', '/etc/nginx/nginx.conf', '/home/ubuntu/app.py',
                   '/var/lib/dockerfiles/x', '/opt/app/gitlog.txt', '/etc/letsencrypt/renewal/x.conf']
        for path in allowed:
            self.assertFalse(gateway.denied(path, CONF), path)

    def test_configured_deny_globs(self):
        conf = dict(CONF, deny=['/var/www/app/config/*', '/opt/vault'])
        for path in ['/var/www/app/config/settings.py', '/var/www/app/config/sub/deep.py', '/opt/vault/x']:
            self.assertTrue(gateway.denied(path, conf), path)
        self.assertFalse(gateway.denied('/var/www/app/config', conf))
        self.assertFalse(gateway.denied('/var/www/app/configs/x', conf))

    def test_symlink_and_dotdot_do_not_bypass(self):
        with tempfile.TemporaryDirectory() as raw:
            temp = str(Path(raw).resolve())
            secret = Path(temp, 'app.env')
            secret.write_text('x')
            secret.rename(Path(temp, '.env'))
            link = Path(temp, 'harmless.txt')
            link.symlink_to(Path(temp, '.env'))
            with self.assertRaises(gateway.Denied):
                gateway.readable_path(str(link), CONF)
            with self.assertRaises(gateway.Denied):
                gateway.readable_path(f'{temp}/sub/../.env', CONF)
            conf = dict(CONF, deny=[f'{temp}/vault'])
            Path(temp, 'vault').mkdir()
            Path(temp, 'vault/note.txt').write_text('x')
            Path(temp, 'door').symlink_to(Path(temp, 'vault'))
            with self.assertRaises(gateway.Denied):
                gateway.readable_path(f'{temp}/door/note.txt', conf)
            # The deny list is checked on the path as given too, so a denied name reached through
            # a linked parent directory (as /etc -> /private/etc on some systems) is still refused.
            with patch.object(gateway.os.path, 'realpath', side_effect=lambda p: '/private' + p):
                with self.assertRaises(gateway.Denied):
                    gateway.readable_path('/etc/ssh/sshd_config', CONF)
            self.assertEqual(gateway.readable_path(f'{temp}/vault/../missing.txt', CONF),
                             str(Path(temp, 'missing.txt').resolve()))


class Masking(unittest.TestCase):
    def test_rules(self):
        cases = [
            ('password = hunter2', 'password = ***'),
            ('DB_PASSWORD: s3cret', 'DB_PASSWORD: ***'),
            ('api-key=abc123 rest', 'api-key=*** rest'),
            ('Authorization: Bearer eyJabc', 'Authorization: ***'),
            ('key AKIAIOSFODNN7EXAMPLE used', 'key *** used'),
            ('aws_secret_access_key = wJalrXUtnFEMI', 'aws_secret_access_key = ***'),
            ('-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----\n', '***\n'),
            ('postgres://user:pw@db.internal/app', 'postgres://***@db.internal/app'),
            ('tokens: 5 remaining', 'tokens: 5 remaining'),
        ]
        for text, expected in cases:
            self.assertEqual(gateway.mask(text), expected)
            self.assertEqual(diag_root.mask(text), expected)


class Execution(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name).resolve()
        self.log = self.dir / 'app.log'
        self.log.write_text('line1\npassword=hunter2\nline3 AKIAIOSFODNN7EXAMPLE\nline4\n')
        import gzip
        with gzip.open(self.dir / 'app.log.1.gz', 'wb') as handle:
            handle.write(b'old1\nold2 token=abc\n')
        (self.dir / '.env').write_text('SECRET=1\n')
        self.conf = dict(CONF, logs={'app': f'{self.dir}/app.log*'})
        self.lock = patch.object(gateway, 'LOCK_PATH', str(self.dir / 'lock'))
        self.lock.start()
        self.audit = patch.object(gateway, 'audit')
        self.audited = self.audit.start()

    def tearDown(self):
        self.audit.stop()
        self.lock.stop()
        self.temp.cleanup()

    def run_gateway(self, command, conf=None):
        stream = io.BytesIO()
        code = gateway.handle(command, conf or self.conf, stream)
        return code, stream.getvalue().decode()

    def test_cat_head_tail_are_masked(self):
        self.assertEqual(self.run_gateway(f'cat {self.log}'),
                         (0, 'line1\npassword=***\nline3 ***\nline4\n'))
        self.assertEqual(self.run_gateway(f'head {self.log} --lines 2'), (0, 'line1\npassword=***\n'))
        self.assertEqual(self.run_gateway(f'tail {self.log} --lines 1'), (0, 'line4\n'))
        self.assertEqual(self.run_gateway(f'tail {self.dir}/app.log.1.gz --lines 1'), (0, 'old2 token=***\n'))
        self.assertEqual(self.run_gateway(f'head {self.dir}/app.log.1.gz --lines 1'), (0, 'old1\n'))

    def test_denied_file_and_missing_file(self):
        code, text = self.run_gateway(f'cat {self.dir}/.env')
        self.assertEqual(code, 2)
        self.assertTrue(text.startswith('denied:'), text)
        code, text = self.run_gateway(f'cat {self.dir}/nope')
        self.assertEqual(code, 1)
        self.assertIn('nope', text)

    def test_grep_masks_and_skips_denied_files(self):
        code, text = self.run_gateway(f'grep {self.dir} --recursive --pattern "SECRET|password|old"')
        self.assertEqual(code, 0)
        self.assertIn(f'{self.log}:2:password=***', text)
        self.assertNotIn('.env', text)
        self.assertNotIn('SECRET', text)
        code, text = self.run_gateway(f'grep {self.dir} --pattern x')
        self.assertEqual(code, 2)
        code, text = self.run_gateway(f'grep {self.log} --pattern LINE --ignore-case --max 1')
        self.assertEqual(text, f'{self.log}:1:line1\n')

    def test_find_filters_denied_names(self):
        code, text = self.run_gateway(f'find {self.dir} --name "*"')
        self.assertEqual(code, 0)
        self.assertIn('app.log', text)
        self.assertNotIn('.env', text)

    def test_logs_lists_registered_files(self):
        code, text = self.run_gateway('logs')
        self.assertEqual(code, 0)
        self.assertIn(f'app: {self.dir}/app.log*', text)
        self.assertIn(str(self.log), text)
        self.assertIn('app.log.1.gz', text)

    def test_deny_exits_2_and_is_audited(self):
        code, text = self.run_gateway('bash')
        self.assertEqual(code, 2)
        self.assertTrue(text.startswith('denied:'))
        verb, args, decision = self.audited.call_args[0]
        self.assertEqual((verb, args), ('bash', []))
        self.assertTrue(decision.startswith('deny:'))
        self.run_gateway(f'head {self.log} --lines 1')
        self.assertEqual(self.audited.call_args[0], ('head', [str(self.log), '--lines', '1'], 'allow'))

    def test_output_cap_marks_truncation(self):
        big = self.dir / 'big.log'
        big.write_bytes((b'x' * 100 + b'\n') * 20)
        with patch.object(gateway, 'OUTPUT_LIMIT', 1000):
            code, text = self.run_gateway(f'cat {big}')
        self.assertIn('showing the first and last part', text)
        self.assertNotIn('[truncated]', text)
        self.assertLess(len(text), 1100)
        with patch.object(gateway, 'OUTPUT_LIMIT', 1000):
            code, text = self.run_gateway(f'grep {big} --pattern x')
        self.assertTrue(text.rstrip().endswith('[truncated]'), text[-40:])
        self.assertLess(len(text), 1100)

    def test_timeout_kills_the_command(self):
        code, out, err, flags = gateway.run_capped([['sleep', '5']], timeout=0.3)
        self.assertIn('timeout', flags)

    def test_missing_program_is_reported(self):
        code, out, err, flags = gateway.run_capped([['no-such-program-diag']])
        self.assertEqual(code, 127)
        self.assertIn(b'not installed', err)

    def test_lock_busy_is_denied(self):
        import fcntl
        with open(self.dir / 'lock', 'a+') as holder:
            fcntl.flock(holder, fcntl.LOCK_EX)
            with patch.object(gateway, 'LOCK_WAIT', 0.2):
                code, text = self.run_gateway('uptime')
        self.assertEqual(code, 2)
        self.assertIn('busy', text)


class DiagRoot(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name).resolve()
        (self.dir / 'app.log').write_text('a\npassword=x\nb\nc\n')
        import gzip
        with gzip.open(self.dir / 'app.log.2.gz', 'wb') as handle:
            handle.write(b'rotated\n')
        (self.dir / 'other.txt').write_text('outside\n')
        self.conf = {'strict': False, 'logs': {'app': f'{self.dir}/app.log*'}, 'deny': []}

    def tearDown(self):
        self.temp.cleanup()

    def run_root(self, argv, conf=None):
        stream = io.BytesIO()
        code = diag_root.handle(argv, conf or self.conf, stream)
        return code, stream.getvalue().decode()

    def test_only_five_verbs(self):
        for argv in [['cat', '/etc/shadow'], ['ls', '/root'], ['bash'], [], ['log'], ['LOG', 'app']]:
            self.assertEqual(self.run_root(argv)[0], 2, argv)

    def test_log_reads_registered_files_only(self):
        self.assertEqual(self.run_root(['log', 'app', '--tail', '2']), (0, f'== {self.dir}/app.log\nb\nc\n'))
        self.assertEqual(self.run_root(['log', 'app', '--file', f'{self.dir}/app.log.2.gz']),
                         (0, f'== {self.dir}/app.log.2.gz\nrotated\n'))
        for argv in [['log', 'app', '--file', f'{self.dir}/other.txt'], ['log', 'app', '--file', '/etc/shadow'],
                     ['log', 'app', '--file', 'app.log'], ['log', 'app', '--file', f'{self.dir}/../app.log'],
                     ['log', 'other'], ['log', 'app', '--tail', '5001'], ['log', 'app', '--context', '6'],
                     ['log', 'app', '--grep', 'x' * 201], ['log', 'app', '--path', '/x'],
                     ['log', 'app', 'extra']]:
            self.assertEqual(self.run_root(argv)[0], 2, argv)

    def test_log_grep_and_mask(self):
        code, text = self.run_root(['log', 'app', '--grep', 'pass|^c', '--context', '1'])
        self.assertEqual(code, 0)
        self.assertIn('2:password=***', text)
        self.assertIn('4:c', text)
        self.assertNotIn('password=x', text)
        code, text = self.run_root(['log', 'app', '--grep', 'nomatch'])
        self.assertEqual(code, 0)

    def test_registered_glob_must_have_fixed_directory(self):
        for pattern in ['/var/log/*/access.log', '/var/log/../etc/shadow', 'relative/x.log', '/var/log/',
                        '/var/[a]/x.log', '/var/log/x\n.log']:
            with self.assertRaises(diag_root.Denied, msg=pattern):
                diag_root.check_log_glob(pattern)
        self.assertEqual(diag_root.check_log_glob('/var/log/nginx/*'), '/var/log/nginx/*')
        conf = {'strict': False, 'logs': {'bad': '/var/*/x.log'}, 'deny': []}
        self.assertEqual(self.run_root(['log', 'bad'], conf)[0], 2)

    def test_journal_argv_is_rebuilt_from_checked_values(self):
        self.assertEqual(diag_root.journal_argv('nginx', '30m', '5', None, 'err'),
                         ['journalctl', '--no-pager', '-o', 'short-iso', '-q', '--since=-30m', '-n', '5',
                          '-u', 'nginx', '-p', 'err'])
        for bad in [dict(unit='a b'), dict(since='9d'), dict(lines='0'), dict(priority='x')]:
            with self.assertRaises(diag_root.Denied):
                diag_root.journal_argv(**{'unit': 'x', 'since': '1h', 'lines': '5', **bad})

    def test_dmesg_and_cron_arguments(self):
        self.assertEqual(self.run_root(['dmesg', '--lines', '2001'])[0], 2)
        self.assertEqual(self.run_root(['dmesg', '--lines', 'x'])[0], 2)
        self.assertEqual(self.run_root(['cron', 'x'])[0], 2)
        self.assertEqual(self.run_root(['listeners', '-p'])[0], 2)
        with patch.object(diag_root, 'CRON_FILES', [str(self.dir / 'app.log')]), \
                patch.object(diag_root, 'CRON_DIRS', []):
            code, text = self.run_root(['cron'])
        self.assertEqual(code, 0)
        self.assertIn('password=***', text)


if __name__ == '__main__':
    unittest.main()
