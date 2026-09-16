"""Stopped work, migration and read-only diagnostics use disposable local state only."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from threading import Barrier
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "cwc" / "scripts"))
from store import Store, StoreError, _scope, _suggested_title


V1_COLUMNS = ("request_id,scope,generation,target_host,target_kind,target_id,mode,"
              "format_version,payload,state,receipt,reply_id,answer,answer_hash")


class StopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cwc-stop-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "state.sqlite3"
        self.scope = {"host_id": "local", "thread_id": "task-a", "workspace": str(self.root)}
        self.target = {"host_id": "chatgpt", "kind": "chatgpt", "conversation_id": "chat-a", "title": "Test"}

    def prepared(self):
        store = Store(self.path)
        store.pair(self.scope, self.target)
        return store, store.prepare(self.scope, "Disposable question.")["request_id"]

    def v1(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("""CREATE TABLE pairs (scope TEXT PRIMARY KEY, target_host TEXT, target_kind TEXT,
                target_id TEXT, title TEXT NOT NULL, generation INTEGER NOT NULL)""")
            db.execute("""CREATE TABLE exchanges (
                request_id TEXT PRIMARY KEY, scope TEXT NOT NULL REFERENCES pairs(scope),
                generation INTEGER NOT NULL, target_host TEXT NOT NULL, target_kind TEXT NOT NULL,
                target_id TEXT NOT NULL, mode TEXT NOT NULL, format_version INTEGER NOT NULL,
                payload TEXT NOT NULL, state TEXT NOT NULL CHECK
                (state IN ('PREPARED','UNKNOWN','WAITING','READY','CONSUMED','CANCELLED')),
                receipt TEXT, reply_id TEXT, answer TEXT, answer_hash TEXT)""")
            for columns, name in (("scope", "one_pending_pair"),
                                  ("target_host,target_kind,target_id", "one_pending_target")):
                db.execute(f"""CREATE UNIQUE INDEX {name} ON exchanges({columns})
                    WHERE state IN ('PREPARED','UNKNOWN','WAITING','READY')""")
            for index, state in enumerate(("PREPARED", "UNKNOWN", "WAITING", "READY", "CONSUMED", "CANCELLED")):
                scope = _scope({**self.scope, "thread_id": f"task-{index}"})
                answer = f"Saved answer {index}" if state in ("READY", "CONSUMED") else None
                db.execute("INSERT INTO pairs VALUES(?,?,?,?,?,?)",
                           (scope, "chatgpt", "chatgpt", f"chat-{index}", f"标题 {index}", index + 9))
                db.execute(f"INSERT INTO exchanges(rowid,{V1_COLUMNS}) VALUES({','.join('?' * 15)})",
                           (index * 3 + 1, "req_" + f"{index + 1:032x}", scope, index + 9,
                            "chatgpt", "chatgpt", f"chat-{index}", "review", 1,
                            f"Frozen payload\n{index} 中文", state,
                            json.dumps({"accepted": True, "message_id": f"message-{index}"}),
                            f"reply-{index}" if answer else None, answer,
                            hashlib.sha256(answer.encode()).hexdigest() if answer else None))
            db.execute("PRAGMA user_version=1")
            db.commit()

    def snapshot(self):
        with closing(sqlite3.connect(self.path)) as db:
            return (db.execute("SELECT scope,target_host,target_kind,target_id,title,generation FROM pairs ORDER BY scope").fetchall(),
                    db.execute(f"SELECT rowid,{V1_COLUMNS} FROM exchanges ORDER BY rowid").fetchall())

    def test_v1_migration_preserves_every_old_field_state_and_row_order(self):
        self.v1()
        before = self.snapshot()
        Store(self.path)
        self.assertEqual(self.snapshot(), before)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 4)
            self.assertEqual({row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")},
                             {"pairs", "exchanges"})
            self.assertEqual(db.execute("SELECT stop_reason,stopped_at FROM exchanges").fetchall(), [(None, None)] * 6)
            self.assertEqual(db.execute("SELECT task_title FROM pairs").fetchall(), [("",)] * 6)
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
        Store(self.path)
        self.assertEqual(self.snapshot(), before)

    def test_failed_migration_rolls_back_schema_and_all_records(self):
        self.v1()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE exchanges SET scope='orphan' WHERE rowid=1")
            db.commit()
        before, raw = self.snapshot(), self.path.read_bytes()
        with self.assertRaises(sqlite3.IntegrityError):
            Store(self.path)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.path.read_bytes(), raw)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(len(db.execute("PRAGMA table_info(exchanges)").fetchall()), 14)
            self.assertEqual(len(db.execute("PRAGMA table_info(pairs)").fetchall()), 6)
            self.assertEqual({row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")},
                             {"pairs", "exchanges"})

    def test_abandoned_request_allows_switch_but_quarantines_old_target_globally(self):
        store, request_id = self.prepared()
        store.claim(self.scope, request_id)
        store.record_send(self.scope, request_id, {"accepted": True, "message_id": "original-receipt"})
        before = store.get_exchange(self.scope, request_id)
        stopped = store.abandon(self.scope, request_id, "Switch to another target", confirmed=True)
        self.assertEqual(stopped["status"], "ABANDONED")
        self.assertTrue(stopped["stopped_at"].endswith("Z"))
        self.assertIsNone(store.status(self.scope)["exchange"])
        self.assertTrue(Store.read_status(self.path, self.scope, details=True)["target_reserved"])
        with self.assertRaises(StoreError):
            store.prepare(self.scope, "Must not reuse old target")
        other = {**self.scope, "thread_id": "task-foreign"}
        store.pair(other, self.target)
        with self.assertRaises(StoreError):
            store.prepare(other, "Other task must also wait")
        foreign = Store.read_status(self.path, other, details=True)
        self.assertTrue(foreign["target_reserved"])
        self.assertEqual(foreign["stopped_exchanges"], [])
        self.assertNotIn(request_id, json.dumps(foreign))
        store.disconnect(self.scope)
        store.pair(self.scope, {**self.target, "conversation_id": "chat-b"})
        next_id = store.prepare(self.scope, "New target is available")["request_id"]
        self.assertNotIn("payload", store.claim(self.scope, request_id))
        self.assertNotIn("answer", store.consume(self.scope, request_id))
        self.assertEqual(store.record_send(self.scope, request_id, {"accepted": True})["state"], "ABANDONED")
        after = store.get_exchange(self.scope, request_id)
        for key in ("payload", "receipt", "generation", "target"):
            self.assertEqual(after[key], before[key])
        self.assertEqual(store.status(self.scope)["exchange"]["request_id"], next_id)

    def test_late_reply_is_discarded_without_delivery_and_releases_old_target(self):
        store, request_id = self.prepared()
        store.claim(self.scope, request_id)
        store.abandon(self.scope, request_id, "No longer waiting", confirmed=True)
        store.pair(self.scope, {**self.target, "conversation_id": "chat-b"})
        next_id = store.prepare(self.scope, "A separate new question")["request_id"]
        before = store.status(self.scope)
        self.assertEqual(store.receive(self.scope, request_id, "late-reply", "Late evidence.")["status"], "DISCARDED")
        self.assertEqual(store.status(self.scope), before)
        self.assertEqual(store.get_exchange(self.scope, request_id)["answer"], "Late evidence.")
        self.assertNotIn("answer", store.consume(self.scope, request_id))
        self.assertEqual(store.receive(self.scope, request_id, "copy", "Late evidence.")["status"], "DISCARDED")
        with self.assertRaises(StoreError):
            store.receive(self.scope, request_id, "conflict", "Different answer.")
        self.assertEqual(store.get_exchange(self.scope, request_id)["reply_id"], "late-reply")
        self.assertEqual(Store.read_status(self.path, self.scope, details=True)["stopped_count"], 0)
        other = {**self.scope, "thread_id": "task-foreign"}
        store.pair(other, self.target)
        self.assertEqual(store.prepare(other, "Old target now released")["status"], "PREPARED")
        self.assertEqual(store.status(self.scope)["exchange"]["request_id"], next_id)

    def test_confirmation_ownership_and_idempotency_preserve_stop_evidence(self):
        store, request_id = self.prepared()
        store.claim(self.scope, request_id)
        before = store.get_exchange(self.scope, request_id)
        for confirmed, reason in ((False, "Reason"), (1, "Reason"), (True, ""),
                                  (True, "\x00"), (True, "汉" * 1366)):
            with self.subTest(confirmed=confirmed, length=len(reason)), self.assertRaises(StoreError):
                store.abandon(self.scope, request_id, reason, confirmed=confirmed)
        with self.assertRaises(StoreError):
            store.abandon({**self.scope, "thread_id": "foreign"}, request_id, "Reason", confirmed=True)
        self.assertEqual(store.get_exchange(self.scope, request_id), before)
        stopped = store.abandon(self.scope, request_id, "First reason", confirmed=True)
        self.assertEqual(Store(self.path).abandon(self.scope, request_id, "New reason", confirmed=True), stopped)
        details = Store.read_status(self.path, self.scope, details=True)
        self.assertEqual(details["stopped_count"], 1)
        self.assertEqual(details["stopped_exchanges"], [{key: stopped[key] for key in
                         ("request_id", "state", "mode", "stop_reason", "stopped_at")}])
        self.assertEqual(details["target"], {key: self.target[key] for key in ("host_id", "kind", "conversation_id")})

    def test_prepared_ready_and_consumed_have_distinct_stop_semantics(self):
        store, request_id = self.prepared()
        self.assertEqual(store.abandon(self.scope, request_id, "Never sent", confirmed=True)["state"], "CANCELLED")
        self.assertNotIn("payload", store.claim(self.scope, request_id))
        request_id = store.prepare(self.scope, "Ready question")["request_id"]
        store.claim(self.scope, request_id)
        store.receive(self.scope, request_id, "reply-ready", "An answer")
        self.assertEqual(store.abandon(self.scope, request_id, "Do not deliver", confirmed=True)["state"], "DISCARDED")
        self.assertNotIn("answer", store.consume(self.scope, request_id))
        self.assertFalse(Store.read_status(self.path, self.scope, details=True)["target_reserved"])
        request_id = store.prepare(self.scope, "Consumed question")["request_id"]
        store.claim(self.scope, request_id)
        store.receive(self.scope, request_id, "reply-consumed", "Consumed answer")
        store.consume(self.scope, request_id)
        before = store.get_exchange(self.scope, request_id)
        self.assertEqual(store.abandon(self.scope, request_id, "Too late", confirmed=True)["status"], "ALREADY_CONSUMED")
        self.assertEqual(store.get_exchange(self.scope, request_id), before)

    def test_consume_and_stop_race_never_delivers_after_a_successful_stop(self):
        store, request_id = self.prepared()
        store.claim(self.scope, request_id)
        store.receive(self.scope, request_id, "reply", "Answer")
        gate = Barrier(2)
        def run(stop):
            gate.wait(timeout=5)
            return (store.abandon(self.scope, request_id, "Racing stop", confirmed=True) if stop
                    else store.consume(self.scope, request_id))
        with ThreadPoolExecutor(max_workers=2) as pool:
            stopped, consumed = list(pool.map(run, (True, False)))
        if stopped["status"] == "DISCARDED":
            self.assertEqual(consumed["status"], "DISCARDED")
            self.assertNotIn("answer", consumed)
        else:
            self.assertEqual(stopped["status"], "ALREADY_CONSUMED")
            self.assertEqual(consumed["answer"], "Answer")

    def test_read_status_keeps_schema1_bytes_and_requires_no_state_creation(self):
        missing = self.root / "missing" / "state.sqlite3"
        self.assertEqual(Store.read_status(missing, self.scope, details=True)["stopped_count"], 0)
        self.assertFalse(missing.parent.exists())
        self.v1()
        scope = {**self.scope, "thread_id": "task-1"}
        before = {path.name: path.read_bytes() for path in self.root.iterdir()}
        for details in (False, True):
            result = Store.read_status(self.path, scope, details=details)
            self.assertEqual(result["exchange"]["state"], "UNKNOWN")
            self.assertEqual(result["task_title"], "")
            self.assertIn("-未命名任务|", result["suggested_title"])
            if details:
                self.assertEqual(result["stopped_exchanges"], [])
                self.assertFalse(result["target_reserved"])
        override = Store.read_status(self.path, scope, task_title="实际任务标题")
        self.assertEqual(override["task_title"], "实际任务标题")
        self.assertIn("-实际任务标题|", override["suggested_title"])
        self.assertEqual({path.name: path.read_bytes() for path in self.root.iterdir()}, before)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)

    def test_suggested_title_is_stable_per_pair_and_present_in_actual_payload(self):
        self.scope["workspace"] = str(self.root / "技能提升")
        store = Store(self.path)
        selected = store.pair(self.scope, self.target, task_title="Astra评估 CWC 插件重构方案")
        title = selected["suggested_title"]
        request_id = store.prepare(self.scope, "Actual question")["request_id"]
        self.assertRegex(title, r"^CWC\|技能提升-Astra评估 CWC 插件重构方案\|[A-F0-9]{8}$")
        self.assertEqual(Store.read_status(self.path, self.scope)["suggested_title"], title)
        self.assertEqual(store.pair(self.scope, {**self.target, "title": "Another model label"})["suggested_title"], title)
        before = self.path.read_bytes()
        override = Store.read_status(self.path, self.scope, task_title="Temporary refresh title")
        self.assertIn("-Temporary refresh title|", override["suggested_title"])
        self.assertEqual(override["suggested_title"].rsplit("|", 1)[1], title.rsplit("|", 1)[1])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(store.status(self.scope)["suggested_title"], title)
        renamed = store.pair(self.scope, self.target, task_title="Renamed task")
        self.assertEqual(renamed["generation"], selected["generation"])
        self.assertIn("-Renamed task|", renamed["suggested_title"])
        self.assertEqual(renamed["suggested_title"].rsplit("|", 1)[1], title.rsplit("|", 1)[1])
        self.assertIn(f"建议对话名称：{title}\n", store.claim(self.scope, request_id)["payload"])
        suffix = title.rsplit("|", 1)[1]
        self.assertNotEqual(_suggested_title(_scope({**self.scope, "thread_id": "another-task"}), self.target).rsplit("|", 1)[1], suffix)
        self.assertNotEqual(_suggested_title(_scope(self.scope), {**self.target, "conversation_id": "another-chat"}).rsplit("|", 1)[1], suffix)
        key = json.dumps(["local", "task", str(self.root / ("long|project\n" * 5))])
        injected = _suggested_title(key, self.target, "inject|task\n\u0085\u200e" * 30)
        self.assertRegex(injected, r"^CWC\|[^|\n]{1,32}-[^|\n]{1,80}\|[A-F0-9]{8}$")
        self.assertTrue(injected.isprintable())

    def test_prepare_updates_task_title_atomically_and_rejects_invalid_metadata(self):
        store, request_id = self.prepared()
        before = store.status(self.scope)
        with self.assertRaises(StoreError):
            store.prepare(self.scope, "Pending request must prevent this", task_title="Must roll back")
        self.assertEqual(store.status(self.scope), before)
        store.cancel(self.scope, request_id)
        request_id = store.prepare(self.scope, "A new question", task_title="Current exact task title")["request_id"]
        changed = store.status(self.scope)
        self.assertEqual(changed["task_title"], "Current exact task title")
        self.assertEqual(changed["suggested_title"].rsplit("|", 1)[1], before["suggested_title"].rsplit("|", 1)[1])
        self.assertIn(changed["suggested_title"], store.get_exchange(self.scope, request_id)["payload"])
        for task_title in (123, "\x00", "汉" * 1366):
            with self.subTest(task_title_type=type(task_title).__name__):
                for operation in (lambda: store.pair(self.scope, self.target, task_title=task_title),
                                  lambda: store.prepare(self.scope, "Invalid", task_title=task_title),
                                  lambda: Store.read_status(self.path, self.scope, task_title=task_title)):
                    with self.assertRaises(StoreError):
                        operation()
        self.assertEqual(store.status(self.scope), changed)


if __name__ == "__main__":
    unittest.main()
