#!/usr/bin/env bash
# Sign in to Gelato Connect and install the skills your account may use.
# Picks a Python (>= 3.9) that can verify HTTPS certificates, then runs setup.py.
set -euo pipefail

dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

# Same check as install.sh: a python.org build on macOS has no CA certificates
# until "Install Certificates.command" is run.
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

py=
for c in python3 /usr/bin/python3; do
  if command -v "$c" >/dev/null && verifies_https "$c"; then py=$c; break; fi
done
[ -n "$py" ] || { echo "error: no python3 (>= 3.9) on this machine can verify HTTPS certificates; with a python.org Python on macOS run its 'Install Certificates.command', then retry" >&2; exit 1; }

# The hook's daily background check comes through here too (notice.py --check), so it gets the same working Python.
script=setup.py
if [ "${1:-}" = --check ]; then script=notice.py; fi
exec "$py" -I "$dir/$script" "$@"
