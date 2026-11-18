"""Install or update Gelato Connect skill packages on native Windows (PowerShell, no bash, curl or unzip).

    py -3 -I install_win.py <name> <version> <url> <digest> [<name> <version> <url> <digest> ...]

The Windows twin of install.sh (which stays byte-identical to the one in the skills ZIP): same arguments,
same environment (GELATO_CONNECT_HOME, GELATO_CONNECT_TARGETS, CODEX_HOME, CLAUDE_CONFIG_DIR), same steps,
same output lines. Standard library only. Runs on any OS (tests do); only the venv layout and the Codex links differ.
Never prints a download URL. installed.json is written last, so a failed run retries.
"""
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

MAX_BYTES = 100 * 1024 * 1024
WINDOWS = os.name == 'nt'
LOCAL = re.compile(r'http://(localhost|127\.0\.0\.1)(:[0-9]+)?/')


class Fail(Exception):
    pass


def say(text):
    print(text, flush=True)


def venv_python(venv):
    return os.path.join(venv, 'Scripts', 'python.exe') if WINDOWS else os.path.join(venv, 'bin', 'python')


# --- links (Codex skills folders) ---------------------------------------------
def is_link(path):
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def link_target(path):
    t = os.readlink(path)
    return t[4:] if t.startswith('\\\\?\\') else t  # Windows hands junctions back with a \\?\ prefix


def make_link(target, path):
    if not WINDOWS:
        return os.symlink(target, path)
    # A junction needs no administrator rights or developer mode, unlike a symbolic link.
    p = subprocess.run(['cmd', '/c', 'mklink', '/J', path, target], capture_output=True)
    if p.returncode:
        raise Fail('cannot link %s (a junction needs an NTFS folder)' % os.path.basename(path))


def drop_link(path):
    (os.rmdir if WINDOWS else os.unlink)(path)  # a junction or directory link goes without touching its target


def inside(path, base):
    path, base = (os.path.normcase(os.path.normpath(p)) for p in (path, base))
    return path == base or path.startswith(base + os.sep)


# --- steps ---------------------------------------------------------------------
class _HttpsRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # like curl --proto-redir =https
        return super().redirect_request(req, fp, code, msg, headers, newurl) if newurl.startswith('https://') else None


def download(url, dest):
    # The package links are on this machine: a proxy (from the environment or the registry) must not get them.
    handlers = [_HttpsRedirects()] + ([urllib.request.ProxyHandler({})] if LOCAL.match(url) else [])
    try:
        with urllib.request.build_opener(*handlers).open(urllib.request.Request(url), timeout=300) as r:
            data = r.read(MAX_BYTES + 1)
    except (OSError, ValueError):
        return None
    if len(data) > MAX_BYTES:
        return None
    with open(dest, 'wb') as f:
        f.write(data)
    return data


def unpack(zip_path, dest):
    """Extract into `dest`; refuses links and any member that would land outside it."""
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            target = os.path.join(dest, *info.filename.split('/'))
            if stat.S_ISLNK(info.external_attr >> 16) or os.path.isabs(info.filename) or not inside(target, dest):
                raise Fail('package contains links or unsafe paths, nothing installed')
        z.extractall(dest)


def plugin_ok(path, name, version):
    try:
        with open(path, encoding='utf-8') as f:
            d = json.load(f)
        return d.get('name') == name and d.get('version') == version
    except (OSError, ValueError, AttributeError):
        return False


def read_installed(path):
    try:
        with open(path, encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace')
    return p.returncode, p.stdout


def register_claude(root, names, versions, claude):
    market = os.path.join(root, 'marketplace')
    plugins = sorted(d for d in os.listdir(market) if os.path.isfile(os.path.join(market, d, '.claude-plugin', 'plugin.json')))
    with open(os.path.join(market, '.claude-plugin', 'marketplace.json'), 'w', encoding='utf-8') as f:
        json.dump({'name': 'gelato-connect', 'owner': {'name': 'Gelato'},
                   'plugins': [{'name': p, 'source': './' + p} for p in plugins]}, f, indent=2)
    if re.search(r'^ *[^ ]* *gelato-connect *$', run([claude, 'plugin', 'marketplace', 'list'])[1], re.M):
        if run([claude, 'plugin', 'marketplace', 'update', 'gelato-connect'])[0]:
            return False
        say('claude: marketplace gelato-connect updated')
    else:
        if run([claude, 'plugin', 'marketplace', 'add', market])[0]:
            return False
        say('claude: marketplace gelato-connect added')
    for n, v in zip(names, versions):
        if n + '@gelato-connect' in run([claude, 'plugin', 'list'])[1]:
            if run([claude, 'plugin', 'update', n + '@gelato-connect'])[0]:
                return False
            say('claude: plugin %s updated to %s' % (n, v))
        else:
            if run([claude, 'plugin', 'install', n + '@gelato-connect'])[0]:
                return False
            say('claude: plugin %s %s installed' % (n, v))
    return True


def register_codex(root, names):
    skills = os.path.join(os.environ.get('CODEX_HOME') or os.path.join(os.path.expanduser('~'), '.codex'), 'skills')
    os.makedirs(skills, exist_ok=True)
    ours = os.path.join(root, 'marketplace')
    for n in names:
        base = os.path.join(ours, n, 'skills')
        for entry in sorted(os.listdir(skills)):  # remove our own stale links (target gone after the folder swap)
            link = os.path.join(skills, entry)
            if is_link(link) and inside(link_target(link), base) and not os.path.exists(link):
                drop_link(link)
                say('codex: removed stale skill %s' % entry)
        for s in sorted(os.listdir(base)):
            src, link = os.path.join(base, s), os.path.join(skills, s)
            if not os.path.isfile(os.path.join(src, 'SKILL.md')):
                continue
            if is_link(link):
                if not inside(link_target(link), ours):
                    say('codex: skipped %s: existing link is not ours' % s)
                    continue
                drop_link(link)
            elif os.path.lexists(link):
                say('codex: skipped %s: existing entry is not ours' % s)
                continue
            make_link(src, link)
        say('codex: skills of %s linked in %s' % (n, skills))


def parse(args):
    if len(args) < 4 or len(args) % 4:
        raise Fail('usage: install_win.py <name> <version> <url> <digest> [<name> <version> <url> <digest> ...]')
    pkgs = []
    for name, version, url, digest in (args[i:i + 4] for i in range(0, len(args), 4)):
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]*', name):
            raise Fail('invalid package name')
        if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+([-+][0-9A-Za-z.-]+)?', version):
            raise Fail('invalid version for ' + name)
        if not (url.startswith('https://') or LOCAL.match(url)):  # plain http only for a server on this machine
            raise Fail('invalid url for ' + name)
        digest = digest[len('sha256:'):] if digest.startswith('sha256:') else digest
        if not re.fullmatch(r'[0-9a-fA-F]{64}', digest):
            raise Fail('invalid digest for %s (need 64 hex characters)' % name)
        pkgs.append((name, version, url, digest.lower()))
    return pkgs


def find(tool):
    """The tool on PATH, else Claude Code's own install folder: its Windows installer does not always reach PATH."""
    if shutil.which(tool):
        return shutil.which(tool)
    native = os.path.join(os.path.expanduser('~'), '.local', 'bin', tool + ('.exe' if WINDOWS else ''))
    return native if tool == 'claude' and os.path.isfile(native) else None


def targets():
    wanted = os.environ.get('GELATO_CONNECT_TARGETS')
    if not wanted:
        return [t for t in ('claude', 'codex') if find(t)]
    out = []
    for t in wanted.replace(',', ' ').split():
        if t == 'claude' and not find('claude'):
            raise Fail("target claude requested but 'claude' is not on PATH")
        if t not in ('claude', 'codex'):
            raise Fail("unknown target '%s' (use claude, codex)" % t)
        out.append(t)
    return out


def install(args):
    pkgs = parse(args)
    if sys.version_info < (3, 9):
        raise Fail('Python 3.9 or newer is required')
    chosen = targets()
    root = os.path.abspath(os.environ.get('GELATO_CONNECT_HOME') or os.path.join(os.path.expanduser('~'), '.gelato-connect-skills'))
    os.makedirs(root, exist_ok=True)
    installed_path, venv = os.path.join(root, 'installed.json'), os.path.join(root, 'venv')
    try:  # one install at a time; released on exit, success or not
        os.mkdir(os.path.join(root, '.lock'))
    except OSError:
        raise Fail('another install is running (remove %s if not)' % os.path.join(root, '.lock')) from None
    tmp = tempfile.mkdtemp()
    try:
        done = read_installed(installed_path)
        todo = []
        for n, v, _, _ in pkgs:  # current only while its files are still there: a deleted folder or venv is rebuilt
            if done.get(n) == v and os.path.isfile(os.path.join(root, 'marketplace', n, '.claude-plugin', 'plugin.json')) \
                    and os.path.isfile(venv_python(venv)):
                say('up to date: %s %s' % (n, v))
            else:
                todo.append((n, v))
        urls = {p[0]: p for p in pkgs}
        for n, v in todo:  # download, verify and unpack everything before touching the install
            _, _, url, digest = urls[n]
            data = download(url, os.path.join(tmp, n + '.zip'))
            if data is None:
                raise Fail('download failed for ' + n)
            if hashlib.sha256(data).hexdigest() != digest:
                raise Fail('digest mismatch for %s, nothing installed' % n)
            say('downloaded and verified: %s %s' % (n, v))
            try:
                unpack(os.path.join(tmp, n + '.zip'), os.path.join(tmp, 'x-' + n))
            except (zipfile.BadZipFile, OSError):
                raise Fail('cannot unzip ' + n) from None
            if not plugin_ok(os.path.join(tmp, 'x-' + n, n, '.claude-plugin', 'plugin.json'), n, v):
                raise Fail('package %s does not contain plugin %s %s' % (n, n, v))
        # ponytail: an existing venv is not re-checked; delete it to rebuild on another Python.
        if todo and not os.path.isfile(venv_python(venv)):
            if subprocess.run([sys.executable, '-m', 'venv', venv], capture_output=True).returncode:
                shutil.rmtree(venv, ignore_errors=True)  # a half-made venv would pass the check above on the next run
                raise Fail('cannot create venv')
            say('created venv: ' + venv)
        for n, v in todo:
            try:
                with open(os.path.join(tmp, 'x-' + n, n, 'pyproject.toml'), encoding='utf-8') as f:
                    text = f.read()
            except OSError:
                text = ''
            if re.search(r'^dependencies *= *\[', text, re.M) and not re.search(r'^dependencies *= *\[ *\]', text, re.M):
                if subprocess.run([venv_python(venv), '-m', 'pip', 'install', '-q', '--no-cache-dir', os.path.join(tmp, n + '.zip')],
                                  capture_output=True).returncode:
                    raise Fail('pip install failed for ' + n)
                say('installed python dependencies: ' + n)
        os.makedirs(os.path.join(root, 'marketplace', '.claude-plugin'), exist_ok=True)
        for n, v in todo:
            shutil.rmtree(os.path.join(root, 'marketplace', n), ignore_errors=True)
            shutil.move(os.path.join(tmp, 'x-' + n, n), os.path.join(root, 'marketplace', n))
            say('unpacked: %s %s' % (n, v))
        failed = False
        for t in chosen:
            if t == 'claude' and not register_claude(root, [p[0] for p in pkgs], [p[1] for p in pkgs], find('claude')):
                failed = True
                say('claude: marketplace registration failed; start Claude Code with:')
                for p in pkgs:
                    say('  claude --plugin-dir %s' % os.path.join(root, 'marketplace', p[0]))
            elif t == 'codex':
                register_codex(root, [p[0] for p in pkgs])
        if failed:  # no installed.json, so the next run retries
            return 1
        if not chosen:
            say('no assistant found (claude, codex); files are in %s' % os.path.join(root, 'marketplace'))
        done.update({n: v for n, v, _, _ in pkgs})
        with open(installed_path + '.tmp', 'w', encoding='utf-8') as f:
            json.dump(done, f, indent=2, sort_keys=True)
        os.replace(installed_path + '.tmp', installed_path)
        say('python: ' + venv_python(venv))
        say('installed: ' + ''.join('%s %s; ' % (n, v) for n, v, _, _ in pkgs))
        if todo:
            say('restart your assistant to load the new version')
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            os.rmdir(os.path.join(root, '.lock'))
        except OSError:
            pass


def main(argv):
    try:
        return install(argv)
    except Fail as e:
        print('error: %s' % e, file=sys.stderr)
        return 1
    except (OSError, subprocess.SubprocessError, zipfile.BadZipFile) as e:
        print('error: %s' % type(e).__name__, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
