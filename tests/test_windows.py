"""Native Windows support. Runs on every OS: the Windows branches are switched on by patching WINDOWS where they can run
anywhere, and the Windows-only facts (junctions, no SIGALRM, the venv layout) are checked for real when the OS is Windows
(GitHub Actions job windows-latest). The older tests assume bash and POSIX stubs and are not run there."""
import contextlib
import hashlib
import http.server
import importlib.util
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
from unittest import mock

import helpers
from helpers import APIS, KNOW, SCRIPTS, TOKEN, fake_api, make_zip, setup


def load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(SCRIPTS, name + '.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


install_win, notice = load('install_win'), load('notice')
NT = os.name == 'nt'


class Serve:
    """The package ZIPs on a localhost port, like the server install_bundle starts."""

    def __init__(self, files):
        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = files.get(self.path)
                self.send_response(200 if body is not None else 404)
                self.send_header('Content-Length', str(len(body or b'')))
                self.end_headers()
                self.wfile.write(body or b'')

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), H)
        self.base = 'http://localhost:%d' % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class WinCase(unittest.TestCase):
    """Temporary home, stub claude first on PATH, no network; works with Windows and POSIX paths."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='gc user ')  # a space, like C:\\Users\\Denise Zanotti
        self.addCleanup(lambda: __import__('shutil').rmtree(self.tmp, ignore_errors=True))
        self.home, self.bin = os.path.join(self.tmp, 'home'), os.path.join(self.tmp, 'bin')
        os.makedirs(self.home)
        os.makedirs(self.bin)
        self.root = os.path.join(self.home, '.gelato-connect-skills')
        self.codex = os.path.join(self.home, '.codex')
        self.log = os.path.join(self.tmp, 'claude.log')
        if NT:  # a .cmd shim, like the one npm installs
            stub, text = 'claude.cmd', '@echo off\r\necho %%* >> "%s"\r\nexit /b 0\r\n' % self.log
        else:
            stub, text = 'claude', '#!/bin/sh\necho "$@" >> "%s"\nexit 0\n' % self.log
        with open(os.path.join(self.bin, stub), 'w') as f:
            f.write(text)
        os.chmod(os.path.join(self.bin, stub), 0o755)
        env = {'HOME': self.home, 'USERPROFILE': self.home, 'GELATO_CONNECT_HOME': self.root, 'CODEX_HOME': self.codex,
               'CLAUDE_CONFIG_DIR': os.path.join(self.home, '.claude'), 'GELATO_CONNECT_TARGETS': 'codex',
               'PATH': self.bin + os.pathsep + os.environ.get('PATH', ''), 'no_proxy': '*', 'NO_PROXY': '*'}
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)

    def stub_venv(self):
        """A venv python that exists, so the installer builds none and stays offline."""
        py = install_win.venv_python(os.path.join(self.root, 'venv'))
        os.makedirs(os.path.dirname(py))
        open(py, 'w').close()

    def packages(self, **override):
        files = {'/%s.zip' % n: make_zip(n, v) for n, v in ((APIS, '1.2.0'), (KNOW, '3.4.5'))}
        files.update(override)
        srv = Serve(files)
        self.addCleanup(srv.close)
        args = []
        for n, v in ((APIS, '1.2.0'), (KNOW, '3.4.5')):
            args += [n, v, srv.base + '/%s.zip' % n, 'sha256:' + hashlib.sha256(files['/%s.zip' % n]).hexdigest()]
        return args

    def install(self, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            return install_win.main(args), out.getvalue(), err.getvalue()

    def installed(self):
        try:
            with open(os.path.join(self.root, 'installed.json')) as f:
                return json.load(f)
        except OSError:
            return None


class InstallWinTests(WinCase):
    def test_should_install_both_packages_and_link_the_codex_skills(self):
        self.stub_venv()
        code, out, err = self.install(self.packages())
        self.assertEqual(code, 0, err)
        self.assertEqual(self.installed(), {APIS: '1.2.0', KNOW: '3.4.5'})
        for n in (APIS, KNOW):
            self.assertTrue(os.path.isfile(os.path.join(self.root, 'marketplace', n, '.claude-plugin', 'plugin.json')))
            link = os.path.join(self.codex, 'skills', n + '-skill')
            self.assertTrue(install_win.is_link(link), link)
            self.assertTrue(os.path.isfile(os.path.join(link, 'SKILL.md')))  # reads through the link
        for secret in ('localhost', '.zip', '127.0.0.1'):
            self.assertNotIn(secret, out + err)
        self.assertIn('restart your assistant', out)

    def test_should_be_up_to_date_the_second_time_and_rebuild_when_a_folder_is_gone(self):
        self.stub_venv()
        args = self.packages()
        self.assertEqual(self.install(args)[0], 0)
        code, out, _ = self.install(args)
        self.assertEqual(code, 0)
        self.assertEqual(out.count('up to date'), 2)
        self.assertNotIn('downloaded', out)
        __import__('shutil').rmtree(os.path.join(self.root, 'marketplace', APIS))
        self.assertIn('downloaded and verified: ' + APIS, self.install(args)[1])

    def test_should_replace_our_stale_link_and_leave_foreign_entries_alone(self):
        self.stub_venv()
        skills = os.path.join(self.codex, 'skills')
        os.makedirs(os.path.join(skills, KNOW + '-skill'))  # a real folder of the user's: not ours
        args = self.packages()
        code, out, _ = self.install(args)
        self.assertEqual(code, 0)
        self.assertIn('skipped %s-skill: existing entry is not ours' % KNOW, out)
        self.assertFalse(install_win.is_link(os.path.join(skills, KNOW + '-skill')))
        # The folder swap of an update leaves the link dangling; the next run removes and re-creates it.
        __import__('shutil').rmtree(os.path.join(self.root, 'marketplace', APIS))
        code, out, _ = self.install(args)
        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(os.path.join(skills, APIS + '-skill', 'SKILL.md')))

    def test_should_register_with_claude_through_its_command(self):
        self.stub_venv()
        os.environ['GELATO_CONNECT_TARGETS'] = 'claude'
        code, out, err = self.install(self.packages())
        self.assertEqual(code, 0, err)
        with open(self.log) as f:
            calls = f.read()
        market = os.path.join(self.root, 'marketplace')
        self.assertIn('plugin marketplace add ', calls)
        self.assertIn(market, calls)  # quoted by Windows, as it holds a space
        self.assertIn('plugin install %s@gelato-connect' % APIS, calls)
        with open(os.path.join(market, '.claude-plugin', 'marketplace.json')) as f:
            self.assertEqual([p['source'] for p in json.load(f)['plugins']], ['./' + APIS, './' + KNOW])
        self.assertFalse(os.path.exists(os.path.join(self.codex, 'skills')))  # only the named assistant

    def test_should_install_nothing_when_a_package_is_bad(self):
        self.stub_venv()
        good = self.packages()
        evil = io.BytesIO()
        with zipfile.ZipFile(evil, 'w') as z:
            z.writestr('../outside.txt', 'x')
        link = io.BytesIO()
        with zipfile.ZipFile(link, 'w') as z:
            info = zipfile.ZipInfo('%s/link' % APIS)
            info.external_attr = 0o120777 << 16
            z.writestr(info, '/etc')
        cases = {'digest': (good[:3] + ['sha256:' + '0' * 64] + good[4:], 'digest mismatch'),
                 'traversal': (self.packages(**{'/%s.zip' % APIS: evil.getvalue()}), 'unsafe paths'),
                 'link': (self.packages(**{'/%s.zip' % APIS: link.getvalue()}), 'unsafe paths'),
                 'url': ([good[0], good[1], 'ftp://x/y.zip', good[3]], 'invalid url'),
                 'missing': ([good[0], good[1], good[2].replace('.zip', '-nope.zip'), good[3]], 'download failed'),
                 'name': (['Bad Name'] + good[1:4], 'invalid package name'),
                 'wrong plugin': ([good[0], '9.9.9'] + good[2:4], 'does not contain plugin')}
        for label, (args, message) in cases.items():
            code, out, err = self.install(args)
            self.assertEqual(code, 1, label)
            self.assertIn(message, err, label)
            self.assertIsNone(self.installed(), label)
            self.assertFalse(os.path.exists(os.path.join(self.root, '.lock')), label)  # released after a failure

    def test_should_refuse_a_second_install_at_the_same_time(self):
        os.makedirs(os.path.join(self.root, '.lock'))
        code, _, err = self.install(self.packages())
        self.assertEqual((code, 'another install is running' in err), (1, True))

    def test_should_refuse_claude_as_target_when_it_is_not_on_path(self):
        os.environ['GELATO_CONNECT_TARGETS'] = 'claude'
        os.environ['PATH'] = os.path.dirname(sys.executable)
        code, _, err = self.install(self.packages())
        self.assertEqual((code, "'claude' is not on PATH" in err), (1, True))

    def test_should_find_claude_in_its_own_install_folder_when_not_on_path(self):
        os.environ['PATH'] = os.path.dirname(sys.executable)
        native = os.path.join(self.home, '.local', 'bin', 'claude.exe' if NT else 'claude')
        self.assertIsNone(install_win.find('claude'))
        os.makedirs(os.path.dirname(native))
        open(native, 'w').close()
        self.assertEqual(install_win.find('claude'), native)
        self.assertIsNone(install_win.find('codex'))

    @unittest.skipUnless(os.environ.get('GELATO_CONNECT_NETWORK_TESTS'), 'downloads from PyPI (set GELATO_CONNECT_NETWORK_TESTS=1)')
    def test_should_install_the_python_dependencies_of_a_package_into_a_real_venv(self):
        pkg = io.BytesIO()
        with zipfile.ZipFile(pkg, 'w') as z:
            z.writestr('%s/.claude-plugin/plugin.json' % APIS, json.dumps({'name': APIS, 'version': '1.2.0'}))
            z.writestr('%s/skills/%s-skill/SKILL.md' % (APIS, APIS), '---\nname: x\n---\nx\n')
            z.writestr('%s/pyproject.toml' % APIS, '[build-system]\nrequires = ["setuptools>=68"]\n'
                       'build-backend = "setuptools.build_meta"\n\n[project]\nname = "gc-test-pkg"\nversion = "1.2.0"\n'
                       'dependencies = ["cryptography>=42", "jsonschema>=4.23"]\n\n[tool.setuptools]\npackages = []\n')
        code, out, err = self.install(self.packages(**{'/%s.zip' % APIS: pkg.getvalue()}))
        self.assertEqual(code, 0, err)
        self.assertIn('installed python dependencies: ' + APIS, out)
        py = install_win.venv_python(os.path.join(self.root, 'venv'))
        p = subprocess.run([py, '-c', 'import cryptography, jsonschema'], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_should_build_a_real_venv_with_the_layout_of_this_os(self):
        code, out, err = self.install(self.packages())  # no stub: the installer makes the venv itself
        self.assertEqual(code, 0, err)
        py = os.path.join(self.root, 'venv', 'Scripts', 'python.exe') if NT else os.path.join(self.root, 'venv', 'bin', 'python')
        self.assertTrue(os.path.isfile(py), py)
        self.assertIn('python: ' + py, out)
        self.assertEqual(subprocess.run([py, '-c', 'print(1)'], capture_output=True, text=True).stdout.strip(), '1')


class SetupOnWindowsTests(WinCase):
    """setup.py with its Windows switch on, through the real Python installer (install_win.py) in a subprocess."""

    def setUp(self):
        super().setUp()
        p = mock.patch.object(setup, 'WINDOWS', True)
        p.start()
        self.addCleanup(p.stop)
        self.stub_venv()

    def test_should_run_the_python_installer_not_bash(self):
        seen = []

        def spy(cmd, **kw):
            seen.append(cmd)
            return subprocess.run(cmd, **kw)
        api = fake_api(skills=helpers.make_skills())
        self.addCleanup(api.close)
        os.environ['GELATO_CONNECT_SKILLS_HOST'] = api.origin
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = setup.main([], signin=lambda: TOKEN, run=spy)
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(seen[0][:5], [sys.executable, '-I', '-X', 'utf8', os.path.join(SCRIPTS, 'install_win.py')])
        self.assertNotIn('bash', seen[0])
        self.assertEqual(self.installed(), {APIS: '1.2.0', KNOW: '3.4.5'})
        with open(os.path.join(self.root, 'targets')) as f:
            self.assertEqual(json.load(f), {'targets': 'codex'})  # remembered, for the background update
        for text in (out.getvalue(), err.getvalue()):
            self.assertNotIn('/skills/download/', text)

    def test_should_take_the_assistant_as_an_argument_in_any_shell(self):
        del os.environ['GELATO_CONNECT_TARGETS']
        api = fake_api(skills=helpers.make_skills())
        self.addCleanup(api.close)
        os.environ['GELATO_CONNECT_SKILLS_HOST'] = api.origin
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(setup.main(['--targets', 'evil'], signin=lambda: TOKEN), 1)
            self.assertEqual(setup.main(['--targets', 'codex'], signin=lambda: TOKEN), 0, err.getvalue())
        self.assertIn('usage: setup.py [--grok] [--targets claude|codex]', err.getvalue())
        self.assertFalse(os.path.exists(self.log))  # claude is on PATH but was not asked for
        self.assertTrue(install_win.is_link(os.path.join(self.codex, 'skills', APIS + '-skill')))
        with open(os.path.join(self.root, 'targets')) as f:
            self.assertEqual(json.load(f), {'targets': 'codex'})

    def test_should_store_and_read_the_sign_in_without_posix_calls(self):
        setup._write_private(self.root if os.path.isdir(self.root) else os.makedirs(self.root) or self.root, 'session.json', {'refresh_token': 'r'})
        self.assertEqual(setup._read_json(os.path.join(self.root, 'session.json'), private=True), {'refresh_token': 'r'})
        self.assertEqual(os.listdir(self.root).count('session.json.%d.tmp' % os.getpid()), 0)

    @unittest.skipIf(NT, 'creating a symlink needs privileges on Windows')
    def test_should_not_read_or_write_through_a_link(self):
        os.makedirs(self.root, exist_ok=True)
        real = os.path.join(self.tmp, 'real.json')
        with open(real, 'w') as f:
            f.write('{"refresh_token": "x"}')
        os.symlink(real, os.path.join(self.root, 'session.json'))
        self.assertIsNone(setup._read_json(os.path.join(self.root, 'session.json'), private=True))
        linked = os.path.join(self.tmp, 'linked-root')
        os.symlink(self.root, linked)
        with self.assertRaises(OSError):
            setup._write_private(linked, 'session.json', {})

    def test_should_refuse_the_forbidden_folder_in_the_windows_spelling(self):
        forbidden = os.path.join(self.home, '.gelato-connect')
        for path in (forbidden, os.path.join(forbidden, 'x', 'y'), os.path.join(self.home, '.GELATO-CONNECT'),
                     forbidden.replace(os.sep, '/')):
            with self.assertRaises(setup.SetupError, msg=path):
                setup.refuse_forbidden(path, self.home)
        setup.refuse_forbidden(self.root, self.home)  # .gelato-connect-skills is ours

    @unittest.skipIf(hasattr(signal, 'SIGALRM'), 'Windows has no SIGALRM')
    def test_should_run_the_background_update_without_sigalrm(self):
        self.assertEqual(setup.auto(), 0)  # no stored sign-in: returns quietly after arming a timer, not an alarm


class NoticeOnWindowsTests(WinCase):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(notice, 'WINDOWS', True)
        p.start()
        self.addCleanup(p.stop)
        os.makedirs(self.root)
        self.latest = os.path.join(self.root, 'latest.json')

    def test_should_write_latest_json_after_the_check_without_posix_calls(self):
        api = fake_api()
        self.addCleanup(api.close)
        os.environ['GELATO_CONNECT_SKILLS_HOST'] = api.origin
        notice.check(self.root)
        self.assertEqual(notice.load(self.latest), {'versions': {APIS: '1.8.0', KNOW: '5.1.7'}})
        self.assertEqual([f for f in os.listdir(self.root) if f.endswith('.tmp')], [])

    def test_should_touch_the_daily_marker_once(self):
        self.assertTrue(notice.due(self.root))
        self.assertFalse(notice.due(self.root))

    @unittest.skipIf(NT, 'creating a symlink needs privileges on Windows')
    def test_should_not_follow_a_linked_file_or_folder(self):
        real = os.path.join(self.tmp, 'real.json')
        with open(real, 'w') as f:
            f.write('{"versions": {}}')
        os.symlink(real, self.latest)
        self.assertIsNone(notice.load(self.latest))
        real_root, other = os.path.join(self.tmp, 'real-root'), os.path.join(self.tmp, 'linked-root')
        os.makedirs(real_root)
        os.symlink(real_root, other)
        api = fake_api()
        self.addCleanup(api.close)
        os.environ['GELATO_CONNECT_SKILLS_HOST'] = api.origin
        notice.check(other)
        self.assertEqual(os.listdir(real_root), [])

    def test_should_start_the_daily_check_with_this_python_detached(self):
        with open(os.path.join(self.root, 'installed.json'), 'w') as f:
            json.dump({APIS: '1.0.0'}, f)
        with mock.patch.object(notice.subprocess, 'Popen') as popen:
            self.assertEqual(notice.main(), None)
        (cmd,), kw = popen.call_args
        self.assertEqual(cmd, [sys.executable, '-I', os.path.join(SCRIPTS, 'notice.py'), '--check'])
        self.assertEqual(kw['creationflags'], 0x08000208)
        self.assertNotIn('start_new_session', kw)

    def test_should_hand_over_to_the_background_update_without_exec(self):
        with open(os.path.join(self.root, 'session.json'), 'w') as f:
            f.write('{}')
        with mock.patch.object(notice.subprocess, 'run') as run, mock.patch.object(notice.os, 'execv') as execv:
            notice.handover(self.root)
        execv.assert_not_called()
        self.assertEqual(run.call_args[0][0][-2:], [os.path.join(SCRIPTS, 'setup.py'), '--auto'])

    def test_should_refuse_the_forbidden_folder_in_the_windows_spelling(self):
        self.assertTrue(notice.under_forbidden(os.path.join(self.home, '.GELATO-CONNECT', 'x')))
        self.assertFalse(notice.under_forbidden(self.root))


class HooksTests(unittest.TestCase):
    def hook(self):
        with open(os.path.join(os.path.dirname(SCRIPTS), '..', '..', 'hooks', 'hooks.json')) as f:
            return json.load(f)['hooks']['SessionStart'][0]['hooks'][0]

    def test_should_try_python3_then_the_windows_launcher(self):
        h = self.hook()
        self.assertIn('python3 -I', h['command'].split('||')[0])
        self.assertIn('py -3 -I', h['command'].split('||')[1])
        self.assertTrue(h['commandWindows'].startswith('py -3 -I -c '))  # Codex: no shell variable syntax, it reads the environment

    @unittest.skipUnless(NT, 'runs the Codex Windows command for real')
    def test_should_run_the_windows_command_for_real(self):
        h = self.hook()
        env = dict(os.environ, CLAUDE_PLUGIN_ROOT=os.path.join(SCRIPTS, '..', '..', '..'), GELATO_CONNECT_HOME=os.path.join(tempfile.mkdtemp(), 'none'))
        p = subprocess.run(h['commandWindows'], shell=True, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual((p.returncode, p.stdout, p.stderr), (0, '', ''))  # a missing folder: the hook stays silent, exit 0


if __name__ == '__main__':
    unittest.main()
