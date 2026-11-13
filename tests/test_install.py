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
        self.assertEqual([r['path'] for r in api.requests],
                         ['/skills', '/account', '/print-partners/ph-1/connect-skills/download'])
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
        for entries in ({helpers.APIS + '-1.2.0.zip': good, 'evil.zip': know},
                        {helpers.APIS + '-1.2.0.zip': good},
                        {helpers.APIS + '-1.2.0.zip': good, helpers.APIS + '-1.3.0.zip': good,
                         helpers.KNOW + '-3.4.5.zip': know}):
            api = fake_api(bundle=helpers.make_bundle(entries))
            self.addCleanup(api.close)
            code, out, err = self.run_main(api)
            self.assertEqual(code, 1, entries.keys())
            self.assertIsNone(self.installed())
            self.assertFalse(os.path.exists(os.path.join(self.root, 'marketplace')))
        api = fake_api()
        self.addCleanup(api.close)
        with mock.patch.object(setup, 'MAX_BYTES', len(api.bundle) - 1):  # oversized bundle
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
            api = fake_api(bundle=None if good else b'not a zip')
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


if __name__ == '__main__':
    unittest.main()
