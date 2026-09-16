"""Controller lifecycle checks with disposable state and synthetic Host results."""

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "cwc" / "scripts"))
from cwc import execute
from store import Store


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cwc-cli-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.workspace = self.root / "workspace"
        self.scope = {"host_id": "local", "thread_id": "task-a", "workspace": str(self.workspace)}
        self.listing = {"threads": [
            {"id": "chat-a", "kind": "chatgpt", "title": "Same title"},
            {"id": "chat-b", "kind": "chatgpt", "title": "Same title"},
            {"id": "codex-a", "kind": "codex", "title": "Same title"},
        ]}
        # Keep the legacy package-state lookup inside this disposable fixture too.
        legacy_file = self.root / "package" / "skills" / "cwc" / "scripts" / "legacy.py"
        self.legacy_patch = patch("legacy.__file__", str(legacy_file))
        self.legacy_patch.start()
        self.addCleanup(self.legacy_patch.stop)

    def run_action(self, action, **data):
        # execute opens the persisted store anew on every invocation.
        return execute({"action": action, "scope": self.scope, **data}, self.home)

    def pair(self):
        return self.run_action("pair", listing=self.listing,
                               conversation_id="chat-a", target_host_id="chatgpt")

    def prepare(self):
        self.pair()
        return self.run_action("prepare", body="Review the small fix.")["request_id"]

    def snapshot(self, request_id, status="completed"):
        return {"thread": {"id": "chat-a", "kind": "chatgpt"},
                "turns": [{"id": "host-turn-4", "status": status,
                           "items": [{"type": "agentMessage", "id": "host-reply-8",
                                      "text": f"CWC RESPONSE {request_id}\nKeep the small fix."}]}]}

    def write_json(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def test_pair_uses_exact_listing_identity_without_emitting_host_call(self):
        selected = self.pair()
        self.assertEqual(selected["status"], "SELECTED")
        self.assertNotIn("tool", selected)
        status = self.run_action("status")
        self.assertIsNone(status["exchange"])
        self.assertIsNone(status["last_exchange"])
        self.assertEqual(self.pair()["generation"], selected["generation"])
        for conversation_id, target_host_id in (("missing", "chatgpt"), ("chat-a", "local"),
                                                ("codex-a", "chatgpt")):
            with self.subTest(conversation_id=conversation_id, host=target_host_id):
                with self.assertRaises(ValueError):
                    self.run_action("pair", listing=self.listing, conversation_id=conversation_id,
                                    target_host_id=target_host_id)
        self.assertEqual(self.run_action("status")["generation"], selected["generation"])

    def test_prepare_claim_emits_frozen_send_arguments_only_once(self):
        request_id = self.prepare()
        claimed = self.run_action("claim", request_id=request_id)
        self.assertEqual(claimed["tool"], "mcp__codex_app__send_message_to_thread")
        self.assertEqual(claimed["arguments"]["threadId"], "chat-a")
        self.assertNotIn("hostId", claimed["arguments"])
        self.assertIn(f"CWC REQUEST {request_id}", claimed["arguments"]["prompt"])
        self.assertIn("Review the small fix.", claimed["arguments"]["prompt"])
        self.assertEqual(self.run_action("status")["exchange"]["state"], "UNKNOWN")
        repeated = self.run_action("claim", request_id=request_id)
        self.assertEqual(repeated["status"], "ALREADY_CLAIMED")
        self.assertNotIn("arguments", repeated)
        self.assertNotIn("tool", repeated)

    def test_pair_and_prepare_freeze_contract_metadata_and_drift_blocks_claim(self):
        selected = self.pair()
        self.assertRegex(selected["contract_hash"], r"^[0-9a-f]{64}$")
        request_id = self.run_action("prepare", body="Check the contract snapshot.")["request_id"]
        exchange = Store(self.home / "cwc" / "state" / "cwc.sqlite3").get_exchange(self.scope, request_id)
        self.assertRegex(exchange["contract_hash"], r"^[0-9a-f]{64}$")
        self.assertIn("CWC CONTRACT v" + exchange["contract_version"] + " sha256=" + exchange["contract_hash"], exchange["payload"])
        with closing(sqlite3.connect(self.home / "cwc" / "state" / "cwc.sqlite3")) as db:
            db.execute("UPDATE exchanges SET contract_hash=? WHERE request_id=?",
                       ("0" * 64, request_id))
            db.commit()
        with self.assertRaisesRegex(ValueError, "frozen CWC contract"):
            self.run_action("claim", request_id=request_id)
        self.assertEqual(self.run_action("status")["exchange"]["state"], "PREPARED")

    def test_bad_frozen_prompt_fails_before_claiming_a_send(self):
        request_id = self.prepare()
        for prompt in ("", f"CWC REQUEST {request_id}\n ", "CWC REQUEST wrong\nbody"):
            with closing(sqlite3.connect(self.home / "cwc/state/cwc.sqlite3")) as db:
                db.execute("UPDATE exchanges SET payload=? WHERE request_id=?", (prompt, request_id))
                db.commit()
            with self.subTest(prompt=prompt), self.assertRaisesRegex(ValueError, "prompt"):
                self.run_action("claim", request_id=request_id)
            self.assertEqual(self.run_action("status")["exchange"]["state"], "PREPARED")

    def test_record_send_rejects_wrong_calls_and_keeps_host_error_uncertain(self):
        request_id = self.prepare()
        arguments = self.run_action("claim", request_id=request_id)["arguments"]
        for altered in ({**arguments, "threadId": "chat-b"},
                        {**arguments, "prompt": "Modified request"}):
            with self.subTest(altered=altered), self.assertRaises(ValueError):
                self.run_action("record-send", request_id=request_id, called_with=altered,
                                result={"accepted": True})
        for receipt in ({"accepted": True, "threadId": "chat-b"},
                        {"isError": True, "structuredContent": {"accepted": True}}):
            result = self.run_action("record-send", request_id=request_id,
                                     called_with=arguments, result=receipt)
            self.assertEqual(result["state"], "UNKNOWN")
        accepted = self.run_action("record-send", request_id=request_id, called_with=arguments,
                                   result={"status": "queued", "threadId": "chat-a",
                                           "messageId": "host-message-7", "turnId": "host-turn-2"})
        self.assertEqual(accepted["state"], "WAITING")
        stored = Store(self.home / "cwc" / "state" / "cwc.sqlite3").get_exchange(self.scope, request_id)
        self.assertEqual(stored["receipt"]["message_id"], "host-message-7")
        self.assertEqual(stored["request_id"], request_id)

    def test_invalid_stop_does_not_create_state(self):
        valid_id = "req_" + "a" * 32
        for fields in ({"request_id": valid_id, "reason": "User stopped", "confirmed": False},
                       {"request_id": "invalid", "reason": "User stopped", "confirmed": True},
                       {"request_id": valid_id, "reason": "", "confirmed": True},
                       {"request_id": valid_id, "reason": "User stopped", "confirmed": True}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.run_action("stop", **fields)
            self.assertFalse(self.home.exists())

    def test_resume_idle_does_not_create_state(self):
        self.assertEqual(self.run_action("resume"), {"status": "IDLE", "host_actions": 0})
        self.assertFalse(self.home.exists())

    def test_resume_recovers_every_phase_without_claiming_or_replaying(self):
        request_id = self.prepare()
        self.assertEqual(self.run_action("resume")["status"], "PREPARED")
        self.assertEqual(self.run_action("status")["exchange"]["state"], "PREPARED")
        self.run_action("claim", request_id=request_id)
        self.assertEqual(self.run_action("resume")["status"], "READ_PENDING")
        self.run_action("receive", request_id=request_id, called_with={"threadId": "chat-a"},
                        result=self.snapshot(request_id))
        first = self.run_action("resume")
        self.assertEqual((first["status"], first["answer"]), ("CONSUMED", "Keep the small fix."))
        self.run_action("disconnect")
        recovered = self.run_action("resume")
        self.assertEqual((recovered["status"], recovered["answer"]), ("RECOVERED", first["answer"]))
        self.assertTrue(recovered["effect_check_required"])
        self.assertNotIn("tool", recovered)
        self.assertEqual(self.run_action("status")["last_exchange"]["state"], "CONSUMED")

    def test_resume_prefers_pending_and_never_delivers_stopped_answers(self):
        request_id = self.prepare()
        self.run_action("claim", request_id=request_id)
        self.run_action("stop", request_id=request_id, reason="User stopped", confirmed=True)
        self.assertEqual(self.run_action("resume")["status"], "READ_PENDING")
        self.run_action("accept", request_id=request_id, called_with={"threadId": "chat-a"},
                        result=self.snapshot(request_id))
        stopped = self.run_action("resume")
        self.assertEqual(stopped["status"], "DISCARDED")
        self.assertNotIn("answer", stopped)
        next_id = self.run_action("prepare", body="A different, new consultation")["request_id"]
        self.assertEqual(self.run_action("resume")["request_id"], next_id)
        with self.assertRaisesRegex(ValueError, "scope"):
            execute({"action": "resume", "scope": {**self.scope, "thread_id": "other-task"},
                     "request_id": next_id}, self.home)

    def test_wrong_version_fails_before_any_state_write(self):
        for action, extra in (("status", {}), ("resume", {}), ("prepare", {"body": "Question"})):
            with self.subTest(action=action), self.assertRaisesRegex(ValueError, "version"):
                self.run_action(action, expected_version="definitely-stale", **extra)
            self.assertFalse(self.home.exists())

    def test_accept_distinguishes_missing_pending_and_format_mismatch(self):
        request_id = self.prepare()
        self.run_action("claim", request_id=request_id)
        missing = self.snapshot("req_" + "0" * 32)
        pending = self.snapshot(request_id, "running")
        formatted = self.snapshot(request_id)
        formatted["turns"][0]["items"][0]["text"] = f"**CWC RESPONSE {request_id}**\nAnswer"
        for raw, code in ((missing, "NO_MATCH"), (pending, "REPLY_PENDING"), (formatted, "RESPONSE_FORMAT_MISMATCH")):
            result = self.run_action("accept", request_id=request_id, called_with={"threadId": "chat-a"}, result=raw)
            self.assertEqual((result["status"], result["reason"], result["state"]), ("NO_MATCH", code, "UNKNOWN"))
            self.assertNotIn("answer", result)

    def test_read_receive_and_consume_late_answer_after_restart(self):
        request_id = self.prepare()
        self.run_action("claim", request_id=request_id)
        read = self.run_action("read", request_id=request_id, cursor="older-page")
        self.assertEqual(read["tool"], "mcp__codex_app__read_thread")
        self.assertEqual(read["arguments"]["threadId"], "chat-a")
        self.assertEqual(read["arguments"]["cursor"], "older-page")
        for snapshot in (self.snapshot(request_id, "running"), self.snapshot("req_" + "0" * 32)):
            result = self.run_action("receive", request_id=request_id,
                                     called_with=read["arguments"], result=snapshot)
            self.assertEqual((result["status"], result["state"]), ("NO_MATCH", "UNKNOWN"))
        received = self.run_action("receive", request_id=request_id,
                                   called_with=read["arguments"], result=self.snapshot(request_id))
        self.assertEqual(received["state"], "READY")
        self.assertNotIn("tool", self.run_action("read", request_id=request_id))
        self.assertEqual(self.run_action("consume", request_id=request_id)["answer"], "Keep the small fix.")
        consumed_again = self.run_action("consume", request_id=request_id)
        self.assertEqual(consumed_again["status"], "ALREADY_CONSUMED")
        self.assertNotIn("answer", consumed_again)

    def test_last_consumed_result_remains_inspectable_without_replay(self):
        request_id = self.prepare()
        self.run_action("claim", request_id=request_id)
        self.run_action("receive", request_id=request_id, called_with={"threadId": "chat-a"},
                        result=self.snapshot(request_id))
        self.run_action("consume", request_id=request_id)
        self.run_action("disconnect")
        status = self.run_action("status")
        self.assertEqual(status["status"], "UNPAIRED")
        self.assertEqual(status["last_exchange"], {"request_id": request_id, "state": "CONSUMED"})
        inspected = self.run_action("inspect", request_id=status["last_exchange"]["request_id"])
        self.assertEqual((inspected["status"], inspected["answer"]),
                         ("READ_ONLY_RECOVERY", "Keep the small fix."))
        self.assertEqual(self.run_action("consume", request_id=request_id)["status"], "ALREADY_CONSUMED")
        self.assertEqual(self.run_action("status"), status)

    def test_legacy_contract_guard_uses_original_null_separated_fingerprint(self):
        self.write_json(self.home / "cwc" / "state" / "bindings.json", {"bindings": {
            "task-a": {"codex_thread_ref": "task-a", "physical_chat_conversation_ref": "chat-a"}}})
        normalized = str(self.workspace.resolve()).replace("\\", "/").casefold()
        fingerprint = hashlib.sha256("\0".join((normalized, "task-a", "chat-a")).encode("utf-8")).hexdigest()
        registry = self.workspace / ".cwc" / "projects" / "registry.json"
        self.write_json(registry, {"projects": {fingerprint: {"reasoner_contract_sync_message_id": "old-message"}}})
        before = registry.read_bytes()
        with self.assertRaisesRegex(ValueError, "LEGACY_PENDING.*contract"):
            self.pair()
        self.assertEqual(self.run_action("status")["status"], "UNPAIRED")
        self.assertEqual(registry.read_bytes(), before)

    def test_consumed_legacy_exchange_overrides_stale_ready_consult(self):
        state = self.home / "cwc" / "state" / "state.json"
        self.write_json(state, {
            "pending_exchanges": {"old-exchange": {"task_id": "old-task", "business_id": "old-consult",
                "physical_conversation_ref": "chat-a", "state": "CONSUMED"}},
            "consults": {"old-consult": {"task_id": "old-task", "consult_id": "old-consult",
                "physical_conversation_ref": "chat-a", "status": "READY"}}})
        before = state.read_bytes()
        self.assertEqual(self.pair()["status"], "SELECTED")
        self.assertEqual(state.read_bytes(), before)
        data = json.loads(before)
        data["pending_exchanges"]["old-exchange"]["state"] = "READY"
        self.write_json(state, data)
        request_id = self.run_action("prepare", body="A request prepared before the legacy check.")["request_id"]
        with self.assertRaisesRegex(ValueError, "LEGACY_PENDING"):
            self.run_action("claim", request_id=request_id)
        self.assertEqual(self.run_action("status")["exchange"]["state"], "PREPARED")

    def test_canonical_legacy_binding_overrides_cached_binding_for_same_owner(self):
        cached = self.home / "plugins" / "cache" / "personal" / "cwc" / "old-version" / "state" / "bindings.json"
        self.write_json(cached, {"bindings": {"task-a": {
            "codex_thread_ref": "task-a", "physical_chat_conversation_ref": "chat-old",
            "pending_binding_id": "old-binding", "pending_physical_conversation_ref": "chat-a"}}})
        with self.assertRaisesRegex(ValueError, "LEGACY_PENDING.*binding"):
            self.pair()
        self.write_json(self.home / "cwc" / "state" / "bindings.json", {"bindings": {"task-a": {
            "codex_thread_ref": "task-a", "physical_chat_conversation_ref": "chat-b"}}})
        before = cached.read_bytes()
        self.assertEqual(self.pair()["status"], "SELECTED")
        self.assertEqual(cached.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
