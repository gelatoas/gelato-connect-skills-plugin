"""Shared test fixtures: temp environment, stub assistants, fake skills API, fixture ZIP."""
import contextlib
import hashlib
import http.client
import http.server
import importlib.util
import io
import json
import os
import tempfile
import threading
import unittest
import urllib.parse
import zipfile
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, 'plugins', 'gelato-connect-setup', 'skills', 'setup', 'scripts')
_spec = importlib.util.spec_from_file_location('connect_setup', os.path.join(SCRIPTS, 'setup.py'))
setup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(setup)

TOKEN = 'fake-access-token-9f3a'
CODE = 'fake-auth-code-77c1'


APIS, KNOW = 'gelato-connect-apis', 'gelato-connect-product-knowledge'


def make_zip(name, version):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr('%s/.claude-plugin/plugin.json' % name, json.dumps({'name': name, 'version': version}))
        z.writestr('%s/skills/%s-skill/SKILL.md' % (name, name), '---\nname: %s-skill\n---\nx\n' % name)
    return buf.getvalue()


def make_bundle(entries=None):
    """One ZIP of ZIPs. `entries` maps inner file name to bytes; the default is the two real packages."""
    if entries is None:
        entries = {'%s-1.2.0.zip' % APIS: make_zip(APIS, '1.2.0'), '%s-3.4.5.zip' % KNOW: make_zip(KNOW, '3.4.5')}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return buf.getvalue()


class FakeApi:
    """Public API stand-in on a localhost port: GET /skills, /skills/download/<token> and /skills/versions."""

    def __init__(self, location=None, versions=None, versions_status=200, hang=None, skills=None, skills_status=200):
        self.location, self.requests = location, []
        # GET /skills/versions: raw body, status, and an Event the handler waits on (to simulate a hang)
        self.versions, self.versions_status, self.hang = versions, versions_status, hang
        # GET /skills: a list of dicts {name, version, data} plus optional url/digest overrides; default is the two packages.
        self.skills, self.skills_status = make_skills() if skills is None else skills, skills_status
        api = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def send(self, status, body=b'', ctype='application/json'):
                self.send_response(status)
                self.send_header('Content-Type', ctype)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                api.requests.append({'path': self.path, 'headers': dict(self.headers)})
                if api.location:
                    self.send_response(302)
                    self.send_header('Location', api.location)
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                elif self.path == '/skills/versions':
                    if api.hang:
                        api.hang.wait(30)
                    good = json.dumps({'versions': {APIS: '1.8.0', KNOW: '5.1.7'}}).encode()
                    self.send(api.versions_status, good if api.versions is None else api.versions)
                elif self.path == '/skills':
                    if api.skills_status != 200:
                        return self.send(api.skills_status)
                    self.send(200, json.dumps([{
                        'name': s['name'], 'description': 'x', 'version': s['version'],
                        'url': s.get('url', '%s/skills/download/%064x' % (api.origin, n)),
                        'digest': s.get('digest', 'sha256:' + hashlib.sha256(s['data']).hexdigest())}
                        for n, s in enumerate(api.skills)]).encode())
                elif self.path.startswith('/skills/download/'):
                    n = int(self.path.rsplit('/', 1)[1], 16)
                    self.send(200, api.skills[n]['data'], 'application/zip') if n < len(api.skills) else self.send(404)
                else:
                    self.send(404)

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), H)
        self.origin = 'http://localhost:%d' % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def make_skills():
    """The two packages as GET /skills serves them (see FakeApi `skills`)."""
    return [{'name': APIS, 'version': '1.2.0', 'data': make_zip(APIS, '1.2.0')},
            {'name': KNOW, 'version': '3.4.5', 'data': make_zip(KNOW, '3.4.5')}]


def fake_api(**kw):
    return FakeApi(**kw)


class EnvCase(unittest.TestCase):
    """Temporary HOME and config folders, stub claude/codex first on PATH, no network."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__('shutil').rmtree(self.tmp, ignore_errors=True))
        self.home = os.path.join(self.tmp, 'home')
        self.bin = os.path.join(self.tmp, 'bin')
        for d in (self.home, self.bin):
            os.makedirs(d)
        for tool in ('claude', 'codex'):
            p = os.path.join(self.bin, tool)
            with open(p, 'w') as f:
                f.write('#!/bin/sh\nexit 0\n')
            os.chmod(p, 0o755)
        self.root = os.path.join(self.home, '.gelato-connect-skills')
        env = {'HOME': self.home, 'GELATO_CONNECT_HOME': self.root,
               'CLAUDE_CONFIG_DIR': os.path.join(self.home, '.claude'), 'CODEX_HOME': os.path.join(self.home, '.codex'),
               'GELATO_CONNECT_TARGETS': 'claude,codex', 'PATH': self.bin + ':/usr/bin:/bin',
               'no_proxy': '*', 'NO_PROXY': '*'}
        p = mock.patch.dict(os.environ, env, clear=True)
        p.start()
        self.addCleanup(p.stop)

    def stub_venv(self):
        """Pre-create the venv python so install.sh does not build one (and stays offline)."""
        os.makedirs(os.path.join(self.root, 'venv', 'bin'))
        p = os.path.join(self.root, 'venv', 'bin', 'python')
        with open(p, 'w') as f:
            f.write('#!/bin/sh\nexit 0\n')
        os.chmod(p, 0o755)

    def run_main(self, api, token=TOKEN, **kw):
        os.environ['GELATO_CONNECT_SKILLS_HOST'] = api.origin
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = setup.main([], signin=kw.pop('signin', lambda: token), **kw)
        return code, out.getvalue(), err.getvalue()


def callback(port, path, host=None):
    c = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
    c.request('GET', path, headers={'Host': host or 'localhost:%d' % port})
    r = c.getresponse()
    r.read()
    c.close()
    return r.status, dict(r.getheaders())


def start_sign_in(**kw):
    """Run setup.sign_in in a thread with a capturing opener. Returns (thread, result dict, port, query)."""
    got, ready, result = {}, threading.Event(), {}

    def opener(url):
        got['url'] = url
        ready.set()
        return True

    def run():
        try:
            result['token'] = setup.sign_in(browser_open=opener, **kw)
        except Exception as e:
            result['error'] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    assert ready.wait(10)
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(got['url']).query)
    port = int(urllib.parse.urlsplit(q['redirect_uri'][0]).port)
    return t, result, port, q
