"""`aws-survey clean` (the tidy-up after a survey) with a fake `aws` and a fake `docker`.

clean decides what is left from files alone, then removes it in dependency order: the EC2 gateways (ssh remove),
a policy left without hosts, the survey role (only one this tool created), and the host side (keys, docker
volumes and image, pulled Lambda code). out/ and environment.json stay. A missing permission on the AWS side
must not stop the rest: the item is reported with the commands to run by hand and clean carries on.
"""
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_cli                                  # noqa: E402

FAKE_AWS = r'''#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps(["aws"] + argv) + "\n")
if "get-caller-identity" in argv:
    if os.environ.get("FAKE_SRC_EXPIRED"):
        sys.stderr.write("Token has expired\n"); sys.exit(255)
    print("arn:aws:iam::000000000000:user/fake"); sys.exit(0)
if argv[0] == "iam":
    op = argv[1]
    if op == "get-role":
        if os.environ.get("FAKE_ROLE_MISSING"):
            sys.stderr.write("An error occurred (NoSuchEntity) when calling the GetRole operation\n"); sys.exit(254)
        print("arn:aws:iam::000000000000:role/fake-role"); sys.exit(0)
    if op == "list-attached-role-policies":
        print("arn:aws:iam::aws:policy/ReadOnlyAccess"); sys.exit(0)
    if op == "list-role-policies":
        print("None"); sys.exit(0)
    if op == "delete-role" and os.environ.get("FAKE_DENY_DELETE_ROLE"):
        sys.stderr.write("An error occurred (AccessDenied) when calling the DeleteRole operation: not authorized\n"); sys.exit(254)
    if op == "detach-role-policy" and os.environ.get("FAKE_DENY_DETACH"):
        sys.stderr.write("An error occurred (AccessDenied) when calling the DetachRolePolicy operation\n"); sys.exit(254)
    print("{}"); sys.exit(0)
print("{}")
'''

FAKE_DOCKER = r'''#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps(["docker"] + argv) + "\n")
if argv[:2] == ["volume", "inspect"] or argv[:2] == ["image", "inspect"]:
    sys.exit(0 if os.environ.get("FAKE_DOCKER_LEFT") else 1)
if argv[:1] == ["ps"]:
    if os.environ.get("FAKE_RUNNING") and any(a == "name=^/%s$" % os.environ["FAKE_RUNNING"] for a in argv):
        print("abc123")
    sys.exit(0)
sys.exit(0)
'''


class CleanCase(test_cli.CliCase):
    def setUp(self):
        super().setUp()
        self.add_tool('aws', FAKE_AWS)
        self.add_tool('docker', FAKE_DOCKER)

    def surveyed(self, role=True, scanned=True):
        setup = {'route_decided': '2026-09-08', 'role_created': '2026-09-08' if role else None,
                 'readonly_verified': '2026-09-08', 'scanned': '2026-09-10T10:00+0900' if scanned else None}
        self.write_environment(setup=setup)
        (self.target / 'trust.json').write_text('{}')
        out = self.target / 'out' / 'report'
        out.mkdir(parents=True, exist_ok=True)
        (out / '構成報告.md').write_text('# 構成報告\n')
        self.write_keys('2099-01-01T00:00:00+00:00')

    def ops(self):
        return [c[2] for c in self.calls() if c[0] == 'aws' and c[1] == 'iam']

    def environment(self):
        return json.loads((self.target / 'environment.json').read_text())


class Listing(CleanCase):
    def test_list_names_what_is_left_and_touches_nothing(self):
        self.surveyed()
        (self.target / 'code' / 'lambda' / 'test-region' / 'fn').mkdir(parents=True)
        (self.target / 'code' / 'lambda' / 'test-region' / 'fn' / '_manifest.json').write_text('{}')
        result = self.run_cli('clean', '--list', FAKE_DOCKER_LEFT='1')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('調査用ロール fake-role', result.stdout)
        self.assertIn('一時キーと SSH の鍵', result.stdout)
        self.assertIn('Docker のイメージ smoke:latest', result.stdout)
        self.assertIn('Lambda のコード: 1 関数', result.stdout)
        self.assertIn('out', result.stdout); self.assertIn('消しません', result.stdout)
        self.assertFalse(any(c[0] == 'aws' for c in self.calls()))
        self.assertTrue((self.home / '.aws-survey/smoke/credentials').exists())

    def test_a_borrowed_role_is_named_but_not_offered(self):
        self.surveyed()
        config = self.environment(); config['auth']['route'] = 'existing_role'
        (self.target / 'environment.json').write_text(json.dumps(config))
        result = self.run_cli('clean', '--list')
        self.assertIn('借りたものなので', result.stdout)
        self.assertNotIn('⚠ 調査用ロール', result.stdout)

    def test_nothing_left_says_so(self):
        self.write_environment(setup={'route_decided': '2026-09-08'})
        result = self.run_cli('clean')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('片付けるものはありません', result.stdout)

    def test_off_a_terminal_it_needs_yes(self):
        self.surveyed()
        result = self.run_cli('clean')
        self.assertEqual(result.returncode, 1)
        self.assertIn('clean --yes', result.stdout)
        self.assertEqual(self.ops(), [])
        self.assertTrue((self.home / '.aws-survey/smoke/credentials').exists())


class Removal(CleanCase):
    def test_yes_removes_the_role_then_the_host_side_and_keeps_out(self):
        self.surveyed()
        result = self.run_cli('clean', '--yes', FAKE_DOCKER_LEFT='1')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.ops(), ['get-role', 'list-attached-role-policies', 'detach-role-policy', 'list-role-policies', 'delete-role'])
        self.assertTrue(all('fake-src' in c for c in self.calls() if c[0] == 'aws'))
        detach = next(c for c in self.calls() if 'detach-role-policy' in c)
        self.assertIn('arn:aws:iam::aws:policy/ReadOnlyAccess', detach)
        setup = self.environment()['setup']
        self.assertIsNone(setup['role_created']); self.assertIsNone(setup['readonly_verified'])
        self.assertEqual(setup['route_decided'], '2026-09-08')                     # the route is a decision, not a resource
        self.assertEqual(setup['scanned'], '2026-09-10T10:00+0900')
        self.assertFalse((self.target / 'trust.json').exists())
        self.assertFalse((self.home / '.aws-survey/smoke').exists())
        rm = [c for c in self.calls() if c[:2] == ['docker', 'volume'] and c[2] == 'rm']
        self.assertEqual(sorted(c[3] for c in rm), ['smoke-claude', 'smoke-cli', 'smoke-codex', 'smoke-npm'])
        self.assertIn(['docker', 'rmi', 'smoke:latest'], self.calls())
        self.assertTrue((self.target / 'out' / 'report' / '構成報告.md').exists())
        self.assertIn('後片付けを終えました', result.stdout)

    def test_a_denied_delete_role_is_reported_with_the_commands_and_the_rest_continues(self):
        self.surveyed()
        result = self.run_cli('clean', '--yes', FAKE_DENY_DELETE_ROLE='1')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('AccessDenied', result.stdout)
        self.assertIn('aws iam delete-role --role-name fake-role', result.stdout)
        self.assertEqual(self.environment()['setup']['role_created'], '2026-09-08')   # still there: keep the record
        self.assertFalse((self.home / '.aws-survey/smoke').exists())                  # the host side was still cleaned
        self.assertIn('消せなかったものがあります', result.stdout)
        self.assertIn('調査用ロール fake-role', result.stdout)

    def test_a_denied_detach_stops_short_of_delete_role(self):
        self.surveyed()
        result = self.run_cli('clean', '--yes', FAKE_DENY_DETACH='1')
        self.assertEqual(result.returncode, 1)
        self.assertNotIn('delete-role', self.ops())
        self.assertEqual(self.environment()['setup']['role_created'], '2026-09-08')

    def test_a_role_already_gone_is_skipped_and_the_record_reset(self):
        self.surveyed()
        result = self.run_cli('clean', '--yes', FAKE_ROLE_MISSING='1')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('もうありません', result.stdout)
        self.assertIsNone(self.environment()['setup']['role_created'])

    def test_an_expired_source_login_skips_aws_but_cleans_the_host(self):
        self.surveyed()
        result = self.run_cli('clean', '--yes', FAKE_SRC_EXPIRED='1')
        self.assertEqual(result.returncode, 1)
        self.assertIn('対象アカウント側は片付けられません', result.stdout)
        self.assertEqual(self.ops(), [])
        self.assertFalse((self.home / '.aws-survey/smoke').exists())
        self.assertEqual(self.environment()['setup']['role_created'], '2026-09-08')

    def test_refuses_while_the_survey_container_runs(self):
        self.surveyed()
        result = self.run_cli('clean', '--yes', FAKE_RUNNING='smoke')
        self.assertEqual(result.returncode, 1)
        self.assertIn('動いています', result.stderr + result.stdout)
        self.assertEqual(self.ops(), [])

    def test_on_a_terminal_each_item_is_asked_and_n_keeps_it(self):
        from test_resources import PtyMixin
        self.surveyed()
        self.state_file = self.base / 'state.json'; self.state_file.write_text('{}')
        code, text = PtyMixin.drive(self, ['clean'], [('ロールを消しますか', b'n\r'), ('鍵', b'\r')])
        self.assertEqual(code, 0, text)                                            # keeping something is a choice, not a failure
        self.assertIn('残します', text)
        self.assertIn('残すと答えたもの', text)
        self.assertEqual(self.ops(), [])
        self.assertFalse((self.home / '.aws-survey/smoke').exists())


class Guidance(CleanCase):
    def test_help_and_the_ready_guide_name_clean_after_the_scan(self):
        self.assertIn('aws-survey clean', self.run_cli('help').stdout)
        self.surveyed(scanned=False)
        self.assertNotIn('aws-survey clean', self.run_cli().stdout)
        self.surveyed()
        self.assertIn('aws-survey clean', self.run_cli().stdout)
        self.assertIn('aws-survey clean', self.run_cli('status').stdout)


if __name__ == '__main__':
    unittest.main()
