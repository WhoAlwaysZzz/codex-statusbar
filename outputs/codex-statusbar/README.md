# Codex Statusbar MVP

一个本地 Windows 小状态栏，用来旁路观察本机 Codex Desktop / CLI session。默认会读取 Windows `%USERPROFILE%\.codex`，并尽量自动发现 WSL 发行版里的 `.codex`。

更详细、按步骤测试的说明见：

```text
QUICK_START.md
```

## 运行

```powershell
Start-Process codex-stat
```

窗口会常驻置顶，可以拖动。它只读取本地文件，不会自动发送消息、切代理、审批命令或修改 Codex 配置。

如果本目录已经在 PATH 里，也可以直接：

```powershell
codex-stat
```

## 全局 Desktop/session 守护

这是日常应该先试的自动恢复入口。它会看所有最近活动的本地 Codex session；只有明确检测到 stream/network 类中断时，才执行极有限恢复：

```powershell
codex-watchdog
```

它现在只会做两种自动动作：

- turn 已经开始后断：`codex exec resume <session_uuid> --json "继续"`
- turn 还没开始就断，且能读到上一条用户消息：`codex exec resume <session_uuid> --json "<上一条用户消息>"`

默认会先让 Codex 自己重连 120 秒；同一个 session 持续 reconnect 超过这个时间才会自动恢复。可以用 `--reconnect-grace-seconds 60` 调整。

登录、额度、权限、approval、危险操作等不会自动处理，只写日志并让状态栏提示人工介入。

先只看它会不会动手、不真正恢复：

```powershell
codex-watchdog --once --dry-run
```

## 自动恢复运行器

如果希望让工具托管一个 Codex CLI 任务，并在连接层失败时做机械恢复，用：

```powershell
powershell -ExecutionPolicy Bypass -File .\run-guarded.ps1 "你的任务描述"
```

它不会点击屏幕，也不会操作 Codex Desktop。它只管理自己启动的 `codex exec --json` 子进程。

恢复规则：

- 如果还没看到 `turn.started` 就失败：重跑原始 prompt。
- 如果已经看到 `turn.started` 且拿到了 session id：执行 `codex exec resume <session_id> --json "继续"`。
- 如果看到登录、额度、权限、approval、危险操作等人工阻塞：停止自动恢复，只提醒。
- 默认最多自动恢复 2 次。

可调参数：

```powershell
# 最多恢复 1 次
.\run-guarded.ps1 --max-recoveries 1 "你的任务"

# 传额外 Codex 参数，例如模型
.\run-guarded.ps1 --codex-arg --model --codex-arg gpt-5.2-codex "你的任务"

# 修改继续提示词
.\run-guarded.ps1 --continue-prompt "继续，从刚才中断处接着做" "你的任务"
```

## app-server 运行器

如果希望用更接近 Codex Desktop/IDE 的 app-server protocol 来启动和监控任务，用：

```powershell
powershell -ExecutionPolicy Bypass -File .\run-appserver.ps1 "你的任务描述"
```

它会启动 `codex app-server --stdio`，发送 `thread/start` 和 `turn/start`，读取 `thread/*`、`turn/*`、`item/*`、`error` 等 JSON-RPC 事件，并把状态写给状态栏。

保守规则：

- 命令执行、文件修改、工具调用事件会显示为 `executing`。
- agent message delta 会显示为 `outputting`。
- stream/network 类错误会显示为 `reconnecting`，并按规则尝试重发原 prompt 或发送“继续”。
- approval、权限、登录、额度、人工输入等请求不会自动批准，会取消请求并标记人工处理。

可调参数：

```powershell
# 最多恢复 1 次
.\run-appserver.ps1 --max-recoveries 1 "你的任务"

# 指定模型
.\run-appserver.ps1 --model gpt-5.2-codex "你的任务"

# 修改继续提示词
.\run-appserver.ps1 --continue-prompt "继续，从刚才中断处接着做" "你的任务"
```

## 数据来源

默认读取：

```text
%USERPROFILE%\.codex\sessions
\\wsl.localhost\<Distro>\home\<User>\.codex\sessions
```

如果设置了 `CODEX_HOME`，会优先把它作为 Windows 侧默认 Codex home。也可以重复传 `--codex-home` 手动指定多个来源：

```powershell
codex-stat --codex-home "$env:USERPROFILE\.codex" --codex-home "\\wsl.localhost\<Distro>\home\<User>\.codex"
```

## 日志

默认写入：

```text
%LOCALAPPDATA%\CodexStatusbar
```

主要文件：

- `status.json`：当前状态快照
- `events.jsonl`：状态变化日志
- `guard_status.json`：自动恢复运行器的当前状态
- `guard_events.jsonl`：自动恢复运行器读取到的原始 JSONL 事件
- `appserver_status.json`：app-server 运行器的当前状态
- `appserver_events.jsonl`：app-server JSON-RPC 事件日志
- `actions.jsonl`：状态栏和恢复器记录的判断/恢复动作日志

## 状态含义

- `working`：Codex 正在思考或 turn 刚开始
- `outputting`：正在输出 assistant 内容
- `executing`：检测到 tool/function call，可能正在执行命令或改文件
- `waiting`：检测到需要权限/人工输入的迹象
- `recovering`：自动恢复运行器正在重发或 resume
- `reconnecting`：检测到流式连接/网络类错误
- `stale`：长时间没有新事件，疑似卡住
- `completed`：任务完成
- `failed`：失败或需要人工处理
- `idle`：没有近期 session

## 常用参数

```powershell
# 只扫描一次并打印 JSON，不打开窗口
.\run-statusbar.ps1 --once

# 调整卡住判定为 10 分钟
.\run-statusbar.ps1 --stale-seconds 600

# 指定 Codex home
.\run-statusbar.ps1 --codex-home C:\Users\you\.codex
```
