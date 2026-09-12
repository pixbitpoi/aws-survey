"""Check that the Homebrew layout works: the repository tree maps onto the keg as is.

The formula installs the repository minus the development-only folders (docs/, tests/) into
the keg, moves the real bin/aws-survey into libexec/, and writes bin/aws-survey as a wrapper
pinning AWS_SURVEY_HOME to the opt path. These tests build that layout from the working tree
and drive the CLI through that wrapper, so a runtime reference to a file the formula does not
ship fails here instead of after `brew install`.

Release steps and the file list are in docs/release.md.
"""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

# Kept in step with Formula/aws-survey.rb in pixbitpoi/homebrew-tap.
EXCLUDED = ['docs', 'tests']
# Development entry points for an agent working on this repository. Nobody works inside the
# installed tree - the host agent's job ends at `aws-survey run` - so these stay in the git
# checkout. Shipping AGENTS.md without .agents/ would leave its routing table dangling.
NOT_SHIPPED_FILES = ['AGENTS.md', 'CLAUDE.md']
# Installed by no one: user-facing docs are read on GitHub, and nothing in the shipped tree
# points at them. Homebrew still drops README.md in the keg as a metafile (build.rb's
# `install_metafiles`), so this list drives the copy but not an absence assertion.
NOT_INSTALLED = ['README.md']
# Dot-prefixed entries the formula ships. Ruby's Dir["*"] skips them anyway, and nothing
# hidden is runtime material, so the formula needs no explicit dot glob.
INCLUDED_HIDDEN = []

# `aws` only has to answer what non-interactive init asks; it never reaches AWS.
FAKE_AWS = '''#!/usr/bin/env python3
import sys
argv = sys.argv[1:]
if argv[:2] == ["configure", "list-profiles"]:
    print("fake-src")
elif argv[:2] == ["configure", "get"]:
    print("ap-northeast-1" if argv[2] == "region" else "")
else:
    print("{}")
'''

INIT_ENV = {'AWS_SURVEY_INIT_NAME': 'dist',
            'AWS_SURVEY_INIT_SOURCE_PROFILE': 'fake-src',
            'AWS_SURVEY_INIT_PRINCIPAL_ARN': 'arn:aws:iam::000000000000:user/fake',
            'AWS_SURVEY_INIT_ACCOUNT_ID': '000000000000',
            'AWS_SURVEY_INIT_REGION': 'ap-northeast-1'}

# Runtime references. Scripts and generated files point at these by absolute path.
REQUIRED = ['libexec/aws-survey', 'bin/aws-survey',
            'Dockerfile', 'VERSION',
            'libexec/load-env.sh', 'libexec/ui.sh', 'libexec/keys.sh', 'libexec/menu.sh',
            'libexec/commands/run.sh', 'libexec/launch.sh',
            'libexec/commands/scan.sh', 'libexec/commands/agent.sh',
            'libexec/commands/credentials.sh', 'libexec/commands/role.sh',
            'libexec/commands/verify.sh', 'libexec/commands/doctor.sh',
            'libexec/commands/ssh.sh', 'libexec/commands/lambda.sh', 'libexec/docker.sh',
            'libexec/container.sh', 'libexec/inventory.sh', 'libexec/commands/ls.sh',
            'libexec/commands/ec2.sh',
            'libexec/session-guard.json',
            'libexec/lambda/extract.py',
            'libexec/ec2/gateway.py', 'libexec/ec2/diag-root.py', 'libexec/ec2/diag.conf',
            'libexec/ec2/install.sh.tmpl',
            'container/instructions/survey-agents.md',
            'container/instructions/survey-claude.md',
            'container/method',
            'container/hooks/aws-readonly-guard.sh',
            'container/hooks/codex-guard.py',
            'container/settings.json',
            'container/survey-status',
            'container/survey-ui.sh',
            'templates/environment.json']


class Distribution(unittest.TestCase):
    """The installed tree stands on its own, without the repository around it."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.home = self.base / 'home'
        self.home.mkdir()
        self.target = self.base / 'target'
        self.target.mkdir()
        self.keg = self.base / 'cellar'
        self.keg.mkdir(parents=True)
        # `prefix.install Dir["{*,.agents}"] - EXCLUDED` over what the release tarball holds:
        # tracked files only, visible entries plus INCLUDED_HIDDEN, minus the dev folders.
        # README.md lands in the prefix, which is where Homebrew wants metafiles anyway.
        for entry in sorted(ROOT / name for name in self.tracked_top_level()):
            if entry.name in EXCLUDED or entry.name in NOT_INSTALLED or entry.name in NOT_SHIPPED_FILES:
                continue
            if entry.is_dir():
                shutil.copytree(entry, self.keg / entry.name,
                                ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            else:
                shutil.copy2(entry, self.keg / entry.name)
        self.bin = self.keg / 'bin'
        # The keg carries the version; `opt` is brew's stable link to the current one. brew
        # links bin/ into the prefix, so the real script moves to libexec/ and a wrapper takes
        # its place: aws-survey's own realpath resolves through to the versioned keg, and
        # `init` bakes that path into the target folder's AGENTS.md.
        self.opt = self.base / 'opt'
        self.opt.symlink_to(self.keg)
        (self.bin / 'aws-survey').rename(self.keg / 'libexec/aws-survey')
        self.cli = self.bin / 'aws-survey'
        self.cli.write_text('#!/bin/bash\n'
                            f'AWS_SURVEY_HOME="{self.opt}" '
                            f'exec "{self.opt}/libexec/aws-survey" "$@"\n')
        self.cli.chmod(0o755)
        self.stub = self.base / 'stub'
        self.stub.mkdir()
        fake_aws = self.stub / 'aws'
        fake_aws.write_text(FAKE_AWS)
        fake_aws.chmod(0o755)

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def tracked_top_level():
        """Top-level entries the formula installs: tracked, visible plus INCLUDED_HIDDEN."""
        listing = subprocess.run(['git', 'ls-files'], cwd=str(ROOT), capture_output=True, text=True, check=True)
        names = {line.split('/', 1)[0] for line in listing.stdout.splitlines() if line}
        return sorted(name for name in names
                      if not name.startswith('.') or name in INCLUDED_HIDDEN)

    def run_cli(self, *args, **extra):
        env = {'PATH': f'{self.bin}:{self.stub}:{os.environ["PATH"]}', 'HOME': str(self.home),
               'LANG': os.environ.get('LANG', 'C.UTF-8'), 'TZ': 'UTC', 'NO_COLOR': '1'}
        env = {k: v for k, v in env.items() if v}
        env.update(extra)
        for key in ['AWS_SURVEY_HOME', 'AWS_SURVEY_DIR', 'AWS_DIR', 'ENV_FILE']:
            env.pop(key, None)
        return subprocess.run([str(self.cli), *args], cwd=str(self.target), env=env,
                              capture_output=True, text=True)

    def test_development_folders_are_not_shipped(self):
        for name in EXCLUDED + NOT_SHIPPED_FILES:
            self.assertFalse((self.keg / name).exists(), name)
        # Nothing hidden ships. A dot glob in the formula would drag repository plumbing
        # and the development rules along with it.
        for name in ['.agents', '.git', '.gitignore']:
            self.assertFalse((self.keg / name).exists(), name)
        self.assertFalse((self.keg / 'environment.json').exists())
        self.assertFalse((self.keg / 'out').exists())
        self.assertFalse((self.keg / 'trust.json').exists())

    def test_every_runtime_path_is_shipped(self):
        for name in REQUIRED:
            self.assertTrue((self.keg / name).exists(), f'not in the distribution: {name}')

    def test_wrapper_resolves_home_to_the_opt_path(self):
        result = self.run_cli('status')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f'本体              {self.opt}', result.stdout)
        self.assertIn(f'対象フォルダ: {self.target}', result.stdout)

    def test_help_names_no_file_outside_the_distribution(self):
        for args in [('--help',), ('--help', '--all')]:
            result = self.run_cli(*args)
            self.assertEqual(result.returncode, 0, result.stderr)
            for name in EXCLUDED:
                self.assertNotIn(f'{name}/', result.stdout)

    def test_version_comes_from_the_shipped_file_without_git(self):
        # The tarball has no .git, so the number must not depend on `git describe`.
        result = self.run_cli('--version')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'aws-survey ' + (ROOT / 'VERSION').read_text().strip())

    def test_init_from_the_installed_tree_points_at_files_that_exist(self):
        if shutil.which('jq') is None:
            self.skipTest('jq is needed for init')
        result = self.run_cli('init', **INIT_ENV)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        config = json.loads((self.target / 'environment.json').read_text())
        self.assertEqual(config['name'], 'dist')
        agents = (self.target / 'AGENTS.md').read_text()
        # Any absolute path baked into the target folder must exist and must go through opt,
        # not the versioned keg: an upgrade replaces the keg and would break a baked path.
        # The host agent's job ends at `aws-survey run`, so the file currently bakes none.
        self.assertNotIn(str(self.keg), agents)
        for path in re.findall(rf'{re.escape(str(self.opt))}/\S+', agents):
            self.assertTrue(Path(path).exists(), f'{path} is named but does not exist')
