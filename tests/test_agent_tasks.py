from __future__ import annotations

import hashlib
import json
import os
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

from telegram_canonical_bridge import agent_task_runner, agent_tasks
from telegram_canonical_bridge import native_delivery_runner
from telegram_canonical_bridge.native_delivery import flush_native_outbox, kick_native_outbox
from telegram_canonical_bridge.state import BridgeState


class _FakeContext:
    def __init__(self, settings=None, terminal_result=None, process_result=None) -> None:
        self.settings = settings or {}
        self.terminal_result = terminal_result or {
            "session_id": "proc_test1234",
            "exit_code": None,
            "error": None,
        }
        self.process_result = process_result or {"status": "killed"}
        self.calls = []

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def dispatch_tool(self, name, args, **kwargs):
        self.calls.append((name, args, kwargs))
        result = self.process_result if name == "process_manage" else self.terminal_result
        return json.dumps(result)


class AgentTaskTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.state_path = Path(self.temp.name) / "tasks.sqlite3"
        self.state = BridgeState(self.state_path)
        self.shared_state_patch = patch.object(
            agent_tasks, "shared_state_path", return_value=self.state_path
        )
        self.shared_state_patch.start()
        self.original_settings = agent_tasks._SETTINGS
        self.original_ctx = agent_tasks._CTX
        self.call_number = 0

    def tearDown(self) -> None:
        agent_tasks._SETTINGS = self.original_settings
        agent_tasks._CTX = self.original_ctx
        self.shared_state_patch.stop()
        self.temp.cleanup()

    def _task(
        self,
        *,
        status: str = "dispatched",
        process_id: str | None = "proc_test1234",
        delivery_target: str = "local",
        profile: str = "default",
    ):
        self.call_number += 1
        task, _ = self.state.create_task(
            delivery_target=delivery_target,
            origin_profile=profile,
            origin_session_id=f"main-session-{self.call_number}",
            origin_turn_id=f"turn-{self.call_number}",
            origin_tool_call_id=f"call-{self.call_number}",
            target="operitrace-agent",
            queue_outbox=False,
        )
        if status == "dispatching" and process_id is None:
            return task
        return self.state.transition_task(
            task.id,
            status=status,
            process_id=process_id,
            progress="runner started",
            evidence="test",
        )

    def test_local_delivery_is_durably_acknowledged(self) -> None:
        self._task()
        self.assertEqual(flush_native_outbox(self.state), {"sent": 1, "failed": 0})
        self.assertEqual(self.state.claim_due_outbox(), [])

    def test_delivery_uses_origin_profile_and_public_send_cli(self) -> None:
        self._task(delivery_target="telegram:123", profile="controller-two")
        completed = Mock(returncode=0, stdout="", stderr="")
        with patch(
            "telegram_canonical_bridge.native_delivery.subprocess.run",
            return_value=completed,
        ) as run:
            self.assertEqual(flush_native_outbox(self.state), {"sent": 1, "failed": 0})
        argv = run.call_args.args[0]
        self.assertEqual(argv[1:5], ["-p", "controller-two", "send", "--to"])
        self.assertEqual(argv[5], "telegram:123")
        self.assertIn("--file", argv)
        self.assertIn("--json", argv)

    def test_default_delivery_profile_is_explicit_even_inside_worker_process(self) -> None:
        self._task(delivery_target="telegram", profile="default")
        completed = Mock(returncode=0, stdout='{"success":true}', stderr="")
        with patch(
            "telegram_canonical_bridge.native_delivery.subprocess.run",
            return_value=completed,
        ) as run:
            self.assertEqual(flush_native_outbox(self.state), {"sent": 1, "failed": 0})
        self.assertEqual(run.call_args.args[0][1:3], ["-p", "default"])

    def test_delivery_failure_keeps_structured_hermes_error(self) -> None:
        self._task(delivery_target="telegram", profile="default")
        completed = Mock(
            returncode=1,
            stdout='{"error":"No home channel set for telegram"}',
            stderr="",
        )
        with patch(
            "telegram_canonical_bridge.native_delivery.subprocess.run",
            return_value=completed,
        ):
            self.assertEqual(flush_native_outbox(self.state), {"sent": 0, "failed": 1})
        row = self.state.claim_due_outbox(limit=1)
        self.assertEqual(row, [])
        with closing(sqlite3.connect(self.state.path)) as connection:
            error = connection.execute("SELECT last_error FROM outbox").fetchone()[0]
        self.assertIn("No home channel set for telegram", error)

    def test_remote_delivery_kick_is_non_blocking(self) -> None:
        self._task(delivery_target="telegram")
        with patch("telegram_canonical_bridge.native_delivery.subprocess.Popen") as popen:
            self.assertTrue(kick_native_outbox(self.state))
        argv = popen.call_args.args[0]
        self.assertEqual(argv[1:3], ["-m", "telegram_canonical_bridge.native_delivery_runner"])
        self.assertIn(str(self.state.path.resolve()), argv)

    def test_delivery_runner_retries_and_drains(self) -> None:
        self._task(delivery_target="telegram")
        claimed = self.state.claim_due_outbox(limit=1)
        self.state.defer_outbox(claimed[0].id, error="temporary", delay_seconds=0.05)
        with patch("telegram_canonical_bridge.native_delivery._send_text", return_value=(True, "")):
            code = native_delivery_runner.run([
                "--state",
                str(self.state.path),
                "--max-runtime",
                "5",
                "--idle-grace",
                "0.1",
            ])
        self.assertEqual(code, 0)
        self.assertEqual(self.state.pending_delivery_targets(), set())

    def test_prepare_start_is_idempotent_per_tool_call(self) -> None:
        agent_tasks._SETTINGS = agent_tasks.AgentTaskSettings(delivery_target="local")
        values = dict(
            origin_profile="default",
            session_id="main-session",
            turn_id="turn-1",
            tool_call_id="same-call",
        )
        first = agent_tasks.prepare_start(
            self.state, {"target": "operitrace-agent", "message": "test"}, **values
        )
        second = agent_tasks.prepare_start(
            self.state, {"target": "operitrace-agent", "message": "test"}, **values
        )
        self.assertEqual(first["args"]["_bridge_task_id"], second["args"]["_bridge_task_id"])
        self.assertEqual(len(self.state.list_tasks()), 1)

    def test_start_dispatches_secret_via_file_and_tracks_process(self) -> None:
        context = _FakeContext()
        agent_tasks._CTX = context
        agent_tasks._SETTINGS = agent_tasks.AgentTaskSettings(
            delivery_target="local", controller_profiles=("default",)
        )
        task = self._task(status="dispatching", process_id=None)
        with (
            patch.object(agent_tasks, "_state_and_profile", return_value=(self.state, "default")),
            patch.object(
                agent_tasks,
                "available_agents",
                return_value={"operitrace-agent": "網站操作"},
            ),
        ):
            result = json.loads(agent_tasks.agent_task_start(
                {
                    "_bridge_task_id": task.id,
                    "target": "operitrace-agent",
                    "message": "SECRET_SENTINEL_CONTENT",
                },
                task_id="gateway-task",
                session_id=task.origin_session_id,
            ))
        self.assertTrue(result["ok"])
        name, args, kwargs = context.calls[0]
        self.assertEqual(name, "terminal")
        self.assertTrue(args["background"])
        self.assertTrue(args["notify"])
        self.assertNotIn("SECRET_SENTINEL_CONTENT", args["command"])
        self.assertIn("--task-id", args["command"])
        self.assertIn(task.id, args["command"])
        self.assertEqual(kwargs["session_id"], task.origin_session_id)
        self.assertEqual(self.state.task(task.id).process_id, "proc_test1234")

    def test_exact_payload_is_byte_preserved_separate_and_digest_is_readable(self) -> None:
        context = _FakeContext()
        agent_tasks._CTX = context
        agent_tasks._SETTINGS = agent_tasks.AgentTaskSettings(
            delivery_target="local", controller_profiles=("default",)
        )
        task = self._task(status="dispatching", process_id=None)
        exact_text = "OT-RC54-TH-REUSE-20260912-002\n\n保留換行、兩個空格  與星星 ⭐⭐"
        with (
            patch.object(agent_tasks, "_state_and_profile", return_value=(self.state, "default")),
            patch.object(
                agent_tasks,
                "available_agents",
                return_value={"operitrace-agent": "網站操作"},
            ),
        ):
            started = json.loads(agent_tasks.agent_task_start(
                {
                    "_bridge_task_id": task.id,
                    "target": "operitrace-agent",
                    "message": "請依 exact payload 執行零副作用回讀；不要發布。",
                    "exact_payload": {"kind": "exact_text", "text": exact_text},
                },
                session_id=task.origin_session_id,
            ))
            status = json.loads(agent_tasks.agent_task_status({"task_id": task.id}))

        self.assertTrue(started["ok"])
        contract = started["exact_payload"]
        body = Path(contract["artifact_path"])
        manifest = Path(contract["manifest_path"])
        binding = (
            self.state_path.parent
            / "exact-payloads"
            / "bindings"
            / f"{task.id}.json"
        )
        expected = exact_text.encode("utf-8")
        self.assertEqual(body.read_bytes(), expected)
        self.assertEqual(contract["byte_length"], len(expected))
        self.assertEqual(contract["sha256"], hashlib.sha256(expected).hexdigest())
        self.assertTrue(contract["verified"])
        self.assertEqual(status["tasks"][0]["exact_payload"]["sha256"], contract["sha256"])
        self.assertTrue(status["tasks"][0]["exact_payload"]["verified"])

        body_text = body.read_text(encoding="utf-8")
        manifest_text = manifest.read_text(encoding="utf-8")
        binding_text = binding.read_text(encoding="utf-8")
        for forbidden in (
            task.id,
            "bridge_task_update",
            "bridge_task_inbox",
            "BEGIN",
            "END",
        ):
            self.assertNotIn(forbidden, body_text)
            self.assertNotIn(forbidden, manifest_text)
            self.assertNotIn(forbidden, binding_text)

        command = context.calls[0][1]["command"]
        argv = shlex.split(command)
        handoff = Path(argv[argv.index("--message-file") + 1]).read_text(encoding="utf-8")
        self.assertNotIn(exact_text, handoff)
        payload_line = next(
            line for line in handoff.splitlines() if line.startswith('{"exact_payload"')
        )
        handoff_contract = json.loads(payload_line)["exact_payload"]
        self.assertEqual(handoff_contract["artifact_path"], contract["artifact_path"])
        self.assertEqual(handoff_contract["sha256"], contract["sha256"])
        self.assertLess(handoff.index("bridge_task_inbox"), handoff.index("Controller @default"))

    def test_exact_payload_binding_refuses_different_reentry(self) -> None:
        task = self._task(status="dispatching", process_id=None)
        root = self.state_path.parent
        first = agent_tasks.create_exact_text_payload(
            root, task.id, {"text": "第一份逐字內容"}
        )
        self.assertTrue(first["verified"])
        with self.assertRaisesRegex(ValueError, "拒絕覆寫"):
            agent_tasks.create_exact_text_payload(
                root, task.id, {"text": "不同的逐字內容"}
            )

    def test_runner_uses_unique_task_conversation_instead_of_bot_chat(self) -> None:
        task = self._task()
        task_id = task.id
        message = Path(self.temp.name) / "message.txt"
        message.write_text("safe test", encoding="utf-8")
        completed = Mock(returncode=0, stdout="done", stderr="")
        with patch.object(agent_task_runner, "_run_worker_command", return_value=(completed, False)) as run:
            code = agent_task_runner.run([
                "--target",
                "operitrace-agent",
                "--task-id",
                task_id,
                "--state",
                str(self.state.path),
                "--message-file",
                str(message),
                "--lock-root",
                str(Path(self.temp.name) / "locks"),
                "--hermes",
                "hermes",
            ])
        self.assertEqual(code, 0)
        argv = run.call_args.args[0]
        self.assertEqual(argv[argv.index("-c") + 1], f"TCB Task {task_id}")
        self.assertNotIn("Bot Chat", argv)
        self.assertEqual(argv[argv.index("--source") + 1], "tool")
        environment = run.call_args.kwargs["environment"]
        for name in agent_task_runner.EXACT_PAYLOAD_ENV.values():
            self.assertNotIn(name, environment)
        self.assertFalse(message.exists())
        self.assertEqual(self.state.task(task_id).status, "completed")

    def test_runner_exposes_verified_exact_payload_reference_without_body(self) -> None:
        task = self._task()
        exact_text = "逐字正文\n\n兩個空格  與 ⭐⭐"
        contract = agent_tasks.create_exact_text_payload(
            self.state_path.parent,
            task.id,
            {"text": exact_text},
        )
        message = Path(self.temp.name) / "exact-message.txt"
        message.write_text("只含控制說明", encoding="utf-8")
        completed = Mock(returncode=0, stdout="done", stderr="")
        stale = {
            name: "stale"
            for name in agent_task_runner.EXACT_PAYLOAD_ENV.values()
        }
        with (
            patch.dict(os.environ, stale),
            patch.object(
                agent_task_runner,
                "_run_worker_command",
                return_value=(completed, False),
            ) as run,
        ):
            code = agent_task_runner.run([
                "--target", task.target,
                "--task-id", task.id,
                "--state", str(self.state.path),
                "--message-file", str(message),
                "--lock-root", str(Path(self.temp.name) / "locks"),
                "--hermes", "hermes",
            ])
        self.assertEqual(code, 0)
        environment = run.call_args.kwargs["environment"]
        self.assertEqual(
            environment[agent_task_runner.EXACT_PAYLOAD_ENV["artifact_path"]],
            contract["artifact_path"],
        )
        self.assertEqual(
            environment[agent_task_runner.EXACT_PAYLOAD_ENV["sha256"]],
            contract["sha256"],
        )
        self.assertEqual(
            environment[agent_task_runner.EXACT_PAYLOAD_ENV["byte_length"]],
            str(contract["byte_length"]),
        )
        exported = {
            name: environment[name]
            for name in agent_task_runner.EXACT_PAYLOAD_ENV.values()
        }
        self.assertNotIn(exact_text, json.dumps(exported, ensure_ascii=False))

    def test_reentered_start_does_not_spawn_duplicate_runner(self) -> None:
        context = _FakeContext()
        agent_tasks._CTX = context
        agent_tasks._SETTINGS = agent_tasks.AgentTaskSettings(
            delivery_target="local", controller_profiles=("default",)
        )
        task = self._task()
        with patch.object(agent_tasks, "_state_and_profile", return_value=(self.state, "default")):
            result = json.loads(agent_tasks.agent_task_start(
                {
                    "_bridge_task_id": task.id,
                    "target": "operitrace-agent",
                    "message": "相同 tool call 重入",
                },
                session_id=task.origin_session_id,
            ))
        self.assertTrue(result["deduplicated"])
        self.assertEqual(context.calls, [])

    def test_cancel_tree_kills_process(self) -> None:
        context = _FakeContext(process_result={"status": "killed"})
        agent_tasks._CTX = context
        task = self._task()
        with patch.object(agent_tasks, "_state_and_profile", return_value=(self.state, "default")):
            result = json.loads(agent_tasks.agent_task_cancel({"task_id": task.id}))
        self.assertTrue(result["ok"])
        self.assertEqual(self.state.task(task.id).status, "cancelled")
        self.assertEqual(context.calls[-1][0], "process_manage")
        self.assertEqual(context.calls[-1][1]["session_id"], "proc_test1234")

    def test_cancel_from_other_controller_preserves_stopping_until_confirmation(self) -> None:
        agent_tasks._CTX = _FakeContext(process_result={"status": "not_found", "error": "unknown handle"})
        task = self._task()
        with patch.object(agent_tasks, "_state_and_profile", return_value=(self.state, "default")):
            result = json.loads(agent_tasks.agent_task_cancel({"task_id": task.id}))
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "stopping")
        self.assertFalse(result["cancellationConfirmed"])
        for status in ("running", "waiting", "returning", None):
            updated = self.state.transition_task(task.id, status=status, progress="late worker update", evidence="late hook")
            self.assertEqual(updated.status, "stopping")
            self.assertEqual(updated.evidence, "agent_task_cancel requested")

    def test_other_profile_cannot_cancel(self) -> None:
        task = self._task(profile="controller-two")
        agent_tasks._CTX = _FakeContext()
        with patch.object(agent_tasks, "_state_and_profile", return_value=(self.state, "default")):
            result = json.loads(agent_tasks.agent_task_cancel({"task_id": task.id}))
        self.assertFalse(result["ok"])
        self.assertEqual(self.state.task(task.id).status, "dispatched")
        self.assertEqual(agent_tasks._CTX.calls, [])

    def test_cancel_without_usable_process_response_keeps_durable_request(self) -> None:
        for response in ([], None, {"status": "unavailable"}):
            with self.subTest(response=response):
                task = self._task()
                agent_tasks._CTX = _FakeContext()
                agent_tasks._CTX.process_result = response
                with patch.object(agent_tasks, "_state_and_profile", return_value=(self.state, "default")):
                    result = json.loads(agent_tasks.agent_task_cancel({"task_id": task.id}))
                self.assertEqual(result["status"], "stopping")
                self.assertFalse(result["cancellationConfirmed"])

    def test_slash_cancel_does_not_report_stopping_as_cancelled(self) -> None:
        with patch.object(agent_tasks, "agent_task_cancel", return_value=json.dumps({
            "ok": True, "task_id": "TCB-20260911-ABC123", "status": "stopping",
            "cancellationConfirmed": False,
        })):
            rendered = agent_tasks._slash_command("cancel TCB-20260911-ABC123")
        self.assertIn("已提出取消要求", rendered)
        self.assertNotIn("已取消", rendered)

    def test_cancel_before_worker_start_does_not_spawn(self) -> None:
        task = self._task()
        self.state.transition_task(task.id, status="stopping", evidence="cancel requested")
        message = Path(self.temp.name) / "cancelled-message.txt"
        message.write_text("do not run", encoding="utf-8")
        with patch.object(agent_task_runner, "_run_worker_command") as run_worker:
            result = agent_task_runner.run([
                "--target", task.target, "--task-id", task.id, "--state", str(self.state.path),
                "--message-file", str(message), "--lock-root", str(Path(self.temp.name) / "locks"),
            ])
        run_worker.assert_not_called()
        self.assertEqual(result, 130)
        self.assertEqual(self.state.task(task.id).status, "cancelled")
        self.assertFalse(message.exists())

    def test_runner_stops_real_owned_child_after_other_controller_cancel(self) -> None:
        task = self._task()
        agent_tasks._CTX = _FakeContext(process_result={"status": "not_found"})
        command = [sys.executable, "-c", "import time; time.sleep(30)"]
        launched = threading.Event()
        children, results, errors = [], [], []
        real_popen = subprocess.Popen

        def launch(args, **kwargs):
            child = real_popen(args, **kwargs)
            if args == command:
                children.append(child)
                launched.set()
            return child

        def run_worker():
            try:
                results.append(agent_task_runner._run_worker_command(command, state=self.state, task_id=task.id))
            except BaseException as exc:
                errors.append(exc)

        def stop_owned(process):
            process.kill()
            process.wait(timeout=5)

        # CI／sandbox 可能禁止 taskkill，即使 child 是本測試建立。此測試要驗證
        # runner 觀察 durable stopping 後確實停止「自己持有的真實 child」；
        # Windows /T /F 的命令契約由下一個獨立測試覆蓋。
        with (
            patch.object(agent_task_runner.subprocess, "Popen", side_effect=launch),
            patch.object(agent_task_runner, "_stop_owned_process_tree", side_effect=stop_owned),
        ):
            thread = threading.Thread(target=run_worker, daemon=True)
            thread.start()
            try:
                self.assertTrue(launched.wait(5), "真實 child 未啟動")
                other_connection = BridgeState(self.state.path)
                with patch.object(agent_tasks, "_state_and_profile", return_value=(other_connection, "default")):
                    requested = json.loads(agent_tasks.agent_task_cancel({"task_id": task.id}))
                self.assertEqual(requested["status"], "stopping")
                thread.join(10)
                self.assertFalse(thread.is_alive(), "取消後 runner 仍等待 child")
                self.assertEqual(errors, [])
                completed, cancelled = results[0]
                self.assertTrue(cancelled)
                self.assertIsNotNone(children[0].poll(), "原 child 並未停止")
                self.assertTrue(agent_task_runner._record_completion(
                    self.state.path, task.id, exit_code=completed.returncode, reason="cancelled",
                ))
                self.assertEqual(self.state.task(task.id).status, "cancelled")
                self.assertEqual(self.state.transition_task(task.id, status="running").status, "cancelled")
            finally:
                for child in children:
                    if child.poll() is None:
                        child.kill()
                        child.wait(timeout=5)
                thread.join(5)

    @unittest.skipUnless(os.name == "nt", "Windows taskkill contract")
    def test_windows_owned_tree_stop_uses_taskkill(self) -> None:
        process = Mock(pid=43210)
        process.poll.return_value = None
        completed = Mock(returncode=0)
        with patch.object(agent_task_runner.subprocess, "run", return_value=completed) as run:
            agent_task_runner._stop_owned_process_tree(process)
        argv = run.call_args.args[0]
        self.assertEqual(argv[-3:], ["43210", "/T", "/F"])
        process.wait.assert_called_once_with(timeout=5)

    def test_new_instruction_is_inbox_note_not_interrupt(self) -> None:
        task = self._task()
        with patch.object(agent_tasks, "_state_and_profile", return_value=(self.state, "default")):
            result = json.loads(agent_tasks.agent_task_message({
                "task_id": task.id,
                "message": "補充條件",
            }))
        self.assertTrue(result["ok"])
        updated, notes = self.state.read_task_notes(task.id, mark_read=False)
        self.assertEqual([note.text for note in notes], ["補充條件"])
        self.assertNotEqual(updated.status, "cancelled")

    def test_slash_status_is_human_readable(self) -> None:
        rendered = agent_tasks._render_status_command(json.dumps({
            "ok": True,
            "tasks": [{
                "task_id": "TCB-20260910-ABC123",
                "target": "operitrace-agent",
                "status": "running",
                "status_label": "Bot 處理中",
                "progress": "正在安全檢查",
                "worker_started": True,
                "final_observed": False,
                "exit_code": None,
                "pending_notes": 1,
                "evidence": "hook:pre_llm_call",
            }],
        }), detailed=True)
        self.assertIn("📋 TCB-20260910-ABC123｜Bot 處理中", rendered)
        self.assertIn("Worker turn：已啟動", rendered)
        self.assertNotIn('{"ok"', rendered)


if __name__ == "__main__":
    unittest.main()
