import builtins  # noqa: F401  (used by eval in the spy test)
import hashlib
import io
import os
import re
import types
import unittest
import urllib.error
import urllib.request
import zipfile
from unittest import mock

import helpers
from helpers import APIS, CODE, KNOW, TOKEN, EnvCase, callback, fake_api, make_bundle, setup, start_sign_in


class CallbackTests(EnvCase):
    def test_should_reject_bad_callbacks_then_accept_the_right_one(self):
        seen = {}

        def fetch(url, form):
            seen.update(form, url=url)
            return {'token_type': 'Bearer', 'access_token': TOKEN, 'refresh_token': 'r', 'id_token': 'i'}

        t, result, port, q = start_sign_in(fetch=fetch)
        state = q['state'][0]
        good = '/oauth/callback?state=%s&code=%s' % (state, CODE)
        self.assertEqual(callback(port, '/other?state=%s&code=c' % state)[0], 400)
        self.assertEqual(callback(port, '/oauth/callback?state=wrong&code=c')[0], 400)
        self.assertEqual(callback(port, good, host='evil.example:%d' % port)[0], 400)
        self.assertEqual(callback(port, '/oauth/callback?state=%s' % state)[0], 400)  # no code
        self.assertTrue(t.is_alive())
        status, headers = callback(port, good)
        self.assertEqual(status, 200)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(headers['Referrer-Policy'], 'no-referrer')
        t.join(10)
        self.assertEqual(result['token'], TOKEN)
        self.assertEqual((seen['code'], seen['grant_type'], seen['client_id']), (CODE, 'authorization_code', setup.CLIENT_ID))
        self.assertEqual(seen['url'], setup.BASE + '/token')
        try:  # a second callback is refused, or the listener is already closed
            self.assertEqual(callback(port, good)[0], 400)
        except OSError:
            pass

    def test_should_end_as_cancelled_on_error_callback(self):
        t, result, port, q = start_sign_in(fetch=lambda *a: self.fail('no token exchange'))
        callback(port, '/oauth/callback?state=%s&error=access_denied' % q['state'][0])
        t.join(10)
        self.assertIn('cancelled', str(result['error']))

    def test_should_fail_clearly_when_all_ports_are_busy(self):
        def busy(*a):
            raise OSError('busy')
        with self.assertRaises(setup.SetupError) as cm:
            setup.sign_in(server_factory=busy, browser_open=lambda u: True)
        self.assertIn('39201-39205', str(cm.exception))

    def test_should_time_out_and_print_the_url_when_the_browser_does_not_open(self):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(setup.SetupError) as cm:
            setup.sign_in(timeout=0.3, browser_open=lambda u: False)
        self.assertIn('timed out', str(cm.exception))
        self.assertIn('/protocol/openid-connect/auth?', out.getvalue())

    def test_should_build_the_sign_in_url(self):
        t, result, port, q = start_sign_in(fetch=lambda *a: {})
        self.assertEqual(q['scope'], [setup.SCOPE])
        self.assertEqual(q['code_challenge_method'], ['S256'])
        self.assertEqual(q['redirect_uri'], ['http://localhost:%d/oauth/callback' % port])
        self.assertEqual(q['client_id'], [setup.CLIENT_ID])
        self.assertIn(port, setup.CALLBACK_PORTS)
        callback(port, '/oauth/callback?state=%s&error=x' % q['state'][0])
        t.join(10)

    def test_should_only_exchange_at_the_literal_token_url(self):
        with self.assertRaises(setup.SetupError):
            setup.exchange_token('https://auth.gelato.com/other', {})
        with self.assertRaises(setup.SetupError):
            setup.exchange_token('http://auth.gelato.com/auth/realms/gelato-api/protocol/openid-connect/token', {})
        sent = []

        class Opener:  # the edge in front of Keycloak rejects urllib's default User-Agent with 403
            def open(self, req, timeout=None):
                sent.append(req.get_header('User-agent'))
                raise OSError()
        with self.assertRaises(setup.SetupError):
            setup.exchange_token(setup.BASE + '/token', {}, opener=Opener())
        self.assertEqual(sent, [setup.USER_AGENT])


class DestinationTests(EnvCase):
    def test_should_refuse_unexpected_hosts_before_any_sign_in(self):
        for bad in ('https://api.gelatoconnect.gelato.com:444', 'https://api.gelatoconnect.gelato.com@evil.example',
                    'https://[removed]',
                    'http://localhost.evil.com', 'http://evil.example', 'https://localhost', 'ftp://localhost',
                    'http://localhost/mcp', 'http://localhost?x=1', 'http://localhost#f', 'http://u:p@localhost',
                    'http://localhost:99999x', 'http://api.gelatoconnect.gelato.com'):
            os.environ['GELATO_CONNECT_SKILLS_HOST'] = bad
            calls = []
            err = self.run_quiet(signin=lambda: calls.append(1))
            self.assertEqual((err[0], calls), (1, []), bad)

    def run_quiet(self, **kw):
        import contextlib
        import io
        e = io.StringIO()
        with contextlib.redirect_stderr(e):
            return setup.main([], **kw), e.getvalue()

    def test_should_accept_live_and_localhost_origins(self):
        self.assertEqual(setup.api_origin(None), setup.API_ORIGIN)
        self.assertEqual(setup.api_origin('https://api.gelatoconnect.gelato.com/'), setup.API_ORIGIN)
        self.assertEqual(setup.API_ORIGIN, 'https://api.gelatoconnect.gelato.com')
        self.assertEqual(setup.api_origin('http://localhost:8080'), 'http://localhost:8080')
        self.assertEqual(setup.api_origin('http://127.0.0.1'), 'http://127.0.0.1')

    def test_should_not_follow_a_redirect_with_the_token(self):
        other = fake_api()
        api = fake_api(location=other.origin + '/account')
        self.addCleanup(api.close)
        self.addCleanup(other.close)
        with self.assertRaises(setup.SetupError):
            setup.fetch_bundle(api.origin, TOKEN)
        self.assertEqual(len(api.requests), 1)
        self.assertEqual(other.requests, [])


class FetchTests(EnvCase):
    def fetch(self, **kw):
        api = fake_api(**kw)
        self.addCleanup(api.close)
        return api, lambda: setup.fetch_bundle(api.origin, TOKEN)

    def test_should_fetch_the_bundle_of_the_accounts_print_house(self):
        api, fetch = self.fetch()
        self.assertEqual(fetch(), api.bundle)
        self.assertEqual([r['path'] for r in api.requests], ['/skills', '/account', '/print-partners/ph-1/connect-skills/download'])

    def test_should_accept_replies_wrapped_in_data(self):
        api, fetch = self.fetch(wrap=True)
        self.assertEqual(fetch(), api.bundle)

    def test_should_retry_once_with_the_first_listed_print_house_on_404(self):
        api, fetch = self.fetch(account_uid='admin-1', ok_uids=('ph-2',), listed=('ph-2', 'ph-3'), wrap=True)
        self.assertEqual(fetch(), api.bundle)
        self.assertEqual([r['path'] for r in api.requests], [
            '/skills', '/account', '/print-partners/admin-1/connect-skills/download', '/print-partners',
            '/print-partners/ph-2/connect-skills/download'])

    def test_should_use_the_list_when_the_own_uid_is_a_placeholder(self):
        # live, 13 Nov: a Gelato admin account's printHouseUid is one character that is no print house
        api, fetch = self.fetch(account_uid='*', ok_uids=('ph-2',), listed=('ph-2',))
        self.assertEqual(fetch(), api.bundle)
        self.assertEqual([r['path'] for r in api.requests], [
            '/skills', '/account', '/print-partners', '/print-partners/ph-2/connect-skills/download'])

    def test_should_fail_cleanly_when_the_fallback_also_404s_or_is_empty(self):
        for kw in ({'ok_uids': ()}, {'account_uid': 'admin-1', 'listed': ()}, {'account_uid': 'a b', 'listed': ('x/y',)}):
            api, fetch = self.fetch(**kw)
            with self.assertRaises(setup.SetupError, msg=kw):
                fetch()
        self.assertLessEqual(len(api.requests), 3)

    def test_should_fail_cleanly_on_other_statuses_and_never_show_token_or_url(self):
        for status in (401, 403, 500):
            api, fetch = self.fetch(status=status)
            with self.assertRaises(setup.SetupError) as cm:
                fetch()
            self.assertEqual(len(api.requests), 3)  # /skills (404), /account, one download: no retry on a non-404
            for secret in (TOKEN, api.origin, 'http'):
                self.assertNotIn(secret, str(cm.exception))

    def test_should_fetch_both_packages_from_the_public_route_and_send_no_token_on_the_links(self):
        api, fetch = self.fetch(skills=helpers.make_skills())
        dest = os.path.join(self.tmp, 'out')
        os.makedirs(dest)
        got = setup.unpack_bundle(fetch(), dest)
        self.assertEqual({(s['name'], s['version']) for s in got}, {(helpers.APIS, '1.2.0'), (helpers.KNOW, '3.4.5')})
        paths = [r['path'] for r in api.requests]
        self.assertEqual((paths[0], len(paths)), ('/skills', 3))  # the older download is not asked
        self.assertEqual(api.requests[0]['headers']['Authorization'], 'Bearer ' + TOKEN)
        for r in api.requests[1:]:
            self.assertTrue(r['path'].startswith('/skills/download/'))
            self.assertNotIn('Authorization', r['headers'])

    def test_should_refuse_a_bad_public_listing_without_falling_back(self):
        good = helpers.make_skills()
        other = fake_api(skills=good)
        self.addCleanup(other.close)
        bad = {'wrong checksum': [dict(good[0], digest='sha256:' + '0' * 64), good[1]],
               'link to another host': [dict(good[0], url=other.origin + '/skills/download/' + '0' * 64), good[1]],
               'link to another path': [dict(good[0], url='PATH'), good[1]],
               'bad version': [dict(good[0], version='1.2'), good[1]],
               'one package': good[:1], 'same package twice': [good[0], good[0]],
               'unknown name': [dict(good[0], name='other'), good[1]]}
        for label, skills in bad.items():
            api, fetch = self.fetch(skills=skills)
            if skills[0].get('url') == 'PATH':
                skills[0]['url'] = api.origin + '/print-partners'
            with self.assertRaises(setup.SetupError, msg=label):
                fetch()
            for r in api.requests:  # never the older download, never a path outside /skills
                self.assertTrue(r['path'].startswith('/skills'), (label, r['path']))
        self.assertEqual(other.requests, [])
        for status in (401, 403, 503):  # signed out, no print house, unavailable: an error, not the older download
            api, fetch = self.fetch(skills=good, skills_status=status)
            with self.assertRaises(setup.SetupError):
                fetch()
            self.assertEqual([r['path'] for r in api.requests], ['/skills'])

    def test_should_refuse_an_oversized_bundle(self):
        api, fetch = self.fetch()
        with mock.patch.object(setup, 'MAX_BYTES', len(api.bundle) - 1), self.assertRaises(setup.SetupError):
            fetch()


class UnpackTests(EnvCase):
    def unpack(self, entries):
        dest = os.path.join(self.tmp, 'dest')
        os.makedirs(dest, exist_ok=True)
        return setup.unpack_bundle(make_bundle(entries), dest), dest

    def test_should_write_the_two_packages_and_their_digests(self):
        skills, dest = self.unpack(None)
        self.assertEqual([(s['name'], s['version']) for s in skills], [(APIS, '1.2.0'), (KNOW, '3.4.5')])
        for s in skills:
            with open(os.path.join(dest, s['name'] + '.zip'), 'rb') as f:
                self.assertEqual(s['digest'], 'sha256:' + hashlib.sha256(f.read()).hexdigest())

    def test_should_refuse_anything_but_the_two_exact_names(self):
        k = '%s-3.4.5.zip' % KNOW
        a = '%s-1.2.0.zip' % APIS
        for bad in ('x/' + a, a + '.bak', a.upper(), '%s-1.2.zip' % APIS, '%s-12345.0.0.zip' % APIS, '%s-1.2.0.zip\n' % APIS,
                    'other-1.2.0.zip', '../' + a, a + '/'):
            with self.assertRaises(setup.SetupError, msg=bad):
                self.unpack({a: b'x', k: b'y', bad: b'z'})
        with self.assertRaises(setup.SetupError):
            self.unpack({a: b'x'})  # missing
        with self.assertRaises(setup.SetupError):
            self.unpack({})
        with self.assertRaises(setup.SetupError):
            setup.unpack_bundle(b'not a zip', self.tmp)

    def test_should_refuse_a_duplicate_and_an_oversized_inner_file(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as z:
            z.writestr('%s-1.2.0.zip' % APIS, b'x')
            z.writestr('%s-1.3.0.zip' % APIS, b'x')
            z.writestr('%s-3.4.5.zip' % KNOW, b'y')
        with self.assertRaises(setup.SetupError):
            setup.unpack_bundle(buf.getvalue(), self.tmp)
        with mock.patch.object(setup, 'MAX_BYTES', 3), self.assertRaises(setup.SetupError):
            self.unpack({'%s-1.2.0.zip' % APIS: b'xxxx', '%s-3.4.5.zip' % KNOW: b'y'})


class LocalServerTests(EnvCase):
    def test_should_serve_only_the_two_files_and_close_after_the_run(self):
        seen = {}

        def run(cmd, **kw):
            urls = cmd[4::4]
            seen['urls'] = urls
            base = urls[0].rsplit('/', 1)[0]
            seen['status'] = {}
            for path in [urls[0], urls[1], base + '/other.zip', base + '/', urls[0].replace('/gelato', '/x/gelato'),
                         urls[0].replace(urls[0].split('/')[3], 'wrong'),
                         'http://localhost:%d/' % int(urls[0].split(':')[2].split('/')[0])]:
                try:
                    with urllib.request.urlopen(path, timeout=10) as r:
                        seen['status'][path] = r.status
                except urllib.error.HTTPError as e:
                    seen['status'][path] = e.code
            req = urllib.request.Request(urls[0], data=b'x', method='POST')
            try:
                urllib.request.urlopen(req, timeout=10)
            except urllib.error.HTTPError as e:
                seen['post'] = e.code
            return types.SimpleNamespace(returncode=0, stdout='', stderr='')

        self.assertEqual(setup.install_bundle(make_bundle(), TOKEN, run), 0)
        urls = seen['urls']
        self.assertTrue(all(re.fullmatch(r'http://localhost:\d+/[\w-]{16,}/gelato-connect-[a-z-]+\.zip', u) for u in urls))
        self.assertEqual([seen['status'][u] for u in urls], [200, 200])
        self.assertEqual(sorted(v for k, v in seen['status'].items() if k not in urls), [404] * 5)
        self.assertIn(seen['post'], (404, 501))
        with self.assertRaises(OSError):  # closed after the run
            urllib.request.urlopen(urls[0], timeout=5)

    def test_should_close_the_server_when_the_installer_fails(self):
        seen = []

        def run(cmd, **kw):
            seen.append(cmd[4])
            raise OSError()
        with self.assertRaises(setup.SetupError):
            setup.install_bundle(make_bundle(), TOKEN, run)
        with self.assertRaises(OSError):
            urllib.request.urlopen(seen[0], timeout=5)


class PathGuardTests(EnvCase):
    def forbidden(self):
        return os.path.join(self.home, '.gelato-connect')

    def test_should_refuse_the_folder_and_anything_below_it(self):
        for p in (self.forbidden(), os.path.join(self.forbidden(), 'x', 'y'), self.forbidden() + '/./z/../w',
                  os.path.join(self.home, 'sub', '..', '.gelato-connect', 'a')):
            with self.assertRaises(setup.SetupError, msg=p):
                setup.refuse_forbidden(p, self.home)

    def test_should_refuse_through_a_symlink_and_a_chain(self):
        link = os.path.join(self.tmp, 'link')
        os.symlink(self.forbidden(), link)  # dangling on purpose: the folder is never created
        chain = os.path.join(self.tmp, 'chain')
        os.symlink(link, chain)
        rel = os.path.join(self.tmp, 'rel')
        os.symlink('home/.gelato-connect', rel)
        sub = os.path.join(self.home, 'sub')
        os.mkdir(sub)
        up = os.path.join(self.tmp, 'up')
        os.symlink(sub, up)  # up/.. is the home folder, not the folder that holds the link
        for p in (link, os.path.join(link, 'deeper'), chain, rel, os.path.join(up, '..', '.gelato-connect')):
            with self.assertRaises(setup.SetupError, msg=p):
                setup.refuse_forbidden(p, self.home)

    def test_should_refuse_a_planted_link_inside_the_install_folder(self):
        os.makedirs(self.root, exist_ok=True)
        planted = os.path.join(self.root, 'installed.json')
        os.symlink(os.path.join(self.forbidden(), 'session.json'), planted)
        signin = mock.Mock()
        self.assertEqual(setup.main([], signin=signin), 1)
        os.remove(planted)
        os.makedirs(os.path.join(self.root, 'marketplace', '.claude-plugin'))
        os.symlink(os.path.join(self.forbidden(), 'x'), os.path.join(self.root, 'marketplace', '.claude-plugin', 'marketplace.json'))
        self.assertEqual(setup.main([], signin=signin), 1)
        signin.assert_not_called()

    def test_should_refuse_both_readings_of_dot_dot(self):
        safe = os.path.join(self.tmp, 'safe', 'sub')
        os.makedirs(safe)
        os.symlink(safe, os.path.join(self.home, 'link'))  # bash `cd` collapses link/.. to the home folder
        with self.assertRaises(setup.SetupError):
            setup.refuse_forbidden(os.path.join(self.home, 'link', '..', '.gelato-connect'), self.home)
        os.symlink(self.forbidden(), os.path.join(self.home, 'alias'))  # dangling: the folder is never created
        with self.assertRaises(setup.SetupError):  # the OS reads this as <tmp>/safe/alias, bash as <home>/alias
            setup.refuse_forbidden(os.path.join(self.home, 'link', '..', 'alias'), self.home)

    def test_should_accept_the_default_and_sibling_names(self):
        for name in ('.gelato-connect-skills', '.gelato-connect2', '.codex', '.claude'):
            setup.refuse_forbidden(os.path.join(self.home, name), self.home)

    def test_should_apply_to_every_configured_folder(self):
        for var in ('GELATO_CONNECT_HOME', 'CODEX_HOME', 'CLAUDE_CONFIG_DIR', 'TMPDIR', 'TEMP', 'TMP'):
            with mock.patch.dict(os.environ, {var: os.path.join(self.forbidden(), 'x')}):
                signin = mock.Mock()
                self.assertEqual(setup.main([], signin=signin), 1, var)
                signin.assert_not_called()

    def test_should_keep_the_local_package_links_away_from_a_proxy(self):
        run = mock.Mock(return_value=mock.Mock(returncode=0, stdout='', stderr=''))
        with mock.patch.dict(os.environ, {'http_proxy': 'http://proxy.invalid:1', 'NO_PROXY': '', 'no_proxy': ''}):
            setup.run_installer([], 'tok', run)
        env = run.call_args.kwargs['env']
        self.assertIn('localhost', env['NO_PROXY'])
        self.assertIn('localhost', env['no_proxy'])

    def test_should_never_touch_the_forbidden_folder(self):
        link = os.path.join(self.tmp, 'link')
        os.symlink(self.forbidden(), link)
        names = ('os.lstat', 'os.stat', 'os.readlink', 'os.scandir', 'os.listdir', 'builtins.open')
        calls = []
        real = {n: eval(n) for n in names}
        banned = {setup._parts(self.forbidden()), setup._parts(os.path.realpath(self.home) + '/.gelato-connect')}

        def spy(n):
            def f(path, *a, **k):
                q = setup._parts(os.fspath(path)) if isinstance(path, (str, bytes, os.PathLike)) else ()
                calls.append((n, q))
                if any(q[:len(b)] == b for b in banned):
                    raise AssertionError('touched %s %s' % (n, path))
                return real[n](path, *a, **k)
            return f

        patches = [mock.patch(n, spy(n)) for n in names]
        for p in patches:
            p.start()
        try:
            for p in (self.forbidden(), os.path.join(self.forbidden(), 'a'), link, os.path.join(link, 'b'),
                      os.path.join(self.home, '.gelato-connect-skills')):
                try:
                    setup.refuse_forbidden(p, self.home)
                except setup.SetupError:
                    pass
        finally:
            for p in patches:
                p.stop()
        self.assertTrue(any(n == 'os.lstat' for n, _ in calls))  # the spy was live


if __name__ == '__main__':
    unittest.main()
