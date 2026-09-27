"""`aws-survey lambda pull / list / remove`, checked with fake `aws`, `curl` and `docker` (no AWS, no Docker).

pull borrows the survey role with an inline policy of four Lambda read actions and no managed policy,
keeps that key out of files and argv, checks it cannot read beyond Lambda, asks get-function only
through --query (environment values and the signed code URL never reach the screen or a file), hands
the URL to curl on stdin, and extracts in a container with no network and no credentials. The fake
docker runs the real extractor on the mounted host paths, so the tree under code/ is the real one.
The raw zips live in a temporary directory that is gone afterwards, whatever happened.
"""
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / 'bin/aws-survey'
TOOLS = ['bash', 'sh', 'env', 'jq', 'sed', 'grep', 'awk', 'cut', 'head', 'tail', 'cat', 'wc', 'tr', 'tee',
         'dirname', 'basename', 'readlink', 'mkdir', 'chmod', 'cp', 'mv', 'rm', 'rmdir', 'stat', 'date',
         'mktemp', 'python3', 'printf', 'echo', 'test', 'touch', 'uname', 'sort', 'ls', 'find', 'id']
LAYER = 'arn:aws:lambda:test-region:000000000000:layer:shared:3'
SIGNATURE = 'X-Amz-Signature=SECRETSIGNATURE'
PULL_SECRET = 'pull-secret-value'
PULL_TOKEN = 'pull-session-token'
ENV_VALUE = 'db-password-value'       # what an unfiltered get-function answer would carry
SECRETS = (PULL_SECRET, PULL_TOKEN, 'SECRETSIGNATURE', ENV_VALUE)

FAKE_AWS = r'''#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
# global options (--region r) may come before the service
words = [a for i, a in enumerate(argv) if a != "--region" and (i == 0 or argv[i - 1] != "--region")]
op = words[1] if len(words) > 1 else ""
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps({"tool": "aws", "op": op, "argv": argv, "key": os.environ.get("AWS_ACCESS_KEY_ID"),
                        "profile": os.environ.get("AWS_PROFILE"), "config": os.environ.get("AWS_CONFIG_FILE")}) + "\n")
def opt(name, default=None):
    return argv[argv.index(name) + 1] if name in argv else default
def fail(message):
    sys.stderr.write("An error occurred (%s)\n" % message); sys.exit(254)
state = json.load(open(os.environ["FAKE_STATE"]))
keyed = os.environ.get("AWS_ACCESS_KEY_ID") == "ASIAPULLKEY"
if op == "get-caller-identity":
    if os.environ.get("FAKE_SRC_EXPIRED") == "1":
        fail("ExpiredToken) when calling the GetCallerIdentity operation: The security token included in the request is expired")
    print("arn:aws:iam::000000000000:user/fake"); sys.exit(0)
if op == "assume-role":
    print("ASIAPULLKEY\t__SECRET__\t__TOKEN__"); sys.exit(0)
if op == "describe-vpcs":
    if keyed and os.environ.get("FAKE_VPC_ALLOWED") != "1":
        fail("UnauthorizedOperation) when calling the DescribeVpcs operation")
    print("vpc-0fake"); sys.exit(0)
if not keyed:
    fail("AccessDenied) the fake answers Lambda only to the pull key")
if op == "list-functions":
    print(json.dumps(sorted(state["functions"]))); sys.exit(0)
if op == "get-function":
    name, qualifier = opt("--function-name"), opt("--qualifier")
    fn = state["functions"].get(name)
    if fn is None:
        fail("ResourceNotFoundException) when calling the GetFunction operation: Function not found")
    if os.environ.get("FAKE_EXPIRE_AT") == name:
        fail("ExpiredTokenException) when calling the GetFunction operation: The security token included in the request is expired")
    code = fn["versions"][qualifier] if qualifier else fn
    config = {"FunctionName": name, "Runtime": fn.get("runtime", "python3.12"), "Handler": "app.handler",
              "PackageType": fn.get("package", "Zip"), "CodeSha256": code["sha"], "Version": qualifier or "$LATEST",
              "Layers": fn.get("layers", []), "EnvironmentVariableNames": ["DB_HOST", "DB_PASSWORD"]}
    if "keys(Environment.Variables" not in opt("--query", ""):
        config["Environment"] = {"Variables": {"DB_PASSWORD": "__ENV__"}}
    print(json.dumps({"c": config, "image": {"ImageUri": fn.get("image")},
                      "location": "https://fake-s3.example/%s?__SIG__" % code.get("zip", "none")}))
    sys.exit(0)
if op == "list-aliases":
    print(json.dumps(state["functions"][opt("--function-name")].get("aliases", []))); sys.exit(0)
if op == "get-layer-version-by-arn":
    arn = opt("--arn"); layer = state["layers"][arn]
    print(json.dumps({"c": {"LayerVersionArn": arn, "Version": int(arn.rsplit(":", 1)[1]), "CodeSha256": layer["sha"]},
                      "location": "https://fake-s3.example/%s?__SIG__" % layer["zip"]}))
    sys.exit(0)
fail("unexpected call " + " ".join(argv))
'''.replace('__SECRET__', PULL_SECRET).replace('__TOKEN__', PULL_TOKEN).replace('__ENV__', ENV_VALUE).replace('__SIG__', SIGNATURE)

FAKE_CURL = r'''#!/usr/bin/env python3
import json, os, re, shutil, sys
argv = sys.argv[1:]
config = sys.stdin.read() if "-K" in argv else ""
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps({"tool": "curl", "argv": argv, "stdin_has_url": "url = " in config}) + "\n")
url = re.search(r'url = "([^"]+)"', config).group(1)
name = url.split("/")[-1].split("?")[0]
if name == os.environ.get("FAKE_CURL_FAIL"):
    sys.stderr.write("curl: (22) The requested URL returned error: 403 for %s\n" % url); sys.exit(22)
shutil.copy(os.path.join(os.environ["FAKE_ZIPS"], name), argv[argv.index("-o") + 1])
'''

FAKE_DOCKER = r'''#!/usr/bin/env python3
import json, os, subprocess, sys
argv = sys.argv[1:]
entry = {"tool": "docker", "argv": argv}
mounts = {}
if argv[:1] == ["run"]:
    for i, a in enumerate(argv):
        if a == "-v":
            parts = argv[i + 1].split(":")
            mounts[parts[1]] = parts[0]
    entry["in"] = mounts.get("/in")
    entry["in_files"] = sorted(os.listdir(mounts["/in"]))
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps(entry) + "\n")
if argv[:1] == ["run"]:
    if os.environ.get("FAKE_DOCKER_FAIL") == "1":
        sys.stderr.write("docker: Error response from daemon: boom\n"); sys.exit(125)
    sys.exit(subprocess.run([sys.executable, "-B", mounts["/x/extract.py"], mounts["/in"], mounts["/out"]]).returncode)
'''


def zip_bytes(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buffer.getvalue()


ZIPS = {
    'fn-py.zip': {'app.py': 'import boto3\npassword = "hunter2"\n', '.env': 'SECRET=1\n'},
    'fn-py-v2.zip': {'app.py': 'OLD = True\n'},
    'fn-node.zip': {'index.mjs': 'export const handler = async () => 1;\n',
                    'node_modules/x/package.json': '{"name": "x", "version": "1.0.0"}', 'node_modules/x/index.js': ''},
    'layer.zip': {'python/lib/python3.12/site-packages/lib1/__init__.py': 'x = 1\n',
                  'python/lib/python3.12/site-packages/lib1-1.0.dist-info/METADATA': 'Name: lib1\nVersion: 1.0\n',
                  'python/lib/python3.12/site-packages/lib1-1.0.dist-info/RECORD': 'lib1/__init__.py,,\n'},
}


def default_state():
    return {'functions': {
        'fn-py': {'sha': 'sha-py-new', 'zip': 'fn-py.zip', 'layers': [LAYER],
                  'aliases': [{'name': 'dev', 'version': '$LATEST'}, {'name': 'live', 'version': '2'},
                              {'name': 'same', 'version': '5'}],
                  'versions': {'2': {'sha': 'sha-py-old', 'zip': 'fn-py-v2.zip'},
                               '5': {'sha': 'sha-py-new', 'zip': 'fn-py.zip'}}},
        'fn-node': {'sha': 'sha-node', 'zip': 'fn-node.zip', 'runtime': 'nodejs20.x'}},
        'layers': {LAYER: {'sha': 'sha-layer', 'zip': 'layer.zip'}}}


class LambdaCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name).resolve()
        self.base = base
        self.home = base / 'home'
        self.target = base / 'target'
        self.bin = base / 'bin'
        self.zips = base / 'zips'
        self.tmp = base / 'tmp'
        for d in (self.home, self.target, self.bin, self.zips, self.tmp):
            d.mkdir()
        for tool in TOOLS:
            found = shutil.which(tool)
            if found:
                (self.bin / tool).symlink_to(found)
        for name, text in (('aws', FAKE_AWS), ('curl', FAKE_CURL), ('docker', FAKE_DOCKER)):
            (self.bin / name).write_text(text)
            (self.bin / name).chmod(0o755)
        for name, files in ZIPS.items():
            (self.zips / name).write_bytes(zip_bytes(files))
        self.log = base / 'calls.jsonl'
        self.state_file = base / 'state.json'
        self.set_state(default_state())
        config = json.loads((ROOT / 'templates/environment.json').read_text())
        config.update(name='smoke', account_id='000000000000', region='test-region')
        config['auth'].update(route='own_role', source_profile='fake-src', principal_arn='arn:aws:iam::000000000000:user/fake',
                              role_name='fake-role', refresh_command='aws-login --profile fake-src')
        (self.target / 'environment.json').write_text(json.dumps(config))
        self.code = self.target / 'code'

    def tearDown(self):
        self.temp.cleanup()

    def set_state(self, state):
        self.state_file.write_text(json.dumps(state))

    def state(self):
        return json.loads(self.state_file.read_text())

    def run_cli(self, *args, **knobs):
        env = {'PATH': str(self.bin), 'HOME': str(self.home), 'LANG': os.environ.get('LANG', 'C.UTF-8'),
               'LC_ALL': os.environ.get('LC_ALL', ''), 'TZ': 'UTC', 'TMPDIR': str(self.tmp),
               'FAKE_LOG': str(self.log), 'FAKE_STATE': str(self.state_file), 'FAKE_ZIPS': str(self.zips),
               'AWS_PROFILE': 'must-not-reach-the-pull-key'}
        env = {k: v for k, v in env.items() if v}
        env.update(knobs)
        return subprocess.run([str(CLI), '--dir', str(self.target), *args], capture_output=True, text=True,
                              errors='replace', env=env)

    def pull(self, *args, **knobs):
        result = self.run_cli('lambda', 'pull', *args, **knobs)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def calls(self, tool=None):
        if not self.log.exists():
            return []
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        return [c for c in calls if tool is None or c['tool'] == tool]

    def ops(self):
        return [c['op'] for c in self.calls('aws')]

    def runs(self):
        return [c for c in self.calls('docker') if c['argv'][0] == 'run']

    def clear_log(self):
        if self.log.exists():
            self.log.unlink()

    def manifest(self, rel):
        return json.loads((self.code / rel / '_manifest.json').read_text())

    def assert_nothing_leaked(self, result):
        text = result.stdout + result.stderr
        for secret in SECRETS:
            self.assertNotIn(secret, text)
        for base in (self.home, self.target, self.tmp):
            for path in base.rglob('*'):
                if path.is_file():
                    data = path.read_bytes()
                    for secret in SECRETS + ('hunter2',):
                        self.assertNotIn(secret.encode(), data, f'{secret} in {path}')
        self.assertEqual(list(self.tmp.iterdir()), [], 'the temporary directory (raw zips) must be gone')


class Pull(LambdaCase):
    def test_one_function_end_to_end(self):
        result = self.pull('fn-py')
        self.assertEqual(self.ops(), ['get-caller-identity', 'assume-role', 'describe-vpcs', 'get-function', 'list-aliases',
                                      'get-function', 'get-function', 'get-layer-version-by-arn'])
        calls = self.calls('aws')
        assume = calls[1]['argv']
        self.assertEqual(assume[assume.index('--profile') + 1], 'fake-src')
        self.assertNotIn('--policy-arns', assume)
        self.assertEqual(assume[assume.index('--duration-seconds') + 1], '900')
        self.assertTrue(assume[assume.index('--role-session-name') + 1].startswith('lambda-pull-'))
        policy = json.loads(assume[assume.index('--policy') + 1])
        [statement] = policy['Statement']
        self.assertEqual(statement['Effect'], 'Allow')
        self.assertEqual(sorted(statement['Action']), ['lambda:GetFunction', 'lambda:GetLayerVersion',
                                                       'lambda:ListAliases', 'lambda:ListFunctions'])
        for call in calls[:2]:
            self.assertIsNone(call['key'])
        for call in calls[2:]:
            self.assertEqual(call['key'], 'ASIAPULLKEY')
            self.assertNotIn('--profile', call['argv'])
            self.assertIsNone(call['profile'], 'AWS_PROFILE must not reach the pull key')
            self.assertEqual(call['config'], '/dev/null')
        query = calls[3]['argv'][calls[3]['argv'].index('--query') + 1]
        self.assertIn('keys(Environment.Variables', query)
        self.assertEqual(query.replace('EnvironmentVariableNames:', '').count('Environment'), 1,
                         'Environment is read only through keys(), never its values')

        curls = self.calls('curl')
        self.assertEqual(len(curls), 3)                          # $LATEST, version 2, the layer
        for call in curls:
            self.assertTrue(call['stdin_has_url'])
            self.assertEqual(call['argv'][call['argv'].index('-K') + 1], '-')
            self.assertEqual(call['argv'][call['argv'].index('--proto') + 1], '=https')
            self.assertIn('--max-filesize', call['argv'])
            self.assertFalse(any('https://' in a for a in call['argv']), 'the URL must not be an argument')

        runs = self.runs()
        self.assertEqual(len(runs), 2)                           # functions, then layers
        # each extraction mounts a new directory: Docker Desktop may hide a jobs.json deleted and recreated in place
        self.assertNotEqual(runs[0]['in'], runs[1]['in'])
        self.assertEqual(runs[1]['in_files'], ['3.zip', 'jobs.json'])     # version 2, $LATEST, then the layer
        for run in runs:
            argv = run['argv']
            self.assertEqual(argv[argv.index('--network') + 1], 'none')
            self.assertIn('--read-only', argv)
            self.assertEqual(argv[argv.index('-u') + 1], f'{os.getuid()}:{os.getgid()}')
            self.assertNotIn('-e', argv)
            mounts = [argv[i + 1] for i, a in enumerate(argv) if a == '-v']
            self.assertEqual(len(mounts), 4)
            self.assertTrue(mounts[0].endswith(':/in:ro'))
            self.assertIn(f'{self.code}:/out', mounts)
            self.assertIn(f'{ROOT}/libexec/lambda/extract.py:/x/extract.py:ro', mounts)
            self.assertIn(f'{ROOT}/libexec/ec2/gateway.py:/x/gateway.py:ro', mounts)
            self.assertEqual(argv[-6:], ['smoke:latest', 'python3', '-B', '/x/extract.py', '/in', '/out'])
            self.assertIn('jobs.json', run['in_files'])
            self.assertFalse(Path(run['in']).exists())

        manifest = self.manifest('lambda/test-region/fn-py')
        self.assertEqual(manifest['CodeSha256'], 'sha-py-new')
        self.assertEqual(manifest['Region'], 'test-region')
        self.assertEqual(manifest['EnvironmentVariableNames'], ['DB_HOST', 'DB_PASSWORD'])
        self.assertNotIn('location', manifest)
        self.assertNotIn('Environment', manifest)
        self.assertEqual(manifest['Aliases'], [{'name': 'dev', 'version': '$LATEST', 'code': '$LATEST'},
                                               {'name': 'live', 'version': '2', 'code': '2/'},
                                               {'name': 'same', 'version': '5', 'code': '$LATEST'}])
        self.assertEqual(manifest['contents']['status'], 'ok')
        self.assertEqual((self.code / 'lambda/test-region/fn-py/src/app.py').read_text(), 'import boto3\npassword = "***"\n')
        self.assertFalse((self.code / 'lambda/test-region/fn-py/src/.env').exists())
        self.assertEqual((self.code / 'lambda/test-region/fn-py/2/src/app.py').read_text(), 'OLD = True\n')
        self.assertEqual(self.manifest('lambda/test-region/fn-py/2')['Aliases'], ['live'])
        self.assertFalse((self.code / 'lambda/test-region/fn-py/5').exists())
        layer = self.manifest('lambda-layers/test-region/shared/3')
        self.assertFalse(layer['contents']['contains_own_code'])
        self.assertIn('取り出しました', result.stdout)
        self.assert_nothing_leaked(result)

    def test_all_follows_the_cli_paging(self):
        self.pull('--all')
        [listing] = [c for c in self.calls('aws') if c['op'] == 'list-functions']
        self.assertNotIn('--max-items', listing['argv'])
        for name in ('fn-py', 'fn-node'):
            self.assertTrue((self.code / f'lambda/test-region/{name}/_manifest.json').exists())
        self.assertEqual(self.manifest('lambda/test-region/fn-node')['contents']['dependencies']['node'],
                         [{'name': 'x', 'version': '1.0.0'}])

    def test_unchanged_code_is_not_downloaded_again(self):
        self.pull('fn-py')
        self.clear_log()
        result = self.pull('fn-py')
        self.assertEqual(self.calls('curl'), [])
        self.assertEqual(self.runs(), [])
        self.assertEqual(result.stdout.count('変わっていません'), 3)
        self.assertIn('checked_at', self.manifest('lambda/test-region/fn-py'))
        self.assert_nothing_leaked(result)

    def test_changed_code_is_pulled_again(self):
        self.pull('fn-py')
        state = self.state()
        state['functions']['fn-py']['sha'] = state['functions']['fn-py']['versions']['5']['sha'] = 'sha-py-newer'
        self.set_state(state)
        self.clear_log()
        self.pull('fn-py')
        self.assertEqual(len(self.calls('curl')), 1)
        self.assertEqual(self.manifest('lambda/test-region/fn-py')['CodeSha256'], 'sha-py-newer')

    def test_a_version_no_alias_points_at_is_dropped(self):
        self.pull('fn-py')
        state = self.state()
        state['functions']['fn-py']['aliases'] = [{'name': 'live', 'version': '5'}]
        self.set_state(state)
        self.pull('fn-py')
        self.assertFalse((self.code / 'lambda/test-region/fn-py/2').exists())
        self.assertEqual(self.manifest('lambda/test-region/fn-py')['Aliases'],
                         [{'name': 'live', 'version': '5', 'code': '$LATEST'}])

    def test_with_deps_is_a_different_extraction(self):
        self.pull('fn-node')
        self.clear_log()
        self.pull('fn-node', '--with-deps')
        self.assertEqual(len(self.calls('curl')), 1)
        self.assertTrue((self.code / 'lambda/test-region/fn-node/src/node_modules/x/index.js').exists())

    def test_image_functions_keep_the_manifest_only(self):
        state = self.state()
        state['functions']['fn-img'] = {'sha': 'sha-img', 'package': 'Image',
                                        'image': '000000000000.dkr.ecr.test-region.amazonaws.com/app:1'}
        self.set_state(state)
        result = self.pull('fn-img')
        self.assertEqual(self.calls('curl'), [])
        manifest = self.manifest('lambda/test-region/fn-img')
        self.assertEqual(manifest['ImageUri'], '000000000000.dkr.ecr.test-region.amazonaws.com/app:1')
        self.assertFalse((self.code / 'lambda/test-region/fn-img/src').exists())
        self.assertIn('コンテナイメージ形式', result.stdout)

    def test_a_key_that_reads_beyond_lambda_stops_before_any_code(self):
        result = self.run_cli('lambda', 'pull', 'fn-py', FAKE_VPC_ALLOWED='1')
        self.assertEqual(result.returncode, 1)
        self.assertNotIn('get-function', self.ops())
        self.assertEqual(self.calls('curl'), [])
        self.assertEqual(self.runs(), [])
        self.assertIn('Lambda 以外', result.stdout + result.stderr)

    def test_a_failed_download_is_reported_and_the_rest_goes_on(self):
        result = self.run_cli('lambda', 'pull', 'fn-py', 'fn-node', FAKE_CURL_FAIL='fn-py.zip')
        self.assertEqual(result.returncode, 1)
        self.assertIn('落とせませんでした', result.stdout)
        self.assertTrue((self.code / 'lambda/test-region/fn-node/src/index.mjs').exists())
        self.assertFalse((self.code / 'lambda/test-region/fn-py/_manifest.json').exists())
        self.assert_nothing_leaked(result)

    def test_an_expired_pull_key_stops_and_the_next_run_resumes(self):
        result = self.run_cli('lambda', 'pull', 'fn-node', 'fn-py', FAKE_EXPIRE_AT='fn-py')
        self.assertEqual(result.returncode, 1)
        self.assertIn('切れました', result.stdout)
        self.assertIn('aws-survey lambda pull fn-node fn-py', result.stdout)
        self.assertTrue((self.code / 'lambda/test-region/fn-node/_manifest.json').exists())
        self.assert_nothing_leaked(result)
        self.clear_log()
        result = self.pull('fn-node', 'fn-py')
        self.assertIn('変わっていません', result.stdout)
        self.assertTrue((self.code / 'lambda/test-region/fn-py/_manifest.json').exists())

    def test_an_expired_source_profile_points_at_the_login(self):
        result = self.run_cli('lambda', 'pull', 'fn-py', FAKE_SRC_EXPIRED='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('assume-role', self.ops())
        self.assertIn('aws-login --profile fake-src', result.stdout)

    def test_a_failing_extractor_is_reported(self):
        result = self.run_cli('lambda', 'pull', 'fn-node', FAKE_DOCKER_FAIL='1')
        self.assertEqual(result.returncode, 1)
        self.assertIn('展開のコンテナが失敗しました', result.stdout)
        self.assert_nothing_leaked(result)

    def test_bad_arguments_touch_nothing(self):
        for args in (['pull'], ['pull', '../x'], ['pull', '--all', 'fn-py'], ['pull', 'fn-py', '--region', 'no/where'],
                     ['pull', 'arn:aws:lambda:r:0:function:f'], ['list', 'x'], ['remove', '--with-deps', 'fn-py'],
                     ['remove'], ['frobnicate'], []):
            with self.subTest(args=args):
                result = self.run_cli('lambda', *args)
                self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.calls(), [])


class ListAndRemove(LambdaCase):
    def test_list_reads_the_manifests_only(self):
        self.pull('fn-py', 'fn-node')
        (self.code / 'lambda/test-region/fn-py/src/_manifest.json').write_text('not the tool\'s manifest')
        self.clear_log()
        result = self.run_cli('lambda', 'list')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.calls(), [])
        for rel in ('lambda/test-region/fn-py ', 'lambda/test-region/fn-py/2 ', 'lambda/test-region/fn-node ',
                    'lambda-layers/test-region/shared/3 '):
            self.assertIn(rel, result.stdout)
        self.assertNotIn('/src', result.stdout)
        self.assertNotIn('読めません', result.stdout)

    def test_list_without_anything(self):
        result = self.run_cli('lambda', 'list')
        self.assertEqual(result.returncode, 0)
        self.assertIn('ありません', result.stdout)

    def test_remove_one_function_keeps_the_rest(self):
        self.pull('fn-py', 'fn-node')
        result = self.run_cli('lambda', 'remove', 'fn-py')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.code / 'lambda/test-region/fn-py').exists())
        self.assertTrue((self.code / 'lambda/test-region/fn-node').exists())
        self.assertTrue((self.code / 'lambda-layers/test-region/shared/3').exists())

    def test_remove_touches_no_aws(self):
        self.pull('fn-py')
        self.clear_log()
        self.assertEqual(self.run_cli('lambda', 'remove', 'fn-py').returncode, 0)
        self.assertEqual(self.calls(), [])

    def test_remove_all(self):
        self.pull('fn-py')
        self.clear_log()
        result = self.run_cli('lambda', 'remove', '--all')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.code.exists())
        self.assertEqual(self.calls(), [])

    def test_removing_what_is_not_there_is_not_an_error(self):
        result = self.run_cli('lambda', 'remove', 'fn-py')
        self.assertEqual(result.returncode, 0)
        self.assertIn('ありません', result.stdout)


class Wiring(LambdaCase):
    def test_run_and_pull_build_the_same_image(self):
        self.pull('fn-node')
        [build] = [c['argv'] for c in self.calls('docker') if c['argv'][0] == 'build']
        self.assertEqual(build[build.index('-t') + 1], 'smoke:latest')
        self.assertEqual(build[-3:], ['-f', f'{ROOT}/Dockerfile', f'{ROOT}/container'])
        run = (ROOT / 'libexec/commands/run.sh').read_text()
        self.assertIn('. "$LIBEXEC_DIR/docker.sh"', run)
        self.assertNotIn('build_image() {', run)

    def test_doctor_names_curl_without_failing_on_it(self):
        self.assertIn('✔ curl', self.run_cli('doctor').stdout)
        (self.bin / 'curl').unlink()
        result = self.run_cli('doctor')
        self.assertIn('⚠ curl', result.stdout)
        self.assertIn('lambda pull', result.stdout)

    def test_help_lists_the_lambda_commands(self):
        result = self.run_cli('--help')
        self.assertIn('aws-survey lambda ', result.stdout)
        self.assertIn('共通オプション', result.stdout)
        # pull / list / remove are what `aws-survey lambda` runs for the user; they only show in full
        self.assertNotIn('aws-survey lambda pull', result.stdout)
        self.assertIn('aws-survey lambda pull', self.run_cli('--help', '--all').stdout)


if __name__ == '__main__':
    unittest.main()
