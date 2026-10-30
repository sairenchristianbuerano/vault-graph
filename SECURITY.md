# Security policy

## Supported versions

Only the `main` branch is supported. Fixes land there.

## Reporting a vulnerability

Please report privately through GitHub: open this repository's **Security** tab and choose **Report a vulnerability**. Do not open a public issue for a security problem.

Include what you found, how to reproduce it, and what an attacker could do with it. You can expect a first response on a best-effort basis, within 7 days.

## In scope

- **The local server** (`vault.py serve` / `ui`): it should bind to `127.0.0.1` only and serve only the page, `graph.json` and `/status`.
- **The `/do` launcher** ("Do it in Claude" and "Mark done"): the page token, the Host header check, the "only tasks on the board" rule, and the prompt-through-a-file handling. Anything that lets another website, another local user, or crafted task text start a process or run a command is in scope.
- **The Claude Code hooks** (`hook`, `activity`) and `setup`, including how `setup` edits `~/.claude/settings.json`.
- **The credential read for the opt-in plan usage** (`"plan_usage": true`): the token in `~/.claude/.credentials.json` must only be sent to `api.anthropic.com` and never stored or logged.
- **Secret handling**: the secret scan in `heal.py` and the redaction in the activity log.

## Out of scope

- Running with `launcher.permission_mode` set to `bypassPermissions`. That mode deliberately lets Claude Code run tools without asking; the README warns about it.
- Problems in Claude Code, wmux, or your browser themselves. Please report those to their maintainers.
