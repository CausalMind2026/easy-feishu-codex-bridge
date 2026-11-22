# Easy Feishu Codex Bridge

简体中文 · [English](README.en.md)

**Message your Agent Team Lead from Lark/Feishu.**

![左侧的人在飞书发送截图，右侧的 Team Lead Agent 和两个队友 Agent 在服务器端处理任务](docs/assets/team-lead-cover.jpg)

在飞书里 `@Team Lead` 发任务，顺手带上截图或日志。服务器上的 Codex CLI 会把结果回复到原消息。引用回复，就能接着上一轮的上下文继续做。

> **你**　@Team Lead 帮我看这份启动日志，先定位第一条报错。
>
> **追问**　引用 Team Lead 的回复，问「能给这个问题补一个回归测试吗？」

[接入 Agent Team](#接入-agent-team) · [Linux 部署](#linux-部署) · [环境变量](#配置) · [本地测试](#本地测试)

## 接入 Agent Team

把 `CODEX_WORKDIR` 指向 Team Lead 使用的工作区。Easy Feishu Codex Bridge 负责把飞书消息和附件交给 Codex，并将结果回复到原消息。角色、工具和任务分工由服务器端的 Agent Team 配置决定。也可以直接用它和单个 Codex CLI 会话聊天。

## 消息如何到达 Codex

```text
飞书私聊 / 群聊 @机器人
        ↓
lark-cli 接收 im.message.receive_v1
        ↓
worker.py 下载附件并调用 codex exec / resume
        ↓
lark-cli 回复原消息
```

收到有效消息后，worker 会先给原消息添加 `Typing` 表情。

## 功能说明

### 私聊与群聊 @

worker 默认接受：

- `p2p` 私聊消息。
- `group` 群聊中明确 `@机器人` 的消息；普通群消息会被忽略。
- `ALLOWED_SENDER_IDS` 白名单中的发送者。
- 文字、富文本，以及可下载的图片、文件、音频和视频资源。

机器人自己的消息会被忽略，避免回复循环。消息使用飞书 `message_id` 去重，不依赖可能重复变化的 `event_id`。

群聊事件需要在飞书开放平台完成以下配置：

1. 开通 `im:message.group_at_msg:readonly`。
2. 订阅 `im.message.receive_v1`。
3. 发布应用版本并把机器人加入目标群。

代码不会绕过飞书后台的权限、应用可用范围或群成员关系。

### 图片和文件

对于图片、文件、音频、视频和包含资源的富文本，worker 会从事件内容提取资源 key，通过 `lark-cli im +messages-resources-download --as bot` 下载到：

```text
$CODEX_WORKDIR/lark-im-resources/<message_id>/
```

- 图片通过 Codex CLI 的 `--image` 参数传入。以文件形式上传的 PNG、JPEG、WebP 截图也会按图片处理。
- 文件保留本地路径。不超过 8 MiB 的 UTF-8 或带 BOM 的 UTF-16 文本文件还会直接提供原文摘录，每条消息合计最多 16000 个字符。长日志保留开头和结尾。
- 群里也可以先上传截图或日志，再引用自己这条附件消息并 `@机器人` 提问。worker 会核对原消息的聊天和发送者，再下载附件。这需要 bot 对原消息具有读取权限。
- 音频和视频按普通文件交给 Codex；能否转写或解析取决于服务器工具和文件格式。
- 表情包、卡片附件、合并转发子消息，以及飞书不允许下载的保密/外部群资源仍不支持。
- 附件目录默认保留 7 天。worker 启动时及每次处理新附件前，会删除最后修改时间超过保留期的消息资源目录。

### 续聊、引用追问和新任务

同一聊天、同一发送者在上一条任务成功后的 3600 秒内继续发消息时，worker 会运行：

```text
codex exec resume <session_id>
```

普通消息每次成功完成后重新计时，超过空闲窗口会新建会话。引用自己的任务消息或机器人给你的结果时，worker 会续接这条引用对应的 Codex 会话。即使中间切换了任务或超过一小时，也可以这样追问。每个聊天的每位发送者保留最近 512 个原消息或回信 ID。映射以 `0600` 权限保存，重启后也可使用。

例如，先让机器人检查任务 A，再发 `新任务：检查任务 B`。需要回到 A 时，引用 A 的回复并提问。群聊中仍需 `@机器人`。如果引用映射或 Codex 会话已经失效，机器人会提示你补充背景。

- 私聊按“会话 + 发送者”隔离。
- 群聊按“群 + 发送者”隔离，避免不同群成员共享 Codex 上下文。
- 设置 `SESSION_WINDOW_SECONDS=0` 可关闭续接，让每条消息新建 session。

每条飞书消息仍会启动一次 Codex CLI 进程；“同一 session”指续接 Codex 对话上下文，不是让一个 CLI 进程永久驻留。

### 沙箱外命令审批通知

worker 使用 `workspace-write` 沙箱和 `--approve-for-me` 自动审查。Codex 的 managed hooks 会关联两类事件：

1. `PermissionRequest`：仅登记确实需要审批的 Bash 命令。
2. `PostToolUse`：同一条命令获准并执行后，向原飞书消息发送审计回复。

通知包含时间、脱敏后的命令和审批理由。被拒绝的请求与普通沙箱内命令不会发送“已批准”通知。token、password、API key、Bearer 等常见密钥形态会被遮盖；临时关联文件只使用命令指纹，不保存命令正文。

为了避免把“申请审批”误报成“已经批准”，通知在命令执行完成后发送，因此长命令的通知会相应延迟。

### 执行与回复限制

- 单次 Codex 执行默认最多 7200 秒（2 小时）。
- 单条飞书回复默认最多 10000 个字符，超过部分会被截断。
- `ping` 或 `/ping` 不调用 Codex，直接回复 `pong`。
- 普通续聊遇到已失效的 Codex 会话时，worker 会清理该记录并尝试新建一次会话。引用追问遇到同样情况会提示用户重新发起任务。
- `Typing` 表情只在任务处理期间存在；回复成功、失败或超时后都会尝试移除。移除失败只记录日志，不影响最终回复。

### 服务器工作范围

随仓库提供的 systemd 配置默认使用以下工作目录，可通过 `CODEX_WORKDIR` 调整。

```text
/var/lib/codex-server-agent/workspace
```

Codex 在服务器上以专用账号 `codex-server-agent` 运行。从源码直接运行 `worker.py` 时，默认工作目录是源码旁的 `workspace/`。

## 当前限制

- worker 是单进程、同步执行；一个任务运行时，后续消息需要等待。
- 没有独立任务队列、优先级、进度查询或取消功能。
- 没有语音自动转写流水线、卡片交互或主动群历史消息读取。
- 私人文档和云盘等依赖个人身份的操作需要该服务器上的有效飞书 `user` 授权。
- 如果文档单独授权给 bot，bot 可能拥有该资源的有限权限；这不等于拥有用户个人云盘权限。

## 配置

systemd 从服务器的 `/etc/codex-server-agent.env` 读取本机配置。将本应用中获准使用机器人的飞书用户 open_id 填入 `ALLOWED_SENDER_IDS`，多个值用逗号分隔。白名单缺失或为空时，服务会在连接飞书前退出，状态码为 78。修正后用 `sudo systemctl restart codex-server-agent` 启动。服务使用 `UMask=0077`，新建的文件和目录默认仅运行账号可访问。

| 环境变量 | 默认值 | 说明 |
|---|---:|---|
| `CODEX_TIMEOUT_SECONDS` | `7200` | 单次 Codex 执行超时，单位秒 |
| `MAX_REPLY_CHARS` | `10000` | 单条飞书回复最大字符数 |
| `SESSION_WINDOW_SECONDS` | `3600` | 同一聊天/发送者续接 session 的空闲窗口 |
| `ALLOWED_CHAT_TYPES` | `p2p,group` | 允许处理的聊天类型 |
| `ALLOWED_SENDER_IDS` | 空 | 必填，只有名单内的发送者可以派工 |
| `BOT_OPEN_ID` | 自动探测 | 用于验证群聊 mention 是否指向当前机器人 |
| `RESOURCE_DOWNLOAD_TIMEOUT_SECONDS` | `600` | 单个附件下载超时，单位秒 |
| `RESOURCE_RETENTION_DAYS` | `7` | 附件保留天数；设为 `0` 可关闭自动清理 |
| `SESSION_STATE_PATH` | 工作目录内状态文件 | Codex session 映射持久化路径 |
| `APPROVAL_AUDIT_DIR` | `/tmp/codex-server-agent-approval-audit` | 审批事件关联指纹目录 |
| `APPROVAL_AUDIT_RETENTION_SECONDS` | `3600` | 清理未完成审批关联指纹的时间 |

## 文件

- `worker.py`：事件过滤、附件下载、session 管理、Codex 执行和飞书回复。
- `codex-server-agent.service` 告诉 systemd 使用哪个账号、在哪个目录启动 `worker.py`，并配置进程退出后的自动重启。
- `requirements.toml`：安装到 `/etc/codex/requirements.toml` 的 managed hook 配置。
- `test_worker.py`：不访问飞书和 Codex 的本地单元测试。

## Linux 部署

以下命令从仓库根目录执行。程序安装到 `/opt/codex-server-agent`，服务账号的主目录是 `/var/lib/codex-server-agent`。升级旧实例时，先把原有白名单填入环境文件。要续用旧任务，请在环境文件中设置原 `CODEX_WORKDIR`，并确保运行账号能访问该目录和原 Codex 会话。沿用原账号时可通过 systemd 的 `User=` 覆盖项指定它。

```bash
id codex-server-agent >/dev/null 2>&1 || sudo useradd --system --user-group \
  --create-home --home-dir /var/lib/codex-server-agent --shell /usr/sbin/nologin codex-server-agent
sudo install -d -o root -g root -m 0755 /opt/codex-server-agent
sudo install -o root -g root -m 0755 worker.py /opt/codex-server-agent/worker.py
sudo install -o root -g root -m 0644 codex-server-agent.service /etc/systemd/system/codex-server-agent.service
sudo test -e /etc/codex-server-agent.env || sudo install -o root -g root -m 0600 .env.example /etc/codex-server-agent.env
sudoedit /etc/codex-server-agent.env
```

以运行账号配置 `lark-cli` 和 `codex` 的登录身份，并确保 systemd 的可执行文件搜索路径中有这两个命令。随仓库提供的 `requirements.toml` 用于 Codex 的系统级审批审计 hook。服务器已有 `/etc/codex/requirements.toml` 时，将本项目所需的条目合入现有策略。首次安装后可用以下命令验证并启动服务。

```bash
sudo install -d -m 0755 /etc/codex
sudo test -e /etc/codex/requirements.toml || sudo install -o root -g root -m 0644 requirements.toml /etc/codex/requirements.toml
sudo -u codex-server-agent -H lark-cli auth status --json --verify
sudo -u codex-server-agent -H codex login status
sudo systemctl daemon-reload
sudo systemctl enable codex-server-agent.service
sudo systemctl restart codex-server-agent.service
systemctl status codex-server-agent.service
journalctl -u codex-server-agent.service --no-pager -n 100
```

部署后可在飞书中验证私聊、群聊 `@`、截图和文本日志，再切换到新任务并引用上一条结果追问，核对回复是否回到原任务。

## 本地测试

```bash
python3 -m unittest -v test_worker
```

单元测试使用本地样本，无需登录飞书或 Codex。

## 身份与安全

检查飞书身份：

```bash
lark-cli auth status --json --verify
```

- `bot` 身份用于接收事件、添加表情、下载消息资源和回复。
- `user` 身份用于访问用户自己的文档、云盘等个人资源。
- 不要把飞书 App Secret、访问令牌、Codex 凭证或其他密钥写入仓库。
- 必须保持严格的 `ALLOWED_SENDER_IDS` 白名单。
- worker 使用 `codex exec --approve-for-me`，默认在 `workspace-write` 沙箱内运行；沙箱外命令仍由 Codex 自动审查。
- `/etc/codex/requirements.toml` 设置 `allow_managed_hooks_only = true`，只运行本仓库声明的审批审计 hook，避免自动信任 workspace 或插件中的其他 hook。

## 许可证

[MIT](LICENSE)
