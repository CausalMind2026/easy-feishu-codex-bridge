import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import worker


class WorkerTests(unittest.TestCase):
    def setUp(self):
        worker.seen_messages.clear()
        worker.session_state.clear()

    def test_trim_reply_uses_configured_limit(self):
        with mock.patch.object(worker, "MAX_REPLY_CHARS", 10):
            reply = worker.trim_reply("1234567890extra")
        self.assertTrue(reply.startswith("1234567890"))
        self.assertIn("已截断", reply)

    def test_approval_audit_only_sends_after_matching_post_tool_use(self):
        request = {
            "session_id": "session-1",
            "turn_id": "turn-1",
            "hook_event_name": "PermissionRequest",
            "tool_name": "Bash",
            "tool_input": {
                "command": "curl https://example.com?token=secret-value",
                "description": "Need API access api_key=secret-value",
            },
        }
        completed = dict(request, hook_event_name="PostToolUse")

        with tempfile.TemporaryDirectory() as temporary_dir, mock.patch.object(
            worker, "APPROVAL_AUDIT_DIR", Path(temporary_dir)
        ), mock.patch.dict(
            os.environ,
            {
                "CODEX_FEISHU_MESSAGE_ID": "om_1",
                "CODEX_FEISHU_EVENT_ID": "evt_1",
            },
        ), mock.patch(
            "worker.reply_to_message"
        ) as reply:
            with mock.patch("sys.stdin", io.StringIO(json.dumps(request))):
                self.assertEqual(worker.handle_codex_hook(), 0)
            reply.assert_not_called()
            self.assertEqual(len(list(Path(temporary_dir).iterdir())), 1)

            with mock.patch("sys.stdin", io.StringIO(json.dumps(completed))):
                self.assertEqual(worker.handle_codex_hook(), 0)

        reply.assert_called_once()
        message_id, audit_text, idempotency_key = reply.call_args.args
        self.assertEqual(message_id, "om_1")
        self.assertIn("已获准并执行", audit_text)
        self.assertNotIn("secret-value", audit_text)
        self.assertEqual(len(idempotency_key), 32)

    def test_approval_redaction_covers_common_secret_shapes(self):
        redacted = worker.redact_approval_text(
            "Authorization=BearerSecret token=abc sk-1234567890 "
            "https://example.com?api_key=query-secret"
        )
        self.assertNotIn("BearerSecret", redacted)
        self.assertNotIn("token=abc", redacted)
        self.assertNotIn("sk-1234567890", redacted)
        self.assertNotIn("query-secret", redacted)

    def test_group_message_must_mention_current_bot(self):
        event = {
            "chat_type": "group",
            "mentions": [{"id": "ou_other", "name": "Other", "key": "@_user_1"}],
        }
        with mock.patch.object(worker, "BOT_OPEN_ID", "ou_bot"):
            self.assertFalse(worker.event_mentions_bot(event))
            event["mentions"].append(
                {"id": "ou_bot", "name": "Codex Bot", "key": "@_user_2"}
            )
            self.assertTrue(worker.event_mentions_bot(event))
            self.assertEqual(
                worker.strip_bot_mention("@Codex Bot 帮我总结", event), "帮我总结"
            )

    def test_startup_requires_at_least_one_sender(self):
        for value in (None, "", " ,  , "):
            with self.subTest(value=value):
                env = os.environ.copy()
                env.pop("ALLOWED_SENDER_IDS", None)
                if value is not None:
                    env["ALLOWED_SENDER_IDS"] = value
                result = subprocess.run(
                    [sys.executable, "-B", worker.__file__],
                    env=env, capture_output=True, text=True, timeout=5,
                )
                self.assertEqual(result.returncode, os.EX_CONFIG)
                self.assertIn("ALLOWED_SENDER_IDS", result.stdout)

    def test_unlisted_sender_cannot_trigger_private_or_group_work(self):
        for allowed in (set(), {"ou_allowed"}):
            for chat_type in ("p2p", "group"):
                with self.subTest(allowed=allowed, chat_type=chat_type), \
                     mock.patch.object(worker, "ALLOWED_SENDER_IDS", allowed), \
                     mock.patch.object(worker, "BOT_OPEN_ID", "ou_bot"), \
                     mock.patch("worker.add_working_reaction") as reaction, \
                     mock.patch("worker.process_message") as dispatch:
                    worker.seen_messages.clear()
                    worker.handle_event({
                        "message_id": "om_test", "chat_type": chat_type, "sender_type": "user",
                        "sender_id": "ou_other", "mentions": [{"id": "ou_bot"}], "content": "ping",
                    })
                    reaction.assert_not_called()
                    dispatch.assert_not_called()

    def test_resource_keys_are_extracted_and_deduplicated(self):
        content = "![Image](img_v3_abc) <file key=\"file_v2_xyz\"/> img_v3_abc"
        self.assertEqual(
            worker.extract_resource_refs(content, "post"),
            [
                {"key": "img_v3_abc", "type": "image"},
                {"key": "file_v2_xyz", "type": "file"},
            ],
        )
        self.assertEqual(worker.extract_resource_refs(content, "text"), [])

    def test_expired_resource_directories_are_removed(self):
        now = 2_000_000_000
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir) / "lark-im-resources"
            old_dir = root / "om_old"
            recent_dir = root / "om_recent"
            unrelated_dir = root / "manual-folder"
            for directory in (old_dir, recent_dir, unrelated_dir):
                directory.mkdir(parents=True)
                (directory / "file.bin").write_bytes(b"data")

            old_time = now - 8 * 86400
            recent_time = now - 6 * 86400
            for path in (old_dir, old_dir / "file.bin", unrelated_dir):
                os.utime(path, (old_time, old_time))
            for path in (recent_dir, recent_dir / "file.bin"):
                os.utime(path, (recent_time, recent_time))

            with mock.patch.object(
                worker, "CODEX_WORKDIR", temporary_dir
            ), mock.patch.object(worker, "RESOURCE_RETENTION_DAYS", 7):
                removed = worker.cleanup_expired_resources(now=now)

            self.assertEqual(removed, 1)
            self.assertFalse(old_dir.exists())
            self.assertTrue(recent_dir.exists())
            self.assertTrue(unrelated_dir.exists())

    def test_zero_resource_retention_disables_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            resource_dir = Path(temporary_dir) / "lark-im-resources" / "om_old"
            resource_dir.mkdir(parents=True)
            with mock.patch.object(
                worker, "CODEX_WORKDIR", temporary_dir
            ), mock.patch.object(worker, "RESOURCE_RETENTION_DAYS", 0):
                self.assertEqual(worker.cleanup_expired_resources(now=2_000_000_000), 0)
            self.assertTrue(resource_dir.exists())

    def test_downloaded_image_is_returned_as_attachment(self):
        event = {
            "message_id": "om_abc",
            "message_type": "image",
            "content": "![Image](img_v3_abc)",
        }

        with tempfile.TemporaryDirectory() as temporary_dir:
            def fake_run(cmd, cwd, **_kwargs):
                output = Path(cwd) / cmd[cmd.index("--output") + 1]
                output.parent.mkdir(parents=True, exist_ok=True)
                output.with_suffix(".png").write_bytes(b"png")
                return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")

            with mock.patch.object(worker, "CODEX_WORKDIR", temporary_dir), mock.patch(
                "worker.subprocess.run", side_effect=fake_run
            ):
                attachments, errors = worker.download_message_resources(event)

        self.assertEqual(errors, [])
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0]["type"], "image")
        self.assertTrue(attachments[0]["path"].endswith("img_v3_abc.png"))

    def test_working_reaction_id_is_saved_and_removed(self):
        create_result = subprocess.CompletedProcess(
            [],
            0,
            stdout=json.dumps({"ok": True, "data": {"reaction_id": "reaction-123"}}),
            stderr="",
        )
        delete_result = subprocess.CompletedProcess([], 0, stdout="{}", stderr="")
        with mock.patch(
            "worker.subprocess.run", side_effect=[create_result, delete_result]
        ) as run:
            reaction_id = worker.add_working_reaction("om_1", "evt_1")
            worker.remove_working_reaction("om_1", reaction_id, "evt_1")

        self.assertEqual(reaction_id, "reaction-123")
        self.assertIn("create", run.call_args_list[0].args[0])
        self.assertEqual(
            run.call_args_list[1].args[0][-5:],
            ["--message-id", "om_1", "--reaction-id", "reaction-123", "--json"],
        )

    def test_codex_commands_support_new_and_resumed_sessions_with_images(self):
        prompt = "do it"
        new_cmd = worker.build_codex_command(prompt, image_paths=["/tmp/image.png"])
        self.assertEqual(new_cmd[:3], ["codex", "--approve-for-me", "exec"])
        self.assertNotIn("resume", new_cmd)
        self.assertIn("--approve-for-me", new_cmd)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", new_cmd)
        self.assertIn("--cd", new_cmd)
        self.assertEqual(new_cmd[-3:], ["--image", "/tmp/image.png", prompt])

        resumed_cmd = worker.build_codex_command(
            prompt, session_id="thread-123", image_paths=["/tmp/image.png"]
        )
        self.assertEqual(
            resumed_cmd[:4], ["codex", "--approve-for-me", "exec", "resume"]
        )
        self.assertIn("--approve-for-me", resumed_cmd)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", resumed_cmd)
        self.assertNotIn("--cd", resumed_cmd)
        self.assertEqual(resumed_cmd[-2:], ["thread-123", prompt])

    def test_codex_jsonl_parser_returns_final_message_and_thread(self):
        output = "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "thread-123"}),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": "intermediate"},
                    }
                ),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": "final"},
                    }
                ),
            ]
        )
        self.assertEqual(worker.parse_codex_jsonl(output), ("final", "thread-123"))

    def test_session_window_is_rolling_and_persisted(self):
        event = {"chat_type": "p2p", "chat_id": "oc_chat", "sender_id": "ou_user"}
        with mock.patch.object(worker, "SESSION_WINDOW_SECONDS", 3600), mock.patch(
            "worker.persist_session_state"
        ) as persist:
            worker.remember_session(event, "thread-123", now=1000)
            self.assertEqual(worker.active_session_id(event, now=4599), "thread-123")
            self.assertIsNone(worker.active_session_id(event, now=4601))
            persist.assert_called_once()

        with tempfile.TemporaryDirectory() as temporary_dir:
            state_path = Path(temporary_dir) / "sessions.json"
            worker.write_session_state(worker.session_state, state_path)
            self.assertEqual(worker.read_session_state(state_path), worker.session_state)
            self.assertEqual(state_path.stat().st_mode & 0o777, 0o600)

    def test_handle_group_mention_strips_mention_and_uses_session(self):
        event = {
            "event_id": "evt_1",
            "message_id": "om_1",
            "chat_id": "oc_1",
            "chat_type": "group",
            "sender_id": "ou_allowed",
            "sender_type": "user",
            "message_type": "text",
            "content": "@Codex Bot 继续总结",
            "mentions": [
                {"id": "ou_bot", "name": "Codex Bot", "key": "@_user_1"}
            ],
        }
        with mock.patch.object(worker, "BOT_OPEN_ID", "ou_bot"), mock.patch.object(
            worker, "ALLOWED_CHAT_TYPES", {"p2p", "group"}
        ), mock.patch.object(
            worker, "ALLOWED_SENDER_IDS", {"ou_allowed"}
        ), mock.patch(
            "worker.add_working_reaction", return_value="reaction-123"
        ), mock.patch(
            "worker.remove_working_reaction"
        ) as remove_reaction, mock.patch(
            "worker.download_message_resources", return_value=([], [])
        ), mock.patch(
            "worker.active_session_id", return_value="thread-123"
        ), mock.patch(
            "worker.run_codex", return_value=("完成", "thread-123")
        ) as run_codex, mock.patch(
            "worker.remember_session"
        ) as remember, mock.patch(
            "worker.reply_to_message", return_value="om_answer"
        ) as reply:
            worker.handle_event(event)

        self.assertEqual(run_codex.call_args.args[0], "继续总结")
        self.assertEqual(run_codex.call_args.kwargs["session_id"], "thread-123")
        self.assertEqual(remember.call_args_list, [
            mock.call(event, "thread-123"), mock.call(event, "thread-123", reply_id="om_answer")
        ])
        reply.assert_called_once_with("om_1", "完成", "om_1:done")
        remove_reaction.assert_called_once_with("om_1", "reaction-123", "evt_1")

    def test_reply_uses_exact_session_after_restart_or_timeout(self):
        user = {"chat_type": "group", "chat_id": "oc_a", "sender_id": "ou_a"}
        with mock.patch("worker.persist_session_state"):
            worker.remember_session(dict(user, message_id="om_one"), "first", now=1, reply_id="om_reply1")
            worker.remember_session(dict(user, message_id="om_two"), "second", now=2, reply_id="om_reply2")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            worker.write_session_state(worker.session_state, path)
            worker.session_state = worker.read_session_state(path)
        for anchor in ("om_one", "om_reply1"):
            self.assertEqual(worker.active_session_id(dict(user, reply_to=anchor), now=10000), "first")
        self.assertEqual(worker.active_session_id(user, now=3), "second")
        self.assertIsNone(worker.active_session_id(dict(user, reply_to="om_unknown"), now=3))
        for change in ({"chat_id": "oc_b"}, {"sender_id": "ou_b"}):
            self.assertIsNone(worker.active_session_id(dict(user, reply_to="om_reply1", **change)))
        with mock.patch("worker.persist_session_state"):
            worker.forget_session(user, "first")
        self.assertEqual(worker.active_session_id(user, now=3), "second")
        self.assertIsNone(worker.active_session_id(dict(user, root_id="om_one"), now=3))

    def test_quoted_upload_checks_original_chat_sender_and_resource(self):
        event = {"chat_id": "oc_a", "sender_id": "ou_a"}
        message = {"message_id": "om_file", "chat_id": "oc_a", "sender": {"id": "ou_a", "sender_type": "user"},
                   "msg_type": "file", "body": {"content": json.dumps({"file_key": "file_abc", "file_name": "error.log"})}}
        for change, allowed in [({}, True), ({"chat_id": "oc_b"}, False),
                                ({"sender": {"id": "ou_b", "sender_type": "user"}}, False),
                                ({"message_id": "om_wrong"}, False)]:
            response = json.dumps({"data": {"items": [dict(message, **change)]}})
            with mock.patch("worker.subprocess.run", return_value=subprocess.CompletedProcess([], 0, response, "")):
                found = worker.quoted_upload(event, "om_file")
            if allowed:
                self.assertEqual(found["content"], "file_abc")
                self.assertEqual(found["message_id"], "om_file")
            else:
                self.assertIsNone(found)

    def test_missing_quoted_session_prompts_user_without_replaying_task(self):
        event = {"chat_type": "p2p", "chat_id": "oc_a", "sender_id": "ou_a", "message_id": "om_first"}
        with mock.patch("worker.persist_session_state"), mock.patch("worker.reply_to_message") as reply, \
             mock.patch("worker.run_codex", side_effect=worker.CodexExecutionError("session not found")) as run:
            worker.remember_session(event, "first", reply_id="om_answer")
            worker.remember_session(dict(event, message_id="om_second"), "second")
            worker.process_message(dict(event, message_id="om_third", reply_to="om_answer"), "继续")
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.kwargs["session_id"], "first")
            self.assertIn("已经失效", reply.call_args.args[1])
            self.assertEqual(worker.active_session_id(event), "second")

    def test_reply_records_message_id_for_both_cli_response_formats(self):
        for payload in ({"message_id": "om_sent"}, {"ok": True, "data": {"message_id": "om_sent"}}):
            result = subprocess.CompletedProcess([], 0, json.dumps(payload), "")
            with mock.patch("worker.subprocess.run", return_value=result) as send:
                self.assertEqual(worker.reply_to_message("om_in", "检查完成", "om_in:done"), "om_sent")
                self.assertEqual(send.call_args.kwargs["input"], "检查完成")

    def test_group_round_trip_with_log_image_and_quoted_reply(self):
        prompts = []
        replies = []
        png = b"\x89PNG\r\n\x1a\n" + b"mock-image"
        def cli(cmd, **kw):
            if cmd[0] == "codex":
                prompts.append((cmd, kw["input"]))
                thread = cmd[-2] if "resume" in cmd else "thread-" + str(len(prompts))
                out = "\n".join([json.dumps({"type": "thread.started", "thread_id": thread}),
                                 json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "完成"}})])
            elif "+messages-resources-download" in cmd:
                path = Path(kw["cwd"]) / cmd[cmd.index("--output") + 1]
                path.write_bytes(png if path.name == "file_picture" else ("HEAD" + "x"*20000 + "TAIL").encode("utf-16"))
                out = "{}"
            elif "+messages-reply" in cmd:
                replies.append(kw["input"])
                out = json.dumps({"data": {"message_id": "om_bot" + str(len(replies))}})
            else:
                self.fail(cmd)
            return subprocess.CompletedProcess(cmd, 0, out, "")
        base = {"chat_type": "group", "chat_id": "oc_a", "sender_type": "user", "sender_id": "ou_a",
                "mentions": [{"id": "ou_bot", "name": "Bot"}], "message_type": "text"}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(worker, "CODEX_WORKDIR", tmp), \
             mock.patch.object(worker, "BOT_OPEN_ID", "ou_bot"), mock.patch.object(worker, "ALLOWED_SENDER_IDS", {"ou_a"}), \
             mock.patch.object(worker, "WORKING_REACTION_EMOJI", ""), mock.patch("worker.subprocess.run", side_effect=cli), \
             mock.patch("worker.persist_session_state"):
            worker.handle_event(dict(base, message_id="om_first", message_type="post",
                                content='@Bot 检查 <file key="file_picture"/> <file key="file_trace"/>'))
            worker.handle_event(dict(base, message_id="om_second", content="@Bot 新任务：检查另一台机器"))
            worker.handle_event(dict(base, message_id="om_third", reply_to="om_bot1", content="@Bot 刚才的错误呢？"))
        self.assertEqual(replies, ["完成"] * 3)
        args, prompt = prompts[0]
        self.assertEqual(args[-1], "-")
        self.assertIn("--image", args)
        self.assertIn("HEAD", prompt)
        self.assertIn("TAIL", prompt)
        self.assertIn("中段省略", prompt)
        self.assertNotIn("resume", prompts[1][0])
        self.assertEqual(prompts[2][0][-2], "thread-1")
        self.assertIn("resume", prompts[2][0])

    def test_working_reaction_is_removed_when_processing_raises(self):
        event = {
            "event_id": "evt_1",
            "message_id": "om_1",
            "chat_id": "oc_1",
            "chat_type": "p2p",
            "sender_id": "ou_allowed",
            "sender_type": "user",
            "message_type": "text",
            "content": "do work",
        }
        with mock.patch.object(worker, "ALLOWED_CHAT_TYPES", {"p2p"}), mock.patch.object(
            worker, "ALLOWED_SENDER_IDS", {"ou_allowed"}
        ), mock.patch(
            "worker.add_working_reaction", return_value="reaction-123"
        ), mock.patch(
            "worker.process_message", side_effect=RuntimeError("boom")
        ), mock.patch(
            "worker.remove_working_reaction"
        ) as remove_reaction:
            with self.assertRaisesRegex(RuntimeError, "boom"):
                worker.handle_event(event)

        remove_reaction.assert_called_once_with("om_1", "reaction-123", "evt_1")


if __name__ == "__main__":
    unittest.main()
