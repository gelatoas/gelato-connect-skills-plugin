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

```bash
bash "${CLAUDE_SKILL_DIR}/scripts/setup.sh"
```

Claude Code substitutes the path. In Codex, if `${CLAUDE_SKILL_DIR}` is still literal text, use the folder that contains this `SKILL.md`.

Run it with a timeout of 600 seconds (sign-in waits up to 240 seconds; installing takes about 1-2 minutes).

## Permissions

Expect a permission prompt. In Codex it needs approval because it opens a browser, listens on a local port (39201-39205), uses the network and writes under the home folder. It cannot run in a read-only sandbox.

## What it does

- Opens the browser so the user signs in with their Gelato account. Never ask for or handle the password. If the browser does not open, the script prints a sign-in link: give it to the user.
- Downloads the account's skills bundle from the Gelato Connect API and checks it holds exactly the two packages.
- Installs them under `~/.gelato-connect-skills`.
- Registers them with Claude Code (local marketplace `gelato-connect`) and/or Codex (links in `~/.codex/skills`).
- Stores only a renewable sign-in (`session.json`, owner-only) for the background updater, never the access token; see above for how to opt out.

## Afterwards

Report the script's output lines (the versions installed) and tell the user to restart the assistant to load the skills.

## On failure

Report the `error:` line. Do not retry in a loop. Do not try to download or install the packages any other way.
