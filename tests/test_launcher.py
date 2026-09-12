"""Check libexec/commands/run.sh (and the launch.sh it shares with scan) without Docker, AWS, or the user's real config.

Paths resolve in two systems: AWS_SURVEY_HOME (this distribution) and AWS_SURVEY_DIR
(the target folder holding environment.json and out/). Temporary keys live under
AWS_DIR, default ~/.aws-survey/<name>/.
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


def write_environment(folder, name='smoke'):
    config = json.loads((ROOT / 'templates/environment.json').read_text())
    config.update(name=name, account_id='000000000000', region='test-region')
    (folder / 'environment.json').write_text(json.dumps(config))


class Launcher(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        for name in ['bin', 'libexec', 'libexec/commands', 'container']:
            (self.root / name).mkdir()
        for name in ['bin/aws-survey', 'libexec/commands/run.sh', 'libexec/load-env.sh', 'libexec/ui.sh', 'libexec/docker.sh',
                     'libexec/launch.sh']:
            shutil.copy(ROOT / name, self.root / name)
        docker = self.root / 'bin/docker'
        docker.write_text('#!/usr/bin/env python3\nimport json, os, sys\nwith open(os.environ["SMOKE_DOCKER_LOG"], "a") as f:\n f.write(json.dumps(sys.argv[1:])+"\\n")\n')
        docker.chmod(0o755)
        self.log = self.root / 'calls.jsonl'
        # A fake HOME keeps the user's real token and keys out of the test and lets the
        # default key location (~/.aws-survey/<name>/) be checked.
        self.home = self.root / 'home'
        self.home.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def run_launcher(self, script, cwd, **extra):
        env = dict(os.environ, PATH=f'{self.root / "bin"}:{os.environ["PATH"]}', HOME=str(self.home), SMOKE_DOCKER_LOG=str(self.log))
        env.pop('AWS_SURVEY_HOME', None)
        env.pop('AWS_SURVEY_DIR', None)
        env.pop('AWS_DIR', None)
        env.update(extra)
        return subprocess.run(['bash', str(script), 'codex'], cwd=str(cwd), env=env, capture_output=True, text=True)

    def last_launch(self):
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        return calls, calls[-1]

    def assert_distribution_mounts(self, launch, home):
        for mount in ['smoke-claude:/home/node/.claude', 'smoke-codex:/home/node/.codex',
                      f'{home}/container/instructions/survey-agents.md:/home/node/aws-survey/AGENTS.md:ro',
                      f'{home}/container/instructions/survey-claude.md:/home/node/aws-survey/CLAUDE.md:ro',
                      f'{home}/container/method:/home/node/aws-survey/method:ro']:
            self.assertIn(mount, launch)
        self.assertEqual(launch[-2:], ['smoke:latest', 'codex'])

    def test_current_directory_is_the_target_folder(self):
        write_environment(self.root)
        keys = self.root / 'keys'
        keys.mkdir()
        (keys / 'credentials').touch()
        result = self.run_launcher(self.root / 'libexec/commands/run.sh', cwd=self.root, AWS_DIR=str(keys))
        self.assertEqual(result.returncode, 0, result.stderr)
        calls, launch = self.last_launch()
        self.assert_distribution_mounts(launch, self.root)
        self.assertIn(f'{keys}:/home/node/.aws-claude:ro', launch)
        self.assertIn(f'{self.root}/out:/home/node/aws-survey/out', launch)
        self.assertTrue((self.root / 'out/01_基礎調査/raw').is_dir())
        build = next(call for call in calls if call[0] == 'build')
        self.assertEqual(build[-3:], ['-f', f'{self.root}/Dockerfile', f'{self.root}/container'])

    def write_share_settings(self, dirs):
        settings = self.root / 'settings-store.json'
        settings.write_text(json.dumps({'FilesharingDirectories': dirs}))
        return str(settings)

    def test_unshared_mount_stops_before_docker_with_guidance(self):
        """Docker Desktop mounts only File Sharing paths: an unshared distribution stops run before docker."""
        write_environment(self.root)
        keys = self.root / 'keys'
        keys.mkdir()
        (keys / 'credentials').touch()
        settings = self.write_share_settings(['/nowhere'])
        result = self.run_launcher(self.root / 'libexec/commands/run.sh', cwd=self.root, AWS_DIR=str(keys),
                                   DOCKER_SHARE_SETTINGS=settings)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.log.exists(), 'docker must not be called')
        self.assertIn('File Sharing', result.stdout)
        self.assertIn(str(self.root), result.stdout)
        self.assertIn('Apply & restart', result.stdout)

    def test_homebrew_keg_suggests_the_prefix(self):
        write_environment(self.root)
        keys = self.root / 'keys'
        keys.mkdir()
        (keys / 'credentials').touch()
        settings = self.write_share_settings(['/Users', '/private'])
        result = self.run_launcher(self.root / 'libexec/commands/run.sh', cwd=self.root, AWS_DIR=str(keys),
                                   DOCKER_SHARE_SETTINGS=settings, AWS_SURVEY_HOME='/opt/homebrew/Cellar/aws-survey/0.2.0')
        # the keg does not exist here: load-env refuses it first, unless the check is what we hit
        if 'libexec/ がありません' not in result.stdout + result.stderr:
            self.assertIn('/opt/homebrew', result.stdout)

    def test_shared_mounts_proceed(self):
        write_environment(self.root)
        keys = self.root / 'keys'
        keys.mkdir()
        (keys / 'credentials').touch()
        settings = self.write_share_settings(['/Users', '/private', '/tmp', str(self.root)])
        result = self.run_launcher(self.root / 'libexec/commands/run.sh', cwd=self.root, AWS_DIR=str(keys),
                                   DOCKER_SHARE_SETTINGS=settings)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.log.exists())

    def test_target_folder_and_keys_are_separate_from_the_distribution(self):
        target = self.root / 'targets/alpha'
        target.mkdir(parents=True)
        write_environment(target)
        keys = self.home / '.aws-survey/smoke'
        keys.mkdir(parents=True)
        (keys / 'credentials').touch()
        elsewhere = self.root / 'elsewhere'
        elsewhere.mkdir()
        result = self.run_launcher(self.root / 'libexec/commands/run.sh', cwd=elsewhere, AWS_SURVEY_DIR=str(target))
        self.assertEqual(result.returncode, 0, result.stderr)
        _, launch = self.last_launch()
        self.assert_distribution_mounts(launch, self.root)
        self.assertIn(f'{keys}:/home/node/.aws-claude:ro', launch)
        self.assertIn(f'{target}/out:/home/node/aws-survey/out', launch)
        self.assertTrue((target / 'out/_環境').is_dir())
        self.assertFalse((self.root / 'out').exists())
        self.assertFalse((elsewhere / 'out').exists())

    def test_code_dir_is_always_mounted_read_only(self):
        """code/ (aws-survey lambda pull) reaches the container read-only. It is created empty so that a pull
        made while the container runs shows up without a restart (the request sentence in method/07 relies on it)."""
        write_environment(self.root)
        keys = self.root / 'keys'
        keys.mkdir()
        (keys / 'credentials').touch()
        self.assertFalse((self.root / 'code').exists())
        result = self.run_launcher(self.root / 'libexec/commands/run.sh', cwd=self.root, AWS_DIR=str(keys))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / 'code').is_dir())
        self.assertNotIn('取り出した Lambda のコード', result.stdout)
        _, launch = self.last_launch()
        self.assertIn(f'{self.root}/code:/home/node/aws-survey/code:ro', launch)
        (self.root / 'code/lambda').mkdir(parents=True)
        result = self.run_launcher(self.root / 'libexec/commands/run.sh', cwd=self.root, AWS_DIR=str(keys))
        self.assertIn('取り出した Lambda のコード', result.stdout)
        _, launch = self.last_launch()
        self.assertIn(f'{self.root}/code:/home/node/aws-survey/code:ro', launch)
        self.assertEqual(launch[-2:], ['smoke:latest', 'codex'])

    def test_missing_keys_name_the_per_target_location(self):
        write_environment(self.root)
        result = self.run_launcher(self.root / 'libexec/commands/run.sh', cwd=self.root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('一時キーがありません', result.stderr)
        self.assertFalse(self.log.exists())



class LoadEnv(unittest.TestCase):
    def run_load_env(self, cwd, **extra):
        env = dict(os.environ)
        for key in ['AWS_SURVEY_HOME', 'AWS_SURVEY_DIR', 'AWS_DIR', 'ENV_FILE']:
            env.pop(key, None)
        env.update(extra)
        script = f'. "{ROOT}/libexec/load-env.sh"; printf "%s\\n" "$AWS_SURVEY_HOME" "$AWS_SURVEY_DIR" "$AWS_DIR" "$LIBEXEC_DIR"'
        return subprocess.run(['bash', '-c', script], cwd=str(cwd), env=env, capture_output=True, text=True)

    def test_defaults_and_normalisation(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory).resolve()
            write_environment(target, name='beta')
            result = self.run_load_env(target, HOME='/fake-home')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines(), [str(ROOT), str(target), '/fake-home/.aws-survey/beta', str(ROOT / 'libexec')])
            relative = self.run_load_env(target.parent, HOME='/fake-home', AWS_SURVEY_DIR=target.name)
            self.assertEqual(relative.returncode, 0, relative.stderr)
            self.assertEqual(relative.stdout.splitlines()[1], str(target))

    def test_missing_environment_names_the_target_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_load_env(directory)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(f'{Path(directory).resolve()}/environment.json', result.stderr)

    def test_bad_home_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            write_environment(Path(directory))
            result = self.run_load_env(directory, AWS_SURVEY_HOME=directory)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('libexec/', result.stderr)


if __name__ == '__main__':
    unittest.main()


class ContainerDelivery(unittest.TestCase):
    """container/ holds delivery material only: every file in it reaches the survey container.

    The folder is the boundary. A file sitting here without a COPY or a mount is ambiguous:
    it reads as container material but never arrives, and a development note left here would
    be picked up by the survey agent instead of the developer.
    """

    @staticmethod
    def delivered_sources():
        """What reaches the survey container: COPY paths (relative to the container/ build context)
        and launch.sh mounts (absolute under AWS_SURVEY_HOME; run and scan share them)."""
        baked = re.findall(r'^COPY\s+(\S+)', (ROOT / 'Dockerfile').read_text(), re.MULTILINE)
        mounted = re.findall(r'\$AWS_SURVEY_HOME/([^:"\s]+):', (ROOT / 'libexec/launch.sh').read_text())
        return {f'container/{path}' for path in baked} | set(mounted)

    def test_every_container_file_is_delivered(self):
        delivered = self.delivered_sources()
        listing = subprocess.run(['git', 'ls-files', 'container'],
                                 cwd=str(ROOT), capture_output=True, text=True, check=True)
        tracked = [line for line in listing.stdout.splitlines() if line]
        self.assertTrue(tracked, 'container/ is tracked')
        for path in tracked:
            covered = any(path == source or path.startswith(f'{source}/') for source in delivered)
            self.assertTrue(covered, f'{path} never reaches the container: no COPY and no mount')


class ContainerVerifiedMilestone(unittest.TestCase):
    """`run` records setup.container_verified once the agent has left its check behind.

    Nothing else fills this key: the host agent's job ends at launch, so a step that had to
    be remembered would simply never happen. run.sh stamps it after the container exits,
    from the one fact it can see on disk. It records that the check was written, not that
    the check passed - judging the content is not something the launcher can do.
    """

    def setUp(self):
        self.case = Launcher('run')
        self.case.setUp()
        self.root = self.case.root
        write_environment(self.root)
        keys = self.root / 'keys'
        keys.mkdir()
        (keys / 'credentials').touch()
        self.keys = keys

    def tearDown(self):
        self.case.tearDown()

    def launch(self):
        return self.case.run_launcher(self.root / 'libexec/commands/run.sh',
                                      cwd=self.root, AWS_DIR=str(self.keys))

    def recorded(self):
        return json.loads((self.root / 'environment.json').read_text())['setup']['container_verified']

    def test_absent_check_records_nothing(self):
        self.launch()
        self.assertIsNone(self.recorded())

    def test_empty_check_records_nothing(self):
        self.launch()
        (self.root / 'out/_環境/00_動作確認.md').write_text('')
        self.launch()
        self.assertIsNone(self.recorded())

    def test_written_check_is_recorded_once(self):
        self.launch()
        (self.root / 'out/_環境/00_動作確認.md').write_text('確認しました\n')
        self.launch()
        first = self.recorded()
        self.assertRegex(first, r'^\d{4}-\d{2}-\d{2}$')
        self.launch()
        self.assertEqual(self.recorded(), first, 'a later run must not overwrite the date')


class BakedNotMounted(unittest.TestCase):
    """Guard configuration is COPYed into the image, never mounted from the host.

    Being root-owned and unwritable from inside the container is part of the defence: a
    mounted guard could be edited by the very agent it constrains, and a host that forgot
    the mount would launch an unguarded container with no visible difference. Neither
    failure raises an error at run time, which is why it is asserted here.
    """

    GUARD_SOURCES = ['settings.json', 'hooks/aws-readonly-guard.sh', 'hooks/codex-guard.py',
                     'codex/config.toml', 'codex/requirements.toml', 'ec2']

    def test_guards_are_copied_into_the_image(self):
        baked = re.findall(r'^COPY\s+(\S+)', (ROOT / 'Dockerfile').read_text(), re.MULTILINE)
        for source in self.GUARD_SOURCES:
            self.assertIn(source, baked, f'{source} is no longer COPYed into the image')

    def test_guards_are_not_mounted_at_launch(self):
        mounted = re.findall(r'\$AWS_SURVEY_HOME/([^:"\s]+):', (ROOT / 'libexec/launch.sh').read_text())
        for source in self.GUARD_SOURCES:
            self.assertNotIn(f'container/{source}', mounted,
                             f'{source} is mounted; a mounted guard can be edited from inside')


class WebToolsDenied(unittest.TestCase):
    """WebFetch and WebSearch are denied in settings.json.

    AWS answers can carry signed URLs (a Lambda function's Code.Location, for one). The Bash
    guard refuses curl and wget, but WebFetch is a tool of its own and never passes the Bash
    hook, so an allowed WebFetch would open such a URL with nothing in the way. The survey
    needs no outside web at all; Codex has web_search disabled in its requirements.
    """

    def setUp(self):
        self.permissions = json.loads((ROOT / 'container/settings.json').read_text())['permissions']

    def test_web_tools_are_denied(self):
        for tool in ('WebFetch', 'WebSearch'):
            self.assertIn(tool, self.permissions['deny'])
            self.assertNotIn(tool, self.permissions['allow'])

    def test_codex_web_search_stays_disabled(self):
        self.assertIn('allowed_web_search_modes = ["disabled"]', (ROOT / 'container/codex/requirements.toml').read_text())

    def test_rg_is_allowed_and_its_program_options_are_guarded(self):
        """rg is allowed without a prompt; --pre / --hostname-bin would start any program, so the guard refuses them."""
        self.assertIn('Bash(rg:*)', self.permissions['allow'])
        self.assertIn('--(pre|hostname-bin)', (ROOT / 'container/hooks/aws-readonly-guard.sh').read_text())

    def test_pulled_code_is_readable_not_editable(self):
        self.assertIn('Read(//home/node/aws-survey/code/**)', self.permissions['allow'])
        self.assertFalse(any(rule.startswith(('Edit(//home/node/aws-survey/code', 'Write(//home/node/aws-survey/code'))
                             for rule in self.permissions['allow']))


class CliInstallLayout(unittest.TestCase):
    """Claude Code updates itself; Codex is pinned. Neither can touch the npm prefix.

    Auto-update needs somewhere writable. Installing Claude Code natively puts it under the
    node user's ~/.local, so it can replace its own binary without /usr/local ever becoming
    writable - the guard's settings.json and hooks stay root-owned either way. launch.sh keeps
    ~/.local in a named volume, otherwise every update would be thrown away with the --rm
    container and re-downloaded next launch.

    Codex stays pinned because the guard rides on its feature contract
    (allow_managed_hooks_only, managed_dir, unified_exec): a version moving under us could
    drop the managed hook and nothing would report it. Raising the pin is deliberate, and
    tests/container_smoke.py is what checks the guard still bites afterwards.
    """

    def setUp(self):
        self.dockerfile = (ROOT / 'Dockerfile').read_text()
        self.launcher = (ROOT / 'libexec/launch.sh').read_text()

    def test_codex_names_a_version(self):
        self.assertIn('@openai/codex@${CODEX_VERSION}', self.dockerfile)
        self.assertRegex(self.dockerfile, re.compile(r'^ARG CODEX_VERSION=\d+\.\d+\.\d+$', re.M))

    def test_the_config_dir_is_set_before_the_install(self):
        """claude install writes installMethod into whatever CLAUDE_CONFIG_DIR points at.

        Set afterwards, the key lands in the default ~/.claude.json, which the runtime then
        does not read: `claude doctor` reports the install method as unset and warns on every
        launch. The value has to be written into the directory the volume carries.
        """
        config = self.dockerfile.index('ENV CLAUDE_CONFIG_DIR=')
        install = self.dockerfile.index('claude install latest')
        self.assertLess(config, install, 'CLAUDE_CONFIG_DIR must be set before the install')

    def test_claude_is_installed_natively(self):
        self.assertIn('claude install latest', self.dockerfile)
        self.assertIn('npm uninstall -g @anthropic-ai/claude-code', self.dockerfile,
                      'the npm bootstrap copy must go, or two installs shadow each other')
        self.assertIn('ENV PATH=/home/node/.local/bin:$PATH', self.dockerfile)

    def test_the_autoupdater_is_left_enabled(self):
        self.assertNotIn('DISABLE_AUTOUPDATER', self.dockerfile)

    def test_updates_survive_the_container(self):
        self.assertIn('-v "$CLI_VOLUME:/home/node/.local"', self.launcher,
                      '~/.local must be a named volume or updates are lost on exit')

    def test_the_npm_prefix_is_not_handed_to_the_agent(self):
        self.assertNotRegex(self.dockerfile, r'chown[^\n]*node[^\n]*/usr/local',
                            'a writable npm prefix would let the agent replace the CLI')


class Ec2Layout(unittest.TestCase):
    """The EC2 path is baked into the image: the ec2 wrapper, openssh-client and the SSM plugin.

    The wrapper is the only entry to the diagnostic gateway and is root-owned like the guards.
    The plugin is what `aws ssm start-session` execs, so it has to be present for the right
    architecture (AWS names it differently from the CLI: ubuntu_arm64 / ubuntu_64bit). The
    keys and config reach the container through the same read-only mount as the temporary
    credentials, so no extra mount may appear for them.
    """

    def setUp(self):
        self.dockerfile = (ROOT / 'Dockerfile').read_text()
        self.launcher = (ROOT / 'libexec/launch.sh').read_text()

    def test_the_wrapper_is_copied_and_executable(self):
        self.assertIn('COPY ec2           /usr/local/bin/ec2', self.dockerfile)
        self.assertRegex(self.dockerfile, r'chmod 755[^\n]*/usr/local/bin/ec2')
        self.assertTrue(os.access(ROOT / 'container/ec2', os.X_OK))

    def test_ssh_client_and_plugin_are_installed_per_architecture(self):
        self.assertIn('openssh-client', self.dockerfile)
        self.assertIn('aarch64) smp=ubuntu_arm64', self.dockerfile)
        self.assertIn('x86_64) smp=ubuntu_64bit', self.dockerfile)
        self.assertIn('session-manager-downloads/plugin/latest/${smp}/session-manager-plugin.deb', self.dockerfile)
        self.assertIn('session-manager-plugin --version', self.dockerfile)

    def test_keys_arrive_through_the_credentials_mount_only(self):
        self.assertIn('-v "$AWS_DIR:/home/node/.aws-claude:ro"', self.launcher)
        self.assertNotIn('/ssh:', self.launcher, 'keys must not get a mount of their own')

    def test_the_wrapper_names_nothing_about_the_survey(self):
        text = (ROOT / 'container/ec2').read_text()
        for word in ['libexec', 'environment.json', '.agents']:
            self.assertNotIn(word, text, f'{word} is host-side and must not be described to the container')


class RegionIsNotBaked(unittest.TestCase):
    """The image must not set ENV AWS_DEFAULT_REGION.

    Environment variables win over profile configuration, so a default frozen into the
    image would silently outrank environment.json: changing the region there would appear
    to work and have no effect. launch.sh passes the configured value with -e instead, and the
    claude-ro profile carries it too.
    """

    def test_dockerfile_does_not_set_a_default_region(self):
        for line in (ROOT / 'Dockerfile').read_text().splitlines():
            if line.startswith('ENV ') or line.startswith('    '):
                self.assertNotIn('AWS_DEFAULT_REGION', line,
                                 'ENV AWS_DEFAULT_REGION would override environment.json')

    def test_launcher_passes_the_configured_region(self):
        self.assertIn('-e "AWS_DEFAULT_REGION=$REGION"',
                      (ROOT / 'libexec/launch.sh').read_text())
