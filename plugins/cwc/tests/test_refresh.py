"""Refresh reloads installed instructions without sending or changing local work."""

from contextlib import chdir, closing
import hashlib
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


SKILL_HEADER = "---\nname: cwc\ndescription: Disposable refresh test.\n---\n\n"


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cwc-refresh-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.path = self.home / "cwc" / "state" / "cwc.sqlite3"
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.scope = {"host_id": "local", "thread_id": "task-a", "workspace": str(self.workspace)}
        self.package = self.root / "installed-plugin"
        self.skill = self.package / "skills" / "cwc"
        (self.skill / "scripts").mkdir(parents=True)
        (self.skill / "references").mkdir()
        self.resources = {
            "skill": self.skill / "SKILL.md",
            "reasoner": self.skill / "REASONER.md",
            "host_workflow": self.skill / "references" / "HOST_WORKFLOW.md",
            "host_relay": self.skill / "scripts" / "host_relay.js",
            "contract": self.skill / "CWC_AGENT_CONTRACT.md",
            "workflow": self.skill / "references" / "WORKFLOW.md",
            "continuation": self.skill / "references" / "CONTINUATION.md",
            "entry": self.skill / "scripts" / "entry.py",
            "entry_relay": self.skill / "scripts" / "entry_relay.js",
        }
        for name, path in self.resources.items():
            header = SKILL_HEADER if name == "skill" else "# CWC Agent Contract v1\n" if name == "contract" else ""
            path.write_text(f"{header}Installed {name} instructions.\n", encoding="utf-8")
        self.manifest = self.package / ".codex-plugin" / "plugin.json"
        self.manifest.parent.mkdir()
        self.manifest.write_text(json.dumps({"name": "cwc", "version": "0.2.1-test"}), encoding="utf-8")
        for module in ("cwc", "store"):
            location = patch(f"{module}.__file__", str(self.skill / "scripts" / f"{module}.py"))
            location.start()
            self.addCleanup(location.stop)

    def refresh(self, **data):
        return execute({"action": "refresh", "scope": self.scope, **data}, self.home)

    def state_files(self):
        return {str(path.relative_to(self.home)): None if path.is_dir() else path.read_bytes()
                for path in self.home.rglob("*")}

    def paired_store(self):
        store = Store(self.path)
        store.pair(self.scope, {"host_id": "chatgpt", "kind": "chatgpt",
                                "conversation_id": "chat-a", "title": "Disposable Chat"})
        return store

    def test_unpaired_refresh_returns_resources_without_creating_state(self):
        result = self.refresh(expected_version="0.2.1-test")
        self.assertEqual(result["status"], "REFRESHED")
        self.assertEqual(result["plugin_version"], "0.2.1-test")
        self.assertEqual(result["binding"]["status"], "UNPAIRED")
        self.assertEqual(result["host_actions"], 0)
        self.assertEqual(result["session_reload"], "NOT_PERFORMED")
        self.assertNotIn("tool", result)
        with self.assertRaisesRegex(ValueError, "version"):
            self.refresh(expected_version="old-installation")
        self.assertFalse(self.home.exists())
        self.assertEqual(set(result["resources"]), set(self.resources))
        for name, path in self.resources.items():
            resource = result["resources"][name]
            self.assertEqual(Path(resource["path"]), path.resolve())
            self.assertNotIn("text", resource)
            self.assertEqual(resource["sha256"], hashlib.sha256(path.read_text(encoding="utf-8-sig").encode("utf-8")).hexdigest())
        self.assertEqual(result["operator_instructions"], self.resources["skill"].read_text(encoding="utf-8"))
        full = self.refresh(full_resources=True)
        self.assertEqual(full["resources"]["host_relay"]["text"], self.resources["host_relay"].read_text(encoding="utf-8"))

    def test_all_pending_states_and_receipts_survive_refresh_byte_for_byte(self):
        store = self.paired_store()
        request_id = store.prepare(self.scope, "Keep this pending question.")["request_id"]
        transitions = (
            ("PREPARED", lambda: None),
            ("UNKNOWN", lambda: store.claim(self.scope, request_id)),
            ("WAITING", lambda: store.record_send(self.scope, request_id,
                {"accepted": True, "message_id": "host-message", "turn_id": "host-turn"})),
            ("READY", lambda: store.receive(self.scope, request_id, "reply-one", "A saved answer.")),
        )
        for state, transition in transitions:
            with self.subTest(state=state):
                transition()
                before = self.state_files()
                result = self.refresh()
                self.assertEqual(self.state_files(), before)
                self.assertEqual(result["binding"]["status"], "SELECTED")
                self.assertEqual(result["binding"]["generation"], 1)
                self.assertEqual(result["binding"]["exchange"]["request_id"], request_id)
                self.assertEqual(result["binding"]["exchange"]["state"], state)
                self.assertEqual(result["host_actions"], 0)

    def test_changed_instructions_reload_but_only_new_requests_use_new_rules(self):
        store = self.paired_store()
        request_id = store.prepare(self.scope, "Original question.")["request_id"]
        frozen = store.get_exchange(self.scope, request_id)["payload"]
        first = self.refresh()
        for name, path in self.resources.items():
            header = SKILL_HEADER if name == "skill" else "# CWC Agent Contract v1\n" if name == "contract" else ""
            path.write_text(f"{header}Changed {name} instructions.\n", encoding="utf-8")
        before = self.state_files()
        self.manifest.write_text(json.dumps({"name":"cwc","version":"0.2.2-test"}),encoding="utf-8")
        second = self.refresh()
        self.assertEqual(second["plugin_version"],"0.2.2-test")
        self.assertEqual(self.state_files(), before)
        for name in self.resources:
            self.assertNotEqual(first["resources"][name]["sha256"], second["resources"][name]["sha256"])
            self.assertIn(name, second["changed_resources"])
        self.assertIn("Changed skill instructions", second["operator_instructions"])
        import entry
        with patch("entry.__file__",str(self.skill/"scripts/entry.py")):
            business=entry.act({"scope":self.scope,"command":"Continue the actual task"},self.home)
        self.assertIn("Changed workflow instructions",business["operator_workflow"]["text"])
        self.assertNotIn("Installed workflow instructions",business["operator_workflow"]["text"])
        self.assertNotIn("text",second["resources"]["workflow"])
        with patch("entry.business_instructions",side_effect=AssertionError("entry must stay thin")):
            self.assertEqual(entry.act({"scope":self.scope,"command":"cwc help"},self.home)["status"],"HELP")
            self.assertEqual(entry.act({"scope":self.scope,"command":"cwc bind"},self.home)["status"],"PENDING")
        self.assertEqual(store.get_exchange(self.scope, request_id)["payload"], frozen)
        store.cancel(self.scope, request_id)
        next_id = store.prepare(self.scope, "Next question.")["request_id"]
        next_payload = store.get_exchange(self.scope, next_id)["payload"]
        self.assertIn("Changed reasoner instructions.", next_payload)
        self.assertNotIn("Installed reasoner instructions.", next_payload)

    def test_bad_or_missing_resources_fail_without_touching_pending_work(self):
        store = self.paired_store()
        request_id = store.prepare(self.scope, "An unresolved question.")["request_id"]
        store.claim(self.scope, request_id)
        cases = ((self.resources["skill"], None),
                 (self.resources["skill"], b"This is not a CWC skill."),
                 (self.resources["reasoner"], b"\xff invalid UTF-8"),
                 (self.resources["host_workflow"], b" \n"),
                 (self.manifest, b"{malformed JSON"),
                 (self.manifest, b'{"name":"cwc"}'))
        for path, replacement in cases:
            with self.subTest(path=path.name, replacement=replacement):
                original = path.read_bytes()
                try:
                    if replacement is None:
                        path.unlink()
                    else:
                        path.write_bytes(replacement)
                    before = self.state_files()
                    with self.assertRaises((OSError, ValueError, KeyError, TypeError)):
                        self.refresh()
                    self.assertEqual(self.state_files(), before)
                    absent_home = self.root / "invalid-home"
                    with self.assertRaises((OSError, ValueError, KeyError, TypeError)):
                        execute({"action": "refresh", "scope": self.scope}, absent_home)
                    self.assertFalse(absent_home.exists())
                finally:
                    path.write_bytes(original)

    def test_refresh_uses_installed_paths_independent_of_working_directory(self):
        (self.workspace / "SKILL.md").write_text("Wrong workspace instructions.", encoding="utf-8")
        (self.workspace / "REASONER.md").write_text("Wrong workspace rules.", encoding="utf-8")
        with chdir(self.workspace):
            result = self.refresh()
        self.assertEqual(result["operator_instructions"], self.resources["skill"].read_text(encoding="utf-8-sig"))
        self.assertFalse(self.home.exists())

    def test_rejected_database_schema_remains_unchanged(self):
        self.paired_store()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("PRAGMA user_version=99")
            db.commit()
        before = self.state_files()
        with self.assertRaisesRegex(ValueError, "schema"):
            self.refresh()
        self.assertEqual(self.state_files(), before)

    def test_wal_database_is_rejected_without_creating_sidecars(self):
        self.paired_store()
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
        before = self.state_files()
        self.assertFalse(Path(str(self.path) + "-wal").exists())
        self.assertFalse(Path(str(self.path) + "-shm").exists())
        with self.assertRaisesRegex(ValueError, "WAL"):
            self.refresh()
        self.assertEqual(self.state_files(), before)


if __name__ == "__main__":
    unittest.main()
