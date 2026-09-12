"""`libexec/inventory.sh`, the resource listing that runs inside the survey image, checked on the host with a fake `aws`.

The script prints one JSON object per line: a `service` line for every service it was asked about
(ok, denied or error - a service the key cannot read is marked, never dropped) followed by its `item`
lines in id order. Services come in a fixed order, so two runs against the same account give the same
text. It reads with whatever `aws` finds in the environment and writes nothing.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'libexec/inventory.sh'
TOOLS = ['bash', 'sh', 'env', 'jq', 'sed', 'grep', 'head', 'tail', 'cat', 'tr', 'printf', 'echo', 'test', 'python3', 'date']

# Answers already shaped the way the real --query would shape them. FAKE_DENY / FAKE_ERROR name the
# services (the word after the global options) that fail with AccessDenied / a connection error.
FAKE_AWS = r'''#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
words = []
skip = False
for a in argv:
    if skip: skip = False; continue
    if a in ("--region", "--output", "--query", "--profile"): skip = True; continue
    words.append(a)
service, op = words[0], words[1]
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps({"service": service, "op": op, "argv": argv,
                        "in_container": os.environ.get("FAKE_IN_CONTAINER", "")}) + "\n")
if not os.environ.get("FAKE_IN_CONTAINER"):
    # the host side (ssh setup / lambda pull) may check who it is; anything past that is out of scope here
    if op == "get-caller-identity":
        print("arn:aws:iam::000000000000:user/fake"); sys.exit(0)
    sys.stderr.write("unexpected host-side call " + " ".join(argv) + "\n"); sys.exit(2)
if service in os.environ.get("FAKE_DENY", "").split(","):
    sys.stderr.write("\nAn error occurred (AccessDeniedException) when calling the %s operation: User is not authorized\n" % op)
    sys.exit(254)
if service in os.environ.get("FAKE_ERROR", "").split(","):
    sys.stderr.write("Could not connect to the endpoint URL: \"https://%s.example/\"\n" % service)
    sys.exit(255)
state = json.load(open(os.environ["FAKE_STATE"]))
answers = {
    ("ec2", "describe-instances"): state.get("instances", []),
    ("ssm", "describe-instance-information"): state.get("ssm", []),
    ("lambda", "list-functions"): state.get("functions", []),
    ("ec2", "describe-vpcs"): state.get("vpcs", []),
    ("s3api", "list-buckets"): state.get("buckets", []),
    ("rds", "describe-db-instances"): state.get("db_instances", []),
    ("rds", "describe-db-clusters"): state.get("db_clusters", []),
    ("ecs", "list-clusters"): state.get("cluster_arns", []),
    ("ecs", "describe-clusters"): state.get("clusters", []),
    ("elbv2", "describe-load-balancers"): state.get("load_balancers", []),
    ("cloudfront", "list-distributions"): state.get("distributions"),
    ("ce", "get-cost-and-usage"): state.get("costs", []),
}
if (service, op) not in answers:
    sys.stderr.write("unexpected call " + " ".join(argv) + "\n"); sys.exit(2)
print(json.dumps(answers[(service, op)]))
'''


def default_state():
    return {
        'instances': [
            {'id': 'i-0bbbbbbbbbbbbbbbb', 'name': 'web2', 'state': 'stopped', 'extra': {'type': 't3.micro', 'az': 'test-region-a', 'launched': '2026-01-02T00:00:00+00:00'}},
            {'id': 'i-0aaaaaaaaaaaaaaaa', 'name': 'web1', 'state': 'running', 'extra': {'type': 't3.small', 'az': 'test-region-a', 'launched': '2026-01-01T00:00:00+00:00'}},
        ],
        'ssm': [{'id': 'i-0aaaaaaaaaaaaaaaa', 'ping': 'Online'}],
        'functions': [
            {'id': 'fn-node', 'name': 'fn-node', 'state': 'nodejs20.x', 'extra': {'handler': 'index.handler', 'memory': 128, 'modified': '2026-01-01T00:00:00.000+0000', 'size': 1024}},
            {'id': 'fn-py', 'name': 'fn-py', 'state': 'python3.12', 'extra': {'handler': 'app.handler', 'memory': 256, 'modified': '2026-01-02T00:00:00.000+0000', 'size': 2048}},
        ],
        'vpcs': [{'id': 'vpc-0fake', 'name': None, 'state': 'available', 'extra': {'cidr': '10.0.0.0/16', 'default': False}}],
        'buckets': [{'id': 'logs-bucket', 'name': 'logs-bucket', 'extra': {'created': '2025-12-31T00:00:00+00:00'}}],
        'db_instances': [{'id': 'db1', 'name': 'db1', 'state': 'available', 'extra': {'engine': 'postgres', 'version': '16.3', 'class': 'db.t4g.micro', 'cluster': None}}],
        'db_clusters': [],
        'cluster_arns': ['arn:aws:ecs:test-region:000000000000:cluster/app'],
        'clusters': [{'id': 'app', 'name': 'app', 'state': 'ACTIVE', 'extra': {'services': 2, 'tasks': 3, 'instances': 0}}],
        'load_balancers': [],
        'distributions': None,       # what list-distributions gives with no distribution: the key is absent
        'costs': [
            {'id': 'Amazon Elastic Compute Cloud - Compute', 'name': 'Amazon Elastic Compute Cloud - Compute', 'extra': {'amount': '12.3456', 'unit': 'USD'}},
            {'id': 'AWS Lambda', 'name': 'AWS Lambda', 'extra': {'amount': '0', 'unit': 'USD'}},
            {'id': 'Amazon Relational Database Service', 'name': 'Amazon Relational Database Service', 'extra': {'amount': '30.1', 'unit': 'USD'}},
        ],
    }


class InventoryCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.bin = self.base / 'bin'
        self.bin.mkdir()
        for tool in TOOLS:
            found = shutil.which(tool)
            if found:
                (self.bin / tool).symlink_to(found)
        aws = self.bin / 'aws'
        aws.write_text(FAKE_AWS)
        aws.chmod(0o755)
        self.log = self.base / 'aws.jsonl'
        self.state_file = self.base / 'state.json'
        self.set_state(default_state())

    def tearDown(self):
        self.temp.cleanup()

    def set_state(self, state):
        self.state_file.write_text(json.dumps(state))

    def run_inventory(self, *args, **extra):
        env = {'PATH': str(self.bin), 'HOME': str(self.base), 'FAKE_LOG': str(self.log), 'FAKE_STATE': str(self.state_file),
               'LANG': os.environ.get('LANG', 'C.UTF-8'), 'FAKE_IN_CONTAINER': '1'}
        env.update(extra)
        return subprocess.run(['bash', str(SCRIPT), '--region', 'test-region', *args], env=env, capture_output=True, text=True)

    def lines(self, result):
        self.assertTrue(result.stdout.strip(), result.stderr)
        return [json.loads(line) for line in result.stdout.splitlines()]

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []


class Output(InventoryCase):
    def test_every_line_is_json_and_services_come_in_a_fixed_order(self):
        result = self.run_inventory()
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = self.lines(result)
        self.assertTrue(all(line['kind'] in ('service', 'item') for line in lines))
        services = [line['service'] for line in lines if line['kind'] == 'service']
        self.assertEqual(services, ['ec2', 'lambda', 'vpc', 's3', 'rds', 'ecs', 'elb', 'cloudfront'])
        self.assertTrue(all(line['status'] == 'ok' for line in lines if line['kind'] == 'service'))

    def test_items_follow_their_service_line_in_id_order(self):
        lines = self.lines(self.run_inventory())
        ec2 = [line for line in lines if line['service'] == 'ec2']
        self.assertEqual(ec2[0]['kind'], 'service')
        self.assertEqual(ec2[0]['count'], 2)
        self.assertEqual([line['id'] for line in ec2[1:]], ['i-0aaaaaaaaaaaaaaaa', 'i-0bbbbbbbbbbbbbbbb'])
        self.assertEqual(ec2[1]['name'], 'web1')
        self.assertEqual(ec2[1]['state'], 'running')
        self.assertEqual(ec2[1]['region'], 'test-region')
        self.assertEqual(ec2[1]['extra']['type'], 't3.small')

    def test_ssm_status_is_folded_into_the_ec2_items(self):
        lines = self.lines(self.run_inventory('ec2'))
        by_id = {line['id']: line for line in lines if line['kind'] == 'item'}
        self.assertEqual(by_id['i-0aaaaaaaaaaaaaaaa']['extra']['ssm'], 'Online')
        self.assertEqual(by_id['i-0bbbbbbbbbbbbbbbb']['extra']['ssm'], 'なし')
        self.assertEqual([line['service'] for line in lines if line['kind'] == 'service'], ['ec2'])

    def test_a_denied_service_keeps_its_line_and_the_rest_still_comes(self):
        result = self.run_inventory(FAKE_DENY='rds')
        self.assertEqual(result.returncode, 1)
        lines = self.lines(result)
        services = {line['service']: line for line in lines if line['kind'] == 'service'}
        self.assertEqual(services['rds']['status'], 'denied')
        self.assertIn('AccessDeniedException', services['rds']['error'])
        self.assertNotIn('count', services['rds'])
        self.assertEqual(services['ecs']['status'], 'ok')
        self.assertEqual([line['service'] for line in lines if line['kind'] == 'service'],
                         ['ec2', 'lambda', 'vpc', 's3', 'rds', 'ecs', 'elb', 'cloudfront'])

    def test_an_unreachable_service_is_an_error_not_a_denial(self):
        lines = self.lines(self.run_inventory('vpc', FAKE_ERROR='ec2'))
        self.assertEqual(lines, [{'kind': 'service', 'service': 'vpc', 'region': 'test-region', 'status': 'error',
                                  'error': 'Could not connect to the endpoint URL: "https://ec2.example/"'}])

    def test_ec2_items_survive_a_denied_ssm(self):
        lines = self.lines(self.run_inventory('ec2', FAKE_DENY='ssm'))
        self.assertEqual([(line['kind'], line['service'], line.get('status')) for line in lines][:2],
                         [('service', 'ssm', 'denied'), ('service', 'ec2', 'ok')])
        self.assertEqual({line['extra']['ssm'] for line in lines if line['kind'] == 'item'}, {'?'})

    def test_global_services_and_empty_answers(self):
        lines = self.lines(self.run_inventory('s3', 'elb', 'cloudfront'))
        s3 = [line for line in lines if line['service'] == 's3']
        self.assertEqual(s3[0]['region'], 'global')
        self.assertEqual(s3[1]['id'], 'logs-bucket')
        self.assertEqual(s3[1]['state'], '')
        elb = [line for line in lines if line['service'] == 'elb']
        self.assertEqual(elb, [{'kind': 'service', 'service': 'elb', 'region': 'test-region', 'status': 'ok', 'count': 0}])
        cf = [line for line in lines if line['service'] == 'cloudfront']
        self.assertEqual(cf[0]['count'], 0)
        self.assertEqual(cf[0]['region'], 'global')

    def test_rds_lists_clusters_after_instances_and_ecs_describes_only_what_it_listed(self):
        state = default_state()
        state['db_clusters'] = [{'id': 'aurora', 'name': 'aurora', 'state': 'available', 'extra': {'engine': 'aurora-postgresql', 'version': '16'}}]
        self.set_state(state)
        lines = self.lines(self.run_inventory('rds', 'ecs'))
        rds = [line for line in lines if line['service'] == 'rds' and line['kind'] == 'item']
        self.assertEqual([(line['id'], line['extra'].get('kind')) for line in rds], [('aurora', 'cluster'), ('db1', None)])
        describe = [c for c in self.calls() if c['op'] == 'describe-clusters']
        self.assertEqual(describe[0]['argv'][describe[0]['argv'].index('--clusters') + 1], state['cluster_arns'][0])
        state['cluster_arns'] = []
        self.set_state(state)
        self.log.unlink()
        lines = self.lines(self.run_inventory('ecs'))
        self.assertEqual(lines[0]['count'], 0)
        self.assertEqual([c['op'] for c in self.calls()], ['list-clusters'])

    def test_every_call_names_the_region_and_asks_for_json(self):
        self.run_inventory()
        for call in self.calls():
            self.assertIn('--output', call['argv'])
            self.assertEqual(call['argv'][call['argv'].index('--output') + 1], 'json')
            if call['service'] not in ('s3api', 'cloudfront'):
                self.assertEqual(call['argv'][call['argv'].index('--region') + 1], 'test-region', call)

    def test_cost_is_only_listed_by_name_and_drops_services_billed_nothing(self):
        lines = self.lines(self.run_inventory())
        self.assertNotIn('cost', [l['service'] for l in lines])          # ls never asks Cost Explorer
        self.assertFalse(any(c['service'] == 'ce' for c in self.calls()))
        lines = self.lines(self.run_inventory('cost'))
        self.assertEqual(lines[0], {'kind': 'service', 'service': 'cost', 'region': 'global', 'status': 'ok', 'count': 2})
        self.assertEqual([l['id'] for l in lines[1:]], ['Amazon Elastic Compute Cloud - Compute', 'Amazon Relational Database Service'])
        self.assertEqual(lines[1]['extra'], {'amount': '12.3456', 'unit': 'USD'})
        call = next(c for c in self.calls() if c['service'] == 'ce')
        self.assertIn('us-east-1', call['argv'])                          # Cost Explorer lives in us-east-1 only
        period = next(a for a in call['argv'] if a.startswith('Start='))
        self.assertRegex(period, r'^Start=\d{4}-\d{2}-01,End=\d{4}-\d{2}-01$')
        self.assertIn('Key=SERVICE', ' '.join(call['argv']))

    def test_cost_unavailable_is_an_error_line_not_a_crash(self):
        lines = self.lines(self.run_inventory('cost', FAKE_ERROR='ce'))
        self.assertEqual(lines[0]['status'], 'error')
        self.assertEqual(len(lines), 1)

    def test_unknown_service_and_missing_region_are_usage_errors(self):
        self.assertEqual(self.run_inventory('iam').returncode, 2)
        env = {'PATH': str(self.bin), 'FAKE_LOG': str(self.log), 'FAKE_STATE': str(self.state_file), 'FAKE_IN_CONTAINER': '1'}
        self.assertEqual(subprocess.run(['bash', str(SCRIPT)], env=env, capture_output=True, text=True).returncode, 2)
        self.assertEqual(self.calls(), [])


class Script(unittest.TestCase):
    """The file is lent to the survey image; it must read like something that belongs there."""

    def test_syntax(self):
        subprocess.run(['bash', '-n', str(SCRIPT)], check=True)

    def test_names_nothing_about_the_host(self):
        text = SCRIPT.read_text()
        for word in ('libexec', 'environment.json', '.agents', 'AWS_SURVEY'):
            self.assertNotIn(word, text)


if __name__ == '__main__':
    unittest.main()
