import contextlib
import io
import json
import os
import threading
import unittest
import urllib.error
import urllib.parse
from unittest import mock

import helpers
from helpers import APIS, CODE, KNOW, TOKEN, EnvCase, callback, fake_api, setup

REFRESH, ROTATED, FRESH = 'fake-refresh-token-1d2e', 'fake-rotated-token-8c4b', 'fake-fresh-access-5a6f'


def mode(path):
    return oct(os.stat(path).st_mode & 0o777)


class Keycloak:
    """Fake token endpoint: setup.exchange_token keeps its real checks, only the opener is replaced."""

    def __init__(self, reply=None, error=None):
        self.reply, self.error, self.forms = reply, error, []
        real, fake = setup.exchange_token, self

        class Opener:
            def open(self, req, timeout=None):
                fake.forms.append({k: v[0] for k, v in urllib.parse.parse_qs(req.data.decode()).items()})
                if fake.error:
                    raise fake.error
                return Reply(fake.reply)

        class Reply:
            status = 200

            def __init__(self, value):
                self.body = json.dumps(value).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, n):
                return self.body

        self.patch = mock.patch.object(setup, 'exchange_token', lambda url, form, opener=None: real(url, form, opener=Opener()))


def http_error(code):
    return urllib.error.HTTPError(setup.BASE + '/token', code, 'x', {}, None)


class SetupStoresSession(EnvCase):
    def setUp(self):
        super().setUp()
        self.stub_venv()
        self.session = os.path.join(self.root, 'session.json')

    def run_setup(self, api):
        def opener(url):
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            port = urllib.parse.urlsplit(q['redirect_uri'][0]).port
            threading.Thread(target=callback, args=(port, '/oauth/callback?state=%s&code=%s' % (q['state'][0], CODE)),
                             daemon=True).start()
            return True
        kc = Keycloak({'token_type': 'Bearer', 'access_token': TOKEN, 'refresh_token': REFRESH, 'refresh_expires_in': 604800})
        with kc.patch, mock.patch.object(setup.webbrowser, 'open', opener):
            return self.run_main(api, signin=setup.sign_in)

    def test_should_store_only_the_refresh_token_owner_only(self):
        api = fake_api()
        self.addCleanup(api.close)
        code, out, err = self.run_setup(api)
        self.assertEqual(code, 0, err)
        with open(self.session) as f:
            self.assertEqual(json.load(f), {'refresh_token': REFRESH})
        self.assertEqual(mode(self.session), '0o600')
        self.assertFalse([n for n in os.listdir(self.root) if n.endswith('.tmp')])
        for text in (out, err):
            for secret in (TOKEN, REFRESH):
                self.assertNotIn(secret, text)

    def test_should_store_nothing_and_remove_an_old_one_when_opted_out(self):
        api = fake_api()
        self.addCleanup(api.close)
        with open(self.session, 'w') as f:
            f.write('{"refresh_token": "old"}')
        with mock.patch.dict(os.environ, {'GELATO_CONNECT_NO_AUTO_UPDATE': '1'}):
            code, out, err = self.run_setup(api)
        self.assertEqual(code, 0, err)
        self.assertFalse(os.path.lexists(self.session))
        self.assertTrue(os.path.exists(os.path.join(self.root, 'installed.json')))

    def test_should_remove_an_old_session_when_opted_out_even_if_the_install_fails(self):
        api = fake_api(skills_status=503)
        self.addCleanup(api.close)
        with open(self.session, 'w') as f:
            f.write('{"refresh_token": "old"}')
        with mock.patch.dict(os.environ, {'GELATO_CONNECT_NO_AUTO_UPDATE': '1'}):
            self.assertEqual(self.run_setup(api)[0], 1)
        self.assertFalse(os.path.lexists(self.session))

    def test_should_keep_no_session_after_a_failed_install(self):
        api = fake_api(skills_status=503)
        self.addCleanup(api.close)
        self.assertEqual(self.run_setup(api)[0], 1)
        self.assertFalse(os.path.lexists(self.session))

    def test_should_keep_the_scope_in_one_constant(self):
        self.assertEqual(setup.SCOPE, 'openid profile email')


class AutoTests(EnvCase):
    def setUp(self):
        super().setUp()
        self.stub_venv()
        self.session = os.path.join(self.root, 'session.json')
        self.put('session.json', {'refresh_token': REFRESH})
        self.put('installed.json', {APIS: '1.0.0', KNOW: '1.0.0'})
        self.put('latest.json', {'versions': {APIS: '1.2.0', KNOW: '3.4.5'}})
        self.api = fake_api()
        self.addCleanup(self.api.close)

    def put(self, name, value, file_mode=0o600):
        os.makedirs(self.root, exist_ok=True)
        path = os.path.join(self.root, name)
        with open(path, 'w') as f:
            json.dump(value, f)
        os.chmod(path, file_mode)

    def read(self, name):
        with open(os.path.join(self.root, name)) as f:
            return json.load(f)

    def auto(self, kc):
        """Run `setup.py --auto`; it must exit 0, print nothing and never reach the browser."""
        os.environ['GELATO_CONNECT_SKILLS_HOST'] = self.api.origin
        out, err = io.StringIO(), io.StringIO()
        with kc.patch, mock.patch.object(setup, 'sign_in') as sign_in, mock.patch.object(setup.webbrowser, 'open') as opener, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = setup.main(['--auto'])
        sign_in.assert_not_called()
        opener.assert_not_called()
        self.assertEqual((code, out.getvalue(), err.getvalue()), (0, '', ''))

    def good(self, **extra):
        return Keycloak(dict({'token_type': 'Bearer', 'access_token': FRESH, 'refresh_token': ROTATED}, **extra))

    def test_should_renew_and_store_the_rotated_token_when_nothing_is_newer(self):
        self.put('installed.json', {APIS: '1.2.0', KNOW: '3.4.5'})
        kc = self.good()
        self.auto(kc)
        self.assertEqual(kc.forms, [{'grant_type': 'refresh_token', 'client_id': setup.CLIENT_ID, 'refresh_token': REFRESH}])
        self.assertEqual(self.read('session.json'), {'refresh_token': ROTATED})
        self.assertEqual(mode(self.session), '0o600')
        self.assertEqual((self.api.requests, os.path.exists(os.path.join(self.root, 'updated.json'))), ([], False))

    def test_should_keep_the_stored_token_when_the_reply_has_none(self):
        self.put('installed.json', {APIS: '1.2.0', KNOW: '3.4.5'})
        self.auto(Keycloak({'token_type': 'Bearer', 'access_token': FRESH, 'refresh_expires_in': 0}))
        self.assertEqual(self.read('session.json'), {'refresh_token': REFRESH})

    def test_should_install_a_newer_version_and_record_it(self):
        self.auto(self.good())
        self.assertEqual(self.read('installed.json'), {APIS: '1.2.0', KNOW: '3.4.5'})
        self.assertEqual(self.read('updated.json'), {'versions': {APIS: '1.2.0', KNOW: '3.4.5'}})
        self.assertEqual(mode(os.path.join(self.root, 'updated.json')), '0o600')
        self.assertEqual(self.read('session.json'), {'refresh_token': ROTATED})
        self.assertEqual(self.api.requests[0]['headers']['Authorization'], 'Bearer ' + FRESH)

    def test_should_not_register_an_assistant_that_is_not_in_the_targets_file(self):
        self.put('targets', {'targets': 'codex'})
        del os.environ['GELATO_CONNECT_TARGETS']
        self.auto(self.good())
        self.assertEqual(self.read('installed.json'), {APIS: '1.2.0', KNOW: '3.4.5'})
        self.assertTrue(os.path.islink(os.path.join(self.home, '.codex', 'skills', APIS + '-skill')))
        self.assertFalse(os.path.exists(os.path.join(self.root, 'marketplace', '.claude-plugin', 'marketplace.json')))

    def test_should_record_nothing_when_the_install_fails(self):
        self.api.close()
        self.api = fake_api(skills_status=503)
        self.addCleanup(self.api.close)
        self.auto(self.good())
        self.assertEqual(self.read('installed.json'), {APIS: '1.0.0', KNOW: '1.0.0'})
        self.assertFalse(os.path.exists(os.path.join(self.root, 'updated.json')))

    def test_should_delete_the_session_when_keycloak_says_400_or_401(self):
        for status in (400, 401):
            self.put('session.json', {'refresh_token': REFRESH})
            self.auto(Keycloak(error=http_error(status)))
            self.assertFalse(os.path.lexists(self.session), status)
        self.assertEqual(self.api.requests, [])
        self.assertEqual(self.read('installed.json'), {APIS: '1.0.0', KNOW: '1.0.0'})

    def test_should_keep_a_session_another_run_renewed_meanwhile(self):
        kc = Keycloak(error=http_error(400))

        def lost_the_race(*a, **k):
            self.put('session.json', {'refresh_token': ROTATED})
            return faked(*a, **k)
        with kc.patch:
            faked = setup.exchange_token  # the fake server, never the real one
            with mock.patch.object(setup, 'exchange_token', lost_the_race):
                self.assertEqual(setup.main(['--auto']), 0)
        self.assertEqual(len(kc.forms), 1)
        self.assertEqual(self.read('session.json'), {'refresh_token': ROTATED})

    def test_should_not_renew_when_opted_out_or_the_folder_is_a_link(self):
        kc = self.good()
        with mock.patch.dict(os.environ, {'GELATO_CONNECT_NO_AUTO_UPDATE': '1'}):
            self.auto(kc)
        real = self.root + '.real'
        os.rename(self.root, real)
        os.symlink(real, self.root)
        self.auto(kc)
        self.assertEqual(kc.forms, [])
        self.assertEqual(self.read('session.json'), {'refresh_token': REFRESH})

    def test_should_keep_the_session_on_a_server_or_network_error(self):
        for error in (http_error(500), http_error(503), urllib.error.URLError('down'), ConnectionResetError()):
            self.auto(Keycloak(error=error))
            self.assertEqual(self.read('session.json'), {'refresh_token': REFRESH}, error)
        self.assertEqual(self.api.requests, [])

    def test_should_ignore_a_session_file_that_is_not_ours_alone(self):
        target = os.path.join(self.tmp, 'target.json')
        with open(target, 'w') as f:
            json.dump({'refresh_token': REFRESH}, f)
        os.chmod(target, 0o600)
        os.remove(self.session)
        os.symlink(target, self.session)
        cases = [('symlink', None), ('mode', 0o644), ('mode', 0o660), ('mode', 0o400),
                 ('shape', []), ('shape', {}), ('shape', {'refresh_token': 5}), ('shape', {'refresh_token': ''}),
                 ('big', {'refresh_token': 'x' * 20000})]
        for kind, arg in cases:
            if kind != 'symlink':
                if os.path.lexists(self.session):
                    os.remove(self.session)
                self.put('session.json', arg if kind != 'mode' else {'refresh_token': REFRESH}, 0o600 if kind != 'mode' else arg)
            kc = self.good()
            self.auto(kc)
            self.assertEqual(kc.forms, [], (kind, arg))
            self.assertTrue(os.path.lexists(self.session))
        with open(target) as f:  # nothing was ever written through the link
            self.assertEqual(json.load(f), {'refresh_token': REFRESH})

    def test_should_not_write_through_a_planted_link_when_storing(self):
        target = os.path.join(self.tmp, 'target.json')
        planted = os.path.join(self.root, 'updated.json')
        os.symlink(target, planted)
        self.auto(self.good())
        self.assertFalse(os.path.exists(target))

    def test_should_refuse_the_forbidden_folder(self):
        forbidden = os.path.join(self.home, '.gelato-connect', 'x')
        kc = self.good()
        with mock.patch.dict(os.environ, {'GELATO_CONNECT_HOME': forbidden}):
            self.auto(kc)
        self.assertEqual(kc.forms, [])
        self.assertFalse(os.path.exists(os.path.join(self.home, '.gelato-connect')))

    def test_should_do_nothing_without_a_session(self):
        os.remove(self.session)
        kc = self.good()
        self.auto(kc)
        self.assertEqual(kc.forms, [])

    def test_should_exit_zero_on_any_unexpected_error(self):
        with mock.patch.object(setup, 'install_bundle', side_effect=RuntimeError('boom')):
            self.auto(self.good())


if __name__ == '__main__':
    unittest.main()
