import hashlib
import json
import os
import subprocess
import threading
import unittest
from unittest import mock

import helpers
from helpers import CODE, TOKEN, EnvCase, callback, fake_api, setup


class InstallTests(EnvCase):
    def setUp(self):
        super().setUp()
        self.stub_venv()  # chosen: pre-made venv stub, so install.sh builds none and stays offline

    def test_should_ship_install_sh_byte_identical_to_the_original(self):
        with open(os.path.join(helpers.SCRIPTS, 'install.sh'), 'rb') as b:
            ours = b.read()
        # Pinned: the copy must only change together with the installer in the skills ZIP download.
        self.assertEqual(hashlib.sha256(ours).hexdigest(), 'ec6642dad78ca4cb105314676c0c05fd8e7109cb6a157c0faed741d86d898e9a')

    def installed(self):
        path = os.path.join(self.root, 'installed.json')
        if not os.path.exists(path):
            return None
        with open(path) as f:
            return json.load(f)

    def test_should_install_both_packages_through_the_real_installer(self):
        api = fake_api()
        self.addCleanup(api.close)
        code, out, err = self.run_main(api)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.installed(), {helpers.APIS: '1.2.0', helpers.KNOW: '3.4.5'})
        for name in (helpers.APIS, helpers.KNOW):
            self.assertTrue(os.path.isfile(os.path.join(self.root, 'marketplace', name, '.claude-plugin', 'plugin.json')))
            self.assertTrue(os.path.islink(os.path.join(self.home, '.codex', 'skills', name + '-skill')))
        self.assertEqual(len(api.requests), 3)
        r = api.requests[0]['headers']
        self.assertEqual((r['Authorization'], r['User-Agent']), ('Bearer ' + TOKEN, setup.USER_AGENT))

    def test_should_install_both_packages_from_the_public_route(self):
        api = fake_api(skills=helpers.make_skills())
        self.addCleanup(api.close)
        code, out, err = self.run_main(api)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.installed(), {helpers.APIS: '1.2.0', helpers.KNOW: '3.4.5'})
        self.assertEqual([r['path'] for r in api.requests][0], '/skills')
        self.assertEqual(len(api.requests), 3)
        for text in (out, err):
            self.assertNotIn('/skills/download/', text)

    def test_should_install_nothing_from_a_bad_bundle(self):
        good = helpers.make_zip(helpers.APIS, '1.2.0')
        know = helpers.make_zip(helpers.KNOW, '3.4.5')
        apis = {'name': helpers.APIS, 'version': '1.2.0', 'data': good}
        for skills in ([apis, {'name': 'evil', 'version': '3.4.5', 'data': know}], [apis],
                       [apis, dict(apis, version='1.3.0'), {'name': helpers.KNOW, 'version': '3.4.5', 'data': know}]):
            api = fake_api(skills=skills)
            self.addCleanup(api.close)
            code, out, err = self.run_main(api)
            self.assertEqual(code, 1, skills)
            self.assertIsNone(self.installed())
            self.assertFalse(os.path.exists(os.path.join(self.root, 'marketplace')))
        api = fake_api()
        self.addCleanup(api.close)
        with mock.patch.object(setup, 'MAX_BYTES', 1):  # oversized bundle
            self.assertEqual(self.run_main(api)[0], 1)
        self.assertIsNone(self.installed())

    def test_should_not_print_token_code_or_download_urls(self):
        def fetch(url, form):
            return {'token_type': 'Bearer', 'access_token': TOKEN, 'refresh_token': 'r'}

        def real_sign_in():
            def opener(url):
                import urllib.parse
                q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
                port = urllib.parse.urlsplit(q['redirect_uri'][0]).port
                threading.Thread(target=callback, args=(port, '/oauth/callback?state=%s&code=%s' % (q['state'][0], CODE)),
                                 daemon=True).start()
                return True
            return setup.sign_in(browser_open=opener, fetch=fetch)

        for good in (False, True):  # failed run first: a good one would make the next 'up to date'
            api = fake_api(skills_status=200 if good else 503)
            self.addCleanup(api.close)
            urls = []

            def spy(cmd, **kw):
                urls.extend(cmd[4::4])
                return subprocess.run(cmd, **kw)
            code, out, err = self.run_main(api, signin=real_sign_in, run=spy)
            self.assertEqual(code == 0, good)
            self.assertEqual(len(urls), 2 * good)
            for text in (out, err):
                for secret in [TOKEN, CODE, '.zip', '127.0.0.1', 'localhost'] + urls:
                    self.assertNotIn(secret, text)
            self.assertTrue(api.requests[0]['headers']['Authorization'].endswith(TOKEN))  # the token did reach the API

    def test_should_redact_secrets_in_installer_output(self):
        import types
        api = fake_api()
        self.addCleanup(api.close)
        fake = lambda cmd, **k: types.SimpleNamespace(returncode=1, stdout='ok ' + TOKEN, stderr='bad ' + cmd[4])
        code, out, err = self.run_main(api, run=fake)
        self.assertEqual((code, out, err), (1, 'ok [redacted]', 'bad [redacted]'))

    def test_should_say_why_the_skills_could_not_be_fetched(self):
        api = fake_api(skills=helpers.make_skills(), skills_status=403)  # no Request-Id: blocked before the API
        self.addCleanup(api.close)
        code, out, err = self.run_main(api)
        self.assertEqual(code, 1)
        self.assertIn('(HTTP 403, not from the Gelato API: blocked on the way); check your internet connection', err)
        api.close()  # nothing listening any more: a network error, not an access problem
        code, out, err = self.run_main(api)
        self.assertIn('could not get the skills (network error: ', err)
        self.assertIn('VPN or proxy', err)
        self.assertNotIn(TOKEN, err)

    def test_should_show_the_request_id_when_the_api_refuses(self):
        import email.message
        hdrs = email.message.Message()
        hdrs['Request-Id'] = 'rid-1'

        class Refuse:
            def open(self, req, timeout):
                raise __import__('urllib.error').error.HTTPError(req.full_url, 403, 'Forbidden', hdrs, None)
        with self.assertRaises(setup.SetupError) as c:
            setup._get('https://x', '/skills', TOKEN, 10, Refuse())
        self.assertEqual(str(c.exception), 'error: could not get the skills (HTTP 403, request id rid-1); '
                                           'sign in again or ask your Gelato contact for access')


class TargetsTests(EnvCase):
    """Setup registers only the assistant that runs it, and the background update keeps to that."""

    def setUp(self):
        super().setUp()
        self.stub_venv()
        self.claude_log = os.path.join(self.tmp, 'claude.log')
        with open(os.path.join(self.bin, 'claude'), 'w') as f:  # records a call; lists nothing, so it adds
            f.write('#!/bin/sh\necho "$@" >> "%s"\n' % self.claude_log)
        self.targets = os.path.join(self.root, 'targets')

    def setup_for(self, targets):
        """Run setup as the given assistant (None: no GELATO_CONNECT_TARGETS), then report who got registered."""
        if targets is None:
            del os.environ['GELATO_CONNECT_TARGETS']
        else:
            os.environ['GELATO_CONNECT_TARGETS'] = targets
        api = fake_api()
        self.addCleanup(api.close)
        code, out, err = self.run_main(api)
        self.assertEqual(code, 0, err)

    def registered(self):
        codex = os.path.islink(os.path.join(self.home, '.codex', 'skills', helpers.APIS + '-skill'))
        return sorted((['claude'] if os.path.exists(self.claude_log) else []) + (['codex'] if codex else []))

    def stored(self):
        with open(self.targets) as f:
            return json.load(f)['targets']

    def test_should_register_only_the_named_assistant_and_remember_it(self):
        self.setup_for('codex')
        self.assertEqual((self.registered(), self.stored()), (['codex'], 'codex'))

    def test_should_keep_to_the_remembered_assistant_without_the_variable(self):
        self.setup_for('codex')
        self.setup_for(None)
        self.assertEqual((self.registered(), self.stored()), (['codex'], 'codex'))

    def test_should_register_both_after_running_setup_in_each(self):
        self.setup_for('claude')
        self.assertEqual(self.registered(), ['claude'])
        self.setup_for('codex')
        self.assertEqual((self.registered(), self.stored()), (['claude', 'codex'], 'claude,codex'))

    def test_should_detect_every_assistant_on_path_when_nothing_is_remembered(self):
        self.setup_for(None)
        self.assertEqual((self.registered(), os.path.exists(self.targets)), (['claude', 'codex'], False))

    def test_should_ignore_a_corrupt_targets_file(self):
        os.makedirs(self.root, exist_ok=True)
        with open(self.targets, 'w') as f:
            f.write('{"targets": "claude,evil"}')
        os.chmod(self.targets, 0o600)
        self.setup_for(None)
        self.assertEqual(self.registered(), ['claude', 'codex'])


if __name__ == '__main__':
    unittest.main()
