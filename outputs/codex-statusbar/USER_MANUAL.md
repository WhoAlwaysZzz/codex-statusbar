# Codex Statusbar 使用手册

这份手册是给当前 MVP 用的。它的目标不是把工具讲得很玄，而是让你能一步一步测试：

1. 状态栏能不能正常显示。
2. 被动 session 监控能不能工作。
3. `codex exec --json` 自动恢复能不能工作。
4. `codex app-server` 运行器能不能工作。
5. 出问题时应该看哪些日志。

当前工具目录：

```text
C:\Users\zzz07\Documents\Codex\2026-07-04\openai-codex-desktop-cli-codex-codex\outputs\codex-statusbar
```

下面的命令默认在这个目录运行：

```powershell
cd C:\Users\zzz07\Documents\Codex\2026-07-04\openai-codex-desktop-cli-codex-codex\outputs\codex-statusbar
```

## 先看结论

这个工具现在有三块：

| 组件 | 文件 | 作用 | 会不会自动恢复 |
|---|---|---|---|
| 状态栏 | `run-statusbar.ps1` | 显示 Codex 当前状态 | 不会 |
| CLI guard | `run-guarded.ps1` | 托管 `codex exec --json` 任务 | 会，保守恢复 |
| app-server runner | `run-appserver.ps1` | 通过 `codex app-server` 启动和监控任务 | 会，保守恢复 |

最推荐的测试顺序：

1. 先跑状态栏。
2. 再跑一个普通 Codex 任务，看状态栏会不会变。
3. 再测 `run-guarded.ps1`。
4. 最后测 `run-appserver.ps1`。

不要一上来就测所有功能。这个工具本身就是为了减少混乱，测试它时也别把自己绕进去。

## 第 1 步：启动状态栏

运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\run-statusbar.ps1
```

你应该看到一个小的置顶窗口。它可以拖动。

状态栏本身是只读的：

- 不会点击屏幕。
- 不会给 Codex 发送消息。
- 不会切代理。
- 不会重启网络。
- 不会批准危险操作。

它读取三个来源：

| 优先级 | 来源 | 文件 |
|---|---|---|
| 1 | app-server runner 当前状态 | `%LOCALAPPDATA%\CodexStatusbar\appserver_status.json` |
| 2 | CLI guard 当前状态 | `%LOCALAPPDATA%\CodexStatusbar\guard_status.json` |
| 3 | Codex 本地 session 日志 | `%USERPROFILE%\.codex\sessions` |

也就是说，如果你正在跑 `run-appserver.ps1` 或 `run-guarded.ps1`，状态栏会优先显示它们的状态。否则它会退回去看 Codex 本地 session 文件。

## 第 2 步：只扫描一次

如果你不想开窗口，只想看当前判断结果，用：

```powershell
powershell -ExecutionPolicy Bypass -File .\run-statusbar.ps1 --once
```

它会打印一段 JSON，例如：

```json
{
  "state": "completed",
  "label": "Completed",
  "detail": "Turn completed.",
  "session_id": "xxx",
  "source_file": "...",
  "last_event_type": "turn/completed",
  "needs_human": false
}
```

重点看这几个字段：

| 字段 | 含义 |
|---|---|
| `state` | 当前状态，比如 `working`、`executing`、`stale` |
| `label` | 给人看的短标题 |
| `detail` | 更具体的说明 |
| `source_file` | 这次判断来自哪个文件 |
| `needs_human` | 是否应该人工处理 |
| `recommended_action` | 建议你下一步做什么 |
| `error_info` | 错误原因 |

## 第 3 步：测试被动状态监控

这一步不使用自动恢复，只测试状态栏能不能看见 Codex 本地 session。

你可以正常使用 Codex CLI 或 Desktop 做一个小任务，然后运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\run-statusbar.ps1 --once
```

如果它能看到最近 session，`source_file` 通常会指向：

```text
C:\Users\zzz07\.codex\sessions\...\*.jsonl
```

这种被动监控的优点是简单、安全、不会干预 Codex。

缺点也很明确：

- 它只能根据已经写入本地的 session 日志判断。
- 它不能真正知道底层 SSE/WebSocket 是否卡死。
- 它不会接管已经打开的 Codex Desktop 会话。
- 它不会自动发送“继续”。

所以被动状态监控适合回答：“Codex 最近有没有动静？”  
不适合回答：“底层流式连接是不是已经死了？”

## 第 4 步：测试 CLI guard

CLI guard 用来托管一个新的 `codex exec --json` 任务。

运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\run-guarded.ps1 "写一句话说明你已经开始工作，然后结束"
```

它实际做的是：

```text
codex exec --json --skip-git-repo-check "你的任务"
```

然后读取 JSONL 事件。

### CLI guard 的恢复规则

| 情况 | 动作 |
|---|---|
| 还没看到 `turn.started` 就断了 | 重发原始 prompt |
| 已经看到 `turn.started`，并且拿到 session id 后断了 | `codex exec resume <session_id> --json "继续"` |
| 登录失效、额度耗尽、权限审批、approval、危险操作 | 停止自动恢复，标记人工处理 |
| 超过最大恢复次数 | 停止自动恢复，标记人工处理 |

默认最多恢复 2 次。

### CLI guard 常用参数

最多恢复 1 次：

```powershell
powershell -ExecutionPolicy Bypass -File .\run-guarded.ps1 --max-recoveries 1 "你的任务"
```

修改“继续”提示词：

```powershell
powershell -ExecutionPolicy Bypass -File .\run-guarded.ps1 --continue-prompt "继续，从刚才中断处接着做" "你的任务"
```

传额外 Codex 参数：

```powershell
powershell -ExecutionPolicy Bypass -File .\run-guarded.ps1 --codex-arg --model --codex-arg gpt-5.2-codex "你的任务"
```

指定工作目录：

```powershell
powershell -ExecutionPolicy Bypass -File .\run-guarded.ps1 --cwd C:\Path\To\Project "你的任务"
```

### CLI guard 适合测什么

适合：

- 测 `codex exec --json` 是否稳定。
- 测“断在 turn 开始前”和“断在 turn 中间后”的恢复逻辑。
- 测状态栏是否能显示 `recovering`、`reconnecting`、`completed`、`failed`。

不适合：

- 接管你已经在 Desktop 里开的会话。
- 自动处理 approval。
- 自动切代理。
- 自动点击屏幕。

## 第 5 步：测试 app-server runner

app-server runner 用的是更接近 Codex Desktop/IDE 的协议。

运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\run-appserver.ps1 "写一句话说明你已经开始工作，然后结束"
```

它实际做的是：

1. 启动 `codex app-server --stdio`。
2. 发送 `initialize`。
3. 发送 `thread/start`。
4. 发送 `turn/start`。
5. 持续读取服务端推送的 JSON-RPC 事件。
6. 把状态写入 `appserver_status.json`。

它会识别这些事件：

| app-server 事件 | 状态栏状态 |
|---|---|
| `thread/started` | `working` |
| `turn/started` | `working` |
| `item/agentMessage/delta` | `outputting` |
| `item/started` 中的 command/file/tool | `executing` |
| `turn/completed` | `completed` |
| `error` 中的 stream/network 错误 | `reconnecting` |
| approval / permission / user input request | `waiting` 或 `failed`，需要人工 |

### app-server runner 的恢复规则

| 情况 | 动作 |
|---|---|
| turn 开始前出现可恢复网络/stream 错误 | 重发原始 prompt |
| turn 已经开始后出现可恢复网络/stream 错误 | 在同一 thread 里再发“继续” |
| approval / permission / user input | 不批准，取消请求，标记人工处理 |
| 登录、额度、权限、auth、quota | 停止，标记人工处理 |

### app-server runner 常用参数

最多恢复 1 次：

```powershell
powershell -ExecutionPolicy Bypass -File .\run-appserver.ps1 --max-recoveries 1 "你的任务"
```

指定模型：

```powershell
powershell -ExecutionPolicy Bypass -File .\run-appserver.ps1 --model gpt-5.2-codex "你的任务"
```

指定 sandbox：

```powershell
powershell -ExecutionPolicy Bypass -File .\run-appserver.ps1 --sandbox read-only "你的任务"
```

可选值通常是：

```text
read-only
workspace-write
danger-full-access
```

指定 approval policy：

```powershell
powershell -ExecutionPolicy Bypass -File .\run-appserver.ps1 --approval-policy never "你的任务"
```

指定工作目录：

```powershell
powershell -ExecutionPolicy Bypass -File .\run-appserver.ps1 --cwd C:\Path\To\Project "你的任务"
```

不把 thread 物化到磁盘：

```powershell
powershell -ExecutionPolicy Bypass -File .\run-appserver.ps1 --ephemeral "你的任务"
```

传额外 app-server 参数：

```powershell
powershell -ExecutionPolicy Bypass -File .\run-appserver.ps1 --appserver-arg --disable --appserver-arg some_feature "你的任务"
```

### app-server runner 适合测什么

适合：

- 测 app-server JSON-RPC 事件是否能稳定读到。
- 测更细的状态变化，比如 agent 输出、命令执行、工具调用、turn 完成。
- 后续迁移到 Desktop/IDE 更精确监控的基础。

暂时不适合：

- 接管已经打开的 Codex Desktop 窗口。
- 控制 Desktop UI。
- 点击 approval。
- 自动切代理。

## 状态含义

| 状态 | 含义 | 你应该做什么 |
|---|---|---|
| `idle` | 没有看到近期 Codex 活动 | 如果你刚启动，先跑一个任务 |
| `working` | Codex 正在思考或 turn 刚开始 | 等 |
| `outputting` | Codex 正在输出文本 | 等 |
| `executing` | Codex 正在执行命令、改文件或调用工具 | 等，必要时看 Codex 窗口 |
| `waiting` | 等待 approval、权限或人工输入 | 需要你打开 Codex 处理 |
| `recovering` | runner 正在重发或发送“继续” | 等它下一次结果 |
| `reconnecting` | 检测到网络、stream、connection 类问题 | 等自动恢复，或检查代理/网络 |
| `stale` | 长时间没有新事件，疑似卡住 | 打开 Codex 看一下 |
| `completed` | 任务完成 | 看结果或日志 |
| `failed` | 失败，通常需要人工 | 看 `error_info` 和日志 |

## 日志在哪里

默认日志目录：

```text
%LOCALAPPDATA%\CodexStatusbar
```

在你的机器上通常是：

```text
C:\Users\zzz07\AppData\Local\CodexStatusbar
```

主要文件：

| 文件 | 作用 |
|---|---|
| `status.json` | 状态栏当前显示的最终状态 |
| `events.jsonl` | 状态栏记录的状态变化 |
| `guard_status.json` | CLI guard 当前状态 |
| `guard_events.jsonl` | CLI guard 读到的 Codex JSONL 事件 |
| `appserver_status.json` | app-server runner 当前状态 |
| `appserver_events.jsonl` | app-server JSON-RPC 消息日志 |
| `actions.jsonl` | 工具做出的判断和动作 |

## 最常看的三个日志

### 1. 看当前状态

```powershell
Get-Content -Encoding utf8 "$env:LOCALAPPDATA\CodexStatusbar\status.json"
```

### 2. 看自动恢复做过什么

```powershell
Get-Content -Encoding utf8 "$env:LOCALAPPDATA\CodexStatusbar\actions.jsonl" -Tail 20
```

你会看到类似：

```json
{"decision":"resume_continue","reason":"recoverable error after turn started","prompt":"继续"}
```

重点看：

| 字段 | 含义 |
|---|---|
| `decision` | 工具决定做什么 |
| `reason` | 为什么这么做 |
| `prompt` | 如果重发/继续，发了什么 |
| `command` | 如果启动进程，启动命令是什么 |

### 3. 看 app-server 原始事件

```powershell
Get-Content -Encoding utf8 "$env:LOCALAPPDATA\CodexStatusbar\appserver_events.jsonl" -Tail 20
```

如果 app-server runner 行为奇怪，先看这个。

## 推荐测试路线

### 测试 A：状态栏能不能启动

运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\run-statusbar.ps1
```

预期：

- 出现小窗口。
- 没有任务时可能显示 `idle` 或最近一次 Codex 状态。

### 测试 B：状态栏单次扫描

运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\run-statusbar.ps1 --once
```

预期：

- 输出 JSON。
- 不报 Python 错误。

### 测试 C：CLI guard 正常完成

运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\run-guarded.ps1 "只回复一句：guard 测试完成"
```

预期：

- 命令最终退出码为 0。
- 状态栏显示 `completed`。
- `guard_status.json` 里 `state` 是 `completed`。

### 测试 D：app-server runner 正常完成

运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\run-appserver.ps1 "只回复一句：app-server 测试完成"
```

预期：

- 命令最终退出码为 0。
- 状态栏显示 `completed`。
- `appserver_status.json` 里 `state` 是 `completed`。

### 测试 E：人工阻塞

可以故意让任务请求一个需要权限的动作，例如让它尝试写工作区外的文件。不要让它做真正危险的事。

预期：

- 工具不应该自动批准。
- 状态应该变成 `waiting` 或 `failed`。
- `needs_human` 应该是 `true`。
- `actions.jsonl` 应该记录为什么没有继续自动处理。

## 常见问题

### 运行脚本提示执行策略问题

用这个形式运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\run-statusbar.ps1
```

`run-guarded.ps1` 和 `run-appserver.ps1` 同理。

### 状态栏一直显示旧状态

可能原因：

1. `guard_status.json` 或 `appserver_status.json` 刚刚更新过，状态栏优先显示它们。
2. `%USERPROFILE%\.codex\sessions` 里最近没有新事件。
3. 状态栏窗口是旧进程，没有重启。

可以先看：

```powershell
Get-Content -Encoding utf8 "$env:LOCALAPPDATA\CodexStatusbar\status.json"
```

如果你想只看一次当前判断：

```powershell
powershell -ExecutionPolicy Bypass -File .\run-statusbar.ps1 --once
```

### `codex` 找不到或启动不了

先检查：

```powershell
where.exe codex
where.exe codex.cmd
```

如果 `python` 里直接启动 `codex` 有权限问题，当前 app-server runner 会优先解析 `codex.cmd` 或 `codex.exe`。

### app-server 启动时出现插件 warning

如果只是 `WARN`，不一定影响任务。  
真正需要关注的是：

- runner 退出码不是 0。
- `appserver_status.json` 里 `state` 是 `failed`。
- `error_info` 里有 auth、quota、permission、connection 等关键字。

### 状态 `stale` 是不是一定卡死

不是。

`stale` 的意思只是：超过阈值没有看到新事件。默认阈值是 300 秒。

可能是：

- Codex 真的卡住。
- Codex 正在长时间思考但没有写事件。
- 底层流式连接没更新。
- 被动 session 日志没有及时刷新。

调整阈值：

```powershell
powershell -ExecutionPolicy Bypass -File .\run-statusbar.ps1 --stale-seconds 600
```

app-server runner 也有类似参数：

```powershell
powershell -ExecutionPolicy Bypass -File .\run-appserver.ps1 --stale-seconds 600 "你的任务"
```

## 当前边界

已经支持：

- 状态栏显示。
- 被动读取 Codex session JSONL。
- CLI guard 自动恢复。
- app-server runner 自动恢复。
- 日志留痕。
- 人工阻塞检测。
- approval 不自动批准。

暂时不支持：

- 真正的 Windows tray icon。
- macOS menu bar。
- 接管已经打开的 Codex Desktop 窗口。
- 点击屏幕。
- 自动切换代理节点。
- 自动重启代理工具。
- 自动处理登录、额度、权限审批。
- 对任意已有 Codex session 做完全可靠恢复。

## 什么时候用哪个入口

| 你想做什么 | 用哪个 |
|---|---|
| 只想看 Codex 最近有没有动静 | `run-statusbar.ps1` |
| 想托管一个 CLI 任务并自动恢复 | `run-guarded.ps1` |
| 想测试 app-server protocol 路线 | `run-appserver.ps1` |
| 想看当前状态 JSON | `run-statusbar.ps1 --once` |
| 想查恢复决策 | 看 `actions.jsonl` |
| 想查 app-server 原始事件 | 看 `appserver_events.jsonl` |

## 最小日常用法

开一个 PowerShell 跑状态栏：

```powershell
cd C:\Users\zzz07\Documents\Codex\2026-07-04\openai-codex-desktop-cli-codex-codex\outputs\codex-statusbar
powershell -ExecutionPolicy Bypass -File .\run-statusbar.ps1
```

再开另一个 PowerShell 跑托管任务：

```powershell
cd C:\Users\zzz07\Documents\Codex\2026-07-04\openai-codex-desktop-cli-codex-codex\outputs\codex-statusbar
powershell -ExecutionPolicy Bypass -File .\run-guarded.ps1 "你的任务"
```

或者测试 app-server 路线：

```powershell
cd C:\Users\zzz07\Documents\Codex\2026-07-04\openai-codex-desktop-cli-codex-codex\outputs\codex-statusbar
powershell -ExecutionPolicy Bypass -File .\run-appserver.ps1 "你的任务"
```

测试完之后，把这几个文件发给我看最有用：

```text
%LOCALAPPDATA%\CodexStatusbar\status.json
%LOCALAPPDATA%\CodexStatusbar\actions.jsonl
%LOCALAPPDATA%\CodexStatusbar\guard_status.json
%LOCALAPPDATA%\CodexStatusbar\appserver_status.json
```
