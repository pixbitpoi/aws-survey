"""`aws-survey ssh setup <target>`, `ssh list`, `ssh rotate` and `ssh remove`, checked with a fake `aws` (no AWS, no EC2).

The fake logs every call and answers the handful of APIs setup uses. Here we check the call
order and arguments (source profile, SSM Online gate, RunShellScript with the assembled script,
tag after success), the fallback to gzip + base64 when SendCommand refuses the size, what is
recorded in environment.json, and the config / known_hosts written for the container. Nothing
is recorded when the install fails. For rotate: every host gets the new key through the same install
script, the key is swapped only after all of them succeeded, and a failure keeps the old key and the
records. For remove: the removal script runs first, the tag and the records go only after it reports
a clean host.
"""
import base64
import gzip
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
TOOLS = ['bash', 'sh', 'env', 'jq', 'sed', 'grep', 'awk', 'cut', 'head', 'tail', 'cat', 'wc', 'tr',
         'dirname', 'basename', 'readlink', 'mkdir', 'chmod', 'cp', 'mv', 'rm', 'stat', 'date',
         'mktemp', 'python3', 'printf', 'echo', 'test', 'touch', 'uname', 'gzip', 'base64', 'fold', 'sleep', 'ssh-keygen']
FAKE_PUBKEY = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeFakeFakeFakeFakeFakeFakeFakeFakeFakeFak smoke-comment'
INSTANCE = 'i-0123456789abcdef0'
HOSTKEYS = ['HOSTKEY ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHostHostHostHostHostHostHostHostHostHostHos',
            'HOSTKEY ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBHost']

FAKE_AWS = r'''#!/usr/bin/env python3
"""Fake aws for ssh setup. Knobs (environment):
  FAKE_PING        PingStatus of the instance (default Online; "none" = not managed)
  FAKE_REJECT_PLAIN  1: the first SendCommand whose script is not packed fails with MaxDocumentSizeExceeded
  FAKE_RUN_STATUS  final Status of the invocation (default Success)
  FAKE_NO_HOSTKEY  1: the output has no HOSTKEY lines
  FAKE_INSTANCES   JSON list of {id,name,state} to answer describe-instances (default one running web1)
  FAKE_FAIL_INSTANCE  instance id whose RunCommand ends with Status Failed
  FAKE_REMOVE_LEFTOVER  1: the removal script reports a leftover (non-zero, no "REMOVED clean")
  FAKE_DELETE_TAG_FAIL  1: delete-tags fails
  FAKE_POLICY_MISSING  1: iam get-policy answers NoSuchEntity
  FAKE_DELETE_POLICY_FAIL  1: iam delete-policy answers DeleteConflict
"""
import json, os, sys
argv = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps(["aws"] + argv) + "\n")
def opt(name, default=None):
    return argv[argv.index(name) + 1] if name in argv else default
instances = json.loads(os.environ.get("FAKE_INSTANCES") or
                       '[{"id": "i-0123456789abcdef0", "name": "web1", "state": "running"}]')
if "get-caller-identity" in argv:
    print("arn:aws:iam::000000000000:user/fake"); sys.exit(0)
if argv[0] == "iam":
    op = argv[1]
    if op == "get-policy":
        if os.environ.get("FAKE_POLICY_MISSING") == "1":
            sys.stderr.write("An error occurred (NoSuchEntity) when calling the GetPolicy operation\n"); sys.exit(254)
        print(opt("--policy-arn")); sys.exit(0)
    if op == "list-policy-versions":
        print("v3\tv2"); sys.exit(0)
    if op == "delete-policy" and os.environ.get("FAKE_DELETE_POLICY_FAIL") == "1":
        sys.stderr.write("An error occurred (DeleteConflict) when calling the DeletePolicy operation\n"); sys.exit(254)
    print("{}"); sys.exit(0)
if argv[:2] == ["ec2", "describe-instances"] or "describe-instances" in argv:
    found = instances
    if "--instance-ids" in argv:
        wanted = [a for a in argv[argv.index("--instance-ids") + 1:] if not a.startswith("--")]
        found = [i for i in instances if i["id"] in wanted]
        if not found:
            sys.stderr.write("An error occurred (InvalidInstanceID.NotFound)\n"); sys.exit(254)
    elif "--filters" in argv:
        name = [a for a in argv if a.startswith("Name=tag:Name,Values=")][0].split("=", 2)[2]
        found = [i for i in instances if i.get("name") == name]
    if "diag:ssh" in " ".join(argv):
        print(json.dumps([{"id": i["id"], "state": i["state"], "tag": i.get("tag")} for i in found]))
    else:
        print(json.dumps([{"id": i["id"], "state": i["state"], "name": i.get("name"), "arch": "x86_64", "image": "ami-0"} for i in found]))
    sys.exit(0)
if "describe-instance-information" in argv:
    ping = os.environ.get("FAKE_PING", "Online")
    if "InstanceInformationList[0]" in " ".join(argv):
        print("null" if ping == "none" else json.dumps({"ping": ping, "platform": "Amazon Linux", "version": "2023", "agent": "3.3"}))
    else:
        print("[]" if ping == "none" else json.dumps([{"id": i["id"], "ping": ping} for i in instances]))
    sys.exit(0)
if "send-command" in argv:
    req = json.loads(opt("--cli-input-json"))
    script = req["Parameters"]["commands"][0]
    packed = "__DIAG_B64__" in script
    d = os.environ["FAKE_SCRIPTS"]
    n = len([f for f in os.listdir(d) if f.endswith(".sh")])
    with open(os.path.join(d, f"{n}-{'packed' if packed else 'plain'}.sh"), "w") as f:
        f.write(script)
    with open(os.path.join(d, f"{n}.request.json"), "w") as f:
        json.dump(req, f)
    if os.environ.get("FAKE_REJECT_PLAIN") == "1" and not packed:
        sys.stderr.write("An error occurred (MaxDocumentSizeExceeded) when calling the SendCommand operation: "
                         "The total size of your parameter(s) and document exceeds the 97KB limit.\n")
        sys.exit(254)
    print(json.dumps({"Command": {"CommandId": "cmd-fake-" + str(n), "Status": "Pending"}})); sys.exit(0)
if "get-command-invocation" in argv:
    n = opt("--command-id").rsplit("-", 1)[1]
    with open(os.path.join(os.environ["FAKE_SCRIPTS"], f"{n}.request.json")) as f:
        req = json.load(f)
    status = os.environ.get("FAKE_RUN_STATUS", "Success")
    if req["InstanceIds"][0] == os.environ.get("FAKE_FAIL_INSTANCE"):
        status = "Failed"
    if req["Comment"] == "diag remove":
        if os.environ.get("FAKE_REMOVE_LEFTOVER") == "1":
            status, out = "Failed", "[diag] removed user diag\nREMOVED leftover /etc/diag\n"
        else:
            out = "[diag] removed the sshd configuration for diag\n[diag] removed user diag\nREMOVED clean\n"
        print(json.dumps({"Status": status, "StandardOutputContent": out, "StandardErrorContent": ""})); sys.exit(0)
    out = "[diag] created user diag\n[diag] self check passed (uptime allowed, bash refused)\n"
    if os.environ.get("FAKE_NO_HOSTKEY") != "1":
        out += "\n".join(__HOSTKEYS__) + "\n"
    print(json.dumps({"Status": status, "StandardOutputContent": out,
                      "StandardErrorContent": "" if status == "Success" else "[diag] error: sshd -t rejected the configuration"}))
    sys.exit(0)
if "create-tags" in argv:
    if os.environ.get("FAKE_TAG_FAIL") == "1":
        sys.stderr.write("An error occurred (UnauthorizedOperation)\n"); sys.exit(254)
    print("{}"); sys.exit(0)
if "delete-tags" in argv:
    if os.environ.get("FAKE_DELETE_TAG_FAIL") == "1":
        sys.stderr.write("An error occurred (UnauthorizedOperation)\n"); sys.exit(254)
    print("{}"); sys.exit(0)
print("{}")
'''.replace('__HOSTKEYS__', json.dumps(HOSTKEYS))


class SshSetupCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name).resolve()
        self.home = base / 'home'
        self.target = base / 'target'
        self.bin = base / 'bin'
        self.scripts = base / 'scripts'
        for d in (self.home, self.target, self.bin, self.scripts):
            d.mkdir()
        for tool in TOOLS:
            found = shutil.which(tool)
            if found:
                (self.bin / tool).symlink_to(found)
        (self.bin / 'aws').write_text(FAKE_AWS)
        (self.bin / 'aws').chmod(0o755)
        self.log = base / 'calls.jsonl'
        self.write_environment()
        self.keys = self.home / '.aws-survey/smoke/ssh'
        self.keys.mkdir(parents=True)
        (self.keys / 'id_ed25519').write_text('fake private key\n')
        (self.keys / 'id_ed25519.pub').write_text(FAKE_PUBKEY + '\n')

    def tearDown(self):
        self.temp.cleanup()

    def write_environment(self, ssh=None):
        config = json.loads((ROOT / 'templates/environment.json').read_text())
        config.update(name='smoke', account_id='000000000000', region='test-region')
        config['auth'].update(route='own_role', source_profile='fake-src',
                              principal_arn='arn:aws:iam::000000000000:user/fake',
                              role_name='fake-role', refresh_command=None)
        if ssh is not None:
            config['ssh'] = ssh
        (self.target / 'environment.json').write_text(json.dumps(config))

    def environment(self):
        return json.loads((self.target / 'environment.json').read_text())

    def run_cli(self, *args, **knobs):
        env = {'PATH': str(self.bin), 'HOME': str(self.home), 'LANG': os.environ.get('LANG', 'C.UTF-8'),
               'LC_ALL': os.environ.get('LC_ALL', ''), 'TZ': 'UTC', 'FAKE_LOG': str(self.log),
               'FAKE_SCRIPTS': str(self.scripts), 'AWS_SURVEY_SSM_POLL': '0'}
        env = {k: v for k, v in env.items() if v}
        env.update(knobs)
        return subprocess.run([str(CLI), '--dir', str(self.target), *args], capture_output=True, text=True,
                              errors='replace', env=env)

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def sent_scripts(self):
        return sorted(p.name for p in self.scripts.iterdir() if p.suffix == '.sh')

    def setup(self, *args, **knobs):
        result = self.run_cli('ssh', 'setup', *args, **knobs)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result


class Setup(SshSetupCase):
    def test_call_order_and_arguments(self):
        result = self.setup('web1')
        calls = self.calls()
        ops = [c[c.index(next(a for a in c if a in ('sts', 'ec2', 'ssm', 'iam'))) + 1] for c in calls]
        self.assertEqual(ops, ['get-caller-identity', 'describe-instances', 'describe-instance-information',
                               'send-command', 'get-command-invocation', 'create-tags'])
        for call in calls:
            self.assertIn('fake-src', call, call)          # every call goes through the source profile
        self.assertNotIn('claude-ro', json.dumps(calls))
        describe = calls[1]
        self.assertIn('Name=tag:Name,Values=web1', describe)
        send = calls[3]
        self.assertIn('test-region', send)
        request = json.loads(send[send.index('--cli-input-json') + 1])
        self.assertEqual(request['DocumentName'], 'AWS-RunShellScript')
        self.assertEqual(request['InstanceIds'], [INSTANCE])
        self.assertEqual(request['Parameters']['executionTimeout'], ['600'])
        for word in ('ai', 'agent', 'survey', 'claude'):
            self.assertNotIn(word, request['Comment'].lower())
        tag = calls[5]
        self.assertIn(INSTANCE, tag)
        self.assertIn('Key=diag:ssh,Value=smoke', tag)
        self.assertIn('送った形: plain', result.stdout)
        self.assertIn('aws-survey ssh list', result.stdout)

    def test_sent_script_is_the_print_output(self):
        printed = self.run_cli('ssh', 'setup', '--print', '--log', 'app=/var/log/app/*.log', '--strict')
        self.setup('web1', '--log', 'app=/var/log/app/*.log', '--strict')
        self.assertEqual(self.sent_scripts(), ['0-plain.sh'])
        self.assertEqual((self.scripts / '0-plain.sh').read_text(), printed.stdout)

    def test_environment_json_record(self):
        self.setup(INSTANCE, '--alias', 'app', '--user', 'ops', '--log', 'app=/var/log/app/*.log',
                   '--deny', '/srv/*.pem', '--strict')
        ssh = self.environment()['ssh']
        host = ssh['hosts']['app']
        self.assertEqual(host['instance_id'], INSTANCE)
        self.assertEqual(host['user'], 'ops')
        self.assertEqual(host['logs'], {'app': '/var/log/app/*.log'})
        self.assertEqual(host['deny'], ['/srv/*.pem'])
        self.assertIs(host['strict'], True)
        self.assertRegex(host['installed_at'], r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$')
        self.assertEqual(set(host), {'instance_id', 'user', 'installed_at', 'logs', 'deny', 'strict'})
        self.assertIn('describe-instances', json.dumps(self.calls()[1]))
        self.assertIn(INSTANCE, self.calls()[1])

    def test_config_and_known_hosts(self):
        self.setup('web1')
        config = (self.keys / 'config').read_text()
        self.assertIn('Host web1\n', config)
        self.assertIn(f'    HostName {INSTANCE}\n', config)
        self.assertIn('    User diag\n', config)
        self.assertIn('    IdentityFile ~/.aws-claude/ssh/id_ed25519\n', config)
        self.assertIn('    UserKnownHostsFile ~/.aws-claude/ssh/known_hosts\n', config)
        self.assertIn('    ProxyCommand aws ssm start-session --target %h --document-name AWS-StartSSHSession --parameters portNumber=%p\n', config)
        for line in ('StrictHostKeyChecking yes', 'BatchMode yes', 'RequestTTY no', 'ForwardAgent no', 'IdentitiesOnly yes'):
            self.assertIn(f'    {line}\n', config)
        known = (self.keys / 'known_hosts').read_text().splitlines()
        self.assertEqual(known, [h.replace('HOSTKEY', INSTANCE) for h in HOSTKEYS])
        self.assertEqual(oct((self.keys / 'config').stat().st_mode & 0o777), '0o644')
        self.assertIn('EC2 の中を調べる', self.run_cli('status').stdout)

    def test_second_host_keeps_the_first(self):
        self.setup('web1')
        instances = json.dumps([{'id': INSTANCE, 'name': 'web1', 'state': 'running'},
                                {'id': 'i-0fedcba9876543210', 'name': 'db1', 'state': 'running'}])
        self.setup('db1', FAKE_INSTANCES=instances)
        hosts = self.environment()['ssh']['hosts']
        self.assertEqual(set(hosts), {'web1', 'db1'})
        config = (self.keys / 'config').read_text()
        self.assertIn('Host web1\n', config)
        self.assertIn('Host db1\n', config)
        known = (self.keys / 'known_hosts').read_text()
        self.assertEqual(known.count(INSTANCE), 2)
        self.assertEqual(known.count('i-0fedcba9876543210'), 2)

    def test_rerun_replaces_known_hosts_lines_not_duplicates(self):
        self.setup('web1')
        self.setup('web1')
        known = (self.keys / 'known_hosts').read_text().splitlines()
        self.assertEqual(len(known), 2)
        self.assertEqual(len(self.environment()['ssh']['hosts']), 1)

    def test_alias_defaults_to_instance_id_when_name_is_unusable(self):
        instances = json.dumps([{'id': INSTANCE, 'name': 'web 1 (prod)', 'state': 'running'}])
        self.setup(INSTANCE, FAKE_INSTANCES=instances)
        self.assertEqual(list(self.environment()['ssh']['hosts']), [INSTANCE])

    def test_alias_used_by_another_instance_is_refused(self):
        self.write_environment({'user': 'diag', 'hosts': {'web1': {'instance_id': 'i-0fedcba9876543210'}}})
        result = self.run_cli('ssh', 'setup', 'web1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('--alias', result.stderr)
        self.assertFalse(any('send-command' in c for c in self.calls()))

    def test_packed_fallback_when_plain_is_too_large(self):
        result = self.setup('web1', FAKE_REJECT_PLAIN='1')
        self.assertEqual(self.sent_scripts(), ['0-plain.sh', '1-packed.sh'])
        self.assertIn('MaxDocumentSizeExceeded', result.stdout)
        self.assertIn('送った形: packed', result.stdout)
        packed = (self.scripts / '1-packed.sh').read_text()
        self.assertTrue(packed.startswith('#!/usr/bin/env bash\n'))
        body = re.search(r"<<'__DIAG_B64__'[^\n]*\n(.*?)\n__DIAG_B64__\n", packed, re.S).group(1)
        unpacked = gzip.decompress(base64.b64decode(body)).decode()
        self.assertEqual(unpacked, (self.scripts / '0-plain.sh').read_text())
        self.assertLess(len(packed), len(unpacked) / 2)
        self.assertEqual(subprocess.run(['bash', '-n'], input=packed, capture_output=True, text=True).returncode, 0)
        self.assertIn('web1', self.environment()['ssh']['hosts'])

    def test_pack_can_be_forced(self):
        self.setup('web1', AWS_SURVEY_SSH_PACK='1')
        self.assertEqual(self.sent_scripts(), ['0-packed.sh'])


class NothingRecordedOnFailure(SshSetupCase):
    def assert_nothing_recorded(self, result):
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.environment()['ssh']['hosts'], {})
        self.assertFalse((self.keys / 'config').exists())
        self.assertFalse((self.keys / 'known_hosts').exists())
        self.assertFalse(any('create-tags' in c for c in self.calls()))

    def test_not_managed_by_ssm_guides_and_stops(self):
        result = self.run_cli('ssh', 'setup', 'web1', FAKE_PING='none')
        self.assert_nothing_recorded(result)
        self.assertIn('AmazonSSMManagedInstanceCore', result.stdout)
        self.assertIn('ssh setup --print', result.stdout)
        self.assertFalse(any('send-command' in c for c in self.calls()))

    def test_ping_not_online(self):
        result = self.run_cli('ssh', 'setup', 'web1', FAKE_PING='ConnectionLost')
        self.assert_nothing_recorded(result)
        self.assertIn('ConnectionLost', result.stdout)

    def test_install_failure(self):
        result = self.run_cli('ssh', 'setup', 'web1', FAKE_RUN_STATUS='Failed')
        self.assert_nothing_recorded(result)
        self.assertIn('Failed', result.stdout)
        self.assertIn('sshd -t rejected', result.stdout)
        self.assertIn('何も記録していません', result.stderr)

    def test_missing_hostkey_lines(self):
        result = self.run_cli('ssh', 'setup', 'web1', FAKE_NO_HOSTKEY='1')
        self.assert_nothing_recorded(result)
        self.assertIn('HOSTKEY', result.stderr)

    def test_tag_failure_records_nothing(self):
        result = self.run_cli('ssh', 'setup', 'web1', FAKE_TAG_FAIL='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.environment()['ssh']['hosts'], {})
        self.assertFalse((self.keys / 'config').exists())
        self.assertIn('diag:ssh=smoke', result.stderr)

    def test_stopped_instance(self):
        instances = json.dumps([{'id': INSTANCE, 'name': 'web1', 'state': 'stopped'}])
        result = self.run_cli('ssh', 'setup', 'web1', FAKE_INSTANCES=instances)
        self.assert_nothing_recorded(result)
        self.assertIn('running', result.stderr)

    def test_ambiguous_name(self):
        instances = json.dumps([{'id': INSTANCE, 'name': 'web1', 'state': 'running'},
                                {'id': 'i-0fedcba9876543210', 'name': 'web1', 'state': 'running'}])
        result = self.run_cli('ssh', 'setup', 'web1', FAKE_INSTANCES=instances)
        self.assert_nothing_recorded(result)
        self.assertIn('2 台', result.stderr)

    def test_unknown_target(self):
        result = self.run_cli('ssh', 'setup', 'nope')
        self.assert_nothing_recorded(result)
        self.assertIn('見つかりません', result.stderr)

    def test_no_target(self):
        result = self.run_cli('ssh', 'setup')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('--print', result.stderr)
        self.assertEqual(self.calls(), [])


class List(SshSetupCase):
    def test_empty(self):
        result = self.run_cli('ssh', 'list')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('登録済みホストはありません', result.stdout)
        self.assertIn('aws-survey ssh setup', result.stdout)

    def test_after_setup_shows_live_state(self):
        self.setup('web1')
        self.log.unlink()
        instances = json.dumps([{'id': INSTANCE, 'name': 'web1', 'state': 'running', 'tag': 'smoke'}])
        result = self.run_cli('ssh', 'list', FAKE_INSTANCES=instances)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('✔ web1', result.stdout)
        self.assertIn(INSTANCE, result.stdout)
        self.assertIn('状態 running / タグ diag:ssh=smoke / SSM Online', result.stdout)
        self.assertTrue(all('fake-src' in c for c in self.calls()))

    def test_missing_tag_is_a_warning(self):
        self.setup('web1')
        instances = json.dumps([{'id': INSTANCE, 'name': 'web1', 'state': 'stopped'}])
        result = self.run_cli('ssh', 'list', FAKE_INSTANCES=instances, FAKE_PING='none')
        self.assertIn('⚠ web1', result.stdout)
        self.assertIn('状態 stopped / タグ diag:ssh=（無し） / SSM なし', result.stdout)

    def test_without_aws_lists_from_file(self):
        self.write_environment({'user': 'diag', 'hosts': {'web1': {'instance_id': INSTANCE, 'user': 'diag',
                                                                    'installed_at': '2026-09-10T00:00:00+09:00',
                                                                    'logs': {'app': '/var/log/app/*.log'}, 'deny': [], 'strict': True}}})
        (self.bin / 'aws').unlink()
        result = self.run_cli('ssh', 'list')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('web1', result.stdout)
        self.assertIn('strict', result.stdout)
        self.assertIn('app=/var/log/app/*.log', result.stdout)
        self.assertIn('確かめていません', result.stdout)


SECOND = 'i-0fedcba9876543210'
TWO_INSTANCES = json.dumps([{'id': INSTANCE, 'name': 'web1', 'state': 'running'},
                            {'id': SECOND, 'name': 'db1', 'state': 'running'}])


class Rotate(SshSetupCase):
    def two_hosts(self):
        self.setup('web1')
        self.setup('db1', '--user', 'ops', '--log', 'app=/var/log/app/*.log', '--strict', FAKE_INSTANCES=TWO_INSTANCES)
        self.log.unlink()
        for p in self.scripts.iterdir():
            p.unlink()
        self.old_key = (self.keys / 'id_ed25519').read_bytes()
        self.old_pub = (self.keys / 'id_ed25519.pub').read_text()

    def ops(self):
        return [c[c.index(next(a for a in c if a in ('sts', 'ec2', 'ssm', 'iam'))) + 1] for c in self.calls()]

    def test_reinstalls_every_host_with_the_new_key_then_swaps(self):
        self.two_hosts()
        result = self.run_cli('ssh', 'rotate', FAKE_INSTANCES=TWO_INSTANCES)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.ops(), ['get-caller-identity',
                                      'describe-instance-information', 'send-command', 'get-command-invocation',
                                      'describe-instance-information', 'send-command', 'get-command-invocation'])
        self.assertTrue(all('fake-src' in c for c in self.calls()))
        self.assertFalse(any('create-tags' in c for c in self.calls()))     # the tag is already there
        scripts = self.sent_scripts()
        self.assertEqual(scripts, ['0-plain.sh', '1-plain.sh'])
        new_pub = (self.keys / 'id_ed25519.pub').read_text()
        self.assertNotEqual(new_pub, self.old_pub)
        self.assertNotEqual((self.keys / 'id_ed25519').read_bytes(), self.old_key)
        self.assertFalse((self.keys / 'id_ed25519.next').exists())
        new_key_body = ' '.join(new_pub.split()[:2])
        requests = [json.loads((self.scripts / f'{i}.request.json').read_text()) for i in (0, 1)]
        self.assertEqual([r['InstanceIds'][0] for r in requests], [SECOND, INSTANCE])   # alphabetical: db1, web1
        for i, (user, strict) in enumerate((('ops', 'true'), ('diag', 'false'))):
            script = (self.scripts / f'{i}-plain.sh').read_text()
            self.assertIn(f"DIAG_PUBKEY='{new_key_body} diag'", script)
            self.assertIn(f"DIAG_USER='{user}'", script)
            self.assertIn(f'"strict": {strict}', script)
        self.assertEqual(self.environment()['ssh']['hosts']['db1']['logs'], {'app': '/var/log/app/*.log'})
        self.assertIn('"/var/log/app/*.log"', (self.scripts / '0-plain.sh').read_text())
        hosts = self.environment()['ssh']['hosts']
        self.assertEqual(set(hosts), {'web1', 'db1'})
        for host in hosts.values():
            self.assertRegex(host['installed_at'], r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$')
        known = (self.keys / 'known_hosts').read_text().splitlines()
        self.assertEqual(len(known), 4)
        self.assertIn('Host db1\n', (self.keys / 'config').read_text())
        self.assertIn('鍵を差し替えました', result.stdout)
        self.assertIn('ssh verify', result.stdout)

    def test_failure_keeps_the_old_key_and_names_the_switched_hosts(self):
        self.two_hosts()
        before = self.environment()
        known_before = (self.keys / 'known_hosts').read_text()
        # db1 (first, alphabetically) succeeds, web1 fails: the old key stays, db1 is reported
        result = self.run_cli('ssh', 'rotate', FAKE_INSTANCES=TWO_INSTANCES, FAKE_FAIL_INSTANCE=INSTANCE)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.keys / 'id_ed25519').read_bytes(), self.old_key)
        self.assertEqual((self.keys / 'id_ed25519.pub').read_text(), self.old_pub)
        self.assertFalse((self.keys / 'id_ed25519.next').exists())
        self.assertFalse((self.keys / 'id_ed25519.next.pub').exists())
        self.assertEqual(self.environment(), before)
        self.assertEqual((self.keys / 'known_hosts').read_text(), known_before)
        self.assertIn('新しい鍵になったホスト: db1', result.stdout)
        self.assertIn('web1 で失敗', result.stdout)
        self.assertIn('ssh rotate', result.stdout)
        self.assertIn('ssh setup <host>', result.stdout)

    def test_first_host_failure_changes_nothing(self):
        self.two_hosts()
        result = self.run_cli('ssh', 'rotate', FAKE_INSTANCES=TWO_INSTANCES, FAKE_FAIL_INSTANCE=SECOND)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.sent_scripts(), ['0-plain.sh'])         # stops at the first failure
        self.assertEqual((self.keys / 'id_ed25519.pub').read_text(), self.old_pub)
        self.assertIn('どのホストも変わっていません', result.stdout)

    def test_offline_host_stops_before_sending(self):
        self.two_hosts()
        result = self.run_cli('ssh', 'rotate', FAKE_INSTANCES=TWO_INSTANCES, FAKE_PING='ConnectionLost')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.sent_scripts(), [])
        self.assertEqual((self.keys / 'id_ed25519.pub').read_text(), self.old_pub)
        self.assertIn('ConnectionLost', result.stdout)

    def test_without_hosts(self):
        result = self.run_cli('ssh', 'rotate')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('登録済みホストがありません', result.stderr)
        self.assertEqual(self.calls(), [])


class Remove(SshSetupCase):
    def two_hosts(self):
        self.setup('web1')
        self.setup('db1', '--user', 'ops', FAKE_INSTANCES=TWO_INSTANCES)
        self.log.unlink()
        for p in self.scripts.iterdir():
            p.unlink()

    def ops(self):
        return [c[c.index(next(a for a in c if a in ('sts', 'ec2', 'ssm', 'iam'))) + 1] for c in self.calls()]

    def test_call_order_and_what_is_removed(self):
        self.two_hosts()
        result = self.run_cli('ssh', 'remove', 'db1', FAKE_INSTANCES=TWO_INSTANCES)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.ops(), ['get-caller-identity', 'describe-instances', 'describe-instance-information',
                                      'send-command', 'get-command-invocation', 'delete-tags'])
        self.assertTrue(all('fake-src' in c for c in self.calls()))
        request = json.loads((self.scripts / '0.request.json').read_text())
        self.assertEqual(request['InstanceIds'], [SECOND])
        self.assertEqual(request['Comment'], 'diag remove')
        script = request['Parameters']['commands'][0]
        self.assertIn("DIAG_USER='ops'", script)
        self.assertIn('userdel -r', script)
        untag = self.calls()[5]
        self.assertIn(SECOND, untag)
        self.assertIn('Key=diag:ssh,Value=smoke', untag)
        hosts = self.environment()['ssh']['hosts']
        self.assertEqual(list(hosts), ['web1'])
        config = (self.keys / 'config').read_text()
        self.assertIn('Host web1\n', config)
        self.assertNotIn('db1', config)
        known = (self.keys / 'known_hosts').read_text()
        self.assertNotIn(SECOND, known)
        self.assertEqual(known.count(INSTANCE), 2)
        self.assertTrue((self.keys / 'id_ed25519').exists())
        self.assertIn('REMOVED clean', result.stdout)
        self.assertIn('残っている登録', result.stdout)

    POLICY_ARN = 'arn:aws:iam::000000000000:policy/diag-ssh-smoke'

    def iam_calls(self):
        return [c for c in self.calls() if c[1] == 'iam']

    def test_last_host_points_to_credentials_and_role(self):
        self.setup('web1')
        result = self.run_cli('ssh', 'remove', 'web1')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.environment()['ssh']['hosts'], {})
        self.assertEqual((self.keys / 'known_hosts').read_text(), '')
        self.assertNotIn('Host ', (self.keys / 'config').read_text())
        self.assertIn('登録済みホストが無くなりました', result.stdout)
        self.assertIn('diag-ssh-smoke', result.stdout)
        self.assertIn('aws-survey credentials', result.stdout)
        self.assertIn('aws-survey role --create', result.stdout)
        self.assertTrue((self.keys / 'id_ed25519').exists())
        self.assertNotIn('EC2 の中を調べる', self.run_cli('status').stdout)

    def test_last_host_detaches_and_deletes_the_policy(self):
        """Own role: after the tag and the records, detach, drop the non-default versions, delete."""
        self.setup('web1')
        self.log.unlink()
        result = self.run_cli('ssh', 'remove', 'web1')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        ops = self.ops()
        self.assertEqual(ops[:6], ['get-caller-identity', 'describe-instances', 'describe-instance-information',
                                   'send-command', 'get-command-invocation', 'delete-tags'])
        self.assertEqual(ops[6:], ['get-policy', 'detach-role-policy', 'list-policy-versions',
                                   'delete-policy-version', 'delete-policy-version', 'delete-policy'])
        iam = self.iam_calls()
        self.assertTrue(all('fake-src' in c for c in iam))
        self.assertTrue(all(self.POLICY_ARN in c for c in iam))
        detach = iam[1]
        self.assertEqual(detach[detach.index('--role-name') + 1], 'fake-role')
        versions = [c[c.index('--version-id') + 1] for c in iam if 'delete-policy-version' in c]
        self.assertEqual(versions, ['v3', 'v2'])
        self.assertIn('diag-ssh-smoke を消しました', result.stdout)
        self.assertIn('5/5', result.stdout)

    def test_not_the_last_host_leaves_the_policy(self):
        self.two_hosts()
        self.run_cli('ssh', 'remove', 'db1', FAKE_INSTANCES=TWO_INSTANCES)
        self.assertEqual(self.iam_calls(), [])

    def test_missing_policy_is_skipped(self):
        self.setup('web1')
        result = self.run_cli('ssh', 'remove', 'web1', FAKE_POLICY_MISSING='1')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([c[2] for c in self.iam_calls()], ['get-policy'])
        self.assertIn('片付けるものはありません', result.stdout)

    def test_policy_delete_failure_reports_and_keeps_the_rest_done(self):
        self.setup('web1')
        result = self.run_cli('ssh', 'remove', 'web1', FAKE_DELETE_POLICY_FAIL='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.environment()['ssh']['hosts'], {})          # EC2, tag and records are done
        self.assertIn('DeleteConflict', result.stdout)
        self.assertIn(f'aws iam delete-policy --policy-arn {self.POLICY_ARN}', result.stdout)
        self.assertIn('aws-survey credentials', result.stdout)

    def test_borrowed_role_shows_the_commands_instead(self):
        self.setup('web1')
        env = self.environment()
        env['auth']['route'] = 'existing_role'
        (self.target / 'environment.json').write_text(json.dumps(env))
        self.log.unlink()
        result = self.run_cli('ssh', 'remove', 'web1')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.iam_calls(), [])
        self.assertEqual(self.environment()['ssh']['hosts'], {})
        self.assertIn(f'aws iam detach-role-policy --role-name fake-role --policy-arn {self.POLICY_ARN}', result.stdout)
        self.assertIn(f'aws iam delete-policy --policy-arn {self.POLICY_ARN}', result.stdout)
        self.assertIn('管理者', result.stdout)

    def test_terminated_last_host_still_cleans_the_policy(self):
        self.setup('web1')
        self.log.unlink()
        instances = json.dumps([{'id': INSTANCE, 'name': 'web1', 'state': 'terminated'}])
        result = self.run_cli('ssh', 'remove', 'web1', FAKE_INSTANCES=instances)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([c[2] for c in self.iam_calls()][-1], 'delete-policy')

    def assert_still_registered(self, alias='web1'):
        self.assertIn(alias, self.environment()['ssh']['hosts'])
        self.assertIn(f'Host {alias}\n', (self.keys / 'config').read_text())
        self.assertIn(INSTANCE, (self.keys / 'known_hosts').read_text())
        self.assertFalse(any('delete-tags' in c for c in self.calls()))

    def test_script_failure_keeps_tag_and_records(self):
        self.setup('web1')
        result = self.run_cli('ssh', 'remove', 'web1', FAKE_FAIL_INSTANCE=INSTANCE)
        self.assertNotEqual(result.returncode, 0)
        self.assert_still_registered()
        self.assertIn('Failed', result.stdout)
        self.assertIn('そのままです', result.stderr)

    def test_leftover_keeps_tag_and_records(self):
        self.setup('web1')
        result = self.run_cli('ssh', 'remove', 'web1', FAKE_REMOVE_LEFTOVER='1')
        self.assertNotEqual(result.returncode, 0)
        self.assert_still_registered()
        self.assertIn('/etc/diag', result.stdout)

    def test_untag_failure_keeps_records(self):
        self.setup('web1')
        result = self.run_cli('ssh', 'remove', 'web1', FAKE_DELETE_TAG_FAIL='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('web1', self.environment()['ssh']['hosts'])
        self.assertIn('Host web1\n', (self.keys / 'config').read_text())
        self.assertIn('diag:ssh=smoke', result.stderr)

    def test_offline_host_points_to_print(self):
        self.setup('web1')
        for p in self.scripts.iterdir():
            p.unlink()
        result = self.run_cli('ssh', 'remove', 'web1', FAKE_PING='ConnectionLost')
        self.assertNotEqual(result.returncode, 0)
        self.assert_still_registered()
        self.assertEqual(self.sent_scripts(), [])
        self.assertIn('ssh remove --print web1', result.stdout)

    def test_terminated_instance_only_forgets(self):
        self.setup('web1')
        for p in self.scripts.iterdir():
            p.unlink()
        instances = json.dumps([{'id': INSTANCE, 'name': 'web1', 'state': 'terminated'}])
        result = self.run_cli('ssh', 'remove', 'web1', FAKE_INSTANCES=instances)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.sent_scripts(), [])
        self.assertFalse(any('delete-tags' in c for c in self.calls()))
        self.assertEqual(self.environment()['ssh']['hosts'], {})
        self.assertIn('terminated', result.stdout)

    def test_vanished_instance_only_forgets(self):
        self.setup('web1')
        result = self.run_cli('ssh', 'remove', 'web1', FAKE_INSTANCES='[]')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.environment()['ssh']['hosts'], {})

    def test_unknown_host(self):
        self.setup('web1')
        result = self.run_cli('ssh', 'remove', 'db1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('未登録のホスト', result.stderr)
        self.assertIn('web1', self.environment()['ssh']['hosts'])

    def test_list_shows_key_and_install_dates(self):
        self.setup('web1')
        result = self.run_cli('ssh', 'list')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout, r'鍵 .*id_ed25519  作成 \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}')
        self.assertRegex(result.stdout, r'web1 .*導入 \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}')
        self.assertIn('ssh rotate', result.stdout)
        self.assertIn('ssh remove', result.stdout)


if __name__ == '__main__':
    unittest.main()
