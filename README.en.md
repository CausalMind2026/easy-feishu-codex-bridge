# Easy Feishu Codex Bridge

[简体中文](README.md) · English

**Message your Agent Team Lead from Lark/Feishu.**

![A person sends a screenshot in Lark, and the Team Lead Agent and two teammate agents handle the task on a server](docs/assets/team-lead-cover.jpg)

Send a task to `@Team Lead` in Lark, with a screenshot or log if needed. Codex CLI runs on your server, and the bridge replies to the original message. Quote a reply to continue in the same Codex conversation.

> **You** · @Team Lead Check this startup log and find the first error.
>
> **Follow-up** · Quote the reply and ask, “Can you add a regression test for it?”

[Connect an Agent Team](#connect-an-agent-team) · [Linux deployment](#linux-deployment) · [Configuration](#configuration) · [Local tests](#local-tests)

## Connect an Agent Team

Point `CODEX_WORKDIR` at the workspace used by your Team Lead. Easy Feishu Codex Bridge passes messages and attachments from Lark to Codex and sends the results back. The Agent Team configuration on your server defines the roles, tools, and task assignments. You can also use the bridge to chat with a single Codex CLI session.

## How messages reach Codex

```text
Lark direct message or group mention
         ↓
lark-cli receives im.message.receive_v1
         ↓
worker.py downloads attachments and runs codex exec or resume
         ↓
lark-cli replies to the original message
```

When the worker accepts a message, it adds a `Typing` reaction to it.

## Features

### Direct messages and group mentions

Senders in `ALLOWED_SENDER_IDS` can use the bot in direct messages (`p2p`) or mention it in a group (`group`). The bot accepts text, rich text, and downloadable images, files, audio, and video.

The worker filters out messages from the bot itself to avoid reply loops. It deduplicates messages by their Lark `message_id`.

Set up group mentions in the Lark/Feishu developer console.

1. Grant the `im:message.group_at_msg:readonly` permission.
2. Subscribe to `im.message.receive_v1`.
3. Publish an app version and add the bot to the target group.

The app's permissions, availability settings, and group membership govern which messages the bot can receive.

### Images and files

The worker reads resource keys from message events and downloads attachments with `lark-cli im +messages-resources-download --as bot` into:

```text
$CODEX_WORKDIR/lark-im-resources/<message_id>/
```

- Images go to Codex CLI through `--image`. PNG, JPEG, and WebP screenshots uploaded as files use the same path.
- File attachments keep their local paths. For UTF-8 or BOM-marked UTF-16 text files up to 8 MiB, the worker includes excerpts of up to 16,000 characters in total per message. Long logs keep their beginning and end.
- In a group, you can upload a screenshot or log, then quote your own attachment message and mention the bot. The worker verifies the original chat and sender before downloading. The bot needs permission to read the quoted message.
- Codex receives audio and video as files. Transcription and analysis depend on the tools available on your server and the file format.
- Unsupported resources include stickers, card attachments, items inside forwarded bundles, and resources that Lark restricts in confidential or external groups.
- The worker retains message resource directories for seven days by default. It removes expired directories at startup and before handling a new attachment, based on the directory's last modification time.

### Follow-ups, quoted replies, and new tasks

A message from the same sender in the same chat within 3,600 seconds of a successful task resumes the current session.

```text
codex exec resume <session_id>
```

Each successful turn restarts the idle timer. A regular message starts a new session after the window closes. Quoting your own task message or the bot's reply lets you resume that particular session, including after switching tasks or waiting more than an hour. The worker stores the last 512 task and reply IDs per sender and chat in a state file with mode `0600`, so quote lookup survives restarts.

For example, ask the bot to investigate task A, then send `新任务：检查任务 B` to start task B. Quote the bot's reply to A to return to that task. In a group, mention the bot again when you follow up. If the quote mapping or Codex session has expired, the bot asks for context.

- Direct messages are isolated by chat and sender.
- Group messages are isolated by group and sender.
- Set `SESSION_WINDOW_SECONDS=0` to disable session resumption and start a new session for each message.

Each Lark message starts a Codex CLI process. A resumed session reuses the conversation context.

### Approval notifications for commands outside the sandbox

The worker runs Codex with a `workspace-write` sandbox and `--approve-for-me` review. Codex managed hooks correlate two events.

1. `PermissionRequest` records Bash commands that need approval.
2. `PostToolUse` sends an audit reply to the original Lark message after an approved command runs.

The reply includes the time, a redacted command, and the approval reason. The worker sends it for approved and executed commands that required review. Common credential formats, including passwords, API keys, and Bearer tokens, are redacted. Temporary correlation records store a command fingerprint.

The notification arrives after execution, so long-running commands produce later notifications.

### Execution and reply limits

- A Codex invocation has a two-hour timeout by default.
- The worker limits a Lark reply to 10,000 characters by default.
- `ping` and `/ping` return `pong` directly.
- When a regular follow-up points to an expired session, the worker clears the record and retries once with a new session. For an expired quoted follow-up, it asks you to start the task again.
- The worker attempts to remove the `Typing` reaction when a task succeeds, fails, or times out. It logs any error removing the reaction and still sends the reply.

### Server workspace

The bundled systemd service uses `/var/lib/codex-server-agent/workspace` by default. Set `CODEX_WORKDIR` to select another directory.

The service runs Codex as a dedicated `codex-server-agent` account. When you run `worker.py` directly, the default workspace is the `workspace/` directory next to it.

## Current limitations

- The worker handles one task at a time in a single process. Later messages wait for the current task to finish.
- A separate task queue, priorities, progress queries, and cancellation are outside the current feature set.
- Automatic voice transcription, interactive cards, and proactive group history retrieval are outside the current feature set.
- Access to personal documents and drives requires a valid Lark `user` authorization on the server. When a document is shared with the bot, its access is limited to the permissions granted on that resource.

## Configuration

systemd reads local settings from `/etc/codex-server-agent.env`. Fill `ALLOWED_SENDER_IDS` with the allowed Lark user open IDs for this app, separated by commas. An empty or missing allowlist causes the service to exit with status 78 before connecting to Lark. After configuring it, run `sudo systemctl restart codex-server-agent`. The service uses `UMask=0077` for files and directories it creates.

| Environment variable | Default | Purpose |
|---|---:|---|
| `CODEX_TIMEOUT_SECONDS` | `7200` | Timeout per Codex invocation, in seconds |
| `MAX_REPLY_CHARS` | `10000` | Character limit per Lark reply |
| `SESSION_WINDOW_SECONDS` | `3600` | Idle window for resuming the same sender's session in a chat |
| `ALLOWED_CHAT_TYPES` | `p2p,group` | Chat types the worker accepts |
| `ALLOWED_SENDER_IDS` | Empty | Required list of senders allowed to start tasks |
| `BOT_OPEN_ID` | Auto-detected | Identifies which group mentions address this bot |
| `RESOURCE_DOWNLOAD_TIMEOUT_SECONDS` | `600` | Timeout per attachment download |
| `RESOURCE_RETENTION_DAYS` | `7` | Number of days to retain attachments. Set `0` to disable cleanup |
| `SESSION_STATE_PATH` | State file in the workspace | Persistent Codex session mappings |
| `APPROVAL_AUDIT_DIR` | `/tmp/codex-server-agent-approval-audit` | Directory for approval correlation fingerprints |
| `APPROVAL_AUDIT_RETENTION_SECONDS` | `3600` | Retention period for unmatched approval fingerprints |

## Repository files

- `worker.py` filters Lark events, downloads attachments, manages sessions, runs Codex, and sends replies.
- `codex-server-agent.service` defines the systemd account, working directory, and restart policy.
- `requirements.toml` contains managed hook settings for `/etc/codex/requirements.toml`.
- `test_worker.py` contains offline unit tests.

## Linux deployment

Run the commands below from the repository root. Install the worker in `/opt/codex-server-agent` and use `/var/lib/codex-server-agent` as the service account's home directory.

If you are upgrading, move your existing allowlist into the environment file first. To retain previous sessions, set `CODEX_WORKDIR` to the existing workspace and give the service account access to that directory and the existing Codex session files. You can keep using a previous service account with a systemd `User=` override.

```bash
id codex-server-agent >/dev/null 2>&1 || sudo useradd --system --user-group \
  --create-home --home-dir /var/lib/codex-server-agent --shell /usr/sbin/nologin codex-server-agent
sudo install -d -o root -g root -m 0755 /opt/codex-server-agent
sudo install -o root -g root -m 0755 worker.py /opt/codex-server-agent/worker.py
sudo install -o root -g root -m 0644 codex-server-agent.service /etc/systemd/system/codex-server-agent.service
sudo test -e /etc/codex-server-agent.env || sudo install -o root -g root -m 0600 .env.example /etc/codex-server-agent.env
sudoedit /etc/codex-server-agent.env
```

Configure and authenticate `lark-cli` and `codex` as the service account and make both commands available to systemd. The bundled `requirements.toml` configures Codex's system-level approval audit hooks. If your server already has `/etc/codex/requirements.toml`, merge the required entries into that policy. Then check authentication and start the service.

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

After deployment, try a direct message, a group mention, a screenshot, and a text log. Then start a new task and quote the previous reply to verify that the worker resumes the original task.

## Local tests

```bash
python3 -m unittest -v test_worker
```

The tests run against local fixtures without Lark or Codex accounts.

## Identity and security

Check your Lark identity with:

```bash
lark-cli auth status --json --verify
```

- The `bot` identity receives events, adds reactions, downloads message attachments, and replies.
- The `user` identity accesses personal documents and drives.
- Keep your Lark App Secret, access tokens, and Codex credentials in private configuration. Restrict `ALLOWED_SENDER_IDS` to trusted senders.
- The worker invokes `codex exec --approve-for-me` with the `workspace-write` sandbox. Codex reviews commands that request execution outside it.
- The bundled `/etc/codex/requirements.toml` policy uses `allow_managed_hooks_only = true` to run the declared approval audit hooks.

## License

[MIT](LICENSE)
