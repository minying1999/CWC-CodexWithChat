"""Entry behavior: menu-scoped selection, one verification, compact refresh and quiet progress."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"skills/cwc/scripts"))
from entry import act, menu_load, menu_path, save, PROBE, load_runtime
from cwc import execute
from progress import check_progress
from store import Store


class EntryChecks(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="cwc-entry-")
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.scope = {"host_id":"local","thread_id":"synthetic-task","workspace":str(self.home)}
        self.listing = {"threads":[{"id":"chat-a","kind":"chatgpt","title":"Alpha"},
                                   {"id":"chat-b","kind":"chatgpt","title":"Beta"}]}
        guard = patch("cwc.check_legacy_pending")
        guard.start(); self.addCleanup(guard.stop)

    def entry(self, command=None, **data):
        return act({"scope":self.scope,"command":command,**data},self.home)

    def list_menu(self):
        start=self.entry("cwc bind")
        return self.entry(action="listed",expected_binding=start["expected_binding"],result=self.listing)

    def bind(self):
        menu=self.list_menu()
        selected=self.entry("2",menu_token=menu["menu_token"])
        return self.entry(action="selected",selection=selected["selection"],menu_token=menu["menu_token"],result=self.listing)

    def test_help_is_one_fixed_local_action_without_loading_work_resources(self):
        with patch("cwc._local_resources",side_effect=AssertionError("no resources for help")):
            expected=self.entry("cwc help")
            self.assertEqual(self.entry(" /CWC   HELP "),expected)
        self.assertEqual(expected["status"],"HELP")
        self.assertFalse((self.home/"cwc").exists())

    def test_loader_uses_enabled_installed_manifest_instead_of_cache_sorting(self):
        for market in ("personal","repo-market"):
            package=self.home/"plugins/cache"/market/"cwc/0.2.0+test"
            (package/".codex-plugin").mkdir(parents=True)
            (package/"skills/cwc/scripts").mkdir(parents=True)
            manifest=package/".codex-plugin/plugin.json"
            manifest.write_text(json.dumps({"name":"cwc","version":"0.2.0+test"}),encoding="utf-8")
            (package/"skills/cwc/scripts/entry_relay.js").write_text("(async()=>{})",encoding="utf-8")
            enabled={"name":"cwc","version":"0.2.0+test","installed":True,"enabled":True,"marketplaceName":market}
            listing={"installed":[{"name":"cwc","version":"999-disabled","installed":True,"enabled":False},enabled]}
            with patch("entry.shutil.which",return_value="codex.exe"), patch("entry.subprocess.run") as run, patch.dict("os.environ",{"CODEX_THREAD_ID":"synthetic-task"}):
                run.return_value.stdout=json.dumps(listing)
                result=load_runtime(self.home)
                self.assertEqual(result["marketplace"],market)
                self.assertEqual(result["expectedVersion"],"0.2.0+test")
                self.assertNotIn("transport_source",result)
                run.return_value.stdout=json.dumps({"installed":[enabled,enabled]})
                with self.assertRaisesRegex(ValueError,"exactly one"):
                    load_runtime(self.home)
                run.return_value.stdout=json.dumps(listing)
                manifest.write_text(json.dumps({"name":"cwc","version":"wrong-version"}),encoding="utf-8")
                with self.assertRaisesRegex(ValueError,"mismatch"):
                    load_runtime(self.home)

    def test_candidates_precede_pairing_and_selection_uses_original_identity(self):
        menu=self.list_menu()
        self.assertEqual(menu["options"],[{"value":1,"title":"Alpha"},{"value":2,"title":"Beta"}])
        self.assertFalse((self.home/"cwc/state/cwc.sqlite3").exists())
        reordered={"threads":list(reversed(self.listing["threads"]))}
        bound=self.entry(action="selected",selection=2,menu_token=menu["menu_token"],result=reordered)
        self.assertEqual(bound["status"],"BOUND")
        self.assertIn("Beta",bound["message"])
        self.assertEqual(bound["host_sends"],0)
        state=Store.read_status(self.home/"cwc/state/cwc.sqlite3",self.scope,details=True)
        self.assertEqual(state["target"]["conversation_id"],"chat-b")
        self.assertIsNone(state["last_exchange"])

    def test_stale_missing_target_and_wrong_menu_cannot_bind(self):
        menu=self.list_menu()
        with self.assertRaisesRegex(ValueError,"disappeared"):
            self.entry(action="selected",selection=2,menu_token=menu["menu_token"],result={"threads":self.listing["threads"][:1]})
        with self.assertRaisesRegex(ValueError,"active"):
            self.entry(action="selected",selection=1,menu_token="wrong",result=self.listing)
        self.assertEqual(self.entry("1")["status"],"BUSINESS")
        self.assertFalse((self.home/"cwc/state/cwc.sqlite3").exists())

    def test_probe_menu_is_consumed_once_and_unknown_only_recovers_same_exchange(self):
        bound=self.bind()
        first=self.entry("1",menu_token=bound["menu_token"])
        req=first["request_id"]
        execute({"action":"claim","scope":self.scope,"request_id":req},self.home)
        again=self.entry("1",menu_token=bound["menu_token"])
        self.assertEqual(again["request_id"],req)
        with closing(sqlite3.connect(self.home/"cwc/state/cwc.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM exchanges").fetchone()[0],1)
        self.assertEqual(execute({"action":"claim","scope":self.scope,"request_id":req},self.home)["status"],"ALREADY_CLAIMED")
        self.assertIn("CWC REQUEST "+req,Store(self.home/"cwc/state/cwc.sqlite3").get_exchange(self.scope,req)["payload"])

    def test_business_closes_menu_and_later_one_is_never_a_probe(self):
        bound=self.bind()
        self.assertEqual(self.entry("Please work on this task")["status"],"BUSINESS")
        self.assertEqual(self.entry("1",menu_token=bound["menu_token"])["status"],"BUSINESS")
        with patch("entry.time.time",return_value=9999999999):
            self.assertEqual(self.entry("1",menu_token=bound["menu_token"])["status"],"BUSINESS")
        self.assertIsNone(Store.read_status(self.home/"cwc/state/cwc.sqlite3",self.scope)["last_exchange"])

    def test_same_menu_grant_prepares_one_exchange_even_with_concurrent_calls(self):
        self.bind()
        data={"action":"prepare","scope":self.scope,"body":PROBE,"idempotency_key":"a"*32}
        with ThreadPoolExecutor(max_workers=3) as pool:
            ids=list(pool.map(lambda _:execute(data,self.home)["request_id"],range(3)))
        self.assertEqual(len(set(ids)),1)
        with self.assertRaisesRegex(ValueError,"conflicts"):
            execute({**data,"body":"A different question"},self.home)

    def test_refresh_exposes_real_operator_instructions_and_changed_resources_compactly(self):
        output=self.entry("cwc refresh")
        self.assertIn("operator_instructions",output)
        self.assertIn("cwc",output["operator_instructions"])
        self.assertTrue(all("text" not in r for r in output["resources"].values()))
        hashes={k:v["sha256"] for k,v in output["resources"].items()}
        self.assertEqual(self.entry("/cwc refresh",known_hashes=hashes)["changed_resources"],[])
        self.assertLess(len(json.dumps(output,ensure_ascii=False)),14000)
        self.assertFalse((self.home/"cwc/state").exists())

    def test_refresh_offers_one_probe_after_success_without_changing_the_database(self):
        bound=self.bind()
        database=self.home/"cwc/state/cwc.sqlite3"
        before=database.read_bytes()
        refreshed=self.entry("/cwc refresh")
        self.assertEqual(refreshed["status"],"REFRESHED")
        self.assertTrue(refreshed["verification_available"])
        self.assertEqual(database.read_bytes(),before)
        self.assertNotEqual(refreshed["menu_token"],bound["menu_token"])
        self.assertEqual(self.entry("1",menu_token=bound["menu_token"])["status"],"MENU_EXPIRED")
        probe=self.entry("1",menu_token=refreshed["menu_token"])
        self.assertEqual(probe["status"],"VERIFY_REQUEST")
        self.assertEqual(self.entry("1",menu_token=refreshed["menu_token"])["request_id"],probe["request_id"])
        row=Store(database).get_exchange(self.scope,probe["request_id"])
        self.assertEqual(row["contract_version"],"3")
        self.assertIn("Executor consults Chat proactively",row["payload"])

    def test_refresh_without_a_free_selected_target_offers_no_new_probe(self):
        self.assertFalse(self.entry("cwc refresh")["verification_available"])
        bound=self.bind()
        probe=self.entry("1",menu_token=bound["menu_token"])
        execute({"action":"claim","scope":self.scope,"request_id":probe["request_id"]},self.home)
        database=self.home/"cwc/state/cwc.sqlite3"
        before=database.read_bytes()
        refreshed=self.entry("cwc refresh")
        self.assertFalse(refreshed["verification_available"])
        self.assertNotIn("options",refreshed)
        self.assertIn("resume",refreshed["hint"])
        self.assertEqual(database.read_bytes(),before)
        self.assertEqual(execute({"action":"resume","scope":self.scope},self.home)["request_id"],probe["request_id"])

    def test_refresh_reserved_target_and_failed_validation_invalidate_old_menu(self):
        bound=self.bind()
        other={**self.scope,"thread_id":"other-task"}
        Store(self.home/"cwc/state/cwc.sqlite3").pair(other,{"host_id":"chatgpt","kind":"chatgpt","conversation_id":"chat-b","title":"Beta"})
        pending=execute({"action":"prepare","scope":other,"body":"Other actual work"},self.home)
        refreshed=self.entry("cwc refresh")
        self.assertFalse(refreshed["verification_available"])
        self.assertNotIn("options",refreshed)
        execute({"action":"cancel","scope":other,"request_id":pending["request_id"]},self.home)
        refreshed=self.entry("cwc refresh")
        with patch("entry.refresh",side_effect=ValueError("bad resources")):
            with self.assertRaisesRegex(ValueError,"bad resources"):
                self.entry("cwc refresh")
        self.assertEqual(menu_load(self.home,self.scope)["menu_kind"],"closed")

    def test_version_change_expires_menu_but_keeps_created_request_recoverable(self):
        bound=self.bind()
        with patch("entry._installed_version",return_value="next-version"):
            self.assertEqual(self.entry("1",menu_token=bound["menu_token"])["status"],"MENU_EXPIRED")
        probe=self.entry("1",menu_token=bound["menu_token"])
        execute({"action":"claim","scope":self.scope,"request_id":probe["request_id"]},self.home)
        with patch("entry._installed_version",return_value="next-version"):
            self.assertEqual(self.entry("1",menu_token=bound["menu_token"])["status"],"MENU_EXPIRED")
        self.assertEqual(execute({"action":"resume","scope":self.scope},self.home)["request_id"],probe["request_id"])

    def test_material_progress_stall_does_not_treat_waiting_as_progress(self):
        work={"goal":"Finish", "required":[{"id":"a","title":"Review","passed":False,"question":"Review evidence"}],
              "last_progress_at":(datetime.now(timezone.utc)-timedelta(minutes=31)).isoformat(),
              "last_progress_evidence":"last verified artifact"}
        first=check_progress(work,pending={"wait_expired":False})
        self.assertEqual(first["status"],"NO_PROGRESS")
        self.assertTrue(first["notify_user"])
        self.assertEqual(check_progress(work,pending={"wait_expired":False}),first)
        work["last_progress_at"]=datetime.now(timezone.utc).isoformat()
        self.assertFalse(check_progress(work,pending={"wait_expired":False})["notify_user"])

    def test_pause_without_human_action_is_quiet(self):
        item={"id":"a","title":"Review","passed":False,"question":"Review evidence"}
        work={"goal":"Finish","required":[item]}
        expired=check_progress(work,pending={"wait_expired":True})
        self.assertEqual(expired["status"],"PAUSED"); self.assertFalse(expired["notify_user"])
        self.assertFalse(check_progress({**work,"budget_exhausted":True})["notify_user"])
        human=check_progress({**work,"required":[{**item,"blocked_by":"User login required","human_action_required":True}]})
        self.assertEqual(human["status"],"BLOCKED"); self.assertTrue(human["notify_user"])

    def test_external_blocker_is_quiet_until_material_progress_deadline(self):
        item={"id":"a","title":"Upstream","passed":False,"blocked_by":"Temporary upstream outage","human_action_required":False}
        work={"goal":"Finish","required":[item]}
        result=check_progress(work)
        self.assertEqual(result["status"],"BLOCKED"); self.assertFalse(result["notify_user"])
        stalled={**work,"last_progress_at":(datetime.now(timezone.utc)-timedelta(minutes=31)).isoformat(),"last_progress_evidence":"Last verified artifact"}
        result=check_progress(stalled)
        self.assertEqual(result["status"],"NO_PROGRESS"); self.assertTrue(result["notify_user"])
        with self.assertRaises(ValueError):
            check_progress({**work,"required":[{**item,"human_action_required":"yes"}]})


if __name__=="__main__":
    unittest.main()
