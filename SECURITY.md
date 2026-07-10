# Security and Privacy

Codex Statusbar is designed to operate locally. It reads local Codex session
transcripts and logs, then writes local status and recovery logs. These files
can contain prompt text, command output, project paths, and other sensitive
context.

## Keep Local Data Local

Do not commit or attach any of the following to issues, pull requests, or
discussions:

- Codex session transcripts or archived sessions.
- Files from `%LOCALAPPDATA%\CodexStatusbar`.
- API keys, tokens, cookies, passwords, proxy URLs, or account information.
- Full command output or screenshots that expose private code or paths.

When sharing a log excerpt, reduce it to the smallest relevant lines and
replace private values with clear placeholders such as `<redacted-token>`.

## Reporting a Vulnerability

Do not publish a public issue with a working exploit, credentials, or private
session data. Use GitHub private vulnerability reporting when it is available
for this repository; otherwise contact the repository owner privately through
GitHub. Include the affected version, a minimal redacted reproduction, and the
security impact.

## Scope

The statusbar and watchdog do not intentionally transmit local transcript data.
They should not be granted broader permissions than a normal local Codex
installation. Changes that expand filesystem access, automate approvals, or
send data over the network need explicit security review.
