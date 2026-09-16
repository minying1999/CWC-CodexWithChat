"""Doctor distinguishes local health from Host evidence without changing either."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "cwc" / "scripts"))
from cwc import execute
from store import Store


TOOLS = [f"mcp__codex_app__{name}" for name in
         ("list_threads", "read_thread", "send_message_to_thread")] + ["exec_command", "apply_patch"]


class DoctorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="cwc-doctor-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "home"
        self.path = self.home / "cwc" / "state" / "cwc.sqlite3"
        self.scope = {"host_id": "local", "thread_id": "task-a", "workspace": str(self.root)}
        package = self.root / "installed-plugin"
        self.skill = package / "skills" / "cwc"
        (self.skill / "scripts").mkdir(parents=True)
        (self.skill / "references").mkdir()
        (self.skill / "SKILL.md").write_text(
            "---\nname: cwc\ndescription: Disposable doctor test.\n---\nLocal rules.\n", encoding="utf-8")
        (self.skill / "REASONER.md").write_text("Disposable reasoner rules.\n", encoding="utf-8")
        (self.skill / "references" / "HOST_WORKFLOW.md").write_text("Read only.\n", encoding="utf-8")
        (self.skill / "scripts" / "host_relay.js").write_text("(async function relay() {})\n", encoding="utf-8")
        (self.skill / "CWC_AGENT_CONTRACT.md").write_text("# CWC Agent Contract v1\nRead only.\n", encoding="utf-8")
        for relative in ("references/WORKFLOW.md","references/CONTINUATION.md","scripts/entry.py","scripts/entry_relay.js"):
            (self.skill / relative).write_text("Disposable resource.\n",encoding="utf-8")
        self.manifest = package / ".codex-plugin" / "plugin.json"
        self.manifest.parent.mkdir()
        self.manifest.write_text(json.dumps({"name": "cwc", "version": "0.2.2-test"}), encoding="utf-8")
        for module in ("cwc", "store"):
            location = patch(f"{module}.__file__", str(self.skill / "scripts" / f"{module}.py"))
            location.start()
            self.addCleanup(location.stop)

    def doctor(self, **data):
        return execute({"action": "doctor", "scope": self.scope, **data}, self.home)

    def state_files(self):
        return {str(path.relative_to(self.home)): None if path.is_dir() else path.read_bytes()
                for path in self.home.rglob("*")}

    def paired_store(self, host="chatgpt"):
        store = Store(self.path)
        store.pair(self.scope, {"host_id": host, "kind": "chatgpt",
                                "conversation_id": "chat-a", "title": "Disposable Chat"})
        return store

    def codes(self, result):
        return {issue["code"] for issue in result["issues"]}

    def test_unpaired_diagnosis_creates_no_state_and_performs_no_host_actions(self):
        result = self.doctor(expected_version="0.2.2-test")
        self.assertEqual(result["status"], "ATTENTION")
        self.assertEqual(result["plugin_version"], "0.2.2-test")
        self.assertEqual(result["binding"]["status"], "UNPAIRED")
        self.assertIn("UNPAIRED", self.codes(result))
        self.assertEqual(result["host_actions"], 0)
        self.assertEqual(result["host_read"]["status"], "NOT_CHECKED")
        self.assertNotIn("probe", result)
        self.assertFalse(self.home.exists())

    def test_tool_inventory_unknown_missing_and_listed_are_distinct(self):
        for inventory, expected, code in ((None, "NOT_CHECKED", "TOOLS_UNCHECKED"),
                                           ([], "MISSING", "MISSING_TOOLS"),
                                           (TOOLS, "LISTED", None)):
            with self.subTest(inventory=inventory):
                result = self.doctor(**({} if inventory is None else {"available_tools": inventory}))
                self.assertEqual(result["tools"], dict.fromkeys(TOOLS, expected))
                if code:
                    self.assertIn(code, self.codes(result))
                else:
                    self.assertNotIn("TOOLS_UNCHECKED", self.codes(result))
                    self.assertNotIn("MISSING_TOOLS", self.codes(result))
                self.assertEqual(result["host_read"]["status"], "NOT_CHECKED")
        self.assertFalse(self.home.exists())

    def test_selected_target_returns_only_one_correct_read_probe(self):
        for host in ("chatgpt", "remote-host"):
            with self.subTest(host=host):
                self.paired_store(host)
                before = self.state_files()
                result = self.doctor(available_tools=TOOLS)
                probe = result["probe"]
                self.assertEqual(probe["tool"], TOOLS[1])
                self.assertEqual(probe["arguments"]["threadId"], "chat-a")
                if host == "chatgpt":
                    self.assertNotIn("hostId", probe["arguments"])
                else:
                    self.assertEqual(probe["arguments"]["hostId"], host)
                self.assertNotIn("prompt", probe["arguments"])
                self.assertEqual(result["host_read"]["status"], "NOT_CHECKED")
                self.assertEqual(result["host_actions"], 0)
                self.assertEqual(self.state_files(), before)
                self.assertNotIn("probe", self.doctor(available_tools=[TOOLS[0], TOOLS[2]]))

    def test_read_observations_require_exact_route_and_complete_thread_metadata(self):
        self.paired_store()
        valid = {"thread": {"id": "chat-a", "kind": "chatgpt"}, "turns": []}
        arguments = self.doctor(available_tools=TOOLS)["probe"]["arguments"]
        cases = (
            (arguments, {"isError": True, "content": []}, "READ_FAILED"),
            ({**arguments, "threadId": "other-chat"}, valid, "READ_FAILED"),
            ({**arguments, "hostId": "local"}, valid, "READ_FAILED"),
            (arguments, {"thread": {"id": "other-chat", "kind": "chatgpt"}}, "READ_FAILED"),
            (arguments, {"thread": {"id": "chat-a", "kind": "codex"}}, "READ_FAILED"),
            (arguments, {"thread": {"id": "chat-a"}}, "READ_FAILED"),
            (arguments, {"turns": []}, "READ_FAILED"),
            (arguments, {"truncated": True, "structuredContent": valid}, "READ_FAILED"),
            (arguments, {"thread": {"id": "chat-a", "kind": "chatgpt", "partial": True}}, "READ_FAILED"),
            (arguments, {"content": [{"type": "text", "text": json.dumps(valid)}]}, "READABLE"),
        )
        before = self.state_files()
        for called_with, raw, expected in cases:
            with self.subTest(called_with=called_with, raw=raw):
                result = self.doctor(available_tools=TOOLS,
                                     read_probe={"called_with": called_with, "result": raw})
                self.assertEqual(result["host_read"]["status"], expected)
                if expected == "READ_FAILED":
                    self.assertEqual(result["status"], "ATTENTION")
                self.assertNotIn("probe", result)
                self.assertEqual(result["host_actions"], 0)
                self.assertEqual(self.state_files(), before)

    def test_pending_and_stopped_requests_remain_intact_without_exposing_bodies(self):
        store = self.paired_store()
        private_body = "Private consultation body must never appear in health output."
        request_id = store.prepare(self.scope, private_body)["request_id"]
        transitions = (
            ("PREPARED", lambda: None),
            ("UNKNOWN", lambda: store.claim(self.scope, request_id)),
            ("WAITING", lambda: store.record_send(self.scope, request_id,
                                                 {"accepted": True, "message_id": "sent-1"})),
        )
        for state, transition in transitions:
            with self.subTest(state=state):
                transition()
                before = self.state_files()
                result = self.doctor(available_tools=TOOLS)
                self.assertEqual(result["binding"]["exchange"]["state"], state)
                self.assertEqual(result["binding"]["exchange"]["request_id"], request_id)
                self.assertNotIn(private_body, json.dumps(result))
                self.assertEqual(self.state_files(), before)
        store.abandon(self.scope, request_id, "Stop waiting for this test request.", confirmed=True)
        before = self.state_files()
        result = self.doctor(available_tools=TOOLS)
        self.assertEqual(result["status"], "ATTENTION")
        self.assertIn("TARGET_RESERVED", self.codes(result))
        self.assertTrue(result["binding"]["target_reserved"])
        self.assertEqual(result["binding"]["stopped_count"], 1)
        self.assertEqual(result["binding"]["stopped_exchanges"][0]["request_id"], request_id)
        self.assertNotIn(private_body, json.dumps(result))
        self.assertEqual(self.state_files(), before)

    def test_missing_or_invalid_local_resources_are_reported_without_state_creation(self):
        cases = ((self.skill / "SKILL.md", None),
                 (self.skill / "scripts" / "host_relay.js", None),
                 (self.skill / "CWC_AGENT_CONTRACT.md", None),
                 (self.skill / "REASONER.md", b"\xff invalid UTF-8"),
                 (self.skill / "references" / "HOST_WORKFLOW.md", b" \n"),
                 (self.manifest, b"{invalid JSON"))
        for path, replacement in cases:
            with self.subTest(path=path.name):
                original = path.read_bytes()
                try:
                    if replacement is None:
                        path.unlink()
                    else:
                        path.write_bytes(replacement)
                    result = self.doctor()
                    self.assertEqual(result["status"], "ATTENTION")
                    self.assertIn("LOCAL_RESOURCE_ERROR", self.codes(result))
                    self.assertEqual(result["host_actions"], 0)
                    self.assertFalse(self.home.exists())
                finally:
                    path.write_bytes(original)

    def test_database_errors_are_reported_without_migration_or_sidecars(self):
        self.paired_store()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("PRAGMA user_version=99")
            db.commit()
        original = self.path.read_bytes()
        for replacement in (original, b"This is not a SQLite database."):
            with self.subTest(format=replacement[:16]):
                self.path.write_bytes(replacement)
                before = self.state_files()
                result = self.doctor(available_tools=TOOLS)
                self.assertEqual(result["status"], "ATTENTION")
                self.assertIn("STATE_READ_ERROR", self.codes(result))
                self.assertEqual(result["host_actions"], 0)
                self.assertEqual(self.state_files(), before)
        self.path.write_bytes(original)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("PRAGMA user_version=2")
            db.execute("PRAGMA journal_mode=WAL")
            db.commit()
        before = self.state_files()
        result = self.doctor(available_tools=TOOLS)
        self.assertIn("STATE_READ_ERROR", self.codes(result))
        self.assertEqual(self.state_files(), before)


if __name__ == "__main__":
    unittest.main()
