"""SessionStart hook: print one fixed line if a newer skills version is known.

The hook itself is offline, no token. It reads two small local files as untrusted
input and prints only validated version strings. At most once a day it starts a
detached `notice.py --check`, which asks the server which versions are published
(no token, no personal data) and writes latest.json. Always exits 0.
"""
import contextlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

NAMES = (("gelato-connect-apis", "APIs"), ("gelato-connect-product-knowledge", "Product Knowledge"))
VERSION = re.compile(r"\d{1,4}\.\d{1,4}\.\d{1,4}")
MAX = 64 * 1024
DAY = 24 * 3600
API_ORIGIN = "https://api.gelatoconnect.gelato.com"  # same as setup.py (not importable under -I); keep in sync
LOCAL = re.compile(r"(localhost|127\.0\.0\.1)(:[0-9]{1,5})?")
WINDOWS = os.name == "nt"  # no bash, O_NOFOLLOW, dir_fd, SIGALRM or execv there; same switch as setup.py
NOFOLLOW, NONBLOCK = getattr(os, "O_NOFOLLOW", 0), getattr(os, "O_NONBLOCK", 0)
_timer = None


def parts(path):
    if WINDOWS:
        path = path[4:] if path.startswith("\\\\?\\") else path
        return [p.casefold() for p in re.split(r"[\\/]", path) if p]
    return [p.casefold() for p in path.split("/") if p]


def is_link(st):
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def hard_stop(seconds):
    """Kill this process after `seconds`: the socket timeout alone does not bound a slow-drip response."""
    global _timer
    if hasattr(signal, "SIGALRM"):
        signal.alarm(seconds)
    else:  # Windows
        _timer = threading.Timer(seconds, os._exit, (0,))
        _timer.daemon = True
        _timer.start()


def under_forbidden(root):
    """True if root is, or resolves into, ~/.gelato-connect. Same rule as setup.py: that folder is never touched."""
    home = os.path.expanduser("~")  # realpath of home only, never of the folder
    banned = [parts(os.path.join(h, ".gelato-connect")) for h in (os.path.abspath(home), os.path.realpath(home))]

    def bad(p):
        return any(parts(p)[: len(b)] == b for b in banned)

    if WINDOWS:  # ponytail: no hand-walk of links here; the spelled and the resolved path are checked
        return any(bad(p) for p in (os.path.abspath(root), os.path.realpath(root)))
    todo, cur, hops = os.path.join(os.getcwd(), root).split("/"), "/", 0  # no normpath: ".." applies after symlinks
    while todo:  # resolve by hand: check every prefix before the first filesystem call on it
        comp = todo.pop(0)
        if comp in ("", "."):
            continue
        if comp == "..":
            cur = os.path.dirname(cur)
            continue
        cand = os.path.join(cur, comp)
        if bad(cand):
            return True
        try:
            link = stat.S_ISLNK(os.lstat(cand).st_mode)
        except OSError:
            link = False
        if not link:
            cur = cand
            continue
        hops += 1
        if hops > 40:
            return True
        target = os.readlink(cand)
        if target.startswith("/"):
            cur = "/"
        todo = target.split("/") + todo
    return False


def load(path):
    if WINDOWS and is_link(os.lstat(path)):  # no O_NOFOLLOW there
        return None
    fd = os.open(path, os.O_RDONLY | NOFOLLOW | NONBLOCK)  # no symlink, never blocks on a pipe
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fd)  # the file we opened, not one swapped in after a check
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX:
            return None
        raw = fh.read(MAX + 1)
    data = json.loads(raw.decode("utf-8")) if len(raw) <= MAX else None
    return data if isinstance(data, dict) else None


def ver(value):
    if isinstance(value, str) and VERSION.fullmatch(value):
        return tuple(int(x) for x in value.split("."))
    return None


def origin():
    """Same rule as setup.py api_origin: the live origin, or http://localhost / 127.0.0.1 for tests."""
    value = os.environ.get("GELATO_CONNECT_SKILLS_HOST")
    if not value:
        return API_ORIGIN
    s = urllib.parse.urlsplit(value)
    live = s.scheme == "https" and s.netloc == API_ORIGIN[len("https://"):]
    local = s.scheme == "http" and LOCAL.fullmatch(s.netloc)
    if not (live or local) or s.path not in ("", "/") or "?" in value or "#" in value:
        raise ValueError("host not allowed")
    s.port  # raises on a bad port
    return "%s://%s" % (s.scheme, s.netloc)


def due(root):
    """True (and the marker is touched first) if the last check is over a day old. Never follows a symlink."""
    marker = os.path.join(root, "update-check")
    try:
        st = os.lstat(marker)
        if not stat.S_ISREG(st.st_mode) or time.time() - st.st_mtime < DAY:
            return False
    except FileNotFoundError:
        pass
    fd = os.open(marker, os.O_WRONLY | os.O_CREAT | NOFOLLOW | NONBLOCK, 0o600)
    try:
        os.utime(marker if WINDOWS else fd)  # utime on a descriptor is not available on Windows
    finally:
        os.close(fd)
    return True


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def check(root):
    hard_stop(30)
    if WINDOWS and is_link(os.lstat(root)):
        return
    dirfd = None if WINDOWS else os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)  # pin the checked folder before the request
    base = origin()
    handlers = [NoRedirect()] + ([urllib.request.ProxyHandler({})] if base.startswith("http://") else [])
    req = urllib.request.Request(base + "/skills/versions", headers={"User-Agent": "gelato-connect-setup/0.1"})
    with urllib.request.build_opener(*handlers).open(req, timeout=5) as r:
        raw = r.read(MAX + 1)
    data = json.loads(raw.decode("utf-8")) if len(raw) <= MAX else None
    versions = data.get("versions") if isinstance(data, dict) else None
    if not isinstance(versions, dict):
        return
    keep = {n: versions[n] for n, _ in NAMES if ver(versions.get(n))}
    if not keep:
        return
    tmp = "latest.json.%d.tmp" % os.getpid()
    if WINDOWS:  # no dir_fd: create exclusively beside the target, rename
        try:
            with open(os.path.join(root, tmp), "x") as fh:
                json.dump({"versions": keep}, fh)
            os.replace(os.path.join(root, tmp), os.path.join(root, "latest.json"))
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.remove(os.path.join(root, tmp))
        return
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dirfd)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump({"versions": keep}, fh)
        os.replace(tmp, "latest.json", src_dir_fd=dirfd, dst_dir_fd=dirfd)
    finally:
        try:
            os.remove(tmp, dir_fd=dirfd)
        except FileNotFoundError:
            pass


def handover(root):
    """After the version check: if a stored sign-in exists and nothing opts out, become `setup.py --auto` (same flags as setup.sh)."""
    if os.environ.get("GELATO_CONNECT_NO_AUTO_UPDATE") or os.environ.get("GELATO_CONNECT_NO_UPDATE_CHECK"):
        return
    try:
        if not stat.S_ISREG(os.lstat(os.path.join(root, "session.json")).st_mode):
            return
    except OSError:
        return
    cmd = [sys.executable, "-I", os.path.join(os.path.dirname(os.path.abspath(__file__)), "setup.py"), "--auto"]
    if WINDOWS:  # no exec on Windows: we are already the detached check process, so run it to the end here
        if _timer:
            _timer.cancel()
        subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=0x08000000)
        return
    signal.alarm(0)  # an alarm survives exec; setup.py sets its own
    os.execv(sys.executable, cmd)


def announce_update(root):
    """Print once what the background updater installed; updated.json is untrusted input like the other files."""
    path = os.path.join(root, "updated.json")
    try:
        data = load(path)
        versions = data.get("versions") if data else None
        found = ["%s %s" % (label, versions[n]) for n, label in NAMES if isinstance(versions, dict) and ver(versions.get(n))]
    except Exception:
        found = []
    try:
        os.remove(path)  # never follows a symlink
    except OSError:
        return
    if found:
        print("Gelato Connect skills were updated automatically: %s. Tell the user; a new session uses the new version." % ", ".join(found))
        sys.stdout.flush()


def main():
    root = os.environ.get("GELATO_CONNECT_HOME") or os.path.join(os.path.expanduser("~"), ".gelato-connect-skills")
    if under_forbidden(root) or not stat.S_ISDIR(os.lstat(root).st_mode):
        return
    if sys.argv[1:] == ["--check"]:
        try:
            check(root)
        finally:
            handover(root)
        return
    announce_update(root)
    installed = load(os.path.join(root, "installed.json"))
    if not installed:
        return
    try:
        latest = load(os.path.join(root, "latest.json"))
        latest = latest.get("versions") if latest else None
        found = []
        for name, label in NAMES:
            old, new = ver(installed.get(name)), ver(latest.get(name)) if isinstance(latest, dict) else None
            if old and new and new > old:
                found.append("%s %s -> %s" % (label, installed[name], latest[name]))
        if found:
            print("Gelato Connect skills update available: %s. Tell the user, and offer to run the Gelato Connect setup skill to update." % ", ".join(found))
            sys.stdout.flush()
    except Exception:
        pass  # a bad latest.json must not stop the update check below
    if not os.environ.get("GELATO_CONNECT_NO_UPDATE_CHECK") and due(root):
        here = os.path.dirname(os.path.abspath(__file__))
        quiet = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
        if WINDOWS:  # this Python (py -3) verifies HTTPS; detached, no console window (DETACHED_PROCESS | NEW_PROCESS_GROUP | NO_WINDOW)
            subprocess.Popen([sys.executable, "-I", os.path.join(here, "notice.py"), "--check"], creationflags=0x08000208, **quiet)
        else:
            # through setup.sh: the hook's own python3 may be unable to verify HTTPS (python.org build on macOS)
            subprocess.Popen(["bash", os.path.join(here, "setup.sh"), "--check"], start_new_session=True, **quiet)


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        pass
    sys.exit(0)
