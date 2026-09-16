"""Behavior checks for persistent waiting, linked rounds, completion and detailed replies."""
import copy
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/cwc/scripts"))
from cwc import execute, _local_resources
from host import HostDataError
from progress import check_progress
from store import Store, wait_policy


class ContinuityChecks(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="cwc-continuity-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.home = self.root / "home"
        self.path = self.home / "cwc/state/cwc.sqlite3"
        self.scope = {"host_id": "local", "thread_id": "task-a", "workspace": str(self.root)}
        self.store = Store(self.path)
        self.target = {"host_id": "chatgpt", "kind": "chatgpt", "conversation_id": "synthetic-a", "title": "Test"}
        self.store.pair(self.scope, self.target)
        legacy = patch("legacy.__file__", str(self.root / "package/skills/cwc/scripts/legacy.py"))
        legacy.start()
        self.addCleanup(legacy.stop)

    def act(self, action, **data):
        return execute({"action": action, "scope": self.scope, **data}, self.home)

    def prepare(self, **data):
        return self.act("prepare", body=data.pop("body", "Review the required change."), **data)["request_id"]

    def raw(self, request_id, text="Verified answer.", status="completed"):
        return {"thread": {"id": self.target["conversation_id"], "kind": "chatgpt"},
                "turns": [{"status": status, "items": [{"id": "reply-" + request_id, "type": "agentMessage",
                           "text": "CWC RESPONSE " + request_id + "\n" + text}]}]}

    def accept(self, request_id, text="Verified answer."):
        return self.act("accept", request_id=request_id, called_with={"threadId": self.target["conversation_id"]},
                        result=self.raw(request_id, text))

    def test_schema_three_migration_preserves_old_columns_and_rowids(self):
        req = self.prepare()
        self.act("claim", request_id=req)
        self.accept(req)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("DROP INDEX exchange_display_id")
            for column in ("display_id", "parent_request_id", "timing", "resource_hashes", "body_hash"):
                db.execute("ALTER TABLE exchanges DROP COLUMN " + column)
            db.execute("PRAGMA user_version=3")
            before = db.execute("SELECT rowid,* FROM exchanges").fetchall()
            columns = [row[1] for row in db.execute("PRAGMA table_info(exchanges)")]
            pairs = db.execute("SELECT * FROM pairs").fetchall()
            db.commit()
        Store(self.path)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 4)
            self.assertEqual(db.execute("SELECT rowid," + ",".join(columns) + " FROM exchanges").fetchall(), before)
            self.assertEqual(db.execute("SELECT * FROM pairs").fetchall(), pairs)
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(self.act("resume")["status"], "RECOVERED")

    def test_display_collision_extends_only_the_new_identifier(self):
        values = [uuid.UUID("a" * 8 + "1" * 24), uuid.UUID("a" * 8 + "2" * 24)]
        with patch("store.uuid.uuid4", side_effect=values):
            first = self.prepare()
            original = self.store.display(self.scope, first)
            self.store.cancel(self.scope, first)
            second = self.prepare()
        self.assertEqual(original["question_id"], "Q-AAAAAAAA")
        self.assertEqual(self.store.display(self.scope, second)["question_id"], "Q-AAAAAAAA22")
        self.assertEqual(Store(self.path).display(self.scope, first), original)

    def test_parent_must_be_consumed_in_the_same_scope_and_generation(self):
        parent = self.prepare()
        with self.assertRaises(ValueError):
            self.prepare(parent_request_id=parent)
        self.act("claim", request_id=parent)
        self.accept(parent)
        with self.assertRaisesRegex(ValueError, "repeats"):
            self.prepare(parent_request_id=parent)
        child = self.prepare(parent_request_id=parent, body="Review actual execution evidence.")
        self.assertEqual(self.store.display(self.scope, child)["parent_answer_id"], self.store.display(self.scope, parent)["answer_id"])
        self.store.cancel(self.scope, child)
        other = {**self.scope, "thread_id": "another-task"}
        self.store.pair(other, {**self.target, "conversation_id": "other"})
        with self.assertRaisesRegex(ValueError, "scope"):
            self.store.prepare(other, "Question", parent_request_id=parent)
        self.store.pair(self.scope, {**self.target, "conversation_id": "replacement"})
        with self.assertRaisesRegex(ValueError, "generation"):
            self.prepare(parent_request_id=parent)

    def test_wait_schedule_persists_across_restarts_and_receipt_repeats(self):
        req = self.prepare()
        with patch("store.time.time", return_value=1000):
            self.act("claim", request_id=req)
        with patch("store.time.time", return_value=1002):
            self.store.record_send(self.scope, req, {"accepted": False})
            self.assertEqual(self.act("read", request_id=req)["wait_ms"], 60000)
        with patch("store.time.time", return_value=1062):
            self.assertEqual(self.act("read-start", request_id=req)["wait_ms"], 0)
        with patch("store.time.time", return_value=1064):
            again = self.act("resume")
            self.assertEqual(again["wait_ms"], 28000)
            self.assertEqual(again["elapsed_ms"], 64000)
            self.store.record_send(self.scope, req, {"accepted": False})
            self.assertEqual(self.act("resume")["wait_ms"], 28000)
        with patch("store.time.time", return_value=1601):
            expired = self.act("read", request_id=req)
            self.assertEqual(expired["status"], "WAIT_EXPIRED")
            self.assertFalse(expired["project_complete"])
            self.assertEqual(self.act("read", request_id=req), expired)
            self.assertTrue(self.act("resume")["late_probe"])
        self.assertEqual(self.store.get_exchange(self.scope, req)["state"], "UNKNOWN")

    def test_only_verified_pending_extends_wait_and_never_past_maximum(self):
        req = self.prepare()
        with patch("store.time.time", return_value=1000):
            self.act("claim", request_id=req)
        for reason in ("NO_MATCH", "RESPONSE_FORMAT_MISMATCH", "READ_FAILED", "WAITING"):
            with patch("store.time.time", return_value=1580):
                self.store.observe(self.scope, req, reason)
            self.assertEqual(json.loads(self.store.get_exchange(self.scope, req)["timing"])["deadline"], 1600000)
        for now in (1580, 1880, 2180, 2480, 2780):
            with patch("store.time.time", return_value=now):
                self.act("accept", request_id=req, called_with={"threadId": self.target["conversation_id"]},
                         result=self.raw(req, status="running"))
        timing = json.loads(self.store.get_exchange(self.scope, req)["timing"])
        self.assertEqual(timing["deadline"], 2800000)
        with patch("store.time.time", return_value=2801):
            self.assertEqual(self.act("read", request_id=req)["status"], "WAIT_EXPIRED")

    def test_invalid_wait_policies_and_read_attempt_limit(self):
        for invalid in ({"poll_interval_ms": -1}, {"initial_delay_ms": "60000"}, {"max_wait_ms": 99999999}, {"unexpected": 1}):
            with self.assertRaises(ValueError):
                wait_policy(invalid)
        req = self.prepare(waiting={"initial_delay_ms": 0, "poll_interval_ms": 0})
        with patch("store.time.time", return_value=1000):
            self.act("claim", request_id=req)
            for _ in range(120):
                self.store.schedule_read(self.scope, req, reserve=True)
            self.assertEqual(self.act("read", request_id=req)["status"], "WAIT_EXPIRED")
        self.assertEqual(self.store.claim(self.scope, req)["status"], "ALREADY_CLAIMED")

    def test_contract_body_and_canonical_resource_hashes_are_frozen(self):
        _, resources = _local_resources()
        req = self.prepare()
        row = self.store.get_exchange(self.scope, req)
        self.assertIn(resources["contract"]["text"], row["payload"])
        self.assertIn(resources["reasoner"]["text"].strip(), row["payload"])
        self.assertEqual(hashlib.sha256(resources["contract"]["text"].encode()).hexdigest(), row["contract_hash"])
        self.assertNotIn(str(self.root), row["payload"])
        for name in resources:
            changed = copy.deepcopy(resources)
            changed[name]["sha256"] = "0" * 64
            with patch("cwc._local_resources", return_value=("test", changed)):
                with self.assertRaisesRegex(ValueError, "resources|contract"):
                    self.act("claim", request_id=req)
        self.assertEqual(self.store.get_exchange(self.scope, req)["state"], "PREPARED")
        self.act("claim", request_id=req)
        with patch("cwc._local_resources", side_effect=ValueError("current resource invalid")):
            self.assertEqual(self.act("resume")["status"], "READ_PENDING")
            self.assertEqual(self.accept(req)["status"], "CONSUMED")

    def test_long_answer_and_explicit_continuation_are_not_silent_truncation(self):
        req = self.prepare(mode="review")
        self.act("claim", request_id=req)
        long_answer = "详细执行步骤。" * 1900
        self.assertGreater(len(long_answer), 12000)
        accepted = self.accept(req, long_answer + "\nCWC CONTINUE: 剩余错误恢复与验收章节")
        self.assertEqual(accepted["answer"], long_answer + "\nCWC CONTINUE: 剩余错误恢复与验收章节")
        self.assertFalse(accepted["answer_complete"])
        checklist = {"goal": "Finish implementation", "required": [{"id": "a", "title": "Code", "passed": True, "evidence": ["actual test log"]}], "review_request_id": req}
        self.assertEqual(self.act("check-progress", checklist=checklist)["status"], "NEED_CONSULTATION")
        follow = self.prepare(parent_request_id=req, body="Continue the remaining error recovery and acceptance sections.")
        self.assertEqual(self.store.display(self.scope, follow)["parent_answer_id"], accepted["answer_id"])
        self.act("claim", request_id=follow)
        with self.assertRaises(HostDataError) as caught:
            self.accept(follow, "长" * 20001)
        self.assertEqual(caught.exception.code, "HOST_LENGTH_LIMIT")
        header_units = len("CWC RESPONSE " + follow + "\n")
        for text in ("长" * (20000 - header_units), "😀" * ((20000 - header_units + 1) // 2)):
            with self.assertRaises(HostDataError) as boundary:
                self.accept(follow, text)
            self.assertEqual(boundary.exception.code, "HOST_LENGTH_LIMIT")
        self.assertEqual(self.store.get_exchange(self.scope, follow)["state"], "UNKNOWN")

    def test_progress_continues_independent_work_and_rejects_unsupported_success(self):
        a = {"id": "a", "title": "Code", "passed": True, "evidence": ["test results"]}
        b = {"id": "b", "title": "Upload", "passed": False, "blocked_by": "User login"}
        c = {"id": "c", "title": "Docs", "passed": False, "authorized": True, "next_action": "Write verified install steps"}
        work = {"goal": "Ship the whole feature", "required": [a, b, c], "chat_says_complete": True}
        self.assertEqual(check_progress(work)["status"], "CONTINUE_EXECUTION")
        self.assertEqual(check_progress({**work, "required": [a, b]})["status"], "BLOCKED")
        self.assertEqual(check_progress({**work, "user_stopped": True})["status"], "STOPPED")
        self.assertEqual(check_progress({**work, "budget_exhausted": True})["status"], "PAUSED")
        self.assertEqual(check_progress({**work, "required": [a]}, review_received=False)["status"], "NEED_CONSULTATION")
        self.assertTrue(check_progress({**work, "required": [a], "review_passed": True}, review_received=True)["project_complete"])
        with self.assertRaises(ValueError):
            check_progress({**work, "required": [{**a, "evidence": []}]})
        self.assertEqual(check_progress({**work, "required": [a]}, pending={"wait_expired": True})["status"], "PAUSED")

    def test_completion_review_must_be_latest_and_from_current_pair(self):
        req = self.prepare(mode="review")
        self.act("claim", request_id=req)
        self.accept(req)
        work = {"goal": "Verify feature", "required": [{"id": "a", "title": "Feature", "passed": True, "evidence": ["verified log"]}], "review_request_id": req, "review_passed": True}
        self.assertEqual(self.act("check-progress", checklist=work)["status"], "COMPLETE")
        self.store.pair(self.scope, {**self.target, "conversation_id": "new-target"})
        self.assertEqual(self.act("check-progress", checklist=work)["status"], "NEED_CONSULTATION")

    def test_expired_late_probe_recovers_without_resetting_or_redelivering_stopped_answers(self):
        for stopped in (False, True):
            req = self.prepare(body="Check late reply " + str(stopped))
            with patch("store.time.time", return_value=1000):
                self.act("claim", request_id=req)
            initial = json.loads(self.store.get_exchange(self.scope, req)["timing"])
            if stopped:
                self.act("stop", request_id=req, reason="User stopped", confirmed=True)
            with patch("store.time.time", return_value=3000):
                self.assertEqual(self.act("read", request_id=req)["status"], "WAIT_EXPIRED")
                self.assertTrue(self.act("resume", request_id=req)["late_probe"])
                self.assertTrue(self.act("read-start", request_id=req, late_probe=True)["late_probe"])
                self.assertEqual(self.act("resume", request_id=req)["status"], "WAIT_EXPIRED")
                result = self.accept(req)
            self.assertEqual(result["status"], "DISCARDED" if stopped else "CONSUMED")
            if stopped:
                self.assertNotIn("answer", result)
            final = json.loads(self.store.get_exchange(self.scope, req)["timing"])
            for key in ("claimed_at", "deadline", "max_deadline"):
                self.assertEqual(final[key], initial[key])
            self.assertNotEqual(self.store.claim(self.scope, req)["status"], "SEND_ONCE")

    def test_consumed_review_is_received_not_passed_and_pending_shape_is_actual(self):
        req = self.prepare(mode="review")
        self.act("claim", request_id=req)
        work = {"goal":"Verify completion", "required":[{"id":"a","title":"Feature","passed":True,"evidence":["verified log"]}], "review_request_id":req}
        self.assertEqual(self.act("check-progress", checklist=work)["status"], "WAITING")
        with patch("store.time.time", return_value=9999999999):
            self.assertEqual(self.act("check-progress", checklist=work)["status"], "PAUSED")
        self.accept(req, "Two blocking defects remain; do not finish.")
        for passed in (None, False):
            candidate = work if passed is None else {**work,"review_passed":passed}
            self.assertEqual(self.act("check-progress", checklist=candidate)["status"], "CONTINUE_EXECUTION")
        with self.assertRaises(ValueError):
            self.act("check-progress", checklist={**work,"review_passed":"true"})
        self.assertEqual(self.act("check-progress", checklist={**work,"review_passed":True})["status"], "COMPLETE")

    def test_wall_clock_rollback_cannot_reduce_observed_elapsed_time(self):
        req = self.prepare()
        with patch("store.time.time", return_value=1000):
            self.act("claim", request_id=req)
        with patch("store.time.time", return_value=1200):
            before = self.act("read", request_id=req)
        with patch("store.time.time", return_value=1100):
            after = self.act("read", request_id=req)
        self.assertEqual(after["elapsed_ms"], before["elapsed_ms"])
        self.assertEqual(after["remaining_ms"], before["remaining_ms"])


if __name__ == "__main__":
    unittest.main()
