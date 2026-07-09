# Codex Statusbar / Watchdog 极简手册

这里只记录明确要求记录的东西：启动方式和参数。

## 最少参数启动

打开状态栏：

```powershell
Start-Process codex-stat
```

打开全局守护：

```powershell
Start-Process -WindowStyle Hidden codex-watchdog
```

如果只是临时测试，也可以直接运行：

```powershell
codex-stat
codex-watchdog
```

其中 `codex-watchdog` 会占住当前终端；所以日常更推荐用 `Start-Process -WindowStyle Hidden codex-watchdog`。

## statusbar 参数

入口：

```powershell
codex-stat [参数]
```

`codex-statusbar` 也可以用，等价于 `codex-stat`。

参数：

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `--codex-home <路径>` | 自动发现 Windows + WSL | 指定 Codex 数据目录；可重复使用 |
| `--state-dir <路径>` | `%LOCALAPPDATA%\CodexStatusbar` | 指定状态和日志目录 |
| `--stale-seconds <秒>` | `300` | 多久没有事件后显示疑似卡住 |
| `--poll-seconds <秒>` | `2` | 状态栏刷新间隔 |
| `--once` | 关闭 | 只扫描一次并打印 JSON，不打开窗口 |

常用例子：

```powershell
codex-stat --once
codex-stat --stale-seconds 600
```

## WSL Codex CLI

statusbar 是 Windows 窗口，但可以读取 WSL 里的 Codex CLI session。默认启动 `codex-stat` 时会自动扫描 Windows `.codex` 和能发现的 WSL `.codex`。

WSL Codex home 通常类似：

```text
\\wsl.localhost\<Distro>\home\<User>\.codex
```

只看 WSL Codex CLI，或者自动发现失败时手动指定：

```powershell
Start-Process codex-stat -ArgumentList '--codex-home','\\wsl.localhost\<Distro>\home\<User>\.codex'
```

手动同时指定 Windows Codex 和 WSL Codex CLI：

```powershell
Start-Process codex-stat -ArgumentList '--codex-home',"$env:USERPROFILE\.codex",'--codex-home','\\wsl.localhost\<Distro>\home\<User>\.codex'
```

从 WSL 里启动 Windows statusbar：

```bash
powershell.exe -NoProfile -Command 'Start-Process codex-stat -ArgumentList "--codex-home","\\\\wsl.localhost\\<Distro>\\home\\<User>\\.codex"'
```

## watchdog 参数

入口：

```powershell
codex-watchdog [参数]
```

参数：

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `--codex-home <路径>` | `%USERPROFILE%\.codex` | 指定 Codex 数据目录 |
| `--state-dir <路径>` | `%LOCALAPPDATA%\CodexStatusbar` | 指定状态和日志目录 |
| `--codex-bin <命令>` | `codex` | 指定 Codex CLI 命令 |
| `--poll-seconds <秒>` | `3` | 扫描间隔 |
| `--max-recoveries-per-session <次数>` | `2` | 每个 session 最多自动恢复次数 |
| `--cooldown-seconds <秒>` | `90` | 两次恢复之间的冷却时间 |
| `--reconnect-grace-seconds <秒>` | `120` | 发现 reconnect 后先让 Codex 自己重试多久 |
| `--continue-prompt <文本>` | `继续` | turn 已开始后中断时发送的恢复提示 |
| `--recent-hours <小时>` | `24` | 只守护最近多久内活跃的 session |
| `--codex-arg <参数>` | 空 | 透传给 `codex exec resume`，可重复使用 |
| `--require-git-repo` | 关闭 | 不自动加 `--skip-git-repo-check` |
| `--dry-run` | 关闭 | 只记录将要做什么，不真的恢复 |
| `--once` | 关闭 | 只扫描一次然后退出 |

常用例子：

```powershell
codex-watchdog --once --dry-run
codex-watchdog --reconnect-grace-seconds 180
codex-watchdog --max-recoveries-per-session 1
```

## 日常推荐

```powershell
Start-Process codex-stat
Start-Process -WindowStyle Hidden codex-watchdog
```
