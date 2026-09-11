"""The Lambda code extractor, checked on zips built here (no AWS, no Docker).

libexec/lambda/extract.py runs in a throwaway container with no network. What reaches the survey
container is only what it writes, so these tests look at the written tree: the deny list (names
kept, contents dropped), masking (literals only in source code), dependencies left out and listed,
binaries listed, nothing written outside src/ whatever the zip says, and the size limits counted
on decompressed bytes.
"""
import base64
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
EXTRACT = ROOT / 'libexec/lambda/extract.py'
GATEWAY = ROOT / 'libexec/ec2/gateway.py'

spec = importlib.util.spec_from_file_location('extract', EXTRACT)
extract = importlib.util.module_from_spec(spec)
spec.loader.exec_module(extract)

DEST = 'lambda/test-region/fn'


def make_zip(path, files, links=None, method=zipfile.ZIP_DEFLATED):
    with zipfile.ZipFile(path, 'w', method) as z:
        for name, data in files.items():
            info = zipfile.ZipInfo(name)          # a ZipInfo keeps the name as given ('../x', '/x')
            info.compress_type = method
            z.writestr(info, data)
        for name, target in (links or {}).items():
            info = zipfile.ZipInfo(name)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            z.writestr(info, target)


def jar_bytes(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buffer.getvalue()


class ExtractCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name).resolve()
        self.inp = base / 'in'
        self.out = base / 'out'
        self.inp.mkdir()
        self.out.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def extract(self, files=None, links=None, meta=None, with_deps=False, dest=DEST, package='f.zip'):
        if files is not None or links:
            make_zip(self.inp / package, files or {}, links)
        jobs = {'with_deps': with_deps, 'jobs': [{'zip': package if files is not None or links else None,
                                                  'dest': dest, 'meta': meta or {'FunctionName': 'fn'}}]}
        (self.inp / 'jobs.json').write_text(json.dumps(jobs))
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = extract.main([str(self.inp), str(self.out)])
        self.results = [json.loads(line) for line in stdout.getvalue().splitlines()]
        return code

    def src(self, rel=''):
        return self.out / DEST / 'src' / rel

    def manifest(self):
        return json.loads((self.out / DEST / '_manifest.json').read_text())

    def contents(self):
        return self.manifest()['contents']

    def written(self):
        return sorted(str(p.relative_to(self.src())) for p in self.src().rglob('*') if p.is_file())

    def all_output_text(self):
        return ''.join(p.read_text(errors='replace') for p in self.out.rglob('*') if p.is_file())


class DenyAndMask(ExtractCase):
    def test_denied_names_keep_the_name_and_drop_the_content(self):
        code = self.extract({'app.py': 'print(1)\n', '.env': 'SECRET_VALUE_1\n', 'config/secrets.yml': 'SECRET_VALUE_2\n',
                             'certs/server.pem': 'SECRET_VALUE_3\n', 'data.db': 'SECRET_VALUE_4\n',
                             '.git/config': 'SECRET_VALUE_5\n'})
        self.assertEqual(code, 0, self.results)
        self.assertEqual(self.written(), ['app.py'])
        reasons = {s['path']: s['reason'] for s in self.contents()['skipped']}
        self.assertEqual(reasons, {'.env': 'name:.env', 'config/secrets.yml': 'name:*secret*',
                                   'certs/server.pem': 'name:*.pem', 'data.db': 'name:*.db',
                                   '.git/config': 'component:.git'})
        self.assertNotIn('SECRET_VALUE', self.all_output_text())

    def test_source_files_are_not_refused_by_name(self):
        self.extract({'secrets.py': 'import boto3\n', 'password_reset.js': 'exports.h = 1\n',
                      'lib/credentials_provider.ts': 'export {}\n'})
        self.assertEqual(self.written(), ['lib/credentials_provider.ts', 'password_reset.js', 'secrets.py'])
        masked_only = {m['path']: m['rule'] for m in self.contents()['masked_only']}
        self.assertEqual(masked_only, {'secrets.py': '*secret*', 'password_reset.js': '*password*',
                                       'lib/credentials_provider.ts': '*credential*'})

    def test_source_masks_literals_only(self):
        self.extract({'app.py': (
            'password = "hunter2"\n'
            "db_password = 'hunter3'\n"
            'SECRET_KEY = f"hunter4"\n'
            'password = os.environ["DB_PASSWORD"]\n'
            'token = event["headers"]["authorization"]\n'
            'secret_name = "prod/db"\n'
            'client = boto3.client("secretsmanager").get_secret_value(SecretId="prod/db")\n'
            'KEY = "AKIAABCDEFGHIJKLMNOP"\n'
            'url = "postgres://admin:hunter5@db.internal/app"\n'
            'PEM = """-----BEGIN RSA PRIVATE KEY-----\nMIIEhunter6\n-----END RSA PRIVATE KEY-----"""\n'),
            'handler.go': 'password := "hunter7"\n', 'index.js': 'const cfg = {"apiKey": `hunter8`};\n'})
        app = self.src('app.py').read_text()
        for secret in ('hunter2', 'hunter3', 'hunter4', 'hunter5', 'hunter6', 'AKIAABCDEFGHIJKLMNOP'):
            self.assertNotIn(secret, app)
        self.assertIn('password = "***"', app)
        self.assertIn('password = os.environ["DB_PASSWORD"]', app)
        self.assertIn('token = event["headers"]["authorization"]', app)
        self.assertIn('secret_name = "prod/db"', app)
        self.assertIn('SecretId="prod/db"', app)
        self.assertNotIn('hunter7', self.src('handler.go').read_text())
        self.assertNotIn('hunter8', self.src('index.js').read_text())

    def test_other_text_gets_the_gateway_rules_and_quoted_keys(self):
        self.extract({'config.json': '{"password": "hunter2", "url": "mysql://u:hunter3@h/db"}\n',
                      'settings.yml': 'password: hunter4\ntoken=hunter5\nname: plain\n',
                      'template.txt': 'password = os.environ["X"]\n'})
        config = self.src('config.json').read_text()
        settings = self.src('settings.yml').read_text()
        self.assertIn('"password": "***"', config)
        self.assertNotIn('hunter3', config)
        self.assertEqual(settings, 'password: ***\ntoken=***\nname: plain\n')
        self.assertEqual(self.src('template.txt').read_text(), 'password = ***\n')


class Dependencies(ExtractCase):
    PYTHON = {
        'app.py': 'import requests\n',
        'requests/__init__.py': 'x = 1\n',
        'requests/api.py': 'y = 1\n',
        'requests-2.31.0.dist-info/METADATA': 'Metadata-Version: 2.1\nName: requests\nVersion: 2.31.0\n',
        'requests-2.31.0.dist-info/RECORD': 'requests/__init__.py,sha256=x,5\nrequests/api.py,sha256=y,5\n'
                                            'requests-2.31.0.dist-info/METADATA,,\n../../bin/tool,,\n',
        'requirements.txt': 'requests==2.31.0\n',
        '__pycache__/app.cpython-312.pyc': b'\x00\x01',
    }

    def test_python_dependencies_follow_record(self):
        self.extract(self.PYTHON)
        self.assertEqual(self.written(), ['app.py', 'requirements.txt'])
        contents = self.contents()
        self.assertEqual(contents['dependencies']['python'], [{'name': 'requests', 'version': '2.31.0'}])
        self.assertEqual(contents['excluded_dependencies']['files'], 5)     # 2 by RECORD, 2 in dist-info, 1 in __pycache__
        self.assertTrue(contents['contains_own_code'])

    def test_with_deps_places_them_too(self):
        self.extract(self.PYTHON, with_deps=True)
        self.assertIn('requests/api.py', self.written())
        self.assertEqual(self.contents()['excluded_dependencies']['files'], 0)

    def test_node_modules_are_listed_and_package_files_kept(self):
        self.extract({'index.mjs': 'import _ from "lodash"\n', 'package.json': '{"name": "fn"}',
                      'package-lock.json': '{"lockfileVersion": 3}',
                      'node_modules/lodash/package.json': '{"name": "lodash", "version": "4.17.21"}',
                      'node_modules/lodash/index.js': 'module.exports = {}\n',
                      'node_modules/@aws-sdk/client-s3/package.json': '{"name": "@aws-sdk/client-s3", "version": "3.600.0"}',
                      'node_modules/@aws-sdk/client-s3/dist/index.js': ''})
        self.assertEqual(self.written(), ['index.mjs', 'package-lock.json', 'package.json'])
        self.assertEqual(sorted(d['name'] for d in self.contents()['dependencies']['node']), ['@aws-sdk/client-s3', 'lodash'])

    def test_a_layer_of_dependencies_has_no_own_code(self):
        self.extract({'python/lib/python3.12/site-packages/lib1/__init__.py': 'x = 1\n',
                      'python/lib/python3.12/site-packages/lib1-1.0.dist-info/METADATA': 'Name: lib1\nVersion: 1.0\n',
                      'python/lib/python3.12/site-packages/lib1-1.0.dist-info/RECORD': 'lib1/__init__.py,,\n'},
                     dest='lambda-layers/test-region/layer1/3')
        manifest = json.loads((self.out / 'lambda-layers/test-region/layer1/3/_manifest.json').read_text())
        self.assertFalse(manifest['contents']['contains_own_code'])
        self.assertEqual(manifest['contents']['dependencies']['python'], [{'name': 'lib1', 'version': '1.0'}])

    def test_ruby_gems_come_from_the_lockfile(self):
        self.extract({'handler.rb': 'require "json"\n', 'Gemfile.lock': 'GEM\n  specs:\n    aws-sdk-s3 (1.140.0)\n      aws-sdk-core (~> 3)\n',
                      'vendor/bundle/ruby/3.2.0/gems/x/lib/x.rb': ''})
        self.assertEqual(self.written(), ['Gemfile.lock', 'handler.rb'])
        self.assertEqual(self.contents()['dependencies']['ruby'], [{'name': 'aws-sdk-s3', 'version': '1.140.0'}])


class UnreadableForms(ExtractCase):
    def test_binaries_are_listed_with_a_hash(self):
        self.extract({'bootstrap': b'\x7fELF\x02\x01\x01\x00' + b'\x00' * 64, 'README': 'notes\n'})
        self.assertEqual(self.written(), ['README'])
        [binary] = self.contents()['binaries']
        self.assertEqual(binary['path'], 'bootstrap')
        self.assertEqual(binary['size'], 72)
        self.assertEqual(len(binary['sha256']), 64)

    def test_java_classes_config_and_jars(self):
        self.extract({'org/lib/Util.class': b'\xca\xfe\xba\xbe\x00', 'com/example/Handler.class': b'\xca\xfe\xba\xbe\x00',
                      'application.properties': 'db.password=hunter2\ndb.url=jdbc:x\n',
                      'META-INF/MANIFEST.MF': 'Manifest-Version: 1.0\n',
                      'lib/dep.jar': jar_bytes({'META-INF/maven/org.acme/acme-core/pom.properties':
                                                'groupId=org.acme\nartifactId=acme-core\nversion=1.2.3\n',
                                                'org/acme/A.class': b'\xca\xfe'})},
                     meta={'handler': 'com.example.Handler::handleRequest'})
        self.assertEqual(self.written(), ['META-INF/MANIFEST.MF', 'application.properties'])
        self.assertEqual(self.src('application.properties').read_text(), 'db.password=***\ndb.url=jdbc:x\n')
        contents = self.contents()
        self.assertEqual(contents['classes'], {'count': 2, 'names': ['com/example/Handler.class', 'org/lib/Util.class']})
        [jar] = contents['jars']
        self.assertEqual((jar['path'], jar['classes']), ('lib/dep.jar', 1))
        self.assertEqual(contents['dependencies']['java'], [{'name': 'org.acme:acme-core', 'version': '1.2.3'}])

    def test_image_function_gets_the_manifest_only(self):
        code = self.extract(meta={'FunctionName': 'fn', 'PackageType': 'Image', 'ImageUri': 'x.dkr.ecr/app:1'})
        self.assertEqual(code, 0)
        self.assertFalse(self.src().exists())
        self.assertEqual(self.manifest()['ImageUri'], 'x.dkr.ecr/app:1')


class UntrustedZip(ExtractCase):
    def test_paths_that_leave_the_destination_are_not_written(self):
        self.extract({'../evil.py': 'x', '/abs.py': 'x', 'a/../../evil2.py': 'x', 'C:/win.py': 'x', 'ok.py': 'x'})
        self.assertEqual(self.written(), ['ok.py'])
        unsafe = sorted(s['path'] for s in self.contents()['skipped'] if s['reason'] == 'unsafe-path')
        self.assertEqual(len(unsafe), 4)
        outside = [p for p in Path(self.temp.name).rglob('*evil*')]
        self.assertEqual(outside, [])

    def test_symlinks_are_recorded_not_created(self):
        self.extract({'app.py': 'x'}, links={'lib/link.py': '/etc/passwd'})
        self.assertFalse(os.path.lexists(self.src('lib/link.py')))
        self.assertEqual(self.contents()['symlinks'], [{'path': 'lib/link.py', 'target': '/etc/passwd'}])

    def test_the_total_limit_counts_decompressed_bytes(self):
        make_zip(self.inp / 'big.zip', {'a.txt': 'a' * 100000})
        with zipfile.ZipFile(self.inp / 'big.zip') as z:
            self.assertLess(z.getinfo('a.txt').compress_size, 5000)
        with patch.object(extract, 'TOTAL_LIMIT', 50000):
            code = self.extract({'a.txt': 'a' * 100000})
        self.assertEqual(code, 1)
        self.assertEqual(self.results[0]['status'], 'error')
        self.assertFalse(self.src().exists())
        self.assertFalse((self.out / DEST / '_manifest.json').exists())
        self.assertFalse((self.out / DEST / '.src.new').exists())

    def test_the_file_limit_stops_the_package(self):
        with patch.object(extract, 'FILE_LIMIT', 1000):
            code = self.extract({'small.py': 'x', 'large.py': 'x' * 2000})
        self.assertEqual(code, 1)
        self.assertIn('large.py', self.results[0]['error'])

    def test_a_broken_zip_keeps_the_previous_extraction(self):
        self.extract({'v1.py': 'x'})
        (self.inp / 'f.zip').write_bytes(b'not a zip')
        (self.inp / 'jobs.json').write_text(json.dumps({'jobs': [{'zip': 'f.zip', 'dest': DEST, 'meta': {}}]}))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(extract.main([str(self.inp), str(self.out)]), 1)
        self.assertEqual(self.written(), ['v1.py'])

    def test_destinations_stay_under_out(self):
        for dest in ('../x/y/z', 'lambda/../../x', '/abs/lambda/r/f', 'other/r/f', 'lambda/r'):
            with self.subTest(dest=dest):
                self.assertEqual(self.extract({'a.py': 'x'}, dest=dest), 1)
                self.assertEqual(self.results[0]['status'], 'error')
        self.assertEqual([p for p in Path(self.temp.name).rglob('a.py')], [])


class Replacement(ExtractCase):
    def test_a_second_pull_replaces_src_and_keeps_alias_versions(self):
        self.extract({'old.py': 'x'})
        version = self.out / DEST / '3'
        version.mkdir()
        (version / '_manifest.json').write_text('{}')
        self.extract({'new.py': 'y'}, meta={'FunctionName': 'fn', 'CodeSha256': 'new'})
        self.assertEqual(self.written(), ['new.py'])
        self.assertTrue((version / '_manifest.json').exists())
        self.assertEqual(self.manifest()['CodeSha256'], 'new')
        self.assertFalse((self.out / DEST / '.src.new').exists())


class SourceMaps(ExtractCase):
    def bundle(self, extra=''):
        return 'var require_x = __commonJS((exports) => 1);\n' + 'x();' * 40 + '\n' + extra

    MAP = {'version': 3, 'sources': ['../src/handler.ts', '../node_modules/lib/x.js', 'webpack://app/./src/util.ts',
                                     '../src/.env.local'],
           'sourcesContent': ['const password = "hunter2";\nimport { S3Client } from "@aws-sdk/client-s3";\n',
                              'module.exports = 1;\n', 'export const u = 1;\n', 'A=hunter3\n'],
           'mappings': ''}

    def test_sources_come_back_under_sources_and_the_map_is_not_written(self):
        with patch.object(extract, 'BUNDLE_SIZE', 100):
            self.extract({'index.js': self.bundle('//# sourceMappingURL=index.js.map\n'),
                          'index.js.map': json.dumps(self.MAP)})
        self.assertEqual(self.written(), ['_sources/src/handler.ts', '_sources/src/util.ts', 'index.js'])
        handler = self.src('_sources/src/handler.ts').read_text()
        self.assertIn('password = "***"', handler)
        self.assertIn('@aws-sdk/client-s3', handler)
        self.assertNotIn('hunter', self.all_output_text())
        contents = self.contents()
        self.assertEqual(contents['bundles'][0]['map'], 'restored')
        self.assertEqual(contents['sourcemaps'][0]['dependencies_skipped'], 1)
        self.assertEqual(contents['sourcemaps'][0]['restored'], 2)
        self.assertIn({'path': '_sources/src/.env.local', 'reason': 'name:.env.*'}, contents['skipped'])

    def test_with_deps_restores_node_modules_sources_too(self):
        with patch.object(extract, 'BUNDLE_SIZE', 100):
            self.extract({'index.js': self.bundle(), 'index.js.map': json.dumps(self.MAP)}, with_deps=True)
        self.assertIn('_sources/node_modules/lib/x.js', self.written())

    def test_inline_maps_are_removed_from_the_bundle(self):
        inline = base64.b64encode(json.dumps(self.MAP).encode()).decode()
        with patch.object(extract, 'BUNDLE_SIZE', 100):
            self.extract({'index.js': self.bundle(f'//# sourceMappingURL=data:application/json;base64,{inline}\n')})
        bundle = self.src('index.js').read_text()
        self.assertNotIn(inline[:40], bundle)
        self.assertIn('inline source map removed', bundle)
        self.assertIn('_sources/src/handler.ts', self.written())
        self.assertNotIn('hunter2', self.all_output_text())
        self.assertEqual(self.contents()['bundles'][0]['map'], 'restored')

    def test_a_bundle_without_sources_is_kept_and_says_so(self):
        without = dict(self.MAP)
        del without['sourcesContent']
        with patch.object(extract, 'BUNDLE_SIZE', 100):
            self.extract({'index.js': self.bundle(), 'index.js.map': json.dumps(without), 'other.js': self.bundle()})
        self.assertEqual(self.written(), ['index.js', 'other.js'])
        maps = {b['path']: b['map'] for b in self.contents()['bundles']}
        self.assertEqual(maps, {'index.js': 'no-sourcesContent', 'other.js': 'none'})


class WiredToTheGateway(unittest.TestCase):
    """The deny list and masking are the gateway's; extract.py must not grow its own copy."""

    def test_rules_are_imported_not_copied(self):
        text = EXTRACT.read_text()
        for name in ('DENY_NAMES =', 'DENY_COMPONENTS =', 'MASK_RULES ='):
            self.assertNotIn(name, text)
        self.assertEqual(Path(extract.gateway.__file__).resolve(), GATEWAY)
        self.assertIn('password', extract.KEY_RULE[0].pattern, 'the gateway rule narrowed for source code moved')
        self.assertEqual(extract.MASK_RULES_SOURCE[1:], extract.gateway.MASK_RULES[1:])
        self.assertEqual(extract.MASK_RULES_OTHER[:-1], extract.gateway.MASK_RULES)

    def test_runs_as_in_the_container_with_both_files_side_by_side(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            (base / 'x').mkdir()
            shutil.copy(EXTRACT, base / 'x/extract.py')
            shutil.copy(GATEWAY, base / 'x/gateway.py')
            (base / 'in').mkdir()
            (base / 'out').mkdir()
            make_zip(base / 'in/f.zip', {'app.py': 'token = "hunter2"\n'})
            (base / 'in/jobs.json').write_text(json.dumps({'jobs': [{'zip': 'f.zip', 'dest': DEST, 'meta': {}}]}))
            result = subprocess.run([sys.executable, '-B', str(base / 'x/extract.py'), str(base / 'in'), str(base / 'out')],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['status'], 'ok')
            self.assertEqual((base / 'out' / DEST / 'src/app.py').read_text(), 'token = "***"\n')


if __name__ == '__main__':
    unittest.main()
