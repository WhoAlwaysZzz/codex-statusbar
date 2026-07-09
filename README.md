# Codex Statusbar

Local Windows statusbar and watchdog utilities for OpenAI Codex Desktop and Codex CLI sessions.

The project currently lives in:

```text
outputs/codex-statusbar
```

## What It Does

- Shows a small always-on-top status window for recent Codex sessions.
- Watches Windows Codex sessions under `%USERPROFILE%\.codex`.
- Best-effort discovers WSL Codex CLI sessions under `\\wsl.localhost\<Distro>\home\<User>\.codex`.
- Displays multiple active sessions at once.
- Provides a conservative watchdog that can resume sessions after clear stream/network failures.

## Quick Start

From `outputs/codex-statusbar` after adding that folder to `PATH`:

```powershell
Start-Process codex-stat
Start-Process -WindowStyle Hidden codex-watchdog
```

For more commands, see:

```text
outputs/codex-statusbar/QUICK_START.md
```

## Safety

The statusbar is read-only. It reads local Codex session files and local logs, then writes status snapshots under `%LOCALAPPDATA%\CodexStatusbar`.

The watchdog is intentionally narrow:

- It does not click the screen.
- It does not approve permissions.
- It does not switch proxy nodes or modify network settings.
- It only attempts mechanical recovery for clear stream/network failures.
- It logs recovery decisions to `%LOCALAPPDATA%\CodexStatusbar\actions.jsonl`.

## Public Repo Hygiene

Runtime state, local logs, Python caches, and build outputs are ignored. Do not commit:

- `%LOCALAPPDATA%\CodexStatusbar`
- `__pycache__`
- `.codex-statusbar`
- local Codex session transcripts
- tokens or credentials

## Development

Run tests:

```powershell
python -m unittest .\outputs\codex-statusbar\test_codex_statusbar.py .\outputs\codex-statusbar\test_codex_appserver.py .\outputs\codex-statusbar\test_codex_watchdog.py
```

Compile-check scripts:

```powershell
python -m py_compile .\outputs\codex-statusbar\codex_statusbar.py .\outputs\codex-statusbar\codex_guard.py .\outputs\codex-statusbar\codex_appserver.py .\outputs\codex-statusbar\codex_watchdog.py
```
