"""`aws-survey ls` (and, later, the interactive `ec2` / `lambda`), checked with a fake `docker` and a fake `aws`.

The listing runs inside the survey image with only the read-only key mounted: no out/, no code/, no
instructions, no terminal. The fake docker plays the container by running the lent inventory script on
the host with the fake `aws`, so what the tests see on screen is what the real script prints. Nothing on
the host side calls `aws`, and nothing is written under the target folder.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_cli                                  # noqa: E402
import test_inventory                            # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

# `run` executes whatever was lent as /x/inventory.sh with the region the container was given; `build` is a no-op.
FAKE_DOCKER = r'''#!/usr/bin/env python3
import json, os, subprocess, sys
argv = sys.argv[1:]
entry = {"tool": "docker", "argv": argv}
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps(entry) + "\n")
if argv[:1] == ["build"]:
    print("sha256:fake"); sys.exit(0)
if argv[:1] != ["run"]:
    sys.exit(0)
if os.environ.get("FAKE_DOCKER_FAIL") == "1":
    sys.stderr.write("docker: Error response from daemon: boom\n"); sys.exit(125)
mounts, env = {}, dict(os.environ)
for i, a in enumerate(argv):
    if a == "-v":
        parts = argv[i + 1].split(":"); mounts[parts[1]] = parts[0]
    if a == "-e" and "=" in argv[i + 1]:
        k, v = argv[i + 1].split("=", 1); env[k] = v
env["FAKE_IN_CONTAINER"] = "1"
cmd = argv[argv.index("bash"):]
cmd[1] = mounts[cmd[1]]
sys.exit(subprocess.run(cmd, env=env).returncode)
'''


class ResourcesCase(test_cli.CliCase):
    def setUp(self):
        super().setUp()
        self.add_tool('docker', FAKE_DOCKER)
        self.add_tool('aws', test_inventory.FAKE_AWS)
        self.state_file = self.base / 'state.json'
        self.set_state(test_inventory.default_state())
        for tool in ('find', 'sleep'):
            found = subprocess.run(['sh', '-c', f'command -v {tool}'], capture_output=True, text=True).stdout.strip()
            if found and not (self.bin / tool).exists():
                (self.bin / tool).symlink_to(found)

    def set_state(self, state):
        self.state_file.write_text(json.dumps(state))

    def ready(self, expiration='2099-01-01T00:00:00+00:00'):
        """Stage 5: environment verified and a valid key on disk."""
        self.write_environment(setup={'route_decided': '2026-09-08', 'role_created': '2026-09-08',
                                      'readonly_verified': '2026-09-08'})
        self.write_keys(expiration=expiration)

    def run_cli(self, *args, **extra):
        extra.setdefault('FAKE_STATE', str(self.state_file))
        return super().run_cli(*args, **extra)

    def docker_runs(self):
        return [c['argv'] for c in self.calls() if c.get('tool') == 'docker' and c['argv'][:1] == ['run']]

    def aws_calls(self):
        return [c for c in self.calls() if 'service' in c]


class Ls(ResourcesCase):
    def test_lists_from_inside_the_container_with_only_the_key(self):
        self.ready()
        result = self.run_cli('ls')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        (run,) = self.docker_runs()
        self.assertIn('--rm', run)
        self.assertNotIn('-it', run)
        self.assertNotIn('-t', run)
        self.assertIn(f'{self.key_dir()}:/home/node/.aws-claude:ro', run)
        self.assertIn(f'{ROOT}/libexec/inventory.sh:/x/inventory.sh:ro', run)
        mounts = [run[i + 1] for i, a in enumerate(run) if a == '-v']
        self.assertEqual(len(mounts), 2, mounts)
        self.assertNotIn('/home/node/aws-survey/out', ' '.join(run))
        self.assertIn('AWS_DEFAULT_REGION=test-region', run)
        self.assertEqual(run[-4:], ['bash', '/x/inventory.sh', '--region', 'test-region'])
        self.assertIn('EC2 インスタンス（2）', result.stdout)
        self.assertIn('i-0aaa  web1', result.stdout)
        self.assertIn('ssm Online', result.stdout)
        self.assertIn('Lambda 関数（2）', result.stdout)
        self.assertIn('fn-py', result.stdout)
        self.assertIn('S3 バケット（1）', result.stdout)
        self.assertIn('ロードバランサー（0）', result.stdout)
        self.assertIn('aws-survey ec2', result.stdout)
        self.assertIn('aws-survey lambda', result.stdout)
        self.assertIn('aws-survey run', result.stdout)

    def test_every_aws_call_happens_inside_the_container_and_nothing_is_written(self):
        self.ready()
        self.run_cli('ls')
        calls = self.aws_calls()
        self.assertTrue(calls)
        self.assertTrue(all(c['in_container'] == '1' for c in calls), calls)
        self.assertEqual(sorted(p.name for p in self.target.iterdir()), ['environment.json'])

    def test_a_service_the_key_cannot_read_is_marked_not_dropped(self):
        self.ready()
        result = self.run_cli('ls', FAKE_DENY='rds,ssm')
        self.assertEqual(result.returncode, 1)
        self.assertIn('RDS', result.stdout)
        self.assertIn('読めません', result.stdout)
        self.assertIn('SSM の管理下かどうかは読めませんでした', result.stdout)
        self.assertIn('ECS クラスター（1）', result.stdout)

    def test_service_filter_and_json_passthrough(self):
        self.ready()
        result = self.run_cli('ls', 'ec2', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        (run,) = self.docker_runs()
        self.assertEqual(run[-5:], ['bash', '/x/inventory.sh', '--region', 'test-region', 'ec2'])
        lines = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
        self.assertEqual([line['service'] for line in lines if line['kind'] == 'service'], ['ec2'])
        self.assertEqual([line['id'] for line in lines if line['kind'] == 'item'], ['i-0aaa', 'i-0bbb'])

    def test_stops_before_docker_without_a_usable_key(self):
        self.write_environment(setup={'route_decided': '2026-09-08', 'role_created': '2026-09-08',
                                      'readonly_verified': '2026-09-08'})
        result = self.run_cli('ls')
        self.assertEqual(result.returncode, 1)
        self.assertIn('一時キーがありません', result.stdout)
        self.assertIn('次に打つコマンド', result.stdout)
        self.write_keys(expiration='2020-01-01T00:00:00+00:00')
        result = self.run_cli('ls')
        self.assertEqual(result.returncode, 1)
        self.assertIn('期限切れ', result.stdout)
        self.assertIn('aws-survey credentials', result.stdout)
        self.write_environment(setup={'route_decided': '2026-09-08', 'role_created': '2026-09-08'})
        self.write_keys(expiration='2099-01-01T00:00:00+00:00')
        result = self.run_cli('ls')
        self.assertEqual(result.returncode, 1)
        self.assertIn('まだ確かめていません', result.stdout)
        self.assertEqual(self.docker_runs(), [])
        self.assertEqual(self.aws_calls(), [])

    def test_a_container_that_fails_to_start_is_reported(self):
        self.ready()
        result = self.run_cli('ls', FAKE_DOCKER_FAIL='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('列挙できませんでした', result.stdout)


if __name__ == '__main__':
    unittest.main()
