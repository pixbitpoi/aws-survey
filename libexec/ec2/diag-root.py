#!/usr/bin/env python3
"""Root read tier for the diagnostic gateway (called as `sudo diag-root <verb> [args]`).

Five verbs only: listeners, dmesg, cron, journal, log. Arguments are validated here again
with the same code the gateway uses; the gateway is not trusted. `log` opens nothing outside
the expansion of a registered glob from /etc/diag/diag.conf. There is no arbitrary-path read.
"""
import collections
import glob
import gzip
import json
import os
import re
import select
import shlex
import shutil
import subprocess
import sys
import threading
import time

# ---- shared begin ----
# Identical in gateway and diag-root. diag-root re-validates every argument with this code
# instead of trusting the gateway. tests/test_gateway.py checks the two copies match.
PATH = '/usr/sbin:/usr/bin:/sbin:/bin'
CONF_PATH = '/etc/diag/diag.conf'
OUTPUT_LIMIT = 1024 * 1024
TIMEOUT = 30
TIMEOUT_LONG = 60
MAX_COMMAND = 4096
LIMITS = {
    'lines': 5000, 'pattern': 200, 'context': 5, 'matches': 2000, 'dmesg': 2000,
    'since_minutes': 7 * 24 * 60, 'find_depth': 5, 'ls_depth': 3, 'ls_entries': 2000,
    'name': 80, 'ps': 500, 'vmstat': 10, 'files': 2000, 'du_lines': 100,
}
DEFAULTS = {'lines': 200, 'journal_lines': 500, 'matches': 500, 'find_depth': 3, 'ps': 100,
            'vmstat': 5, 'dmesg': 200}
UNIT_RE = re.compile(r'^[A-Za-z0-9@._-]{1,80}$')
SINCE_RE = re.compile(r'^(\d{1,4})([mhd])$')
LOG_NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$')
PID_RE = re.compile(r'^[0-9]{1,8}$')
PRIORITIES = ('emerg', 'alert', 'crit', 'err', 'warning', 'notice', 'info', 'debug')
MASK_RULES = [
    (re.compile(r'((?:password|passwd|secret|token|api[_-]?key|private[_-]?key)\s*[:=]\s*)\S+', re.I), r'\1***'),
    (re.compile(r'(Authorization:\s+)\S+\s+\S+', re.I), r'\1***'),
    (re.compile(r'AKIA[0-9A-Z]{16}'), '***'),
    (re.compile(r'(aws_secret_access_key\s*=\s*)\S+', re.I), r'\1***'),
    (re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----', re.S), '***'),
    (re.compile(r'://[^/@\s]+:[^/@\s]+@'), '://***@'),
]


class Denied(Exception):
    """The request is refused. The message is the one-line reason shown to the caller."""


def mask(text):
    for pattern, replacement in MASK_RULES:
        text = pattern.sub(replacement, text)
    return text


def check_tokens(argv):
    for token in argv:
        if any(ord(c) < 0x20 or c == '\x7f' for c in token):
            raise Denied('control character in argument')
    return argv


def check_int(value, name, maximum, minimum=1):
    if not re.fullmatch(r'[0-9]{1,9}', value or ''):
        raise Denied(f'{name} must be a number')
    number = int(value)
    if number < minimum or number > maximum:
        raise Denied(f'{name} must be between {minimum} and {maximum}')
    return number


def check_since(value):
    match = SINCE_RE.match(value or '')
    if not match:
        raise Denied('--since must look like 30m, 2h or 1d')
    amount, unit = int(match.group(1)), match.group(2)
    minutes = amount * {'m': 1, 'h': 60, 'd': 24 * 60}[unit]
    if minutes < 1 or minutes > LIMITS['since_minutes']:
        raise Denied('--since must be between 1m and 7d')
    return f'{amount}{unit}'


def check_unit(value):
    if not UNIT_RE.match(value or ''):
        raise Denied('unit name must match [A-Za-z0-9@._-]{1,80}')
    return value


def check_pattern(value):
    if not value:
        raise Denied('pattern must not be empty')
    if len(value) > LIMITS['pattern']:
        raise Denied(f'pattern must be at most {LIMITS["pattern"]} characters')
    return value


def check_priority(value):
    if value not in PRIORITIES:
        raise Denied('--priority must be one of ' + ', '.join(PRIORITIES))
    return value


def check_log_name(value):
    if not LOG_NAME_RE.match(value or ''):
        raise Denied('log name must match [A-Za-z0-9][A-Za-z0-9_.-]{0,39}')
    return value


def journal_argv(unit=None, since='1d', lines=None, grep=None, priority=None):
    """Build the journalctl command. Every value passes the checks again."""
    argv = ['journalctl', '--no-pager', '-o', 'short-iso', '-q',
            f'--since=-{check_since(since)}',
            '-n', str(check_int(str(lines or DEFAULTS['journal_lines']), '--lines', LIMITS['lines']))]
    if unit is not None:
        argv += ['-u', check_unit(unit)]
    if grep is not None:
        argv += [f'--grep={check_pattern(grep)}']
    if priority is not None:
        argv += ['-p', check_priority(priority)]
    return argv


def check_log_glob(pattern):
    """Registered log globs: an absolute path whose directory part is fixed (no wildcard, no '..')."""
    if not isinstance(pattern, str) or not pattern.startswith('/'):
        raise Denied('log path must be absolute')
    directory, name = os.path.split(pattern)
    if not name or '..' in pattern.split('/') or any(c in directory for c in '*?['):
        raise Denied('log path must have a fixed directory and a file name')
    if any(ord(c) < 0x20 for c in pattern):
        raise Denied('log path contains a control character')
    return pattern


def expand_log(pattern):
    """Regular files matching a registered glob, newest first. Nothing outside the list is opened."""
    check_log_glob(pattern)
    files = [f for f in glob.glob(pattern) if os.path.isfile(f) and not os.path.islink(f)]
    return sorted(files, key=lambda f: os.stat(f).st_mtime, reverse=True)


def open_log(path):
    if path.endswith('.gz'):
        return gzip.open(path, 'rb')
    return open(path, 'rb')


def read_head(path, lines):
    out = bytearray()
    with open_log(path) as handle:
        for count, line in enumerate(handle):
            if count >= lines or len(out) >= OUTPUT_LIMIT:
                break
            out += line
    return bytes(out)


def read_tail(path, lines):
    with open_log(path) as handle:
        kept = collections.deque(handle, maxlen=lines)
    return b''.join(kept)[-OUTPUT_LIMIT:]


def lower_priority():
    """Diagnostics yield to the host's real work. Children inherit both settings."""
    try:
        os.nice(19)
    except OSError:
        pass
    if shutil.which('ionice'):
        subprocess.run(['ionice', '-c', '3', '-p', str(os.getpid())], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=False)


def run_capped(chain, timeout=TIMEOUT, limit=None, stdin=None):
    """Run argv lists as a pipeline without a shell. Returns (code, stdout, stderr, flags).

    Output is cut at `limit` bytes and the pipeline killed; the same happens at `timeout` seconds.
    flags holds 'truncated' and/or 'timeout'. A missing program gives code 127.
    """
    limit = limit or OUTPUT_LIMIT
    env = {'PATH': PATH, 'LC_ALL': 'C.UTF-8', 'LANG': 'C.UTF-8'}
    procs = []
    try:
        for index, argv in enumerate(chain):
            last = index == len(chain) - 1
            if index == 0:
                source = subprocess.PIPE if stdin is not None else subprocess.DEVNULL
            else:
                source = procs[-1].stdout
            proc = subprocess.Popen(list(argv), stdin=source, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE if last else subprocess.DEVNULL,
                                    env=env, close_fds=True)
            if index > 0:
                procs[-1].stdout.close()
            procs.append(proc)
    except FileNotFoundError as error:
        for proc in procs:
            proc.kill()
            proc.wait()
        return 127, b'', f'{error.filename}: not installed\n'.encode(), set()
    if stdin is not None:
        feeder = threading.Thread(target=_feed, args=(procs[0].stdin, stdin), daemon=True)
        feeder.start()
    last = procs[-1]
    out, err, flags = bytearray(), bytearray(), set()
    pending = {last.stdout: out, last.stderr: err}
    deadline = time.monotonic() + timeout
    while pending:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            flags.add('timeout')
            break
        ready, _, _ = select.select(list(pending), [], [], remaining)
        if not ready:
            flags.add('timeout')
            break
        for handle in ready:
            chunk = os.read(handle.fileno(), 65536)
            if not chunk:
                del pending[handle]
            elif handle is last.stdout:
                room = limit - len(out)
                if len(chunk) > room:
                    out += chunk[:room]
                    flags.add('truncated')
                    pending = {}
                    break
                out += chunk
            elif len(err) < 65536:
                err += chunk
    for proc in procs:
        if flags:
            proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        for handle in (proc.stdout, proc.stderr, proc.stdin):
            if handle:
                handle.close()
    return last.returncode, bytes(out), bytes(err), flags


def _feed(handle, data):
    try:
        handle.write(data)
    except (BrokenPipeError, OSError):
        pass
    finally:
        try:
            handle.close()
        except OSError:
            pass


class Output:
    """Collects the reply and enforces the 1 MiB cap. Masking happens once, at the end."""

    def __init__(self, limit=None):
        self.limit = limit or OUTPUT_LIMIT
        self.buffer = bytearray()
        self.truncated = False
        self.code = 0

    def add(self, data):
        if self.truncated:
            return
        room = self.limit - len(self.buffer)
        if len(data) > room:
            self.buffer += data[:room]
            self.truncated = True
        else:
            self.buffer += data

    def text(self, text):
        self.add(text.encode())

    def result(self, code, out, err, flags):
        self.add(out)
        if err:
            self.add(err if err.endswith(b'\n') else err + b'\n')
        if 'timeout' in flags:
            self.text('[timeout]\n')
        if 'truncated' in flags:
            self.truncated = True
        if code and not self.code:
            self.code = code

    def finish(self, do_mask):
        text = self.buffer.decode('utf-8', 'replace')
        if do_mask:
            text = mask(text)
        if self.truncated:
            text += '\n[truncated]\n'
        return text.encode()


def head_lines(data, count):
    lines = data.splitlines(keepends=True)
    return b''.join(lines[:count])


def load_conf(path=None):
    """/etc/diag/diag.conf (JSON): {"strict": bool, "logs": {name: glob}, "deny": [glob]}."""
    try:
        with open(path or CONF_PATH, 'rb') as handle:
            raw = json.load(handle)
    except (OSError, ValueError) as error:
        raise Denied(f'configuration unavailable: {error}')
    if not isinstance(raw, dict):
        raise Denied('configuration must be a JSON object')
    logs = raw.get('logs', {})
    deny = raw.get('deny', [])
    if not isinstance(logs, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in logs.items()):
        raise Denied('configuration: logs must map names to paths')
    if not isinstance(deny, list) or not all(isinstance(d, str) for d in deny):
        raise Denied('configuration: deny must be a list of globs')
    return {'strict': bool(raw.get('strict', False)), 'logs': dict(logs), 'deny': list(deny)}
# ---- shared end ----


VERBS = ('listeners', 'dmesg', 'cron', 'journal', 'log')
CRON_FILES = ['/etc/crontab']
CRON_DIRS = ['/etc/cron.d', '/var/spool/cron/crontabs', '/var/spool/cron']


def take_options(args, spec):
    positional, options = [], {}
    tokens = iter(args)
    for token in tokens:
        if token.startswith('-'):
            if token not in spec:
                raise Denied(f'unknown option {token}')
            value = next(tokens, None)
            if value is None:
                raise Denied(f'{token} needs a value')
            options[token] = value
        else:
            positional.append(token)
    return positional, options


def check_positional(positional, minimum, maximum, what):
    if len(positional) < minimum:
        raise Denied(f'missing {what}')
    if len(positional) > maximum:
        raise Denied(f'unexpected argument {shlex.quote(positional[maximum])}')


def run_listeners(pos, opt, conf, out):
    check_positional(pos, 0, 0, '')
    out.result(*run_capped([['ss', '-tulpn']]))


def run_dmesg(pos, opt, conf, out):
    check_positional(pos, 0, 0, '')
    lines = check_int(opt.get('--lines', str(DEFAULTS['dmesg'])), '--lines', LIMITS['dmesg'])
    code, data, err, flags = run_capped([['dmesg', '-T']])
    kept = data.splitlines(keepends=True)[-lines:]
    out.result(code, b''.join(kept), err, flags)


def run_cron(pos, opt, conf, out):
    """System crontabs and every user's spool entry (what `crontab -l -u <user>` would print)."""
    check_positional(pos, 0, 0, '')
    paths = [p for p in CRON_FILES if os.path.isfile(p)]
    for directory in CRON_DIRS:
        if os.path.isdir(directory):
            paths += sorted(os.path.join(directory, n) for n in os.listdir(directory)
                            if os.path.isfile(os.path.join(directory, n)))
    if not paths:
        out.text('no crontab files found\n')
    for path in paths:
        out.text(f'== {path}\n')
        try:
            with open(path, 'rb') as handle:
                out.add(handle.read(65536))
        except OSError as error:
            out.text(f'{path}: {error.strerror or error}\n')


def run_journal(pos, opt, conf, out):
    check_positional(pos, 0, 1, 'unit')
    argv = journal_argv(pos[0] if pos else None, opt.get('--since', '1d'),
                        opt.get('--lines', str(DEFAULTS['journal_lines'])), opt.get('--grep'), opt.get('--priority'))
    out.result(*run_capped([argv], timeout=TIMEOUT_LONG))


def pick_log_file(pattern, files, wanted):
    """The file to read: the one named by --file (must be in the expansion), the live file, or the newest."""
    if wanted is not None:
        if wanted not in files:
            raise Denied('--file must be one of the paths listed by: logs')
        return wanted
    live = pattern.rstrip('*')
    if live in files:
        return live
    if not files:
        raise Denied('no file matches the registered log right now')
    return files[0]


def run_log(pos, opt, conf, out):
    check_positional(pos, 1, 1, 'log name')
    name = check_log_name(pos[0])
    pattern = conf['logs'].get(name)
    if pattern is None:
        raise Denied(f'{name}: not a registered log')
    tail = check_int(opt.get('--tail', str(DEFAULTS['lines'])), '--tail', LIMITS['lines'])
    context = check_int(opt.get('--context', '0'), '--context', LIMITS['context'], minimum=0)
    grep = check_pattern(opt['--grep']) if '--grep' in opt else None
    wanted = opt.get('--file')
    if wanted is not None and not wanted.startswith('/'):
        raise Denied('--file must be an absolute path')
    path = pick_log_file(pattern, expand_log(pattern), wanted)
    out.text(f'== {path}\n')
    try:
        data = read_tail(path, tail)
    except OSError as error:
        out.text(f'{path}: {error.strerror or error}\n')
        out.code = 1
        return
    if grep is None:
        out.add(data)
        return
    argv = ['grep', '-E', '-n']
    if context:
        argv += ['-C', str(context)]
    argv += ['-e', grep, '--']
    code, found, err, flags = run_capped([argv], timeout=TIMEOUT_LONG, stdin=data)
    out.result(0 if code == 1 else code, found, err, flags)


HANDLERS = {
    'listeners': (run_listeners, {}),
    'dmesg': (run_dmesg, {'--lines'}),
    'cron': (run_cron, {}),
    'journal': (run_journal, {'--since', '--lines', '--grep', '--priority'}),
    'log': (run_log, {'--tail', '--grep', '--context', '--file'}),
}


def handle(argv, conf, stream):
    """Validate the argv again and run one of the five verbs. Returns the exit code."""
    out = Output()
    try:
        check_tokens(argv)
        if not argv or argv[0] not in VERBS:
            raise Denied('unknown verb; allowed: ' + ', '.join(VERBS))
        handler, spec = HANDLERS[argv[0]]
        positional, options = take_options(argv[1:], spec)
        handler(positional, options, conf, out)
    except Denied as error:
        stream.write(f'denied: {error}\n'.encode())
        stream.flush()
        return 2
    stream.write(out.finish(argv[0] in ('log', 'cron')))
    stream.flush()
    return out.code


def main():
    os.environ['PATH'] = PATH
    lower_priority()
    try:
        conf = load_conf()
    except Denied as error:
        sys.stdout.buffer.write(f'denied: {error}\n'.encode())
        sys.exit(2)
    sys.exit(handle(sys.argv[1:], conf, sys.stdout.buffer))


if __name__ == '__main__':
    main()
