#!/usr/bin/env python3
"""Lambda code extractor: unpack deployment packages, keep what can be read, mask it.

Runs in a throwaway container with no network and no credentials:

    python3 extract.py <in> <out>

<in>/jobs.json lists the packages the host downloaded:

    {"with_deps": false,
     "jobs": [{"zip": "f1.zip" | null, "dest": "lambda/<region>/<name>", "meta": {...}}]}

For each job, <out>/<dest>/src/ is replaced with the filtered code and <out>/<dest>/_manifest.json
with the host's meta plus what was kept, skipped and listed ("contents"). A job without a zip
(container image functions) gets the manifest only. One JSON line per job goes to stdout.

The package belongs to the target and is not trusted: no path leaves <dest>/src, links are
recorded but never created, and bytes are counted as they are decompressed. Raw contents stay
in memory; only masked text is written. The deny list and the masking rules are the EC2
gateway's (imported, not copied); only its `password = ...` rule is narrowed for source code.
"""
import base64
import binascii
import csv
import fnmatch
import hashlib
import io
import json
import os
import posixpath
import re
import shutil
import stat
import sys
import zipfile
import zlib

sys.dont_write_bytecode = True
_HERE = os.path.dirname(os.path.abspath(__file__))
# In the container both files sit side by side; in the repository the gateway is in ../ec2.
for _dir in (_HERE, os.path.join(os.path.dirname(_HERE), 'ec2')):
    if os.path.isfile(os.path.join(_dir, 'gateway.py')):
        sys.path.insert(0, _dir)
        break
import gateway  # noqa: E402

TOTAL_LIMIT = 250 * 1024 * 1024      # Lambda's own limit for an unzipped package
FILE_LIMIT = TOTAL_LIMIT
ENTRY_LIMIT = 100000
META_LIMIT = 10 * 1024 * 1024        # metadata read only for the dependency list
LIST_LIMIT = 2000                    # entries kept per list in the manifest
BUNDLE_SIZE = 100 * 1024
BUNDLE_MARKERS = ('__webpack_require__', '__commonJS(', '__toESM(', 'System.register(')

# Files with these extensions are code: the deny list's name rules (*secret*, *password* ...)
# do not apply to them, because secrets.py or password_reset.js is exactly what should be read.
SOURCE_EXTENSIONS = {
    '.py', '.pyi', '.js', '.mjs', '.cjs', '.jsx', '.ts', '.mts', '.cts', '.tsx', '.rb', '.java',
    '.kt', '.kts', '.scala', '.groovy', '.go', '.rs', '.cs', '.fs', '.vb', '.php', '.swift', '.c',
    '.h', '.cc', '.cpp', '.hpp', '.sh', '.bash', '.ps1', '.lua', '.pl', '.pm', '.r', '.dart', '.ex',
    '.exs', '.erl', '.clj',
}
JS_EXTENSIONS = {'.js', '.mjs', '.cjs'}

# The gateway's first rule masks `password = <anything>`. In code that hides where a value comes
# from (os.environ[...], event[...]), which is what the survey wants to know, so for source files
# it is replaced by one that masks only a quoted literal. The key may be quoted ("password": "x"),
# which the gateway's rule misses, so the literal rule is added for the other text files too.
KEY_RULE = gateway.MASK_RULES[0]
LITERAL_RULE = (re.compile(
    r'''((?:password|passwd|secret|secret[_-]?key|token|api[_-]?key|private[_-]?key)["']?\s*(?::=|==|[:=])\s*[rbfu]{0,2})'''
    r'''(["'`])(?:\\.|(?!\2)[^\\\n])*\2''', re.I), r'\1\2***\2')
MASK_RULES_SOURCE = [LITERAL_RULE] + [rule for rule in gateway.MASK_RULES if rule is not KEY_RULE]
MASK_RULES_OTHER = gateway.MASK_RULES + [LITERAL_RULE]

INLINE_MAP = re.compile(r'^[ \t]*//[#@][ \t]*sourceMappingURL=data:[^,\n]*;base64,([A-Za-z0-9+/=_-]+)[ \t]*$', re.M)
URL_MAP = re.compile(r'^[ \t]*//[#@][ \t]*sourceMappingURL=([^\s]+)[ \t]*$', re.M)
DEST_RE = re.compile(r'^(lambda|lambda-layers)(/[A-Za-z0-9._-]+){2,3}$')


class JobError(Exception):
    """The package cannot be extracted as a whole. The message names sizes and paths, never contents."""


def mask_text(text, source):
    for pattern, replacement in (MASK_RULES_SOURCE if source else MASK_RULES_OTHER):
        text = pattern.sub(replacement, text)
    return text


def extension(name):
    return posixpath.splitext(name)[1].lower()


def is_source(name):
    return extension(name) in SOURCE_EXTENSIONS


def name_rule(name):
    """The deny-list pattern a file name matches, or None (same matching as the gateway)."""
    lower = name.lower()
    for pattern in gateway.DENY_NAMES:
        if fnmatch.fnmatchcase(lower, pattern.lower()):
            return pattern
    return None


def component_rule(parts):
    for component in gateway.DENY_COMPONENTS:
        if component in parts:
            return f'component:{component}'
    return None


def safe_parts(name):
    """Path components of a zip entry, or None when it would leave the destination."""
    name = name.replace('\\', '/')
    if name.startswith('/') or re.match(r'^[A-Za-z]:', name):
        return None
    parts = [p for p in name.split('/') if p not in ('', '.')]
    if not parts or '..' in parts or any(ord(c) < 0x20 or c == '\x7f' for p in parts for c in p):
        return None
    return parts


def source_parts(source):
    """A source map entry (webpack://app/./src/x.ts, ../../src/x.ts, file:///...) as a relative path."""
    source = re.sub(r'^[A-Za-z][A-Za-z0-9+.-]*:(//[^/]*)?', '', source.replace('\\', '/'))
    parts = [p for p in source.split('/') if p not in ('', '.', '..')]
    return [p for p in parts if not any(ord(c) < 0x20 or c == '\x7f' for c in p)]


def as_text(data):
    if b'\0' in data:
        return None
    try:
        return data.decode('utf-8')
    except UnicodeDecodeError:
        return None


def check_dest(dest):
    if not isinstance(dest, str) or not DEST_RE.match(dest) or any(p in ('.', '..') for p in dest.split('/')):
        raise JobError(f'invalid destination {dest!r}')
    return dest


class Extraction:
    def __init__(self, zf, staging, meta, with_deps):
        self.zf = zf
        self.staging = staging
        self.meta = meta
        self.with_deps = with_deps
        self.read_bytes = 0
        self.written_bytes = 0
        self.written = set()
        self.files = 0
        self.own_code = 0
        self.skipped_count = 0
        self.truncated = set()
        self.lists = {'skipped': [], 'symlinks': [], 'masked_only': [], 'binaries': [], 'jars': [],
                      'sourcemaps': [], 'bundles': []}
        self.deps = {'python': [], 'node': [], 'ruby': [], 'java': []}
        self.dep_seen = set()
        self.excluded = {'files': 0, 'bytes': 0}
        self.classes = []
        self.pending_bundles = []            # (path, record) until the maps have been seen
        self.maps_done = set()

    # ---- bookkeeping ----

    def add(self, name, item):
        if len(self.lists[name]) < LIST_LIMIT:
            self.lists[name].append(item)
        else:
            self.truncated.add(name)

    def skip(self, path, reason):
        self.skipped_count += 1
        self.add('skipped', {'path': path, 'reason': reason})

    def dependency(self, kind, name, version):
        key = (kind, name, version)
        if not name or key in self.dep_seen:
            return
        self.dep_seen.add(key)
        if len(self.deps[kind]) < LIST_LIMIT:
            self.deps[kind].append({'name': name, 'version': version})
        else:
            self.truncated.add(f'dependencies.{kind}')

    def read(self, zf, info, limit=None):
        """Decompress one entry, counting the bytes actually produced (headers are not trusted)."""
        limit = min(FILE_LIMIT, limit or FILE_LIMIT)
        out = bytearray()
        with zf.open(info) as handle:
            while True:
                chunk = handle.read(65536)
                if not chunk:
                    break
                out += chunk
                self.read_bytes += len(chunk)
                if len(out) > limit:
                    raise JobError(f'{info.filename}: larger than {limit} bytes when unpacked')
                if self.read_bytes > TOTAL_LIMIT:
                    raise JobError(f'package larger than {TOTAL_LIMIT} bytes when unpacked')
        return bytes(out)

    def read_meta(self, info):
        if info.file_size > META_LIMIT:
            return None
        try:
            return self.read(self.zf, info).decode('utf-8', 'replace')
        except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, RuntimeError):
            return None

    def write(self, parts, text, own):
        rel = '/'.join(parts)
        if rel in self.written:
            self.skip(rel, 'duplicate')
            return False
        data = text.encode('utf-8')
        if self.written_bytes + len(data) > TOTAL_LIMIT:
            raise JobError(f'output larger than {TOTAL_LIMIT} bytes')
        target = os.path.join(self.staging, *parts)
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, 'xb') as handle:        # never follows or replaces an existing path
                handle.write(data)
        except OSError:
            self.skip(rel, 'unwritable')
            return False
        self.written.add(rel)
        self.written_bytes += len(data)
        self.files += 1
        if own and is_source(parts[-1]):
            self.own_code += 1
        return True

    # ---- the package ----

    def run(self):
        infos = self.zf.infolist()
        if len(infos) > ENTRY_LIMIT:
            raise JobError(f'package has more than {ENTRY_LIMIT} entries')
        entries = []
        for info in infos:
            if info.is_dir():
                continue
            parts = safe_parts(info.filename)
            if parts is None:
                self.skip(info.filename, 'unsafe-path')
                continue
            entries.append((info, parts))
        dep_paths = self.scan_metadata(entries)
        maps = {}
        for info, parts in entries:
            self.entry(info, parts, dep_paths, maps)
        for path, bundle in self.pending_bundles:
            self.bundle_map(path, bundle, maps)
        for path, info in sorted(maps.items()):
            if path not in self.maps_done:
                self.restore(path, self.read(self.zf, info), own=True)
        return self.contents()

    def scan_metadata(self, entries):
        """Paths that belong to dependencies (from *.dist-info/RECORD) and the dependency list."""
        dep_paths = set()
        for info, parts in entries:
            name = parts[-1]
            parent = parts[-2] if len(parts) > 1 else ''
            if parent.endswith('.dist-info') and name == 'RECORD':
                text = self.read_meta(info)
                base = parts[:-2]
                for row in csv.reader((text or '').splitlines()):
                    if row and row[0]:
                        path = posixpath.normpath('/'.join(base + [row[0]]))
                        if not path.startswith('..'):
                            dep_paths.add(path)
            elif (parent.endswith('.dist-info') and name == 'METADATA') or (parent.endswith('.egg-info') and name == 'PKG-INFO'):
                headers = dict(re.findall(r'^(Name|Version):\s*(.+?)\s*$', self.read_meta(info) or '', re.M))
                self.dependency('python', headers.get('Name'), headers.get('Version'))
            elif name == 'package.json' and self.node_package(parts):
                try:
                    doc = json.loads(self.read_meta(info) or '')
                except ValueError:
                    continue
                if isinstance(doc, dict) and isinstance(doc.get('name'), str):
                    self.dependency('node', doc['name'], str(doc.get('version', '')))
            elif name == 'Gemfile.lock' and parts[:2] != ['vendor', 'bundle']:
                for gem, version in re.findall(r'^    ([^\s(]+) \(([^)]+)\)$', self.read_meta(info) or '', re.M):
                    self.dependency('ruby', gem, version)
            elif name == 'pom.properties' and len(parts) >= 5 and parts[-5:-3] == ['META-INF', 'maven']:
                self.pom(self.read_meta(info))
        return dep_paths

    @staticmethod
    def node_package(parts):
        """node_modules/<name>/package.json or node_modules/@scope/<name>/package.json."""
        for tail in (3, 4):
            if len(parts) >= tail and parts[-tail] == 'node_modules' and (tail == 3 or parts[-3].startswith('@')):
                return True
        return False

    def pom(self, text):
        props = dict(re.findall(r'^\s*(groupId|artifactId|version)\s*=\s*(.+?)\s*$', text or '', re.M))
        if props.get('artifactId'):
            self.dependency('java', f"{props.get('groupId', '')}:{props['artifactId']}", props.get('version'))

    @staticmethod
    def is_dependency(parts, dep_paths):
        dirs = parts[:-1]
        if 'node_modules' in dirs or '__pycache__' in dirs:
            return True
        if any(d.endswith(('.dist-info', '.egg-info')) for d in dirs):
            return True
        if parts[:2] in (['vendor', 'bundle'], ['ruby', 'gems']):
            return True
        return '/'.join(parts) in dep_paths

    def entry(self, info, parts, dep_paths, maps):
        path = '/'.join(parts)
        kind = stat.S_IFMT(info.external_attr >> 16)
        if kind == stat.S_IFLNK:
            target = self.read(self.zf, info, limit=4096).decode('utf-8', 'replace')
            self.add('symlinks', {'path': path, 'target': target})
            return
        if kind == stat.S_IFDIR:
            return
        if kind not in (0, stat.S_IFREG):
            self.skip(path, 'special-file')
            return
        if info.flag_bits & 0x1:
            self.skip(path, 'encrypted')
            return
        rule = component_rule(parts)
        if rule:
            self.skip(path, rule)
            return
        dep = self.is_dependency(parts, dep_paths)
        if dep and not self.with_deps:
            self.excluded['files'] += 1
            self.excluded['bytes'] += info.file_size
            return
        ext = extension(parts[-1])
        if ext == '.map':
            maps[path] = info                # restored after the scan; the map itself is never written
            return
        if ext == '.class':
            self.classes.append(path)
            return
        source = ext in SOURCE_EXTENSIONS
        rule = name_rule(parts[-1])
        if rule and not source:
            self.skip(path, f'name:{rule}')
            return
        try:
            data = self.read(self.zf, info)
        except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, RuntimeError):
            self.skip(path, 'unreadable')
            return
        if ext == '.jar':
            self.jar(path, data)
            return
        text = as_text(data)
        if text is None:
            self.add('binaries', {'path': path, 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
            return
        if rule:
            self.add('masked_only', {'path': path, 'rule': rule})
        if ext in JS_EXTENSIONS:
            text = self.javascript(path, text, own=not dep)
        self.write(parts, mask_text(text, source), own=not dep)

    def jar(self, path, data):
        """A nested jar (Java dependencies under lib/): listed with its Maven coordinates, not unpacked."""
        entry = {'path': path, 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest(), 'classes': 0}
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as inner:
                for info in inner.infolist()[:ENTRY_LIMIT]:
                    if info.filename.endswith('.class'):
                        entry['classes'] += 1
                    elif re.fullmatch(r'META-INF/maven/[^/]+/[^/]+/pom\.properties', info.filename) \
                            and info.file_size <= META_LIMIT:
                        self.pom(self.read(inner, info).decode('utf-8', 'replace'))
        except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, RuntimeError):
            entry['error'] = 'not a readable jar'
        self.add('jars', entry)

    # ---- bundled JavaScript and source maps ----

    def javascript(self, path, text, own):
        """Strip inline source maps (base64 hides the original source from masking) and note bundles."""
        inline = []

        def strip(match):
            inline.append(match.group(1))
            return '//# sourceMappingURL=(inline source map removed; sources are under _sources/)'
        text = INLINE_MAP.sub(strip, text)
        restored = None
        for encoded in inline:
            try:
                raw = base64.b64decode(encoded + '=' * (-len(encoded) % 4), altchars=b'-_' if '-' in encoded or '_' in encoded else None)
            except (binascii.Error, ValueError):
                continue
            self.read_bytes += len(raw)
            restored = self.restore(f'{path} (inline)', raw, own)
        lines = text.count('\n') + 1
        if len(text) >= BUNDLE_SIZE and (any(m in text for m in BUNDLE_MARKERS) or len(text) / lines > 500):
            reference = None
            match = URL_MAP.search(text)
            if match and not match.group(1).startswith(('data:', '(')) and '://' not in match.group(1):
                reference = posixpath.normpath(posixpath.join(posixpath.dirname(path), match.group(1)))
            self.pending_bundles.append(
                (path, {'path': path, 'size': len(text), 'lines': lines, 'map': restored, '_ref': reference}))
        return text

    def bundle_map(self, path, bundle, maps):
        """Restore the bundle's own map (sourceMappingURL, else <bundle>.map) and record which it was."""
        reference = bundle.pop('_ref')
        if not bundle['map']:
            for candidate in (reference, path + '.map'):
                if candidate and candidate in maps and candidate not in self.maps_done:
                    self.maps_done.add(candidate)
                    bundle['map'] = self.restore(candidate, self.read(self.zf, maps[candidate]), own=True)
                    break
        bundle['map'] = bundle['map'] or 'none'
        self.add('bundles', bundle)

    def restore(self, label, raw, own):
        """Write a source map's sourcesContent under _sources/, with the same rules as files."""
        try:
            doc = json.loads(raw.decode('utf-8'))
        except (UnicodeDecodeError, ValueError):
            self.add('sourcemaps', {'path': label, 'error': 'not a JSON source map'})
            return 'invalid'
        sections = [s.get('map') for s in doc.get('sections', []) if isinstance(s, dict)] if isinstance(doc, dict) else []
        entry = {'path': label, 'sources': 0, 'restored': 0, 'dependencies_skipped': 0}
        has_content = False
        for part in ([doc] if isinstance(doc, dict) else []) + [s for s in sections if isinstance(s, dict)]:
            sources = part.get('sources') or []
            contents = part.get('sourcesContent') or []
            entry['sources'] += len(sources)
            for source, content in zip(sources, contents):
                if not isinstance(source, str) or not isinstance(content, str):
                    continue
                has_content = True
                parts = source_parts(source)
                if not parts:
                    continue
                if 'node_modules' in parts and not self.with_deps:
                    entry['dependencies_skipped'] += 1
                    continue
                path = '/'.join(['_sources'] + parts)
                rule = component_rule(parts)
                if rule:
                    self.skip(path, rule)
                    continue
                source_file = is_source(parts[-1])
                rule = name_rule(parts[-1])
                if rule and not source_file:
                    self.skip(path, f'name:{rule}')
                    continue
                if rule:
                    self.add('masked_only', {'path': path, 'rule': rule})
                if self.write(['_sources'] + parts, mask_text(content, source_file),
                              own=own and 'node_modules' not in parts):
                    entry['restored'] += 1
        self.add('sourcemaps', entry)
        return 'restored' if has_content else 'no-sourcesContent'

    # ---- result ----

    def contents(self):
        handler = str(self.meta.get('handler') or '').split('::')[0]
        package = handler.rsplit('.', 1)[0].replace('.', '/') + '/' if '.' in handler else None
        classes = sorted(self.classes, key=lambda c: (not (package and c.startswith(package)), c))
        result = {
            'status': 'ok',
            'with_deps': self.with_deps,
            'files': self.files,
            'bytes': self.written_bytes,
            'contains_own_code': self.own_code > 0,
            'skipped_count': self.skipped_count,
            'excluded_dependencies': self.excluded,
            'dependencies': {k: v for k, v in self.deps.items() if v},
            'classes': {'count': len(classes), 'names': classes[:LIST_LIMIT]},
        }
        result.update({k: v for k, v in self.lists.items()})
        if len(classes) > LIST_LIMIT:
            self.truncated.add('classes')
        if self.truncated:
            result['truncated_lists'] = sorted(self.truncated)
        return result


def write_json(path, doc):
    temporary = os.path.join(os.path.dirname(path), '.' + os.path.basename(path) + '.new')
    with open(temporary, 'w', encoding='utf-8') as handle:
        json.dump(doc, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    os.replace(temporary, path)


def run_job(job, in_dir, out_dir, with_deps):
    """Extract one package. The previous src/ and manifest stay unless this one succeeds."""
    dest = job.get('dest') if isinstance(job, dict) else None
    staging = None
    try:
        dest = check_dest(dest)
        dest_dir = os.path.join(out_dir, *dest.split('/'))
        os.makedirs(dest_dir, exist_ok=True)
        staging = os.path.join(dest_dir, '.src.new')
        shutil.rmtree(staging, ignore_errors=True)
        os.makedirs(staging)
        meta = job.get('meta') if isinstance(job.get('meta'), dict) else {}
        package = job.get('zip')
        if package:
            if not isinstance(package, str) or '/' in package or package.startswith('.'):
                raise JobError(f'invalid package name {package!r}')
            with zipfile.ZipFile(os.path.join(in_dir, package)) as zf:
                contents = Extraction(zf, staging, meta, with_deps).run()
        else:
            contents = {'status': 'ok', 'files': 0, 'note': 'no package to extract (container image)'}
        src = os.path.join(dest_dir, 'src')
        shutil.rmtree(src, ignore_errors=True)
        if package:
            os.rename(staging, src)
        else:
            shutil.rmtree(staging, ignore_errors=True)
        manifest = dict(meta)
        manifest['contents'] = contents
        write_json(os.path.join(dest_dir, '_manifest.json'), manifest)
    except (JobError, zipfile.BadZipFile, OSError) as error:
        if staging:
            shutil.rmtree(staging, ignore_errors=True)
        return {'dest': dest, 'status': 'error', 'error': str(error)}
    except Exception as error:        # anything unexpected fails this job only, never leaves a half src/
        if staging:
            shutil.rmtree(staging, ignore_errors=True)
        return {'dest': dest, 'status': 'error', 'error': f'internal error ({type(error).__name__})'}
    return {'dest': dest, 'status': 'ok', 'files': contents.get('files', 0),
            'skipped': contents.get('skipped_count', 0), 'binaries': len(contents.get('binaries', [])),
            'excluded_dependencies': contents.get('excluded_dependencies', {}).get('files', 0),
            'restored_sources': sum(m.get('restored', 0) for m in contents.get('sourcemaps', []))}


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        sys.stderr.write('usage: extract.py <in> <out>\n')
        return 2
    in_dir, out_dir = args
    with open(os.path.join(in_dir, 'jobs.json'), encoding='utf-8') as handle:
        plan = json.load(handle)
    with_deps = bool(plan.get('with_deps'))
    failed = 0
    for job in plan.get('jobs', []):
        result = run_job(job, in_dir, out_dir, with_deps)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        failed += result['status'] != 'ok'
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
