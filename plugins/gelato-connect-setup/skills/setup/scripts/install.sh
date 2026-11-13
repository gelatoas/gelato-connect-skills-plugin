#!/usr/bin/env bash
# Install or update Gelato Connect skill packages.
#   install.sh <name> <version> <url> <digest> [<name> <version> <url> <digest> ...]
# Env: GELATO_CONNECT_HOME (install root, default ~/.gelato-connect-skills),
#      GELATO_CONNECT_TARGETS ("claude", "codex" or both; default: whatever is on PATH),
#      CODEX_HOME (default ~/.codex), CLAUDE_CONFIG_DIR (read by the claude CLI).
# Never prints a download URL. installed.json is written last, so a failed run retries.
# Versions decide downloads only; the assistant registrations are reconciled on every run.
set -euo pipefail

die() { echo "error: $*" >&2; exit 1; }
say() { echo "$*"; }

[ $# -ge 4 ] && [ $(($# % 4)) -eq 0 ] || die "usage: install.sh <name> <version> <url> <digest> [<name> <version> <url> <digest> ...]"

names=(); versions=(); urls=(); digests=()
while [ $# -gt 0 ]; do
  [[ $1 =~ ^[a-z0-9][a-z0-9-]*$ ]] || die "invalid package name"
  [[ $2 =~ ^[0-9]+\.[0-9]+\.[0-9]+([-+][0-9A-Za-z.-]+)?$ ]] || die "invalid version for $1"
  # Plain http only for a server on this machine.
  [[ $3 =~ ^(https://|http://(localhost|127\.0\.0\.1)(:[0-9]+)?/) ]] || die "invalid url for $1"
  d=${4#sha256:}
  [[ $d =~ ^[0-9a-fA-F]{64}$ ]] || die "invalid digest for $1 (need 64 hex characters)"
  names+=("$1"); versions+=("$2"); urls+=("$3"); digests+=("$(echo "$d" | tr 'A-F' 'a-f')")
  shift 4
done

command -v python3 >/dev/null || die "python3 is required"
command -v curl >/dev/null || die "curl is required"
command -v unzip >/dev/null || die "unzip is required"

# --- targets ---
targets=()
if [ -n "${GELATO_CONNECT_TARGETS:-}" ]; then
  for t in ${GELATO_CONNECT_TARGETS//,/ }; do
    case $t in
      claude) command -v claude >/dev/null || die "target claude requested but 'claude' is not on PATH"; targets+=(claude) ;;
      codex) targets+=(codex) ;;
      *) die "unknown target '$t' (use claude, codex)" ;;
    esac
  done
else
  command -v claude >/dev/null && targets+=(claude)
  command -v codex >/dev/null && targets+=(codex)
fi

root=${GELATO_CONNECT_HOME:-$HOME/.gelato-connect-skills}
mkdir -p "$root"
root=$(cd "$root" && pwd)
installed="$root/installed.json"

# One install at a time; released on exit, success or not.
mkdir "$root/.lock" 2>/dev/null || die "another install is running (remove $root/.lock if not)"
trap 'rmdir "$root/.lock"' EXIT

# --- which packages need work (installed.json: {"<name>": "<version>"}) ---
installed_version() {
  python3 -I - "$installed" "$1" <<'PY'
import json, sys
try:
    print(json.load(open(sys.argv[1])).get(sys.argv[2], ""))
except (OSError, ValueError):
    print("")
PY
}
todo=()
for i in "${!names[@]}"; do
  # Current only while its files are still there: a deleted folder or venv is rebuilt.
  if [ "$(installed_version "${names[$i]}")" = "${versions[$i]}" ] &&
    [ -f "$root/marketplace/${names[$i]}/.claude-plugin/plugin.json" ] && [ -x "$root/venv/bin/python" ]; then
    say "up to date: ${names[$i]} ${versions[$i]}"
  else
    todo+=("$i")
  fi
done

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"; rmdir "$root/.lock"' EXIT

# --- download, verify and unpack everything before touching the install ---
for i in ${todo[@]+"${todo[@]}"}; do
  n=${names[$i]}; v=${versions[$i]}
  curl -fsSL --globoff --proto '=https,http' --proto-redir '=https' --max-time 300 -o "$tmp/$n.zip" "${urls[$i]}" 2>/dev/null || die "download failed for $n"
  actual=$(python3 -I -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$tmp/$n.zip")
  [ "$actual" = "${digests[$i]}" ] || die "digest mismatch for $n, nothing installed"
  say "downloaded and verified: $n $v"
  mkdir "$tmp/x-$n"
  unzip -q "$tmp/$n.zip" -d "$tmp/x-$n" || die "cannot unzip $n"
  # Our packages hold plain files; a link could point outside the install root.
  [ -z "$(find "$tmp/x-$n" -type l -print -quit)" ] || die "package $n contains links, nothing installed"
  python3 -I - "$tmp/x-$n/$n/.claude-plugin/plugin.json" "$n" "$v" <<'PY' || die "package $n does not contain plugin $n $v"
import json, sys
d = json.load(open(sys.argv[1]))
sys.exit(0 if d.get("name") == sys.argv[2] and d.get("version") == sys.argv[3] else 1)
PY
done

# --- python venv and dependencies ---
venv="$root/venv"

# The skills sign in over HTTPS with the venv's Python. A python.org build on
# macOS has no CA certificates until "Install Certificates.command" is run, and
# then every call fails with an unhelpful error: build the venv on one that verifies.
verifies_https() {
  "$1" -I - <<'PY' 2>/dev/null
import socket, ssl, sys
if sys.version_info < (3, 9):
    sys.exit(1)
try:
    with socket.create_connection(("auth.gelato.com", 443), 10) as s:
        ssl.create_default_context().wrap_socket(s, server_hostname="auth.gelato.com").close()
except ssl.SSLCertVerificationError:
    sys.exit(1)
except OSError:
    pass  # offline or blocked: cannot tell, do not stop the install
PY
}
# ponytail: an existing venv is not re-checked; delete it to rebuild on another Python.
if [ ${#todo[@]} -gt 0 ] && [ ! -x "$venv/bin/python" ]; then
  py=
  for c in python3 /usr/bin/python3; do
    if command -v "$c" >/dev/null && verifies_https "$c"; then py=$c; break; fi
  done
  [ -n "$py" ] || die "no python3 (>= 3.9) on this machine can verify HTTPS certificates; with a python.org Python on macOS run its 'Install Certificates.command', then retry"
  # A half-made venv would pass the check above on the next run.
  "$py" -m venv "$venv" || { rm -rf "${venv:?}"; die "cannot create venv"; }
  say "created venv: $venv"
fi
for i in ${todo[@]+"${todo[@]}"}; do
  n=${names[$i]}
  pyproject="$tmp/x-$n/$n/pyproject.toml"
  if grep -Eq '^dependencies *= *\[' "$pyproject" 2>/dev/null && ! grep -Eq '^dependencies *= *\[ *\]' "$pyproject"; then
    "$venv/bin/python" -m pip install -q --no-cache-dir "$tmp/$n.zip" || die "pip install failed for $n"
    say "installed python dependencies: $n"
  fi
done

# --- unpack into the marketplace folder ---
mkdir -p "$root/marketplace/.claude-plugin"
for i in ${todo[@]+"${todo[@]}"}; do
  n=${names[$i]}
  rm -rf "${root:?}/marketplace/$n"
  mv "$tmp/x-$n/$n" "$root/marketplace/$n"
  say "unpacked: $n ${versions[$i]}"
done

# --- Claude Code: local marketplace "gelato-connect" with relative sources ---
register_claude() {
  python3 -I - "$root/marketplace" <<'PY'
import json, os, sys
m = sys.argv[1]
plugins = sorted(d for d in os.listdir(m) if os.path.isfile(os.path.join(m, d, ".claude-plugin", "plugin.json")))
json.dump({"name": "gelato-connect", "owner": {"name": "Gelato"},
           "plugins": [{"name": p, "source": "./" + p} for p in plugins]},
          open(os.path.join(m, ".claude-plugin", "marketplace.json"), "w"), indent=2)
PY
  # Here-strings: with pipefail, grep -q closing the pipe early could fail a match.
  if grep -Eq '^ *[^ ]* *gelato-connect *$' <<<"$(claude plugin marketplace list 2>/dev/null)"; then
    claude plugin marketplace update gelato-connect >/dev/null || return 1
    say "claude: marketplace gelato-connect updated"
  else
    claude plugin marketplace add "$root/marketplace" >/dev/null || return 1
    say "claude: marketplace gelato-connect added"
  fi
  for i in "${!names[@]}"; do
    n=${names[$i]}
    if grep -Fq "$n@gelato-connect" <<<"$(claude plugin list 2>/dev/null)"; then
      claude plugin update "$n@gelato-connect" >/dev/null || return 1
      say "claude: plugin $n updated to ${versions[$i]}"
    else
      claude plugin install "$n@gelato-connect" >/dev/null || return 1
      say "claude: plugin $n ${versions[$i]} installed"
    fi
  done
}

# --- Codex: one symlink per skill folder in $CODEX_HOME/skills ---
register_codex() {
  local skills=${CODEX_HOME:-$HOME/.codex}/skills link target i n s base
  mkdir -p "$skills"
  for i in "${!names[@]}"; do
    n=${names[$i]}; base="$root/marketplace/$n/skills"
    # remove our own stale links (target gone after the folder swap)
    for link in "$skills"/*; do
      [ -L "$link" ] || continue
      target=$(readlink "$link")
      case $target in "$base"/*) [ -e "$link" ] || { rm -f "$link"; say "codex: removed stale skill $(basename "$link")"; } ;; esac
    done
    for s in "$base"/*/; do
      [ -f "${s}SKILL.md" ] || continue
      s=${s%/}; link="$skills/$(basename "$s")"
      if [ -L "$link" ]; then
        case $(readlink "$link") in
          "$root/marketplace/"*) ln -sfn "$s" "$link" ;;
          *) say "codex: skipped $(basename "$s"): existing link is not ours"; continue ;;
        esac
      elif [ -e "$link" ]; then
        say "codex: skipped $(basename "$s"): existing entry is not ours"; continue
      else
        ln -s "$s" "$link"
      fi
    done
    say "codex: skills of $n linked in $skills"
  done
}

failed=0
for t in ${targets[@]+"${targets[@]}"}; do
  case $t in
    claude)
      if ! register_claude; then
        failed=1
        say "claude: marketplace registration failed; start Claude Code with:"
        for i in "${!names[@]}"; do say "  claude --plugin-dir $root/marketplace/${names[$i]}"; done
      fi ;;
    codex) register_codex ;;
  esac
done
# No installed.json on a failed registration, so the next run retries.
[ "$failed" -eq 0 ] || exit 1
[ ${#targets[@]} -gt 0 ] || say "no assistant found (claude, codex); files are in $root/marketplace"

# --- installed.json last ---
pairs=()
for i in "${!names[@]}"; do pairs+=("${names[$i]}=${versions[$i]}"); done
python3 -I - "$installed" "${pairs[@]}" <<'PY'
import json, os, sys
path = sys.argv[1]
try:
    data = json.load(open(path))
except (OSError, ValueError):
    data = {}
data.update(p.split("=", 1) for p in sys.argv[2:])
json.dump(data, open(path + ".tmp", "w"), indent=2, sort_keys=True)
os.replace(path + ".tmp", path)
PY
say "python: $venv/bin/python"
say "installed: $(for i in "${!names[@]}"; do printf '%s %s; ' "${names[$i]}" "${versions[$i]}"; done)"
[ ${#todo[@]} -eq 0 ] || say "restart your assistant to load the new version"
