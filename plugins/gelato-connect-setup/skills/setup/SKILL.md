---
name: setup
description: Set up, install or update the Gelato Connect skills. Use when the user asks to install, set up or update the Gelato Connect skills, or when a session-start line says "Gelato Connect skills update available".
---

# Gelato Connect setup

## When to run

- The user asks to set up, install or update the Gelato Connect skills: run setup.
- A session-start notice says "Gelato Connect skills update available": tell the user and offer to run setup. Do not run it unasked.

The plugin checks once a day whether a newer version is published (versions only, no sign-in, no personal data). `GELATO_CONNECT_NO_UPDATE_CHECK=1` turns that off.

New versions install automatically in the background. For that, setup keeps a renewable sign-in in an owner-only file `session.json` in `~/.gelato-connect-skills`; it is renewed in the background, and if it expires the next update simply waits until the user runs setup again. `GELATO_CONNECT_NO_AUTO_UPDATE=1` keeps only the notice and stores no sign-in; `GELATO_CONNECT_NO_UPDATE_CHECK=1` turns both off; deleting `session.json` signs the updater out. A session-start line "Gelato Connect skills were updated automatically" needs no action: tell the user that a new session uses the new version.

## Command

Install only for the assistant that runs setup. In Claude Code:

```bash
GELATO_CONNECT_TARGETS=claude bash "${CLAUDE_SKILL_DIR}/scripts/setup.sh"
```

In Codex (use the folder that contains this `SKILL.md`):

```bash
GELATO_CONNECT_TARGETS=codex bash "<folder of this SKILL.md>/scripts/setup.sh"
```

On native Windows (no WSL), run the Python entry point instead; it needs no bash, curl or unzip. Pass the assistant as an argument (works in PowerShell and Git Bash alike). In Claude Code:

```powershell
py -3 -I "${CLAUDE_SKILL_DIR}\scripts\setup.py" --targets claude
```

In Codex: `py -3 -I "<folder of this SKILL.md>\scripts\setup.py" --targets codex`.

If `py` is not found, try `python` instead of `py -3`. If neither is Python 3.9 or newer, tell the user, and only after they agree install it with `winget install -e --id Python.Python.3.12`, then open a new terminal and run setup.

Run it with a timeout of 600 seconds (sign-in waits up to 240 seconds; installing takes about 1-2 minutes).

## Permissions

Expect a permission prompt. In Codex it needs approval because it opens a browser, listens on a local port (39201-39205), uses the network and writes under the home folder. It cannot run in a read-only sandbox.

## What it does

- Opens the browser so the user signs in with their Gelato account. Never ask for or handle the password. If the browser does not open, the script prints a sign-in link: give it to the user.
- Downloads the account's skills bundle from the Gelato Connect API and checks it holds exactly the two packages.
- Installs them under `~/.gelato-connect-skills`.
- Registers them only with the assistant that runs setup: Claude Code (local marketplace `gelato-connect`) or Codex (links in `~/.codex/skills`; junctions on Windows). Someone who wants both runs setup in each assistant; the background update then keeps both current.
- Stores only a renewable sign-in (`session.json`, owner-only; on Windows it relies on your user folder's permissions) for the background updater, never the access token; see above for how to opt out.

## Afterwards

Report the script's output lines (the versions installed) and tell the user to restart the assistant to load the skills.

## On failure

Report the `error:` line. Do not retry in a loop. Do not try to download or install the packages any other way.
