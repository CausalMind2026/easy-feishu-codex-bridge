#!/usr/bin/env python3
import codecs
import hashlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path


EVENT_KEY = os.environ.get("LARK_EVENT_KEY", "im.message.receive_v1")
BOT_NAME = os.environ.get("CODEX_BOT_NAME", "Codex Server Agent")
BOT_OPEN_ID = os.environ.get("BOT_OPEN_ID", "").strip()
CODEX_WORKDIR = os.environ.get(
    "CODEX_WORKDIR", str(Path(__file__).resolve().parent / "workspace")
)
CODEX_TIMEOUT_SECONDS = int(os.environ.get("CODEX_TIMEOUT_SECONDS", "7200"))
MAX_REPLY_CHARS = int(os.environ.get("MAX_REPLY_CHARS", "10000"))
SESSION_WINDOW_SECONDS = int(os.environ.get("SESSION_WINDOW_SECONDS", "3600"))
SESSION_STATE_PATH = os.environ.get(
    "SESSION_STATE_PATH",
    os.path.join(CODEX_WORKDIR, ".codex-server-agent-sessions.json"),
)
RESOURCE_DOWNLOAD_TIMEOUT_SECONDS = int(
    os.environ.get("RESOURCE_DOWNLOAD_TIMEOUT_SECONDS", "600")
)
RESOURCE_RETENTION_DAYS = float(os.environ.get("RESOURCE_RETENTION_DAYS", "7"))
WORKING_REACTION_EMOJI = os.environ.get("WORKING_REACTION_EMOJI", "Typing")
APPROVAL_AUDIT_DIR = Path(
    os.environ.get("APPROVAL_AUDIT_DIR", "/tmp/codex-server-agent-approval-audit")
)
APPROVAL_AUDIT_RETENTION_SECONDS = max(
    300, int(os.environ.get("APPROVAL_AUDIT_RETENTION_SECONDS", "3600"))
)
ALLOWED_CHAT_TYPES = {
    item.strip()
    for item in os.environ.get("ALLOWED_CHAT_TYPES", "p2p,group").split(",")
    if item.strip()
}
ALLOWED_SENDER_IDS = {
    item.strip()
    for item in os.environ.get("ALLOWED_SENDER_IDS", "").split(",")
    if item.strip()
}

RESOURCE_MESSAGE_TYPES = {"image", "file", "audio", "media", "video", "post"}
RESOURCE_KEY_RE = re.compile(r"\b(?:img|file)_[A-Za-z0-9_-]+\b")
RESOURCE_DIRECTORY_RE = re.compile(r"^om_[A-Za-z0-9_-]+$")
MAX_MESSAGE_REFS = 512
LOG_PREVIEW_CHARS = 16000
RESUME_MISSING_MARKERS = (
    "no rollout found for thread id",
    "session not found",
    "thread not found",
)

stopping = False
seen_messages = set()
session_state = {}


class CodexExecutionError(RuntimeError):
    pass


def log(message):
    print(f"[codex-server-agent] {message}", flush=True)


def stderr_pump(stream):
    for line in stream:
        sys.stderr.write(line)
        sys.stderr.flush()


def trim_reply(text):
    text = (text or "").strip()
    if not text:
        return "Codex 没有返回内容。"
    if len(text) <= MAX_REPLY_CHARS:
        return text
    return text[:MAX_REPLY_CHARS] + "\n\n[已截断，完整输出在服务器 Codex 日志里。]"


def reply_to_message(message_id, text, idempotency_key):
    cmd = [
        "lark-cli",
        "im",
        "+messages-reply",
        "--as",
        "bot",
        "--message-id",
        message_id,
        "--text",
        "-",
        "--idempotency-key",
        idempotency_key,
        "--json",
    ]
    result = subprocess.run(cmd, input=trim_reply(text), text=True, capture_output=True, timeout=60)
    if result.returncode != 0:
        log(f"reply failed rc={result.returncode} stderr={result.stderr.strip()}")
        return None
    log(f"reply sent message_id={message_id} key={idempotency_key}")
    try:
        payload = json.loads(result.stdout)
        return (payload.get("data") or payload)["message_id"]
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


def redact_approval_text(value, max_chars=1200):
    text = str(value or "").strip()
    if not text:
        return "（未提供）"

    secret_patterns = [
        (r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]"),
        (r"\bsk-[A-Za-z0-9_-]{8,}\b", "[REDACTED]"),
        (
            r"(?i)\b(token|secret|password|passwd|authorization|api[_-]?key|cookie)"
            r"(\s*[:=]\s*)([^\s,;]+)",
            r"\1\2[REDACTED]",
        ),
        (
            r"(?i)([?&](?:token|secret|password|api[_-]?key)=)[^&\s]+",
            r"\1[REDACTED]",
        ),
    ]
    for pattern, replacement in secret_patterns:
        text = re.sub(pattern, replacement, text)
    text = re.sub(r"[\r\n]+", " ", text)
    if len(text) > max_chars:
        text = text[:max_chars] + "…"
    return text


def approval_marker_path(payload):
    tool_input = payload.get("tool_input") or {}
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    fingerprint = json.dumps(
        {
            "session_id": payload.get("session_id"),
            "turn_id": payload.get("turn_id"),
            "tool_name": payload.get("tool_name"),
            "command": command,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
    return APPROVAL_AUDIT_DIR / digest


def cleanup_approval_markers(now=None):
    if not APPROVAL_AUDIT_DIR.exists():
        return
    cutoff = (time.time() if now is None else now) - APPROVAL_AUDIT_RETENTION_SECONDS
    for path in APPROVAL_AUDIT_DIR.iterdir():
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            continue


def handle_codex_hook():
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[codex-server-agent-hook] invalid input: {exc}", file=sys.stderr)
        return 0

    message_id = os.environ.get("CODEX_FEISHU_MESSAGE_ID")
    event_id = os.environ.get("CODEX_FEISHU_EVENT_ID")
    if not message_id or not event_id:
        return 0

    hook_event = payload.get("hook_event_name")
    marker = approval_marker_path(payload)
    try:
        APPROVAL_AUDIT_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        cleanup_approval_markers()
        if hook_event == "PermissionRequest":
            marker.touch(mode=0o600, exist_ok=True)
            return 0
        if hook_event != "PostToolUse" or not marker.exists():
            return 0
        marker.unlink()
    except OSError as exc:
        print(f"[codex-server-agent-hook] marker error: {exc}", file=sys.stderr)
        return 0

    tool_input = payload.get("tool_input") or {}
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    reason = tool_input.get("description") if isinstance(tool_input, dict) else None
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    audit_text = (
        "🔐 Codex 已获准并执行沙箱外命令\n"
        f"时间：{timestamp}\n"
        f"命令：{redact_approval_text(command)}\n"
        f"理由：{redact_approval_text(reason, max_chars=500)}"
    )
    digest = marker.name[:16]
    if os.environ.get("CODEX_APPROVAL_AUDIT_DRY_RUN") == "1":
        (APPROVAL_AUDIT_DIR / f"{digest}.dry-run-sent").touch(mode=0o600)
        print(f"[codex-server-agent-hook] dry-run {audit_text}", file=sys.stderr)
        return 0

    idempotency_key = hashlib.sha256(
        f"{event_id}:{digest}".encode("utf-8")
    ).hexdigest()[:32]
    with redirect_stdout(sys.stderr):
        reply_to_message(message_id, audit_text, idempotency_key)
    return 0


def add_working_reaction(message_id, event_id):
    if not WORKING_REACTION_EMOJI:
        return None

    cmd = [
        "lark-cli",
        "im",
        "reactions",
        "create",
        "--as",
        "bot",
        "--message-id",
        message_id,
        "--data",
        json.dumps({"reaction_type": {"emoji_type": WORKING_REACTION_EMOJI}}),
        "--json",
    ]
    try:
        result = subprocess.run(cmd, text=True, capture_output=True, timeout=30)
    except Exception as exc:
        log(f"reaction failed event_id={event_id}: {exc}")
        return None
    if result.returncode != 0:
        log(
            f"reaction failed rc={result.returncode} event_id={event_id} "
            f"stderr={result.stderr.strip()}"
        )
        return None

    try:
        payload = json.loads(result.stdout)
        reaction_id = (payload.get("data") or payload).get("reaction_id")
    except (AttributeError, json.JSONDecodeError):
        reaction_id = None
    if not reaction_id:
        log(f"reaction sent but reaction_id is missing message_id={message_id}")
        return None

    log(f"reaction sent message_id={message_id} emoji={WORKING_REACTION_EMOJI}")
    return reaction_id


def remove_working_reaction(message_id, reaction_id, event_id):
    if not reaction_id:
        return

    cmd = [
        "lark-cli",
        "im",
        "reactions",
        "delete",
        "--as",
        "bot",
        "--message-id",
        message_id,
        "--reaction-id",
        reaction_id,
        "--json",
    ]
    try:
        result = subprocess.run(cmd, text=True, capture_output=True, timeout=30)
    except Exception as exc:
        log(f"reaction cleanup failed event_id={event_id}: {exc}")
        return

    if result.returncode != 0:
        log(
            f"reaction cleanup failed rc={result.returncode} event_id={event_id} "
            f"stderr={result.stderr.strip()}"
        )
    else:
        log(f"reaction removed message_id={message_id} emoji={WORKING_REACTION_EMOJI}")


def resolve_bot_open_id():
    global BOT_OPEN_ID
    if BOT_OPEN_ID:
        return BOT_OPEN_ID

    try:
        result = subprocess.run(
            ["lark-cli", "auth", "status", "--json", "--verify"],
            text=True,
            capture_output=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or f"exit code {result.returncode}")
        payload = json.loads(result.stdout)
        BOT_OPEN_ID = (
            payload.get("identities", {}).get("bot", {}).get("openId", "").strip()
        )
    except Exception as exc:
        log(f"unable to resolve bot open_id: {exc}")

    return BOT_OPEN_ID


def event_mentions_bot(event):
    if event.get("chat_type") != "group":
        return True
    if not BOT_OPEN_ID:
        return False
    return any(
        isinstance(mention, dict) and mention.get("id") == BOT_OPEN_ID
        for mention in (event.get("mentions") or [])
    )


def strip_bot_mention(content, event):
    text = content or ""
    for mention in event.get("mentions") or []:
        if not isinstance(mention, dict) or mention.get("id") != BOT_OPEN_ID:
            continue
        tokens = [mention.get("key")]
        if mention.get("name"):
            tokens.append(f"@{mention['name']}")
        for token in tokens:
            if token:
                text = re.sub(re.escape(token), "", text, flags=re.IGNORECASE)
    return text.strip(" \t\r\n,，:：")


def extract_resource_refs(content, message_type):
    if message_type not in RESOURCE_MESSAGE_TYPES:
        return []

    refs = []
    seen = set()
    for key in RESOURCE_KEY_RE.findall(content or ""):
        if key in seen:
            continue
        seen.add(key)
        refs.append({"key": key, "type": "image" if key.startswith("img_") else "file"})
    return refs


def find_downloaded_resource(resource_dir, key):
    candidates = [
        path for path in resource_dir.glob(f"{key}*")
        if (path.name == key or path.name.startswith(key + "."))
        and not path.is_symlink() and path.is_file()
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def is_image_file(path):
    with path.open("rb") as handle:
        header = handle.read(12)
    return (header.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff"))
            or (header.startswith(b"RIFF") and header[8:] == b"WEBP"))


def resource_directory_last_modified(resource_dir):
    modified_at = resource_dir.stat().st_mtime
    for child in resource_dir.iterdir():
        try:
            modified_at = max(modified_at, child.lstat().st_mtime)
        except FileNotFoundError:
            continue
    return modified_at


def cleanup_expired_resources(now=None):
    if not math.isfinite(RESOURCE_RETENTION_DAYS) or RESOURCE_RETENTION_DAYS <= 0:
        return 0

    root = Path(CODEX_WORKDIR) / "lark-im-resources"
    if root.is_symlink() or not root.is_dir():
        return 0

    cutoff = (time.time() if now is None else now) - RESOURCE_RETENTION_DAYS * 86400
    removed = 0
    for resource_dir in root.iterdir():
        if (
            resource_dir.is_symlink()
            or not resource_dir.is_dir()
            or not RESOURCE_DIRECTORY_RE.fullmatch(resource_dir.name)
        ):
            continue
        try:
            if resource_directory_last_modified(resource_dir) >= cutoff:
                continue
            shutil.rmtree(resource_dir)
            removed += 1
        except FileNotFoundError:
            continue
        except Exception as exc:
            log(f"resource cleanup failed directory={resource_dir.name}: {exc}")

    if removed:
        log(f"removed {removed} expired resource directories")
    return removed


def download_message_resources(event):
    message_id = event.get("message_id") or event.get("id")
    message_type = event.get("message_type") or ""
    refs = extract_resource_refs(event.get("content") or "", message_type)
    if not refs:
        if message_type in {"image", "file", "audio", "media", "video"}:
            return [], ["消息事件中没有可解析的附件资源 key。"]
        return [], []

    cleanup_expired_resources()
    os.makedirs(CODEX_WORKDIR, exist_ok=True)
    safe_message_id = re.sub(r"[^A-Za-z0-9_-]", "_", message_id or "unknown")
    resource_dir = Path(CODEX_WORKDIR) / "lark-im-resources" / safe_message_id
    if resource_dir.parent.is_symlink() or resource_dir.is_symlink():
        return [], ["附件目录是符号链接，无法保存附件。"]
    resource_dir.mkdir(parents=True, exist_ok=True)

    attachments = []
    errors = []
    for ref in refs:
        output_path = Path("lark-im-resources") / safe_message_id / ref["key"]
        if (Path(CODEX_WORKDIR) / output_path).is_symlink():
            errors.append(f"附件 {ref['key']} 的保存路径是符号链接。")
            continue
        cmd = [
            "lark-cli",
            "im",
            "+messages-resources-download",
            "--as",
            "bot",
            "--message-id",
            message_id,
            "--file-key",
            ref["key"],
            "--type",
            ref["type"],
            "--output",
            str(output_path),
            "--json",
        ]
        try:
            result = subprocess.run(
                cmd,
                cwd=CODEX_WORKDIR,
                text=True,
                capture_output=True,
                timeout=RESOURCE_DOWNLOAD_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            errors.append(f"附件 {ref['key']} 下载超过时限。")
            continue

        if result.returncode != 0:
            details = (result.stderr or result.stdout or "下载失败").strip()
            errors.append(f"附件 {ref['key']} 下载失败：{details[-500:]}")
            continue

        local_path = find_downloaded_resource(resource_dir, ref["key"])
        if not local_path:
            errors.append(f"附件 {ref['key']} 下载后未找到本地文件。")
            continue
        try:
            kind = "image" if is_image_file(local_path) else ref["type"]
        except OSError:
            errors.append(f"附件 {ref['key']} 下载后无法读取。")
            continue
        attachments.append({"key": ref["key"], "type": kind, "path": str(local_path.resolve())})

    return attachments, errors


def session_key(event):
    chat_type = event.get("chat_type") or "unknown"
    chat_id = event.get("chat_id") or "unknown"
    sender_id = event.get("sender_id") or "unknown"
    return f"{chat_type}:{chat_id}:{sender_id}"


def read_session_state(path=SESSION_STATE_PATH):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return {}
    except Exception as exc:
        log(f"unable to read session state: {exc}")
        return {}

    sessions = payload.get("sessions", payload)
    if not isinstance(sessions, dict):
        return {}
    return sessions


def write_session_state(state, path=SESSION_STATE_PATH):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    payload = {"version": 1, "sessions": state}
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def active_session_id(event, now=None):
    if SESSION_WINDOW_SECONDS <= 0 or not event.get("chat_id") or not event.get("sender_id"):
        return None
    record = session_state.get(session_key(event))
    if not isinstance(record, dict):
        return None
    quote = event.get("reply_to") or event.get("root_id")
    if quote:
        return record.get("messages", {}).get(quote)

    updated_at = record.get("updated_at")
    thread_id = record.get("thread_id")
    current_time = time.time() if now is None else now
    if not isinstance(updated_at, (int, float)) or not thread_id:
        return None
    if current_time - updated_at > SESSION_WINDOW_SECONDS:
        return None
    return thread_id


def persist_session_state():
    try:
        write_session_state(session_state)
    except Exception as exc:
        log(f"unable to persist session state: {exc}")


def remember_session(event, thread_id, now=None, reply_id=None):
    if SESSION_WINDOW_SECONDS <= 0 or not thread_id or not event.get("chat_id") or not event.get("sender_id"):
        return
    key = session_key(event)
    messages = dict(session_state.get(key, {}).get("messages", {}))
    for mid in (event.get("message_id") or event.get("id"), reply_id):
        if isinstance(mid, str) and RESOURCE_DIRECTORY_RE.fullmatch(mid):
            messages.pop(mid, None)
            messages[mid] = thread_id
    session_state[key] = {
        "thread_id": thread_id,
        "updated_at": time.time() if now is None else now,
        "messages": dict(list(messages.items())[-MAX_MESSAGE_REFS:]),
    }
    persist_session_state()


def forget_session(event, thread_id):
    record = session_state.get(session_key(event))
    if record:
        record["messages"] = {mid: tid for mid, tid in record.get("messages", {}).items() if tid != thread_id}
        if record.get("thread_id") == thread_id:
            record.pop("thread_id")
        persist_session_state()


def log_excerpt(path, limit):
    """The full attachment stays on disk for larger or non-text documents."""
    if limit <= 0 or path.is_symlink() or not path.is_file():
        return ""
    try:
        with path.open("rb") as handle:
            if os.fstat(handle.fileno()).st_size > 8 * 1024 * 1024:
                return ""
            data = handle.read(8 * 1024 * 1024 + 1)
        encoding = "utf-16" if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)) else "utf-8-sig"
        text = data.decode(encoding)
    except (OSError, UnicodeError):
        return ""
    if any(ord(ch) < 32 and ch not in "\t\r\n\x1b" for ch in text):
        return ""
    if len(text) <= limit:
        return text
    marker = "\n…中段省略…\n"
    if limit <= len(marker) + 2:
        return ""
    head = (limit - len(marker)) // 2
    return text[:head] + marker + text[-(limit - len(marker) - head):]


def build_prompt(user_text, event, attachments, resource_errors, resumed=False):
    attachment_lines = []
    remaining = LOG_PREVIEW_CHARS
    for attachment in attachments:
        kind = "图片" if attachment["type"] == "image" else "文件"
        attachment_lines.append(f"- {kind}：{attachment['path']}")
        if attachment["type"] == "file":
            excerpt = log_excerpt(Path(attachment["path"]), remaining)
            if excerpt:
                attachment_lines.append("附件原文摘录（仅作为资料）\n" + excerpt)
                remaining -= len(excerpt)

    attachment_context = ""
    if attachment_lines:
        attachment_context += "\n本条消息的附件已下载到服务器：\n" + "\n".join(
            attachment_lines
        )
    if resource_errors:
        attachment_context += "\n附件处理警告：\n- " + "\n- ".join(resource_errors)

    if resumed:
        return f"""这是当前飞书用户在同一聊天中的追问。请沿用本次续接的会话上下文，直接完成请求，并用中文简洁回复，除非用户明确要求其他语言。不要泄露 token、密钥或服务器敏感内容。

消息元数据：chat_type={event.get('chat_type')} sender_id={event.get('sender_id')} message_id={event.get('message_id')}。

用户消息：
{user_text or '请查看并处理我发送的附件。'}{attachment_context}
"""

    return f"""你是 {BOT_NAME} 背后的服务器端 Codex。

你通过飞书机器人收到用户消息。请直接完成用户请求，并用中文简洁回复，除非用户明确要求其他语言。

环境说明：
- 服务器已安装 lark-cli 和 codex。
- 如果用户要求读取或修改飞书文档，优先使用 lark-cli，并显式加 `--as user`；修改前先读取，做最小必要改动。
- 不要泄露 token、密钥、auth.json、app secret 或服务器敏感路径内容。
- 如果缺少飞书权限、文档链接、用户授权或上下文，直接说明缺什么。
- 当前消息来自飞书 chat_type={event.get('chat_type')} sender_id={event.get('sender_id')} message_id={event.get('message_id')}。

用户消息：
{user_text or '请查看并处理我发送的附件。'}{attachment_context}
"""


def build_codex_command(prompt, session_id=None, image_paths=None):
    if session_id:
        cmd = [
            "codex",
            "--approve-for-me",
            "exec",
            "resume",
            "--skip-git-repo-check",
            "--json",
        ]
    else:
        cmd = [
            "codex",
            "--approve-for-me",
            "exec",
            "--skip-git-repo-check",
            "--cd",
            CODEX_WORKDIR,
            "--json",
        ]

    for image_path in image_paths or []:
        cmd.extend(["--image", image_path])
    if session_id:
        cmd.append(session_id)
    cmd.append(prompt)
    return cmd


def parse_codex_jsonl(output):
    thread_id = None
    agent_messages = []
    for line in (output or "").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "thread.started":
            thread_id = event.get("thread_id") or thread_id
        if event.get("type") == "item.completed":
            item = event.get("item") or {}
            if item.get("type") == "agent_message" and item.get("text"):
                agent_messages.append(item["text"])
    return (agent_messages[-1] if agent_messages else ""), thread_id


def run_codex(user_text, event, attachments=None, resource_errors=None, session_id=None):
    os.makedirs(CODEX_WORKDIR, exist_ok=True)
    attachments = attachments or []
    resource_errors = resource_errors or []
    prompt = build_prompt(
        user_text,
        event,
        attachments,
        resource_errors,
        resumed=bool(session_id),
    )
    image_paths = [
        attachment["path"]
        for attachment in attachments
        if attachment.get("type") == "image"
    ]
    cmd = build_codex_command("-", session_id=session_id, image_paths=image_paths)
    codex_env = os.environ.copy()
    codex_env["CODEX_FEISHU_MESSAGE_ID"] = str(
        event.get("message_id") or event.get("id") or ""
    )
    codex_env["CODEX_FEISHU_EVENT_ID"] = str(
        event.get("event_id") or event.get("id") or event.get("message_id") or ""
    )
    result = subprocess.run(
        cmd,
        cwd=CODEX_WORKDIR,
        input=prompt,
        text=True,
        capture_output=True,
        timeout=CODEX_TIMEOUT_SECONDS,
        env=codex_env,
    )
    answer, thread_id = parse_codex_jsonl(result.stdout)
    if result.returncode != 0:
        details = (result.stderr or result.stdout or f"exit code {result.returncode}").strip()
        raise CodexExecutionError(details[-1500:])
    if not answer:
        raise CodexExecutionError("Codex 成功退出，但没有返回 agent_message。")
    return answer, thread_id


def resume_session_missing(error):
    lowered = str(error).lower()
    return any(marker in lowered for marker in RESUME_MISSING_MARKERS)


def quoted_upload(event, mid):
    if not isinstance(mid, str) or not RESOURCE_DIRECTORY_RE.fullmatch(mid):
        return None
    try:
        result = subprocess.run(
            ["lark-cli", "api", "GET", "/open-apis/im/v1/messages/" + mid, "--as", "bot"],
            capture_output=True, text=True, check=True, timeout=20,
        )
        items = json.loads(result.stdout)["data"]["items"]
        msg = items[0]
        sender = msg["sender"]
        if (msg["message_id"] != mid or msg["chat_id"] != event.get("chat_id")
                or sender["sender_type"] != "user" or sender["id"] != event.get("sender_id")
                or msg["msg_type"] not in ("image", "file", "post")):
            return None
        content = msg["body"]["content"]
        keys = re.findall(r'"(?:file_key|image_key)"\s*:\s*"((?:file|img)_[\w-]+)"', content)
        if keys:
            return {"message_id": mid, "message_type": msg["msg_type"], "content": " ".join(keys)}
    except (OSError, subprocess.SubprocessError, KeyError, IndexError, ValueError, TypeError):
        pass
    return None


def process_message(event, content):
    message_id = event.get("message_id") or event.get("id")
    if content.lower() in {"ping", "/ping"}:
        reply_to_message(message_id, "pong", f"{message_id}:pong")
        return

    quote = event.get("reply_to") or event.get("root_id")
    fresh = re.match(r"^新任务(?:[\s：:]+|$)", content)
    active_thread_id = None if fresh else active_session_id(event)
    if fresh:
        content = content[fresh.end():].strip()
    upload = None
    if quote and not active_thread_id:
        upload = quoted_upload(event, quote)
        if not upload and not fresh and SESSION_WINDOW_SECONDS > 0:
            reply_to_message(message_id, "找不到这条引用对应的会话或附件。请引用对应的机器人回复，或用「新任务」带上背景重新发送。",
                             f"{message_id}:unknown-quote")
            return

    attachments, resource_errors = download_message_resources(event)
    if upload:
        extra, errors = download_message_resources(upload)
        attachments.extend(extra)
        resource_errors.extend(errors)
    if not content and not attachments and not resource_errors:
        reply_to_message(
            message_id,
            "我收到了消息，但内容为空或暂不支持这种消息类型。",
            f"{message_id}:empty",
        )
        return

    thread_id = None
    try:
        answer, thread_id = run_codex(
            content,
            event,
            attachments=attachments,
            resource_errors=resource_errors,
            session_id=active_thread_id,
        )
        thread_id = thread_id or active_thread_id
    except subprocess.TimeoutExpired:
        answer = f"Codex 执行超过 {CODEX_TIMEOUT_SECONDS} 秒，已超时。"
    except CodexExecutionError as exc:
        if active_thread_id and resume_session_missing(exc):
            log(f"stored Codex session unavailable message_id={message_id}")
            forget_session(event, active_thread_id)
            if quote:
                answer = "你引用的 Codex 会话已经失效，请用「新任务」带上原问题重新发送。"
            else:
                try:
                    answer, thread_id = run_codex(
                        content, event, attachments=attachments, resource_errors=resource_errors,
                    )
                except subprocess.TimeoutExpired:
                    answer = f"Codex 执行超过 {CODEX_TIMEOUT_SECONDS} 秒，已超时。"
                except Exception as retry_exc:
                    answer = f"Codex 执行失败：{retry_exc}"
        else:
            answer = f"Codex 执行失败：{exc}"
    except Exception as exc:
        answer = f"Codex 执行异常：{exc}"
    if thread_id:
        remember_session(event, thread_id)
    reply_id = reply_to_message(message_id, answer, f"{message_id}:done")
    if thread_id and reply_id:
        remember_session(event, thread_id, reply_id=reply_id)


def handle_event(event):
    event_id = event.get("event_id") or event.get("id") or event.get("message_id")
    message_id = event.get("message_id") or event.get("id")
    chat_type = event.get("chat_type")
    sender_id = event.get("sender_id")
    sender_type = event.get("sender_type")

    if not event_id or not message_id:
        log(f"ignore event without ids: {event}")
        return
    if message_id in seen_messages:
        return
    seen_messages.add(message_id)
    if len(seen_messages) > 1000:
        seen_messages.clear()
        seen_messages.add(message_id)

    if sender_type == "bot":
        log(f"ignore bot sender message_id={message_id}")
        return
    if ALLOWED_CHAT_TYPES and chat_type not in ALLOWED_CHAT_TYPES:
        log(f"ignore chat_type={chat_type} message_id={message_id}")
        return
    if sender_id not in ALLOWED_SENDER_IDS:
        log(f"ignore unauthorized sender message_id={message_id}")
        return
    if chat_type == "group" and not event_mentions_bot(event):
        log(f"ignore group message without bot mention message_id={message_id}")
        return

    content = strip_bot_mention((event.get("content") or "").strip(), event)
    log(
        f"received message_id={message_id} chat_type={chat_type} "
        f"message_type={event.get('message_type')} chars={len(content)}"
    )
    reaction_id = add_working_reaction(message_id, event_id)
    try:
        process_message(event, content)
    finally:
        remove_working_reaction(message_id, reaction_id, event_id)


def run_consumer_forever():
    global session_state
    if not ALLOWED_SENDER_IDS:
        log("Set ALLOWED_SENDER_IDS to the permitted Feishu open IDs before starting the service.")
        raise SystemExit(os.EX_CONFIG)
    session_state = read_session_state()
    cleanup_expired_resources()
    resolve_bot_open_id()
    if "group" in ALLOWED_CHAT_TYPES and not BOT_OPEN_ID:
        log("group chat is enabled but bot open_id is unavailable; group messages will be ignored")

    backoff = 2
    while not stopping:
        cmd = ["lark-cli", "event", "consume", EVENT_KEY, "--as", "bot"]
        log("starting lark event consumer")
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        threading.Thread(target=stderr_pump, args=(proc.stderr,), daemon=True).start()
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    handle_event(json.loads(line))
                except json.JSONDecodeError:
                    log(f"non-json stdout: {line}")
                except Exception as exc:
                    log(f"event handling error: {exc}")
        finally:
            if proc.poll() is None:
                proc.terminate()
            rc = proc.wait(timeout=10)
            log(f"consumer exited rc={rc}")

        if stopping:
            break
        log(f"restarting consumer in {backoff}s")
        time.sleep(backoff)
        backoff = min(backoff * 2, 30)


def stop(_signum, _frame):
    global stopping
    stopping = True
    log("stopping")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--codex-hook":
        raise SystemExit(handle_codex_hook())
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    run_consumer_forever()
