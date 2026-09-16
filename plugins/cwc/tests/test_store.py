"""Behavioral checks for the local store; all data and process races are disposable."""

import importlib.util
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "cwc" / "scripts"
sys.path.insert(0, str(SCRIPTS))
from host import HostDataError, reply
from store import Store, StoreError


RACE_WORKER = """
import json, pathlib, sys, time
sys.path.insert(0, sys.argv[1])
from store import Store
db = Store(sys.argv[2])
operation, scope, value = sys.argv[3], json.loads(sys.argv[4]), sys.argv[5]
ready, gate = pathlib.Path(sys.argv[6]), pathlib.Path(sys.argv[7])
ready.write_text('ready')
deadline = time.monotonic() + 10
while not gate.exists():
    if time.monotonic() > deadline:
        raise TimeoutError('race gate did not open')
    time.sleep(.01)
try:
    result = getattr(db, operation)(scope, value)
except Exception as error:
    result = {'error': type(error).__name__, 'message': str(error)}
print(json.dumps(result))
"""


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cwc-store-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "state.sqlite3"
        self.store = Store(self.path)
        self.scope = {"host_id": "local", "thread_id": "task-a", "workspace": str(self.root)}
        self.target = {"host_id": "chatgpt", "kind": "chatgpt",
                       "conversation_id": "chat-a", "title": "Test"}

    def prepare(self, body="Inspect the failing check.", mode="consult"):
        self.store.pair(self.scope, self.target)
        return self.store.prepare(self.scope, body, mode)["request_id"]

    def state(self, request_id):
        return self.store.get_exchange(self.scope, request_id)["state"]

    def race(self, operations):
        gate = self.root / "race-go"
        processes, ready_files = [], []
        try:
            for index, (operation, scope, value) in enumerate(operations):
                ready = self.root / f"ready-{index}"
                ready_files.append(ready)
                processes.append(subprocess.Popen(
                    [sys.executable, "-B", "-c", RACE_WORKER, str(SCRIPTS), str(self.path),
                     operation, json.dumps(scope), value, str(ready), str(gate)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                ))
            deadline = time.monotonic() + 10
            while not all(path.exists() for path in ready_files):
                self.assertLess(time.monotonic(), deadline, "race workers did not become ready")
                self.assertTrue(all(process.poll() is None for process in processes),
                                "a race worker exited before the gate")
                time.sleep(.01)
            gate.write_text("go", encoding="utf-8")
            results = []
            for process in processes:
                output, error = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 0, error)
                results.append(json.loads(output))
            return results
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.communicate()

    def test_pair_is_local_selection_and_same_target_preserves_generation(self):
        self.assertEqual(self.store.status(self.scope)["status"], "UNPAIRED")
        selected = self.store.pair(self.scope, self.target)
        renamed = self.store.pair(self.scope, {**self.target, "title": "Renamed"})
        self.assertEqual(selected["status"], "SELECTED")
        self.assertEqual(renamed["generation"], selected["generation"])
        self.assertIsNone(self.store.status(self.scope)["exchange"])
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM exchanges").fetchone()[0], 0)
        self.assertEqual(self.store.disconnect(self.scope), {"status": "UNPAIRED"})

    def test_invalid_inputs_do_not_create_a_pair_or_exchange(self):
        for scope in ({**self.scope, "workspace": "relative"}, {**self.scope, "thread_id": ""}):
            with self.subTest(scope=scope), self.assertRaises(StoreError):
                self.store.pair(scope, self.target)
        for target in ({**self.target, "kind": "codex"}, {**self.target, "conversation_id": ""},
                       {**self.target, "conversation_id": "chat-a\nchat-b"}):
            with self.subTest(target=target), self.assertRaises(StoreError):
                self.store.pair(self.scope, target)
        self.assertEqual(self.store.status(self.scope)["status"], "UNPAIRED")
        self.store.pair(self.scope, self.target)
        for body, mode in (("", "consult"), ("secret\x00suffix", "consult"), ("work", "execute")):
            with self.subTest(body=body, mode=mode), self.assertRaises(StoreError):
                self.store.prepare(self.scope, body, mode)
        self.assertIsNone(self.store.status(self.scope)["exchange"])

    def test_prepare_hides_payload_and_claim_releases_it_once_across_restart(self):
        self.store.pair(self.scope, self.target)
        prepared = self.store.prepare(self.scope, "Keep this exact body.", mode="review")
        request_id = prepared["request_id"]
        self.assertEqual(prepared["status"], "PREPARED")
        self.assertNotIn("payload", prepared)
        self.assertNotIn("body", prepared)
        self.assertNotIn("payload", self.store.status(self.scope)["exchange"])
        claimed = self.store.claim(self.scope, request_id)
        self.assertEqual((claimed["status"], claimed["state"]), ("SEND_ONCE", "UNKNOWN"))
        self.assertEqual(claimed["target"]["conversation_id"], "chat-a")
        self.assertIn("Keep this exact body.", claimed["payload"])
        self.assertIn(f"CWC REQUEST {request_id}", claimed["payload"])
        repeated = Store(self.path).claim(self.scope, request_id)
        self.assertEqual(repeated["status"], "ALREADY_CLAIMED")
        self.assertNotIn("payload", repeated)
        self.assertEqual(self.state(request_id), "UNKNOWN")

    def test_receipt_ids_remain_independent_and_conflicts_do_not_overwrite(self):
        request_id = self.prepare()
        with self.assertRaises(StoreError):
            self.store.record_send(self.scope, request_id, {"accepted": True})
        self.store.claim(self.scope, request_id)
        uncertain = {"accepted": False, "error": "Host result unavailable"}
        self.assertEqual(self.store.record_send(self.scope, request_id, uncertain)["state"], "UNKNOWN")
        receipt = {"accepted": True, "message_id": "host-message-7", "turn_id": "host-turn-3"}
        self.assertEqual(self.store.record_send(self.scope, request_id, receipt)["state"], "WAITING")
        record = self.store.get_exchange(self.scope, request_id)
        self.assertEqual(record["request_id"], request_id)
        self.assertEqual(record["receipt"]["message_id"], "host-message-7")
        with self.assertRaises(StoreError):
            self.store.record_send(self.scope, request_id, {**receipt, "message_id": "other-message"})
        self.assertEqual(self.store.get_exchange(self.scope, request_id)["receipt"], record["receipt"])

    def test_successful_receipt_without_host_message_id_does_not_invent_one(self):
        request_id = self.prepare()
        self.store.claim(self.scope, request_id)
        self.store.record_send(self.scope, request_id, {"accepted": True})
        record = self.store.get_exchange(self.scope, request_id)
        self.assertEqual(record["state"], "WAITING")
        self.assertIsNone(record["receipt"]["message_id"])
        self.assertIsNone(record["receipt"]["turn_id"])

    def test_unknown_exchange_cannot_retry_cancel_or_change_its_target(self):
        request_id = self.prepare()
        self.store.claim(self.scope, request_id)
        operations = [lambda: self.store.cancel(self.scope, request_id),
                      lambda: self.store.disconnect(self.scope),
                      lambda: self.store.pair(self.scope, {**self.target, "conversation_id": "chat-b"}),
                      lambda: self.store.prepare(self.scope, "Retry")]
        for operation in operations:
            with self.assertRaises(StoreError):
                operation()
        generation = self.store.status(self.scope)["generation"]
        self.store.pair(self.scope, {**self.target, "title": "Label only"})
        self.assertEqual(self.store.status(self.scope)["generation"], generation)
        self.assertNotIn("payload", self.store.claim(self.scope, request_id))
        self.assertEqual(self.state(request_id), "UNKNOWN")

    def test_only_complete_correlated_host_reply_enters_the_store(self):
        request_id = self.prepare()
        self.store.claim(self.scope, request_id)
        def raw(marker=request_id, status="completed", chat="chat-a"):
            return {"threadId": chat, "turns": [{"id": "turn-reply", "status": status,
                    "items": [{"id": "reply-1", "type": "agentMessage",
                               "text": f"CWC RESPONSE {marker}\nUse the small fix."}]}]}
        self.assertIsNone(reply(raw(status="running"), self.target, request_id))
        self.assertIsNone(reply(raw(marker="req_" + "0" * 32), self.target, request_id))
        with self.assertRaises(HostDataError):
            reply(raw(chat="other-chat"), self.target, request_id)
        self.assertEqual(self.state(request_id), "UNKNOWN")
        accepted = reply(raw(), self.target, request_id)
        result = self.store.receive(self.scope, request_id, **accepted)
        self.assertEqual(result["status"], "READY")

    def test_restart_accepts_late_reply_once_and_consumption_is_idempotent(self):
        request_id = self.prepare()
        self.store.claim(self.scope, request_id)
        restarted = Store(self.path)
        self.assertEqual(restarted.receive(self.scope, request_id, "reply-1", "One answer.")["state"], "READY")
        duplicate = restarted.receive(self.scope, request_id, "reply-copy", "One answer.")
        self.assertEqual(duplicate["status"], "DUPLICATE")
        self.assertEqual(restarted.get_exchange(self.scope, request_id)["reply_id"], "reply-1")
        with self.assertRaises(StoreError):
            restarted.receive(self.scope, request_id, "reply-1", "Conflicting answer.")
        with self.assertRaises(StoreError):
            restarted.disconnect(self.scope)
        consumed = restarted.consume(self.scope, request_id)
        self.assertEqual((consumed["status"], consumed["answer"]), ("CONSUMED", "One answer."))
        repeated = Store(self.path).consume(self.scope, request_id)
        self.assertEqual(repeated["status"], "ALREADY_CONSUMED")
        self.assertNotIn("answer", repeated)
        self.assertEqual(restarted.receive(self.scope, request_id, "reply-copy", "One answer.")["status"], "DUPLICATE")
        self.assertIsNone(restarted.status(self.scope)["exchange"])

    def test_foreign_task_scope_cannot_read_or_mutate_an_exchange(self):
        request_id = self.prepare()
        foreign = {**self.scope, "thread_id": "task-b"}
        self.store.pair(foreign, {**self.target, "conversation_id": "chat-b"})
        operations = [lambda: self.store.get_exchange(foreign, request_id),
                      lambda: self.store.claim(foreign, request_id),
                      lambda: self.store.record_send(foreign, request_id, {"accepted": True}),
                      lambda: self.store.receive(foreign, request_id, "reply-1", "Answer"),
                      lambda: self.store.consume(foreign, request_id),
                      lambda: self.store.cancel(foreign, request_id)]
        for operation in operations:
            with self.assertRaises(StoreError):
                operation()
        for foreign in ({**self.scope, "host_id": "other-host"},
                        {**self.scope, "workspace": str(self.root / "other-workspace")}):
            with self.assertRaises(StoreError):
                self.store.get_exchange(foreign, request_id)
        self.assertEqual(self.state(request_id), "PREPARED")

    def test_cancel_before_claim_releases_target_without_reviving_old_generation(self):
        request_id = self.prepare()
        other = {**self.scope, "thread_id": "task-b"}
        self.store.pair(other, self.target)
        with self.assertRaises(StoreError):
            self.store.prepare(other, "Same global target")
        self.assertEqual(self.store.cancel(self.scope, request_id)["state"], "CANCELLED")
        self.assertNotIn("payload", self.store.claim(self.scope, request_id))
        self.assertEqual(self.store.prepare(other, "Target released")["status"], "PREPARED")
        generation = self.store.status(self.scope)["generation"]
        self.store.pair(self.scope, {**self.target, "conversation_id": "chat-b"})
        self.assertGreater(self.store.status(self.scope)["generation"], generation)
        with self.assertRaises(StoreError):
            self.store.receive(self.scope, request_id, "stale-reply", "Old target answer")
        self.assertEqual(self.store.disconnect(self.scope)["status"], "UNPAIRED")

    def test_prompt_update_and_rejected_schema_preserve_pending_payload(self):
        package = self.root / "copied-skill"
        (package / "scripts").mkdir(parents=True)
        shutil.copyfile(SCRIPTS / "store.py", package / "scripts" / "store.py")
        rules = package / "REASONER.md"
        rules.write_text("Original reasoning rules.", encoding="utf-8")
        spec = importlib.util.spec_from_file_location("isolated_cwc_store", package / "scripts" / "store.py")
        isolated = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(isolated)
        original = isolated.Store(self.path)
        original.pair(self.scope, self.target)
        request_id = original.prepare(self.scope, "Original request.")["request_id"]
        frozen = original.get_exchange(self.scope, request_id)["payload"]
        rules.write_text("Changed reasoning rules.", encoding="utf-8")
        self.assertEqual(isolated.Store(self.path).claim(self.scope, request_id)["payload"], frozen)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("PRAGMA user_version=99")
            db.commit()
        with self.assertRaises(isolated.StoreError):
            isolated.Store(self.path)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 99)
            self.assertEqual(db.execute("SELECT state,payload FROM exchanges WHERE request_id=?",
                                        (request_id,)).fetchone(), ("UNKNOWN", frozen))

    def test_two_processes_can_claim_only_one_sendable_payload(self):
        request_id = self.prepare()
        results = self.race([("claim", self.scope, request_id)] * 2)
        self.assertCountEqual([result.get("status") for result in results], ["SEND_ONCE", "ALREADY_CLAIMED"])
        self.assertEqual(sum("payload" in result for result in results), 1)
        self.assertEqual(self.state(request_id), "UNKNOWN")

    def test_two_task_processes_cannot_reserve_the_same_global_target(self):
        other = {**self.scope, "thread_id": "task-b"}
        self.store.pair(self.scope, self.target)
        self.store.pair(other, self.target)
        results = self.race([("prepare", self.scope, "First task"), ("prepare", other, "Second task")])
        self.assertEqual(sum(result.get("status") == "PREPARED" for result in results), 1)
        self.assertEqual(sum(result.get("error") == "StoreError" for result in results), 1)
        self.assertEqual(sum(self.store.status(scope)["exchange"] is not None
                             for scope in (self.scope, other)), 1)


if __name__ == "__main__":
    unittest.main()
