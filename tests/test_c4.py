"""`aws-survey c4` (the report's C4 diagram, Structurizr DSL → PNG) with a fake `docker`.

The survey agent writes out/report/c4/workspace.dsl inside the container; it has no docker and no Java, so the
host renders. The fake docker plays the Structurizr image: it writes PNGs into the output directory the command
names (through the mount it was given) or fails like a DSL error would. Nothing here touches AWS or the key.
"""
import json
import os
from pathlib import Path
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_cli                                  # noqa: E402

FAKE_DOCKER = r'''#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps(["docker"] + argv) + "\n")
if argv[:2] == ["image", "inspect"]:
    sys.exit(0)
if argv[:1] != ["run"]:
    sys.exit(0)
mounts = {}
for i, a in enumerate(argv):
    if a == "-v":
        src, dst = argv[i + 1].split(":")[:2]; mounts[dst] = src
out = argv[argv.index("-output") + 1]
root = mounts["/usr/local/structurizr"]
# 渡された DSL（本体か .new/ の写し）を残す。1 行の tags を分けたかをテストが見る
with open(os.path.join(root, argv[argv.index("-workspace") + 1])) as f:
    open(os.environ["FAKE_LOG"] + ".dsl", "w").write(f.read())
out_host = os.path.join(root, os.path.relpath(out, "/usr/local/structurizr"))
if os.environ.get("FAKE_C4_FAIL"):
    print("12:00:00.000 [main] ERROR com.structurizr.command.ExportCommand -- Unexpected end of DSL content - are one or more closing curly braces missing?", file=sys.stderr)
    sys.exit(1)
os.makedirs(out_host, exist_ok=True)
for key in json.loads(os.environ.get("FAKE_C4_VIEWS", '["landscape", "shop-containers"]')):
    for name in (key + ".png", key + "-key.png"):
        open(os.path.join(out_host, name), "wb").write(b"\x89PNG fake")
        print("12:00:00.000 [main] INFO com.structurizr.command.PlaywrightExporterImpl -- Writing " + os.path.join(out, name))
sys.exit(0)
'''

DSL = 'workspace {\n    model {\n        u = person "u"\n    }\n    views {\n        systemLandscape "landscape" {\n            include *\n            autoLayout lr\n        }\n    }\n}\n'


class C4Case(test_cli.CliCase):
    def setUp(self):
        super().setUp()
        self.add_tool('docker', FAKE_DOCKER)
        self.add_tool('id', '#!/bin/sh\ncase "$1" in -u) echo 501 ;; -g) echo 20 ;; esac\n')
        self.write_environment(setup={'route_decided': '2026-09-08', 'role_created': '2026-09-08', 'readonly_verified': '2026-09-08'})
        self.c4 = self.target / 'out' / 'report' / 'c4'

    def write_dsl(self):
        self.c4.mkdir(parents=True, exist_ok=True)
        (self.c4 / 'workspace.dsl').write_text(DSL)

    def age(self, path, seconds):
        t = time.time() - seconds
        os.utime(path, (t, t))

    def runs(self):
        return [c for c in self.calls() if c[:2] == ['docker', 'run']]


class Render(C4Case):
    def test_without_a_dsl_it_says_so_and_touches_no_docker(self):
        result = self.run_cli('c4')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('C4 図の DSL がありません', result.stderr + result.stdout)
        self.assertEqual(self.runs(), [])

    def test_auto_without_a_dsl_is_silent(self):
        result = self.run_cli('c4', '--auto')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), '')
        self.assertEqual(self.runs(), [])

    def test_renders_with_the_structurizr_image_offline_and_without_the_key(self):
        self.write_dsl()
        result = self.run_cli('c4')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        run = self.runs()[-1]
        image = next(a for a in run if a.startswith('structurizr/structurizr:'))
        self.assertIn('-playwright', image)                                       # the tag that carries Playwright and Chromium
        self.assertIn('--network', run); self.assertEqual(run[run.index('--network') + 1], 'none')
        self.assertIn('--user', run); self.assertEqual(run[run.index('--user') + 1], '501:20')
        mounts = [run[i + 1] for i, a in enumerate(run) if a == '-v']
        self.assertEqual(mounts, [f'{self.c4}:/usr/local/structurizr'])            # only the c4 folder: no key, no out/, no code/
        self.assertFalse(any('.aws-claude' in m for m in mounts))
        self.assertEqual(run[run.index(image) + 1:], ['export', '-workspace', 'workspace.dsl', '-format', 'png', '-output', '/usr/local/structurizr/.new'])
        self.assertEqual(sorted(p.name for p in self.c4.glob('*.png')),
                         ['landscape-key.png', 'landscape.png', 'shop-containers-key.png', 'shop-containers.png'])
        self.assertFalse((self.c4 / '.new').exists())
        self.assertFalse((self.c4 / '_render-error.txt').exists())
        self.assertIn('C4 図を描きました（2 枚', result.stdout)                     # legends are not counted

    def test_one_line_tags_are_split_in_a_copy_before_rendering(self):
        # `... { tags "外部" }` や `{ include * autoLayout lr }` を 1 行に詰めた DSL は Structurizr が拒む。
        # 本体は触らず、.new/ の写しで文の先頭語の前で行を分けて描く（引用符の中の語や `border dashed` は切らない）
        self.write_dsl()
        dsl = DSL.replace('u = person "u"', 'u = person "u" "利用者 tags" { tags "外部" "推測" }\n        s = softwareSystem "s" { tags "外部" }')
        dsl = dsl.replace('            include *\n            autoLayout lr\n        }', '            include *\n            autoLayout lr\n        }\n'
                          '        styles {\n            element "x" { background #ffffff color #cc0000 border dashed }\n'
                          '            relationship "推測" { thickness 2 dashed true }\n        }')
        dsl = dsl.replace('systemLandscape "landscape" {\n            include *\n            autoLayout lr\n        }',
                          'systemLandscape "landscape" { include * autoLayout lr }')
        (self.c4 / 'workspace.dsl').write_text(dsl)
        result = self.run_cli('c4')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        run = self.runs()[-1]
        self.assertEqual(run[run.index('-workspace') + 1], '.new/workspace.dsl')
        self.assertEqual((self.c4 / 'workspace.dsl').read_text(), dsl)               # 本体はそのまま
        self.assertFalse((self.c4 / '.new').exists())                              # 写しは残らない
        self.assertTrue((self.c4 / 'landscape.png').exists())
        copy = (self.log.parent / (self.log.name + '.dsl')).read_text()      # 偽 docker が受け取った写し
        self.assertIn('u = person "u" "利用者 tags" {\n    tags "外部" "推測"\n}\n', copy)
        self.assertIn('s = softwareSystem "s" {\n    tags "外部"\n}\n', copy)
        self.assertIn('systemLandscape "landscape" {\n    include *\n    autoLayout lr\n}\n', copy)
        self.assertIn('element "x" {\n    background #ffffff\n    color #cc0000\n    border dashed\n}\n', copy)
        self.assertIn('relationship "推測" {\n    thickness 2\n    dashed true\n}\n', copy)
        # 見本どおりに書かれていれば、本体をそのまま渡す
        self.write_dsl()
        self.run_cli('c4', '--force')
        self.assertEqual(self.runs()[-1][self.runs()[-1].index('-workspace') + 1], 'workspace.dsl')

    def test_a_dsl_error_leaves_the_cause_for_the_agent_and_keeps_the_old_pngs(self):
        self.write_dsl()
        (self.c4 / 'old.png').write_bytes(b'old')
        self.age(self.c4 / 'old.png', 100)
        result = self.run_cli('c4', FAKE_C4_FAIL='1')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('C4 図を描けませんでした', result.stdout)
        err = (self.c4 / '_render-error.txt').read_text()
        self.assertIn('closing curly braces', err)
        self.assertNotIn('[main]', err)                                            # the log prefix is stripped
        self.assertTrue((self.c4 / 'old.png').exists())
        self.assertFalse((self.c4 / '.new').exists())

    def test_up_to_date_pngs_are_not_redrawn_unless_forced(self):
        self.write_dsl()
        self.age(self.c4 / 'workspace.dsl', 100)
        for name in ('landscape.png', 'landscape-key.png'):
            (self.c4 / name).write_bytes(b'png')
        result = self.run_cli('c4')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('C4 図は最新です（1 枚', result.stdout)
        self.assertEqual(self.runs(), [])
        result = self.run_cli('c4', '--auto')
        self.assertEqual(result.stdout.strip(), '')
        self.assertEqual(self.runs(), [])
        result = self.run_cli('c4', '--force')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.runs()), 1)

    def test_a_rewritten_dsl_is_redrawn_and_removed_views_disappear(self):
        self.write_dsl()
        (self.c4 / 'gone.png').write_bytes(b'png')
        self.age(self.c4 / 'gone.png', 100)                                        # older than the DSL: stale
        result = self.run_cli('c4', '--auto')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.runs()), 1)
        self.assertFalse((self.c4 / 'gone.png').exists())
        self.assertTrue((self.c4 / 'landscape.png').exists())

    def test_a_previous_error_is_retried_even_if_pngs_look_fresh(self):
        self.write_dsl()
        (self.c4 / '_render-error.txt').write_text('old error\n')
        (self.c4 / 'landscape.png').write_bytes(b'png')
        result = self.run_cli('c4', '--auto')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.runs()), 1)
        self.assertFalse((self.c4 / '_render-error.txt').exists())

    def test_nothing_but_pngs_and_the_error_note_are_written(self):
        self.write_dsl()
        before = sorted(str(p.relative_to(self.target)) for p in self.target.rglob('*') if p.is_file())
        self.run_cli('c4')
        after = sorted(str(p.relative_to(self.target)) for p in self.target.rglob('*') if p.is_file())
        self.assertEqual([p for p in after if p not in before],
                         ['out/report/c4/landscape-key.png', 'out/report/c4/landscape.png',
                          'out/report/c4/shop-containers-key.png', 'out/report/c4/shop-containers.png'])
        self.assertEqual((self.c4 / 'workspace.dsl').read_text(), DSL)


class Guidance(C4Case):
    def ready(self):
        self.write_environment(setup={'route_decided': '2026-09-08', 'role_created': '2026-09-08',
                                      'readonly_verified': '2026-09-08', 'scanned': '2026-09-10T10:00+0900'})
        from datetime import datetime, timedelta, timezone
        self.write_keys((datetime.now(timezone.utc) + timedelta(minutes=45)).strftime('%Y-%m-%dT%H:%M:%S+00:00'))

    def test_status_reports_the_diagram_state(self):
        self.ready()
        self.assertNotIn('C4 図', self.run_cli('status').stdout)
        self.write_dsl()
        self.assertIn('C4 図: DSL より古い', self.run_cli('status').stdout)
        self.age(self.c4 / 'workspace.dsl', 100)
        (self.c4 / 'landscape.png').write_bytes(b'png'); (self.c4 / 'landscape-key.png').write_bytes(b'png')
        self.assertIn('C4 図: 1 枚', self.run_cli('status').stdout)
        (self.c4 / '_render-error.txt').write_text('x\n')
        self.assertIn('C4 図: 描けていません', self.run_cli('status').stdout)

    def test_the_guide_offers_c4_only_while_the_pngs_are_stale(self):
        self.ready()
        self.assertNotIn('aws-survey c4', self.run_cli().stdout)
        self.write_dsl()
        self.assertIn('aws-survey c4', self.run_cli().stdout)
        self.age(self.c4 / 'workspace.dsl', 100)
        (self.c4 / 'landscape.png').write_bytes(b'png')
        self.assertNotIn('aws-survey c4', self.run_cli().stdout)

    def test_help_names_c4_among_the_internal_commands(self):
        self.assertNotIn('aws-survey c4', self.run_cli('help').stdout)
        self.assertIn('aws-survey c4', self.run_cli('help', '--all').stdout)


if __name__ == '__main__':
    unittest.main()
