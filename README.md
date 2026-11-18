# Gelato Connect setup plugin

A small public setup plugin for Claude Code and Codex. It only sets up the
Gelato Connect skills. The skills themselves stay behind Gelato sign-in and are
not in this repository.

## Install

Claude Code:

```bash
claude plugin marketplace add gelatoas/gelato-connect-skills-plugin
claude plugin install gelato-connect-setup@gelato-connect-skills
```

Codex:

```bash
codex plugin marketplace add gelatoas/gelato-connect-skills-plugin
codex plugin add gelato-connect-setup@gelato-connect-skills
```

In Codex, trust the session-start hook once via `/hooks`.

On native Windows (PowerShell, no WSL) the same commands work. Python 3.9 or
newer (`py -3`) is needed; setup uses `install_win.py`, a Python installer, instead
of bash. The session-start update check needs Git Bash for Claude Code (its
default shell on Windows) and runs through `py -3`.

The setup runs in Claude Code (the terminal app, or the Code tab of the Claude
desktop app) and in Codex. Claude desktop's Chat and Cowork modes do not load
plugins installed with the `claude` command.

Then ask your assistant: "Set up the Gelato Connect skills."

## What is public here

- The OAuth client id and realm URL
- The Gelato Connect API host (`api.gelatoconnect.gelato.com`) and, until its skills routes are live, the Gelato platform host (`gnx-platform.live.gelato.tech`)
- The installer
- The two package names

## Trust boundary

After sign-in the setup asks the Gelato Connect API for the account's two skills
packages (`GET /skills`), downloads each through its 5-minute link and checks it
against the checksum the API returned, then installs them through a short-lived
server on 127.0.0.1. While that route is not live yet (404) it uses the existing
Connect skills download on the Gelato platform instead. The checksum comes from
the same server, so it catches corruption, not a compromised server.

## Update check

Once a day, in the background, the plugin asks the Gelato Connect API which skills
versions are published (versions only, no sign-in, no personal data) so it can
tell you when an update is available. Set `GELATO_CONNECT_NO_UPDATE_CHECK=1` to
turn this off.

## Automatic updates

When a newer skills version is published, the daily background check installs it
without a browser. For that, setup keeps a renewable sign-in in an owner-only
file, `session.json`, in the install folder (`~/.gelato-connect-skills`). It is
renewed in the background while the assistant is used; if it expires, the next
update simply waits until you run setup again. The access token is never stored.
At the next session start the assistant tells you what was updated.

- `GELATO_CONNECT_NO_AUTO_UPDATE=1`: keep only the "update available" notice;
  setup stores no sign-in and removes an existing `session.json`.
- `GELATO_CONNECT_NO_UPDATE_CHECK=1`: turns the check and the automatic update off.
- Deleting `session.json` signs the updater out.

## Origin

`plugins/gelato-connect-setup/skills/setup/scripts/install.sh` is a
byte-identical copy of the installer in the Gelato Connect skills ZIP download
and must only change together with it.
`install_win.py` next to it is the same installer for Windows, written in Python;
it takes the same arguments and has to change together with `install.sh`.

## Tests

```bash
python3 -m unittest discover -s tests -v
```
