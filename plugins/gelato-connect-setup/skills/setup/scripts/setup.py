"""Sign in to Gelato Connect, download the account's skills packages, run install.sh on them.

Only the bootstrap is public. Nothing from the server is executed except the
packages that install.sh unpacks (served to it from this machine, digest-checked). Never prints tokens or download links.
"""
import base64
import contextlib
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zipfile
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CLIENT_ID = 'gelato-connect-local-ai-assistant'
BASE = 'https://auth.gelato.com/auth/realms/gelato-api/protocol/openid-connect'
CALLBACK_PORTS = (39201, 39202, 39203, 39204, 39205)
API_ORIGIN = 'https://api.gelatoconnect.gelato.com'  # public API: GET /skills, /skills/download/<token>, /skills/versions
OLD_ORIGIN = 'https://[removed]'  # used only while the public routes answer 404; remove with _fetch_old
MAX_BYTES = 50 * 1024 * 1024
ENTRY = re.compile(r'(gelato-connect-apis|gelato-connect-product-knowledge)-([0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4})\.zip')
# Sign-in scope. Add ' offline_access' once the Keycloak client allows it (today it refuses it and sign-in breaks):
# the stored token is then an offline token that lasts weeks instead of days. Nothing else depends on it.
SCOPE = 'openid profile email'
NAMES = ('gelato-connect-apis', 'gelato-connect-product-knowledge')
VERSION = re.compile(r'[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}')
LINK = re.compile(r'/skills/download/[0-9a-f]{64}')
DIGEST = re.compile(r'sha256:[0-9a-f]{64}')
USER_AGENT = 'gelato-connect-setup/0.1'  # auth.gelato.com's Cloudflare answers 403 to urllib's default
ASSISTANTS = ('claude', 'codex')
FORBIDDEN = '.gelato-connect'  # never touched; the install folder is .gelato-connect-skills
WINDOWS = os.name == 'nt'  # native Windows: no bash, symlinks, O_NOFOLLOW, dir_fd, SIGALRM or uid


class SetupError(Exception):
    status = None  # the HTTP status of a failed token request, if there was one


# --- path guard -------------------------------------------------------------
def _parts(p):
    if WINDOWS:
        p = p[4:] if p.startswith('\\\\?\\') else p
        return tuple(c.casefold() for c in re.split(r'[\\/]', p) if c)
    return tuple(c.casefold() for c in p.split('/') if c)


def refuse_forbidden(path, home):
    """Refuse `path` if it is, or resolves into, <home>/.gelato-connect, without touching that folder."""
    banned = {_parts(os.path.join(h, FORBIDDEN)) for h in (os.path.abspath(home), os.path.realpath(home))}

    def bad(p):
        q = _parts(p)
        return any(q[:len(b)] == b for b in banned)

    def refuse():
        raise SetupError('error: a configured folder is inside ~/%s, which this tool never touches' % FORBIDDEN)

    if WINDOWS:  # ponytail: no hand-walk of links here (no bash `cd` reading of '..'); the spelled and the resolved path are checked
        if any(bad(p) for p in (os.path.abspath(path), os.path.realpath(path))):
            refuse()
        return
    path = os.path.join(os.getcwd(), path)
    # Both readings of '..': the OS applies it after symlinks, bash's `cd` (install.sh) before them.
    for start in (path, os.path.normpath(path)):
        if bad(start):
            refuse()
        todo, cur, hops = start.split('/'), '/', 0
        while todo:  # resolve by hand: check every prefix BEFORE the first filesystem call on it
            comp = todo.pop(0)
            if comp in ('', '.'):
                continue
            if comp == '..':
                cur = os.path.dirname(cur)
                continue
            cand = os.path.join(cur, comp)
            if bad(cand):
                refuse()
            try:
                link = stat.S_ISLNK(os.lstat(cand).st_mode)
            except OSError:
                link = False
            if not link:
                cur = cand
                continue
            hops += 1
            if hops > 40:
                raise SetupError('error: too many symbolic links in a configured folder')
            target = os.readlink(cand)
            if target.startswith('/'):
                cur = '/'
            todo = target.split('/') + todo


# --- API origin -------------------------------------------------------------
def api_origin(value):
    """The live origin, or http://localhost[:port] / http://127.0.0.1[:port] for local testing."""
    if not value:
        return API_ORIGIN
    try:
        s = urllib.parse.urlsplit(value)
        s.port
    except ValueError:
        raise SetupError('error: GELATO_CONNECT_SKILLS_HOST is not an allowed address') from None
    live = s.scheme == 'https' and s.netloc == API_ORIGIN[len('https://'):]
    local = s.scheme == 'http' and re.fullmatch(r'(localhost|127\.0\.0\.1)(:[0-9]{1,5})?', s.netloc)
    if not (live or local) or s.path not in ('', '/') or '?' in value or '#' in value:
        raise SetupError('error: GELATO_CONNECT_SKILLS_HOST is not an allowed address')
    return '%s://%s' % (s.scheme, s.netloc)


# --- HTTP -------------------------------------------------------------------
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def exchange_token(url, form, opener=None):
    """POST the authorization-code exchange; the target must be the literal token endpoint."""
    if url != BASE + '/token':
        raise SetupError('error: unknown sign-in target')
    req = urllib.request.Request(url, data=urllib.parse.urlencode(form).encode(),
                                 headers={'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json',
                                          'User-Agent': USER_AGENT})
    try:
        with (opener or urllib.request.build_opener(NoRedirect())).open(req, timeout=30) as r:
            if r.status != 200:
                raise ValueError()
            value = json.loads(r.read(1_000_001))
            if not isinstance(value, dict):
                raise ValueError()
            return value
    except (OSError, ValueError) as e:  # HTTPError is an OSError
        err = SetupError('error: sign-in failed; check your connection and try again')
        err.status = getattr(e, 'code', None)
        raise err from None


# --- sign-in ----------------------------------------------------------------
def sign_in(*, timeout=240, browser_open=None, fetch=None, server_factory=ThreadingHTTPServer, session=None):
    """Browser sign-in (authorization code + PKCE). Returns the access token.

    If `session` is a dict it receives the refresh token (the caller decides whether to keep it); nothing else is kept."""
    verifier, state = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    done, lock, result, bound = threading.Event(), threading.Lock(), {}, {}

    class Handler(BaseHTTPRequestHandler):
        timeout = 5

        def log_message(self, *args):
            pass  # the request line holds the one-use authorization code

        def do_GET(self):
            u = urllib.parse.urlsplit(self.path)
            q = urllib.parse.parse_qs(u.query, keep_blank_values=True)
            ok = (self.path.startswith('/') and u.path == '/oauth/callback'
                  and self.headers.get_all('Host') == ['localhost:%d' % bound['port']]
                  and len(q.get('state', [])) == 1 and q['state'][0].isascii()
                  and hmac.compare_digest(q['state'][0], state))
            with lock:
                if not ok or done.is_set():
                    status, text = 400, 'Invalid or expired sign-in callback.'
                elif 'error' in q:
                    result['cancelled'] = True
                    status, text = 200, 'Sign-in cancelled. Return to your assistant.'
                    done.set()
                elif len(q.get('code', [])) == 1 and q['code'][0]:
                    result['code'] = q['code'][0]
                    status, text = 200, 'Sign-in received. Return to your assistant.'
                    done.set()
                else:
                    status, text = 400, 'Invalid sign-in callback.'
            if status == 200:  # take the one-use code and state out of the address bar
                text = ('<!doctype html><meta charset="utf-8"><title>Gelato Connect sign-in</title><p>%s</p>'
                        '<script>try{history.replaceState(null,"",location.pathname)}catch(e){}</script>' % text)
            body = text.encode()
            self.send_response(status)
            self.send_header('Content-Type', ('text/html' if status == 200 else 'text/plain') + '; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = None
    for port in CALLBACK_PORTS:
        try:
            server = server_factory(('127.0.0.1', port), Handler)
            bound['port'] = port
            break
        except OSError:
            continue
    if server is None:
        raise SetupError('error: sign-in ports 39201-39205 are busy; close other sign-ins and retry (nothing was stopped)')
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.1}, daemon=True)
    thread.start()
    redirect = 'http://localhost:%d/oauth/callback' % bound['port']
    url = BASE + '/auth?' + urllib.parse.urlencode({
        'response_type': 'code', 'client_id': CLIENT_ID, 'redirect_uri': redirect, 'scope': SCOPE,
        'state': state, 'code_challenge': challenge, 'code_challenge_method': 'S256'})
    try:
        deadline = time.monotonic() + timeout
        opened = []

        def launch():
            try:
                opened.append((browser_open or webbrowser.open)(url))
            except Exception:
                pass
        launcher = threading.Thread(target=launch, daemon=True)
        launcher.start()
        launcher.join(10)  # a launcher that hangs counts as "could not open"
        if not (opened and opened[0]):
            print('Open this address in a browser to sign in:\n%s' % url, flush=True)
        while not done.wait(0.1):
            if time.monotonic() >= deadline:
                raise SetupError('error: sign-in timed out; run the setup again')
        if result.get('cancelled'):
            raise SetupError('error: sign-in was cancelled')
        reply = (fetch or exchange_token)(BASE + '/token', {'grant_type': 'authorization_code', 'client_id': CLIENT_ID,
                                        'code': result['code'], 'redirect_uri': redirect, 'code_verifier': verifier})
        token = reply.get('access_token')
        if str(reply.get('token_type', '')).lower() != 'bearer' or not isinstance(token, str) or not token:
            raise SetupError('error: sign-in returned an unexpected reply')
        if isinstance(session, dict) and isinstance(reply.get('refresh_token'), str) and reply['refresh_token']:
            session['refresh_token'] = reply['refresh_token']
        return token
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


# --- skills bundle ----------------------------------------------------------
def _fail():
    raise SetupError('error: could not get the skills; sign in again or ask your Gelato contact for access')


def _get(origin, path, token, limit, opener):
    """GET with the bearer token (none if `token` is None). Returns the body, or None on 404; any other failure (a redirect too) is an error."""
    headers = {'Accept': '*/*', 'User-Agent': USER_AGENT}
    if token is not None:
        headers['Authorization'] = 'Bearer ' + token
    req = urllib.request.Request(origin + path, headers=headers)
    try:
        with opener.open(req, timeout=60) as r:
            raw = r.read(limit + 1)
            if r.status != 200 or len(raw) > limit:
                raise ValueError()
            return raw
    except urllib.error.HTTPError as e:
        if e.code != 404:
            _fail()
    except (OSError, ValueError):
        _fail()


def _data(raw):
    value = json.loads(raw)
    return value['data'] if isinstance(value.get('data'), dict) else value


def _fetch_public(origin, token, opener):
    """Public API: GET /skills lists the two packages, each with a 5-minute link and the server's sha256.

    Returns them as one bundle (the shape unpack_bundle takes), or None while the route is not there yet (404)."""
    raw = _get(origin, '/skills', token, 1_000_000, opener)
    if raw is None:
        return None
    try:
        items = json.loads(raw)
        if not isinstance(items, list) or sorted(i['name'] for i in items) != sorted(NAMES):
            raise ValueError()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as z:
            for i in items:
                path = i['url'][len(origin):]
                if not (VERSION.fullmatch(i['version']) and DIGEST.fullmatch(i['digest'])
                        and i['url'].startswith(origin) and LINK.fullmatch(path)):  # links only to the host we asked
                    raise ValueError()
                data = _get(origin, path, None, MAX_BYTES, opener)  # the link is the access: no token on it
                if data is None or 'sha256:' + hashlib.sha256(data).hexdigest() != i['digest']:
                    raise ValueError()
                z.writestr('%s-%s.zip' % (i['name'], i['version']), data)
        return buf.getvalue()
    except (ValueError, KeyError, TypeError):
        _fail()


def fetch_bundle(origin, token, opener=None):
    """The account's two skills packages as one ZIP of ZIPs: from the public API, else the older GNX download."""
    opener = opener or urllib.request.build_opener(NoRedirect())
    raw = _fetch_public(origin, token, opener)
    return raw if raw is not None else _fetch_old(OLD_ORIGIN if origin == API_ORIGIN else origin, token, opener)


def _fetch_old(origin, token, opener):
    """The GNX bundle download. Remove (with OLD_ORIGIN) once the public routes are live."""

    def download(uid):
        if not isinstance(uid, str) or not re.fullmatch(r'[0-9A-Za-z_-]{1,100}', uid):
            return None  # a Gelato admin account's own uid is a one-character placeholder (seen live, 13 Nov)
        return _get(origin, '/print-partners/%s/connect-skills/download' % uid, token, MAX_BYTES, opener)

    try:
        raw = download(_data(_get(origin, '/account', token, 1_000_000, opener))['user']['printHouseUid'])
        if raw is None:  # an admin account: its own uid is not a print house, so try the first listed one
            raw = download(_data(_get(origin, '/print-partners', token, 1_000_000, opener))['printHouses'][0]['uid'])
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        _fail()
    return raw or _fail()


def unpack_bundle(raw, dest):
    """Write the two package ZIPs into `dest`; returns [{name, version, digest}]. Anything unexpected is an error."""
    found = {}
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            for info in z.infolist():
                m = ENTRY.fullmatch(info.filename)
                if not m or m[1] in found or info.file_size > MAX_BYTES:
                    raise ValueError()
                with z.open(info) as f:
                    data = f.read(MAX_BYTES + 1)  # the declared size can lie: cap what is really read
                if len(data) > MAX_BYTES:
                    raise ValueError()
                with open(os.path.join(dest, m[1] + '.zip'), 'wb') as f:
                    f.write(data)
                found[m[1]] = {'name': m[1], 'version': m[2], 'digest': 'sha256:' + hashlib.sha256(data).hexdigest()}
    except (OSError, ValueError, RuntimeError, EOFError, zlib.error, zipfile.BadZipFile):
        _fail()
    return list(found.values()) if len(found) == 2 else _fail()


# --- installer --------------------------------------------------------------
def run_installer(skills, token, run=subprocess.run, targets=None):
    here = os.path.dirname(os.path.abspath(__file__))
    # Windows has no bash, curl or unzip: install_win.py is the same installer in Python (utf8: paths may hold any character).
    cmd = ([sys.executable, '-I', '-X', 'utf8', os.path.join(here, 'install_win.py')] if WINDOWS
           else ['bash', os.path.join(here, 'install.sh')]) + [s[k] for s in skills for k in ('name', 'version', 'url', 'digest')]
    try:
        # the package links are on this machine: a proxy from the environment must not get them
        env = dict(os.environ, NO_PROXY='localhost,127.0.0.1', no_proxy='localhost,127.0.0.1')
        if targets:
            env['GELATO_CONNECT_TARGETS'] = targets
        p = run(cmd, capture_output=True, text=True, timeout=1800, env=env, **({'encoding': 'utf-8', 'errors': 'replace'} if WINDOWS else {}))
    except (OSError, subprocess.TimeoutExpired):
        raise SetupError('error: the installer could not run to the end') from None

    def clean(text):
        for secret in [token] + [s['url'] for s in skills]:
            text = text.replace(secret, '[redacted]')
        return text

    sys.stdout.write(clean(p.stdout or ''))
    if p.returncode:
        sys.stderr.write(clean(p.stderr or ''))
    return p.returncode


def install_bundle(raw, token, run=subprocess.run, targets=None):
    """Unpack the bundle and hand install.sh the two packages from a short-lived server on this machine."""
    with tempfile.TemporaryDirectory() as tmp:
        skills = unpack_bundle(raw, tmp)
        secret = secrets.token_urlsafe(16)  # nothing else on this machine can guess the paths
        allowed = {'/%s/%s.zip' % (secret, s['name']): os.path.join(tmp, s['name'] + '.zip') for s in skills}

        class Handler(BaseHTTPRequestHandler):
            timeout = 30

            def log_message(self, *args):
                pass

            def do_GET(self):
                path = allowed.get(self.path)
                if path is None:
                    self.send_error(404)
                    return
                with open(path, 'rb') as f:
                    body = f.read()
                self.send_response(200)
                self.send_header('Content-Type', 'application/zip')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.1}, daemon=True)
        thread.start()
        try:
            base = 'http://localhost:%d/%s/' % (server.server_address[1], secret)
            return run_installer([dict(s, url='%s%s.zip' % (base, s['name'])) for s in skills], token, run, targets)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


# --- stored sign-in (session.json) and automatic update ----------------------
def _is_link(st):
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _read_json(path, private=False):
    """A small JSON object from a regular file, never through a symlink; `private`: ours and mode 0600 only (on Windows the
    folder's own permissions apply). Else None."""
    if WINDOWS and _is_link(os.lstat(path)):  # no O_NOFOLLOW there
        return None
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(fd, 'rb') as fh:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > 16 * 1024:
            return None
        if private and not WINDOWS and (st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o600):
            return None
        value = json.loads(fh.read(16 * 1024 + 1).decode('utf-8'))
    return value if isinstance(value, dict) else None


def _write_private(root, name, value):
    """Atomic 0600 write of a JSON file in `root`: temp file created exclusively in the pinned folder, then renamed."""
    if WINDOWS:  # no dir_fd or O_NOFOLLOW: refuse a linked folder, create exclusively beside the target, rename
        if _is_link(os.lstat(root)):
            raise OSError('linked folder')
        tmp = os.path.join(root, '%s.%d.tmp' % (name, os.getpid()))
        try:
            with open(tmp, 'x') as fh:
                json.dump(value, fh)
            os.replace(tmp, os.path.join(root, name))
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.remove(tmp)
        return
    dirfd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    tmp = '%s.%d.tmp' % (name, os.getpid())
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dirfd)
        try:
            with os.fdopen(fd, 'w') as fh:
                json.dump(value, fh)
            os.replace(tmp, name, src_dir_fd=dirfd, dst_dir_fd=dirfd)
        finally:
            try:
                os.remove(tmp, dir_fd=dirfd)
            except FileNotFoundError:
                pass
    finally:
        os.close(dirfd)


def _remove(root, name):
    try:
        os.remove(os.path.join(root, name))  # unlinks a planted link itself, never its target
    except OSError:
        pass


def _words(value):
    """The assistant names in a comma or space list, or [] if there is none or any is unknown."""
    words = str(value).replace(',', ' ').split()
    return words if words and all(w in ASSISTANTS for w in words) else []


def _stored_targets(root):
    try:
        return _words((_read_json(os.path.join(root, 'targets'), private=True) or {}).get('targets', ''))
    except (OSError, ValueError):
        return []  # none or corrupt: install.sh then detects what is on PATH


def _stored_for_installer(root):
    """What to pass install.sh as GELATO_CONNECT_TARGETS when the caller set none: the assistants setup ran in so far."""
    return None if os.environ.get('GELATO_CONNECT_TARGETS') else ','.join(_stored_targets(root)) or None


def keep_targets(root, requested):
    """After a good setup with explicit targets: remember them, joined with earlier ones, for the background update."""
    try:
        mine = _words(requested)
        if mine:
            _write_private(root, 'targets', {'targets': ','.join(sorted(set(mine) | set(_stored_targets(root))))})
    except OSError:
        pass  # the update then registers what it did before


def _folders(home):
    """The install folder; refuses it and everything the installer opens if any is, or leads into, ~/.gelato-connect."""
    root = os.environ.get('GELATO_CONNECT_HOME') or os.path.join(home, '.gelato-connect-skills')
    codex = os.environ.get('CODEX_HOME') or os.path.join(home, '.codex')
    # The folders, and what the installer opens or writes in them (a planted link must not lead there).
    # ponytail: links deeper inside an existing install are not walked; the installer replaces those folders.
    for folder in [root, codex, os.environ.get('CLAUDE_CONFIG_DIR') or os.path.join(home, '.claude'),
                   os.path.join(codex, 'skills')] + [os.path.join(root, n) for n in (
                       'installed.json', 'installed.json.tmp', 'targets', 'marketplace', 'marketplace/.claude-plugin',
                       'marketplace/.claude-plugin/marketplace.json', 'venv', 'venv/bin', 'venv/bin/python', 'venv/Scripts',
                       'venv/Scripts/python.exe', '.lock',
                       'session.json', 'updated.json')] + [
                           os.environ[v] for v in ('TMPDIR', 'TEMP', 'TMP') if os.environ.get(v)]:  # temp files
        refuse_forbidden(folder, home)
    return root


def _versions(data):
    """{name: (x, y, z)} for the known names that hold a strict x.y.z string."""
    data = data if isinstance(data, dict) else {}
    return {n: tuple(int(x) for x in data[n].split('.')) for n in NAMES
            if isinstance(data.get(n), str) and VERSION.fullmatch(data[n])}


def keep_session(root, refresh_token):
    """After a good setup: keep the renewable sign-in for the background updater, unless the user opted out."""
    try:
        if os.environ.get('GELATO_CONNECT_NO_AUTO_UPDATE'):
            _remove(root, 'session.json')
        elif refresh_token:
            _write_private(root, 'session.json', {'refresh_token': refresh_token})
    except OSError:
        pass  # the update then stays a notice; setup itself succeeded


def _hard_stop(seconds):
    if hasattr(signal, 'SIGALRM'):
        signal.signal(signal.SIGALRM, lambda *a: os._exit(0))
        signal.alarm(seconds)
    else:  # Windows
        t = threading.Timer(seconds, os._exit, (0,))
        t.daemon = True  # must not keep a finished run alive
        t.start()


def auto(run=subprocess.run, fetcher=fetch_bundle):
    """Background update: renew the stored sign-in, install a newer published version. Silent, never opens a browser, exits 0."""
    _hard_stop(300)
    try:
        home = os.path.expanduser('~')
        root = _folders(home)
        origin = api_origin(os.environ.get('GELATO_CONNECT_SKILLS_HOST'))
        if os.environ.get('GELATO_CONNECT_NO_AUTO_UPDATE'):
            return 0
        if os.path.islink(root):  # the rotated token could not be stored there: renewing would lose the sign-in
            return 0

        def held():
            try:
                return (_read_json(os.path.join(root, 'session.json'), private=True) or {}).get('refresh_token')
            except (OSError, ValueError):
                return None
        stored = held()
        if not isinstance(stored, str) or not stored:
            return 0
        try:
            reply = exchange_token(BASE + '/token', {'grant_type': 'refresh_token', 'client_id': CLIENT_ID, 'refresh_token': stored})
        except SetupError as e:
            # Expired or revoked: the user runs setup again. Not when another run renewed it meanwhile.
            if e.status in (400, 401) and held() == stored:
                _remove(root, 'session.json')
            return 0
        new = reply.get('refresh_token')  # not rotated by every server: then the stored one stays
        if isinstance(new, str) and new:
            _write_private(root, 'session.json', {'refresh_token': new})  # first: the old one may stop working
        token = reply.get('access_token')
        if str(reply.get('token_type', '')).lower() != 'bearer' or not isinstance(token, str) or not token:
            return 0
        old = _versions(_read_json(os.path.join(root, 'installed.json')))
        latest = _read_json(os.path.join(root, 'latest.json')) or {}
        latest = _versions(latest.get('versions'))
        if not any(n in old and v > old[n] for n, v in latest.items()):
            return 0  # the renewal alone keeps the sign-in alive
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            if install_bundle(fetcher(origin, token), token, run, _stored_for_installer(root)):
                return 0
        now = _versions(_read_json(os.path.join(root, 'installed.json')))
        changed = {n: '.'.join(map(str, v)) for n, v in now.items() if v != old.get(n)}
        if changed:
            _write_private(root, 'updated.json', {'versions': changed})
    except BaseException:
        pass
    return 0


def main(argv=None, signin=sign_in, fetcher=fetch_bundle, run=subprocess.run):
    if argv == ['--auto']:
        return auto(run, fetcher)
    try:
        if argv:
            raise SetupError('usage: setup.sh (no arguments)')
        home = os.path.expanduser('~')
        root = _folders(home)
        origin = api_origin(os.environ.get('GELATO_CONNECT_SKILLS_HOST'))
        if os.environ.get('GELATO_CONNECT_NO_AUTO_UPDATE'):
            _remove(root, 'session.json')  # opted out: no stored sign-in, whatever happens next
        session = {}
        token = signin(session=session) if signin is sign_in else signin()
        code = install_bundle(fetcher(origin, token), token, run, _stored_for_installer(root))
        if code == 0:
            keep_session(root, session.get('refresh_token'))
            keep_targets(root, os.environ.get('GELATO_CONNECT_TARGETS', ''))
        return code
    except SetupError as e:
        print(e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('error: interrupted', file=sys.stderr)
        return 1
    except Exception:
        print('error: setup failed unexpectedly', file=sys.stderr)
        return 1


if __name__ == '__main__':
    if WINDOWS:  # a console code page that cannot show a character must not crash the run
        for stream in (sys.stdout, sys.stderr):
            stream.reconfigure(errors='replace')
    sys.exit(main(sys.argv[1:]))
