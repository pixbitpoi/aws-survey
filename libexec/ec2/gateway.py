#!/usr/bin/env python3
"""Diagnostic gateway: the only program a diag login can run (sshd ForceCommand).

Reads SSH_ORIGINAL_COMMAND, validates it as `<verb> [args]`, and runs the matching
diagnostic with argv lists. No shell is involved, so `;`, `|` and `$()` have no meaning.
Two read tiers: the general tier runs as the login user with a deny list and masking;
the root tier is `sudo diag-root` with five fixed verbs and registered logs only.
Every request is written to the journal (tag diag-gateway) before it runs.
"""
import collections
import contextlib
import datetime
import fcntl
import fnmatch
import glob
import gzip
import json
import os
import pwd
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


DIAG_ROOT = '/usr/local/lib/diag/diag-root'
LOCK_PATH = '/run/diag.lock'
LOCK_WAIT = 30
LOG_TAG = 'diag-gateway'

# General read tier: names, directories and extensions that are never opened for reading.
# `ls` still lists them; the OS permission model is the real boundary and this list only
# catches secrets that happen to be world-readable.
DENY_NAMES = ['.env', '.env.*', '*.pem', '*.key', '*.p12', '*.pfx', 'id_rsa*', 'id_ed25519*', 'id_ecdsa*',
              '*secret*', '*credential*', '*password*', '*.keystore', '*.jks', 'wp-config.php',
              'database.yml', 'secrets.yml', 'master.key', '.htpasswd', '.netrc', '.pgpass', '.my.cnf',
              '*.sqlite', '*.sqlite3', '*.db']
DENY_DIRS = ['/root', '/home/*/.ssh', '/home/*/.aws', '/home/*/.gnupg', '/etc/ssh', '/etc/diag',
             '/etc/sudoers*', '/etc/shadow*', '/etc/letsencrypt/live', '/etc/letsencrypt/archive',
             '/var/lib/docker', '/proc/*/environ', '/proc/*/mem', '/dev', '/sys/firmware']
DENY_COMPONENTS = ['.git']

ROOT_VERBS = ('listeners', 'dmesg', 'cron', 'journal', 'log')


class Plan:
    """What a validated request will do. Steps run in order; nothing here came from a shell."""

    def __init__(self, verb, mask=False, line_cap=None, timeout=TIMEOUT):
        self.verb = verb
        self.mask = mask
        self.line_cap = line_cap
        self.timeout = timeout
        self.steps = []

    def exec(self, argv, timeout=None, line_cap=None, line_filter=None):
        self.steps.append(('exec', [list(argv)], timeout or self.timeout, line_cap, line_filter))
        return self

    def pipe(self, chain, timeout=None, line_cap=None):
        self.steps.append(('exec', [list(a) for a in chain], timeout or self.timeout, line_cap, None))
        return self

    def text(self, text):
        self.steps.append(('text', text, None, None, None))
        return self

    def call(self, function):
        self.steps.append(('call', function, None, None, None))
        return self

    def commands(self):
        return [argv for kind, chains, *_ in self.steps if kind == 'exec' for argv in chains]


# ---- validation ----

def tokenize(original):
    if original is None or not original.strip():
        raise Denied('no command given; try: help')
    if len(original) > MAX_COMMAND:
        raise Denied(f'command longer than {MAX_COMMAND} characters')
    try:
        argv = shlex.split(original, posix=True)
    except ValueError as error:
        raise Denied(f'cannot parse command: {error}')
    if not argv:
        raise Denied('no command given; try: help')
    return check_tokens(argv)


def take_options(args, spec):
    """Split `args` into positionals and known options. Anything unknown is refused."""
    positional, options = [], {}
    tokens = iter(args)
    for token in tokens:
        if token.startswith('--'):
            if token not in spec:
                raise Denied(f'unknown option {token}')
            if spec[token] == 'flag':
                options[token] = True
            else:
                value = next(tokens, None)
                if value is None:
                    raise Denied(f'{token} needs a value')
                options[token] = value
        elif token.startswith('-') and len(token) > 1:
            raise Denied(f'unknown option {token}')
        else:
            positional.append(token)
    return positional, options


def check_positional(positional, minimum, maximum, what):
    if len(positional) < minimum:
        raise Denied(f'missing {what}')
    if len(positional) > maximum:
        raise Denied(f'unexpected argument {shlex.quote(positional[maximum])}')


def check_abs(value):
    if not value.startswith('/'):
        raise Denied('path must be absolute')
    return os.path.normpath(value)


def prefix_match(real, pattern):
    parts = real.split('/')
    wanted = pattern.rstrip('/').split('/')
    if len(parts) < len(wanted):
        return False
    return all(fnmatch.fnmatchcase(p, w) for p, w in zip(parts, wanted))


def denied(real, conf):
    """True when the general read tier must not open `real` (an absolute, resolved path)."""
    parts = real.split('/')
    name = parts[-1].lower()
    if any(fnmatch.fnmatchcase(name, p.lower()) for p in DENY_NAMES):
        return True
    if any(component in parts for component in DENY_COMPONENTS):
        return True
    return any(prefix_match(real, p) for p in DENY_DIRS + conf['deny'])


def readable_path(value, conf):
    """Apply the deny list to the path as given and to its symlink-resolved form, so neither a
    link into a denied place nor a denied place reached through a linked parent gets through."""
    path = check_abs(value)
    real = os.path.realpath(path)
    if denied(path, conf) or denied(real, conf):
        raise Denied(f'{value}: not readable through this gateway')
    return real


def check_glob_name(value):
    if len(value) > LIMITS['name']:
        raise Denied(f'--name must be at most {LIMITS["name"]} characters')
    if '/' in value:
        raise Denied('--name must be a file name pattern, not a path')
    return value


def check_datetime(value):
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?', value or ''):
        raise Denied('--newer must be YYYY-MM-DD or YYYY-MM-DD HH:MM')
    return value


def check_size(value):
    match = re.fullmatch(r'([0-9]{1,9})([kMG])?', value or '')
    if not match:
        raise Denied('--larger must be a number with optional k, M or G')
    return f'+{match.group(1)}{match.group(2) or "c"}'


def journal_readable():
    """Can this user read the system journal without root? Checked on the files, not the group."""
    for directory in ('/var/log/journal', '/run/log/journal'):
        for path in glob.glob(os.path.join(directory, '*', '*.journal')):
            return os.access(path, os.R_OK)
    return False


def root_step(plan, verb, args):
    return plan.exec(['sudo', '-n', '--', DIAG_ROOT, verb] + args)


# ---- general read tier ----

def verb_ls(pos, opt, conf):
    check_positional(pos, 1, 1, 'path')
    path = check_abs(pos[0])
    plan = Plan('ls')
    if opt.get('--recursive'):
        return plan.exec(['find', path, '-maxdepth', str(LIMITS['ls_depth']), '-ls'],
                         timeout=TIMEOUT_LONG, line_cap=LIMITS['ls_entries'])
    return plan.exec(['ls', '-la', '--time-style=long-iso', '--', path])


def verb_find(pos, opt, conf):
    check_positional(pos, 1, 1, 'directory')
    argv = ['find', check_abs(pos[0]), '-maxdepth',
            str(check_int(opt.get('--depth', str(DEFAULTS['find_depth'])), '--depth', LIMITS['find_depth']))]
    if '--name' in opt:
        argv += ['-name', check_glob_name(opt['--name'])]
    if '--newer' in opt:
        argv += ['-newermt', check_datetime(opt['--newer'])]
    if '--larger' in opt:
        argv += ['-size', check_size(opt['--larger'])]
    return Plan('find', timeout=TIMEOUT_LONG).exec(
        argv, line_cap=LIMITS['files'],
        line_filter=lambda line: not (denied(line, conf) or denied(os.path.realpath(line), conf)))


def verb_stat(pos, opt, conf):
    check_positional(pos, 1, 1, 'path')
    return Plan('stat').exec(['stat', '--', check_abs(pos[0])])


def verb_du(pos, opt, conf):
    check_positional(pos, 1, 1, 'directory')
    path = check_abs(pos[0])
    depth = opt.get('--depth', '1')
    if depth not in ('1', '2'):
        raise Denied('--depth must be 1 or 2')
    plan = Plan('du', timeout=TIMEOUT_LONG)
    plan.exec(['du', '-sh', '--', path])
    return plan.pipe([['du', '-d', depth, '-h', '--', path], ['sort', '-hr']], line_cap=LIMITS['du_lines'])


def read_whole(path):
    def step(out):
        try:
            size = os.stat(path).st_size
            if size <= OUTPUT_LIMIT and not path.endswith('.gz'):
                with open(path, 'rb') as handle:
                    out.add(handle.read(OUTPUT_LIMIT))
                return
            out.text(f'[{path}: {size} bytes; showing the first and last part. Use head/tail/grep for the rest]\n')
            out.add(read_head(path, 2000)[:OUTPUT_LIMIT // 2])
            out.text('\n[...]\n')
            out.add(read_tail(path, 1000)[-(OUTPUT_LIMIT // 4):])
        except OSError as error:
            out.text(f'{path}: {error.strerror or error}\n')
            out.code = 1
    return step


def read_part(path, lines, reader):
    def step(out):
        try:
            out.add(reader(path, lines))
        except OSError as error:
            out.text(f'{path}: {error.strerror or error}\n')
            out.code = 1
    return step


def verb_cat(pos, opt, conf):
    check_positional(pos, 1, 1, 'path')
    return Plan('cat', mask=True).call(read_whole(readable_path(pos[0], conf)))


def verb_head(pos, opt, conf):
    check_positional(pos, 1, 1, 'path')
    lines = check_int(opt.get('--lines', str(DEFAULTS['lines'])), '--lines', LIMITS['lines'])
    return Plan('head', mask=True).call(read_part(readable_path(pos[0], conf), lines, read_head))


def verb_tail(pos, opt, conf):
    check_positional(pos, 1, 1, 'path')
    lines = check_int(opt.get('--lines', str(DEFAULTS['lines'])), '--lines', LIMITS['lines'])
    return Plan('tail', mask=True).call(read_part(readable_path(pos[0], conf), lines, read_tail))


def walk_files(root, conf, depth=LIMITS['ls_depth'], limit=LIMITS['files']):
    """Regular files under `root`, at most `depth` levels down, skipping denied ones."""
    found = []
    base = root.rstrip('/').count('/')
    for current, dirs, files in os.walk(root, followlinks=False):
        if current.count('/') - base >= depth:
            dirs[:] = []
        dirs.sort()
        for name in sorted(files):
            path = os.path.join(current, name)
            if os.path.islink(path) or not os.path.isfile(path) or denied(path, conf) \
                    or denied(os.path.realpath(path), conf):
                continue
            found.append(path)
            if len(found) >= limit:
                return found
    return found


def verb_grep(pos, opt, conf):
    check_positional(pos, 1, 50, 'path')
    if '--pattern' not in opt:
        raise Denied('--pattern is required')
    argv = ['grep', '-E', '-n', '-H', '-s']
    if opt.get('--ignore-case'):
        argv.append('-i')
    context = check_int(opt.get('--context', '0'), '--context', LIMITS['context'], minimum=0)
    if context:
        argv += ['-C', str(context)]
    matches = check_int(opt.get('--max', str(DEFAULTS['matches'])), '--max', LIMITS['matches'])
    argv += ['-m', str(matches), '-e', check_pattern(opt['--pattern']), '--']
    plan = Plan('grep', mask=True, line_cap=matches, timeout=TIMEOUT_LONG)
    files = []
    for value in pos:
        real = readable_path(value, conf)
        if os.path.isdir(real):
            if not opt.get('--recursive'):
                raise Denied(f'{value} is a directory; add --recursive')
            files += walk_files(real, conf)
        else:
            files.append(real)
    if not files:
        return plan.text('no readable files\n')
    for start in range(0, len(files), 200):
        plan.exec(argv + files[start:start + 200])
    return plan


# ---- fixed diagnostics ----

def verb_uptime(pos, opt, conf):
    check_positional(pos, 0, 0, '')
    return Plan('uptime').exec(['uptime'])


def read_file_step(path):
    def step(out):
        try:
            with open(path, 'rb') as handle:
                out.add(handle.read(65536))
        except OSError as error:
            out.text(f'{path}: {error.strerror or error}\n')
    return step


def verb_os(pos, opt, conf):
    check_positional(pos, 0, 0, '')
    return (Plan('os').call(read_file_step('/etc/os-release')).exec(['uname', '-a'])
            .exec(['hostnamectl', '--no-pager']))


def verb_ps(pos, opt, conf):
    check_positional(pos, 0, 0, '')
    top = check_int(opt.get('--top', str(DEFAULTS['ps'])), '--top', LIMITS['ps'])
    sort = opt.get('--sort', 'cpu')
    if sort not in ('cpu', 'mem'):
        raise Denied('--sort must be cpu or mem')
    return Plan('ps').exec(['ps', '-eo', 'pid,ppid,user,%cpu,%mem,rss,etime,stat,args', f'--sort=-%{sort}'],
                           line_cap=top + 1)


def verb_proc(pos, opt, conf):
    check_positional(pos, 1, 1, 'pid')
    pid = pos[0]
    if not PID_RE.match(pid):
        raise Denied('pid must be a number')

    def step(out):
        base = f'/proc/{pid}'
        for name in ('status', 'limits'):
            out.text(f'== {base}/{name}\n')
            read_file_step(f'{base}/{name}')(out)
        out.text(f'== {base}/cmdline\n')
        try:
            with open(f'{base}/cmdline', 'rb') as handle:
                out.add(handle.read(65536).replace(b'\0', b' ').rstrip() + b'\n')
            out.text(f'== open files: {len(os.listdir(f"{base}/fd"))}\n')
        except OSError as error:
            out.text(f'{base}: {error.strerror or error}\n')
    return Plan('proc').call(step)


def verb_free(pos, opt, conf):
    check_positional(pos, 0, 0, '')
    return Plan('free').exec(['free', '-m'])


def verb_vmstat(pos, opt, conf):
    check_positional(pos, 0, 0, '')
    count = check_int(opt.get('--count', str(DEFAULTS['vmstat'])), '--count', LIMITS['vmstat'])
    return Plan('vmstat', timeout=count + TIMEOUT).exec(['vmstat', '1', str(count)])


def verb_df(pos, opt, conf):
    check_positional(pos, 0, 0, '')
    return Plan('df').exec(['df', '-hT']).exec(['df', '-ih'])


SAR_FLAGS = {'cpu': ['-u'], 'mem': ['-r'], 'io': ['-b'], 'net': ['-n', 'DEV'], 'load': ['-q']}


def sar_file(day):
    date = datetime.date.today()
    if day == 'yesterday':
        date -= datetime.timedelta(days=1)
    for directory in ('/var/log/sa', '/var/log/sysstat'):
        path = os.path.join(directory, f'sa{date.day:02d}')
        if os.path.exists(path):
            return path
    return None


def verb_sar(pos, opt, conf):
    check_positional(pos, 1, 1, 'kind (cpu|mem|io|net|load)')
    if pos[0] not in SAR_FLAGS:
        raise Denied('sar kind must be one of cpu, mem, io, net, load')
    day = opt.get('--day', 'today')
    if day not in ('today', 'yesterday'):
        raise Denied('--day must be today or yesterday')
    plan = Plan('sar')
    if not shutil.which('sar'):
        return plan.text('sar: not installed (sysstat)\n')
    path = sar_file(day)
    if not path:
        return plan.text(f'sar: no data file for {day}\n')
    return plan.exec(['sar'] + SAR_FLAGS[pos[0]] + ['-f', path])


def verb_net(pos, opt, conf):
    check_positional(pos, 0, 0, '')
    return Plan('net').exec(['ip', '-br', 'addr']).exec(['ip', 'route']).exec(['ss', '-tuln'])


def verb_services(pos, opt, conf):
    check_positional(pos, 0, 0, '')
    return Plan('services').exec(['systemctl', 'list-units', '--type=service', '--all', '--no-pager'])


def verb_failed(pos, opt, conf):
    check_positional(pos, 0, 0, '')
    return Plan('failed').exec(['systemctl', '--failed', '--no-pager'])


def verb_service(pos, opt, conf):
    check_positional(pos, 1, 1, 'unit')
    return Plan('service').exec(['systemctl', 'status', check_unit(pos[0]), '--no-pager', '-l', '-n', '50'])


def verb_unit(pos, opt, conf):
    check_positional(pos, 1, 1, 'unit')
    return Plan('unit').exec(['systemctl', 'cat', check_unit(pos[0]), '--no-pager'])


def verb_timers(pos, opt, conf):
    check_positional(pos, 0, 0, '')
    return Plan('timers').exec(['systemctl', 'list-timers', '--all', '--no-pager'])


def journal_args(pos, opt):
    check_positional(pos, 0, 1, 'unit')
    unit = pos[0] if pos else None
    since = opt.get('--since', '1d')
    lines = opt.get('--lines', str(DEFAULTS['journal_lines']))
    argv = journal_argv(unit, since, lines, opt.get('--grep'), opt.get('--priority'))
    args = ['--since', since, '--lines', lines]
    if unit:
        args = [unit] + args
    if '--grep' in opt:
        args += ['--grep', opt['--grep']]
    if '--priority' in opt:
        args += ['--priority', opt['--priority']]
    return argv, args


def verb_journal(pos, opt, conf):
    argv, args = journal_args(pos, opt)
    plan = Plan('journal', timeout=TIMEOUT_LONG)
    if journal_readable():
        return plan.exec(argv)
    return root_step(plan, 'journal', args)


def verb_logs(pos, opt, conf):
    check_positional(pos, 0, 0, '')

    def step(out):
        if not conf['logs']:
            out.text('no registered logs\n')
        for name, pattern in sorted(conf['logs'].items()):
            out.text(f'{name}: {pattern}\n')
            try:
                files = expand_log(pattern)
            except Denied as error:
                out.text(f'  (skipped: {error})\n')
                continue
            for path in files:
                info = os.stat(path)
                when = datetime.datetime.fromtimestamp(info.st_mtime).strftime('%Y-%m-%d %H:%M')
                out.text(f'  {info.st_size:>12} {when} {path}\n')
    return Plan('logs').call(step)


HELP = '''verbs (general read tier; disabled on strict hosts):
  ls <path> [--recursive]
  find <dir> [--name <glob>] [--depth N] [--newer <YYYY-MM-DD[ HH:MM]>] [--larger <N[kMG]>]
  stat <path>
  du <dir> [--depth 1|2]
  cat <path>
  head <path> [--lines N]
  tail <path> [--lines N]
  grep <path>... --pattern <ERE> [--context N] [--ignore-case] [--recursive] [--max N]
fixed diagnostics:
  uptime | os | free | df | net | services | failed | timers | logs | help
  ps [--top N] [--sort cpu|mem]
  proc <pid>
  vmstat [--count N]
  sar cpu|mem|io|net|load [--day today|yesterday]
  service <unit> | unit <unit>
  journal [unit] [--since 30m|2h|1d] [--lines N] [--grep <pat>] [--priority <p>]
root read tier (registered logs and fixed diagnostics only):
  listeners
  dmesg [--lines N]
  cron
  log <name> [--tail N] [--grep <pat>] [--context N] [--file <path from logs>]
Paths must be absolute. File contents are masked for credentials. Output stops at 1 MiB.
'''


def verb_help(pos, opt, conf):
    check_positional(pos, 0, 0, '')
    return Plan('help').text(HELP)


# ---- root read tier (validated here, validated again in diag-root) ----

def verb_listeners(pos, opt, conf):
    check_positional(pos, 0, 0, '')
    return root_step(Plan('listeners'), 'listeners', [])


def verb_dmesg(pos, opt, conf):
    check_positional(pos, 0, 0, '')
    lines = check_int(opt.get('--lines', str(DEFAULTS['dmesg'])), '--lines', LIMITS['dmesg'])
    return root_step(Plan('dmesg'), 'dmesg', ['--lines', str(lines)])


def verb_cron(pos, opt, conf):
    check_positional(pos, 0, 0, '')
    return root_step(Plan('cron'), 'cron', [])


def verb_log(pos, opt, conf):
    check_positional(pos, 1, 1, 'log name')
    name = check_log_name(pos[0])
    if name not in conf['logs']:
        raise Denied(f'{name}: not a registered log (see: logs)')
    args = [name, '--tail', str(check_int(opt.get('--tail', str(DEFAULTS['lines'])), '--tail', LIMITS['lines']))]
    if '--grep' in opt:
        args += ['--grep', check_pattern(opt['--grep'])]
    if '--context' in opt:
        args += ['--context', str(check_int(opt['--context'], '--context', LIMITS['context'], minimum=0))]
    if '--file' in opt:
        args += ['--file', check_abs(opt['--file'])]
    return root_step(Plan('log', timeout=TIMEOUT_LONG), 'log', args)


# verb -> (handler, options, tier). Tier 'general' is off on strict hosts.
VERBS = {
    'ls': (verb_ls, {'--recursive': 'flag'}, 'general'),
    'find': (verb_find, {'--name': 'value', '--depth': 'value', '--newer': 'value', '--larger': 'value'}, 'general'),
    'stat': (verb_stat, {}, 'general'),
    'du': (verb_du, {'--depth': 'value'}, 'general'),
    'cat': (verb_cat, {}, 'general'),
    'head': (verb_head, {'--lines': 'value'}, 'general'),
    'tail': (verb_tail, {'--lines': 'value'}, 'general'),
    'grep': (verb_grep, {'--pattern': 'value', '--context': 'value', '--ignore-case': 'flag',
                         '--recursive': 'flag', '--max': 'value'}, 'general'),
    'uptime': (verb_uptime, {}, 'fixed'),
    'os': (verb_os, {}, 'fixed'),
    'ps': (verb_ps, {'--top': 'value', '--sort': 'value'}, 'fixed'),
    'proc': (verb_proc, {}, 'fixed'),
    'free': (verb_free, {}, 'fixed'),
    'vmstat': (verb_vmstat, {'--count': 'value'}, 'fixed'),
    'df': (verb_df, {}, 'fixed'),
    'sar': (verb_sar, {'--day': 'value'}, 'fixed'),
    'net': (verb_net, {}, 'fixed'),
    'services': (verb_services, {}, 'fixed'),
    'failed': (verb_failed, {}, 'fixed'),
    'service': (verb_service, {}, 'fixed'),
    'unit': (verb_unit, {}, 'fixed'),
    'timers': (verb_timers, {}, 'fixed'),
    'journal': (verb_journal, {'--since': 'value', '--lines': 'value', '--grep': 'value', '--priority': 'value'}, 'fixed'),
    'logs': (verb_logs, {}, 'fixed'),
    'help': (verb_help, {}, 'fixed'),
    'listeners': (verb_listeners, {}, 'root'),
    'dmesg': (verb_dmesg, {'--lines': 'value'}, 'root'),
    'cron': (verb_cron, {}, 'root'),
    'log': (verb_log, {'--tail': 'value', '--grep': 'value', '--context': 'value', '--file': 'value'}, 'root'),
}


def build(argv, conf):
    """Turn a token list into a Plan, or raise Denied. No file is opened and nothing runs here
    except path resolution for the deny check."""
    verb = argv[0]
    if verb not in VERBS:
        raise Denied(f'unknown verb {shlex.quote(verb)}; try: help')
    handler, spec, tier = VERBS[verb]
    if tier == 'general' and conf['strict']:
        raise Denied(f'{verb}: not available on this host (strict mode); registered logs via: log')
    positional, options = take_options(argv[1:], spec)
    return handler(positional, options, conf)


# ---- execution ----

def execute(plan, stream):
    out = Output()
    remaining_lines = plan.line_cap
    for kind, payload, timeout, line_cap, line_filter in plan.steps:
        if out.truncated:
            break
        if kind == 'text':
            out.text(payload)
            continue
        if kind == 'call':
            payload(out)
            continue
        code, data, err, flags = run_capped(payload, timeout=timeout, limit=out.limit - len(out.buffer) + 1)
        if line_filter:
            data = b''.join(line for line in data.splitlines(keepends=True)
                            if line_filter(line.rstrip(b'\n').decode('utf-8', 'replace')))
        cap = min(x for x in (line_cap, remaining_lines) if x is not None) if (line_cap or remaining_lines) else None
        if cap is not None:
            kept = head_lines(data, cap)
            if remaining_lines is not None:
                remaining_lines -= kept.count(b'\n')
            data = kept
        out.result(code, data, err, flags)
    stream.write(out.finish(plan.mask))
    stream.flush()
    return out.code


def current_user():
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        return str(os.getuid())


def audit(verb, args, decision):
    """journal record the caller cannot reach: `<user> <verb> <args> allow|deny:<reason>`."""
    if not shutil.which('logger'):
        return
    # shlex.join は 3.8 以降。Amazon Linux 2 の 3.7 でも動くように quote で組む
    message = f"{current_user()} {verb} {' '.join(shlex.quote(a) for a in args)} {decision}"
    subprocess.run(['logger', '-t', LOG_TAG, '--', message], env={'PATH': PATH},
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


@contextlib.contextmanager
def single_run():
    """One request at a time per host. The lock file is created at install time."""
    try:
        handle = open(LOCK_PATH, 'a+')
    except OSError as error:
        raise Denied(f'busy: cannot open lock ({error.strerror})')
    deadline = time.monotonic() + LOCK_WAIT
    with handle:
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise Denied('busy: another request is running; retry later')
                time.sleep(0.2)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def handle(original, conf, stream):
    """Validate, record, run. Returns the exit code."""
    argv = []
    try:
        argv = tokenize(original)
        if conf is None:
            conf = load_conf()
        plan = build(argv, conf)
    except Denied as error:
        audit(argv[0] if argv else '-', argv[1:], f'deny:{error}')
        stream.write(f'denied: {error}\n'.encode())
        stream.flush()
        return 2
    audit(plan.verb, argv[1:], 'allow')
    try:
        with single_run():
            return execute(plan, stream)
    except Denied as error:
        stream.write(f'denied: {error}\n'.encode())
        stream.flush()
        return 2


def main():
    os.environ['PATH'] = PATH
    lower_priority()
    conf = None
    try:
        conf = load_conf()
    except Denied:
        pass
    original = os.environ.get('SSH_ORIGINAL_COMMAND')
    if conf is None:
        # Fail closed, but still say why so the operator can fix the install.
        sys.stdout.buffer.write(b'denied: configuration unavailable\n')
        audit('-', [original or ''], 'deny:configuration unavailable')
        sys.exit(2)
    sys.exit(handle(original, conf, sys.stdout.buffer))


if __name__ == '__main__':
    main()
