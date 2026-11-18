import io
import contextlib
import json
import os
import shutil
import unittest
import zipfile
from unittest import mock

from helpers import APIS, KNOW, EnvCase, fake_api, make_bundle, setup
from test_auto import FRESH, REFRESH, ROTATED, Keycloak


class GrokCase(EnvCase):
    def setUp(self):
        super().setUp()
        self.stub_venv()
        self.api = fake_api()
        self.addCleanup(self.api.close)
        self.skills = os.path.join(self.home, '.grok', 'skills')
        self.forbidden = os.path.join(self.home, '.gelato-connect')

    def link(self, name):
        return os.path.join(self.skills, name)

    def setup_grok(self, *args):
        os.environ['GELATO_CONNECT_SKILLS_HOST'] = self.api.origin
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = setup.main(list(args), signin=lambda: 'tok')
        return code, out.getvalue(), err.getvalue()

    def update(self):
        for name, value in (('session.json', {'refresh_token': REFRESH}), ('installed.json', {APIS: '1.0.0', KNOW: '1.0.0'}),
                            ('latest.json', {'versions': {APIS: '1.2.0', KNOW: '3.4.5'}})):
            path = os.path.join(self.root, name)
            with open(path, 'w') as f:
                json.dump(value, f)
            os.chmod(path, 0o600)
        os.environ['GELATO_CONNECT_SKILLS_HOST'] = self.api.origin
        kc = Keycloak({'token_type': 'Bearer', 'access_token': FRESH, 'refresh_token': ROTATED})
        with kc.patch:
            self.assertEqual(setup.main(['--auto']), 0)

    def opt_in(self):
        os.makedirs(self.root, exist_ok=True)
        setup._write_private(self.root, 'grok.json', {'grok': True})


class GrokSetupTests(GrokCase):
    def test_should_link_the_skills_on_a_grok_only_machine(self):
        del os.environ['GELATO_CONNECT_TARGETS']
        os.environ['PATH'] = '/usr/bin:/bin'
        code, out, err = self.setup_grok('--grok')
        self.assertEqual(code, 0, err)
        for n in (APIS, KNOW):
            self.assertEqual(os.readlink(self.link(n + '-skill')), os.path.join(self.root, 'marketplace', n, 'skills', n + '-skill'))
        self.assertFalse(os.path.exists(os.path.join(self.home, '.codex')))
        self.assertFalse(os.path.exists(os.path.join(self.root, 'targets')))
        self.assertEqual(setup._read_json(os.path.join(self.root, 'grok.json'), private=True), {'grok': True})

    def test_should_leave_claude_and_codex_registration_unchanged_with_grok(self):
        del os.environ['GELATO_CONNECT_TARGETS']  # PATH detection finds both stubs
        code, out, err = self.setup_grok('--grok')
        self.assertEqual(code, 0, err)
        self.assertIn('claude: marketplace gelato-connect added', out)
        self.assertTrue(os.path.islink(os.path.join(self.home, '.codex', 'skills', APIS + '-skill')))
        self.assertTrue(os.path.islink(self.link(APIS + '-skill')))
        self.assertFalse(os.path.exists(os.path.join(self.root, 'targets')))  # targets file stays out of it

    def test_should_do_the_same_as_before_without_grok(self):
        code, out, err = self.setup_grok()
        self.assertEqual(code, 0, err)
        self.assertTrue(os.path.islink(os.path.join(self.home, '.codex', 'skills', APIS + '-skill')))
        self.assertFalse(os.path.exists(os.path.join(self.home, '.grok')))
        self.assertFalse(os.path.exists(os.path.join(self.root, 'grok.json')))

    def test_should_refuse_grok_on_windows(self):
        with mock.patch.object(setup, 'WINDOWS', True):
            code, out, err = self.setup_grok('--grok')
        self.assertEqual(code, 1)
        self.assertIn('not supported on Windows', err)
        self.assertFalse(os.path.exists(self.root) and os.path.exists(os.path.join(self.root, 'grok.json')))
        self.assertEqual(self.api.requests, [])

    def test_should_accept_grok_with_targets(self):
        code, out, err = self.setup_grok('--targets', 'codex', '--grok')
        self.assertEqual(code, 0, err)
        self.assertTrue(os.path.islink(self.link(APIS + '-skill')))
        self.assertEqual(setup._stored_targets(self.root), ['codex'])

    def test_should_never_replace_a_foreign_entry(self):
        os.makedirs(self.link(KNOW + '-skill'))
        with open(os.path.join(self.link(KNOW + '-skill'), 'mine'), 'w') as f:
            f.write('x')
        os.symlink('/nonexistent-elsewhere', self.link(APIS + '-skill'))
        code, out, err = self.setup_grok('--grok')
        self.assertEqual(code, 0, err)
        self.assertTrue(os.path.isfile(os.path.join(self.link(KNOW + '-skill'), 'mine')))
        self.assertEqual(os.readlink(self.link(APIS + '-skill')), '/nonexistent-elsewhere')
        self.assertEqual(out.count('is not ours'), 2)

    def test_should_refuse_a_grok_folder_that_leads_into_the_forbidden_folder(self):
        for linked in ('.grok', 'skills'):
            if linked == 'skills':
                os.mkdir(os.path.join(self.home, '.grok'))
            os.symlink(self.forbidden, os.path.join(self.home, '.grok') if linked == '.grok' else self.skills)  # dangling
            code, out, err = self.setup_grok('--grok')
            self.assertEqual(code, 1, linked)
            self.assertIn('.gelato-connect', err)
            self.assertEqual(self.api.requests, [], linked)  # refused before sign-in or download
            self.assertFalse(os.path.exists(self.forbidden))
            os.remove(os.path.join(self.home, '.grok') if linked == '.grok' else self.skills)
            if linked == 'skills':
                os.rmdir(os.path.join(self.home, '.grok'))

    def test_should_link_absolute_paths_when_the_home_is_relative(self):
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.tmp)
        os.environ['GELATO_CONNECT_HOME'] = os.path.relpath(self.root, self.tmp)
        code, out, err = self.setup_grok('--grok')
        self.assertEqual(code, 0, err)
        target = os.readlink(self.link(APIS + '-skill'))
        self.assertTrue(os.path.isabs(target))
        self.assertEqual(os.path.realpath(target), os.path.realpath(os.path.join(self.root, 'marketplace', APIS, 'skills', APIS + '-skill')))
        self.assertTrue(os.path.isfile(os.path.join(target, 'SKILL.md')))

    def test_should_refuse_a_skills_folder_or_file_that_leads_into_the_forbidden_folder(self):
        self.assertEqual(self.setup_grok('--grok')[0], 0)
        folder = os.path.join(self.root, 'marketplace', APIS, 'skills')
        for kind in ('folder', 'dir', 'file'):
            with self.subTest(kind):
                if kind == 'folder':
                    os.rename(folder, folder + '.bak')
                    os.symlink(self.forbidden, folder)
                elif kind == 'dir':
                    os.symlink(self.forbidden, os.path.join(folder, 'evil'))
                else:
                    os.makedirs(os.path.join(folder, 'evil'))
                    os.symlink(os.path.join(self.forbidden, 'SKILL.md'), os.path.join(folder, 'evil', 'SKILL.md'))
                with self.assertRaises(setup.SetupError):
                    setup.link_grok(self.root, self.home)
                self.assertFalse(os.path.exists(self.forbidden))
                self.assertFalse(os.path.lexists(self.link('evil')))
                if kind == 'folder':
                    os.remove(folder)
                    os.rename(folder + '.bak', folder)
                else:
                    shutil.rmtree(os.path.join(folder, 'evil'), ignore_errors=True)
                    if os.path.islink(os.path.join(folder, 'evil')):
                        os.remove(os.path.join(folder, 'evil'))

    def test_should_skip_a_symlinked_skill_folder_or_skill_file(self):
        self.assertEqual(self.setup_grok('--grok')[0], 0)
        folder = os.path.join(self.root, 'marketplace', APIS, 'skills')
        other = os.path.join(self.tmp, 'other')
        os.makedirs(other)
        with open(os.path.join(other, 'SKILL.md'), 'w') as f:
            f.write('x')
        os.symlink(other, os.path.join(folder, 'dirlink'))
        os.makedirs(os.path.join(folder, 'filelink'))
        os.symlink(os.path.join(other, 'SKILL.md'), os.path.join(folder, 'filelink', 'SKILL.md'))
        setup.link_grok(self.root, self.home)
        self.assertEqual(sorted(os.listdir(self.skills)), [APIS + '-skill', KNOW + '-skill'])


class GrokIsolationTests(GrokCase):
    def test_should_ignore_a_grok_state_file_that_leads_into_the_forbidden_folder(self):
        os.makedirs(self.root, exist_ok=True)
        os.symlink(os.path.join(self.forbidden, 'grok.json'), os.path.join(self.root, 'grok.json'))  # dangling
        code, out, err = self.setup_grok()
        self.assertEqual(code, 0, err)
        self.assertFalse(os.path.exists(os.path.join(self.home, '.grok')))
        self.update()
        self.assertFalse(os.path.exists(self.forbidden))
        self.assertFalse(os.path.exists(os.path.join(self.home, '.grok')))
        self.assertTrue(os.path.exists(os.path.join(self.root, 'updated.json')))

    def test_should_only_warn_when_the_links_fail_after_a_plain_setup(self):
        self.opt_in()
        os.symlink(self.forbidden, os.path.join(self.home, '.grok'))
        code, out, err = self.setup_grok()
        self.assertEqual(code, 0, err)
        self.assertIn('warning: grok links not updated', err)
        self.assertFalse(os.path.exists(self.forbidden))


class GrokUpdateTests(GrokCase):
    def test_should_link_after_an_update_when_opted_in_and_not_otherwise(self):
        self.update()
        self.assertFalse(os.path.exists(os.path.join(self.home, '.grok')))
        self.opt_in()
        self.update()
        self.assertTrue(os.path.islink(self.link(APIS + '-skill')))

    def test_should_reconcile_renamed_new_and_removed_skills(self):
        self.opt_in()
        self.update()
        # skills we did not make stay; a renamed skill's old link goes, the new name appears, a removed one goes
        os.symlink('/elsewhere', self.link('foreign'))
        old = os.path.join(self.root, 'marketplace', APIS, 'skills', APIS + '-skill')
        ghost = self.link('ghost')
        os.symlink(os.path.join(self.root, 'marketplace', KNOW, 'skills', 'gone'), ghost)  # ours: dangling after a swap
        self.assertTrue(os.path.islink(ghost))
        self.api.close()

        def pkg(name, version, skill):
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, 'w') as z:
                z.writestr('%s/.claude-plugin/plugin.json' % name, json.dumps({'name': name, 'version': version}))
                z.writestr('%s/skills/%s/SKILL.md' % (name, skill), 'x')
            return buf.getvalue()
        self.api = fake_api(bundle=make_bundle({'%s-1.2.0.zip' % APIS: pkg(APIS, '1.2.0', 'renamed-skill'),
                                                '%s-3.4.5.zip' % KNOW: pkg(KNOW, '3.4.5', 'new-skill')}))
        self.addCleanup(self.api.close)
        self.update()
        self.assertEqual(sorted(os.listdir(self.skills)), ['foreign', 'new-skill', 'renamed-skill'])
        self.assertEqual(os.readlink(self.link('foreign')), '/elsewhere')
        self.assertFalse(os.path.lexists(self.link(APIS + '-skill')))  # the renamed skill's old link went
        self.assertFalse(os.path.lexists(ghost))

    def test_should_refuse_a_symlinked_grok_folder_on_update_and_leave_claude_codex_alone(self):
        self.opt_in()
        os.symlink(self.forbidden, os.path.join(self.home, '.grok'))  # dangling: the folder is never created
        self.update()
        self.assertFalse(os.path.exists(self.forbidden))
        self.assertTrue(os.path.islink(os.path.join(self.home, '.codex', 'skills', APIS + '-skill')))  # installer still ran
        self.assertEqual(setup._read_json(os.path.join(self.root, 'updated.json')), {'versions': {APIS: '1.2.0', KNOW: '3.4.5'}})
        self.assertEqual(setup._read_json(os.path.join(self.root, 'installed.json')), {APIS: '1.2.0', KNOW: '3.4.5'})
        os.remove(os.path.join(self.home, '.grok'))  # destination fixed: the next update repairs the links
        self.update()
        self.assertTrue(os.path.islink(self.link(APIS + '-skill')))

    def test_should_refuse_a_symlinked_grok_skills_folder_on_update(self):
        self.opt_in()
        os.mkdir(os.path.join(self.home, '.grok'))
        os.symlink(self.forbidden, self.skills)
        self.update()
        self.assertFalse(os.path.exists(self.forbidden))
        self.assertEqual(os.readlink(self.skills), self.forbidden)

    def test_should_keep_the_installer_targets_independent_of_grok(self):
        self.opt_in()
        os.environ['GELATO_CONNECT_TARGETS'] = 'claude'
        self.update()
        self.assertEqual(setup._stored_targets(self.root), [])
        self.assertEqual(setup.ASSISTANTS, ('claude', 'codex'))


if __name__ == '__main__':
    unittest.main()
