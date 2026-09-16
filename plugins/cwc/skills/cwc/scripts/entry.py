"""Fixed CWC entry actions. Host listing and optional verification use the trusted JS launcher."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid

from cwc import execute as control, refresh, _installed_version
from host import candidates, _unwrap, continuation_needed
from store import Store, _scope

HELP = "CWC：bind 选择对话；status 查看绑定；refresh 刷新本地说明；resume 恢复原咨询；sync 查收；stop 停止等待；doctor 诊断；audit 查看记录；progress 查看任务进度；disconnect 断开。cwc 与 /cwc 等价。"
PROBE = "这是用户在绑定菜单选择1后授权的一次通信核验，不是业务任务。请按指定响应首行返回，随后简短确认：已收到本次核验、当前Q/A编号、上方给出的建议对话名。不要展开规划，不改名，不调用工具。"


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".part")
    try:
        temp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def menu_path(home, scope):
    return home / "cwc/menus" / (hashlib.sha256(_scope(scope).encode()).hexdigest() + ".json")


def snapshot(home, scope):
    return Store.read_status(home / "cwc/state/cwc.sqlite3", scope, details=True)


def binding_key(value):
    return {k: value.get(k) for k in ("target", "generation")}


def menu_load(home, scope):
    path = menu_path(home, scope)
    if not path.exists():
        return None
    if path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError("CWC menu exceeds 8 MiB")
    result = json.loads(path.read_text(encoding="utf-8-sig"))
    if result.get("scope") != scope:
        raise ValueError("CWC menu scope differs")
    return result


def close_menu(home, scope):
    previous = menu_load(home, scope)
    if previous and previous["menu_kind"] != "closed":
        save(menu_path(home, scope), {**previous,"menu_kind":"closed","consumed":True})
    return bool(previous)


def communication(home, scope, binding):
    if binding.get("status") != "SELECTED":
        return "NOT_VERIFIED"
    with closing(sqlite3.connect((home / "cwc/state/cwc.sqlite3").as_uri() + "?mode=ro", uri=True)) as db:
        row = db.execute("""SELECT e.answer FROM exchanges e JOIN pairs p ON e.scope=p.scope
             AND e.generation=p.generation AND e.target_host=p.target_host AND e.target_id=p.target_id
             WHERE e.scope=? AND e.state='CONSUMED' ORDER BY e.rowid DESC LIMIT 1""", (_scope(scope),)).fetchone()
    return "PREVIOUSLY_VERIFIED" if row and not continuation_needed(row[0]) else "NOT_VERIFIED"


def display_binding(home, scope, binding, menu=None):
    verified = communication(home, scope, binding)
    text = "已绑定：《" + binding["title"] + "》\n建议对话名：" + binding["suggested_title"]
    text += "\n通信状态：" + ("已有完整往返核验记录" if verified == "PREVIOUSLY_VERIFIED" else "尚未核验")
    result = {"status":"BOUND", "message":text, "communication":verified,
              "generation":binding["generation"], "host_sends":0}
    if menu:
        result.update(menu_token=menu["menu_token"], options=[{"value":1,"title":"发送一次验证消息"}],
                      hint="输入1发送一次核验，或直接描述任务。")
    return result


def offer_verification(home, scope, binding, version):
    menu = None
    if binding.get("status") == "SELECTED" and not binding.get("exchange") and not binding.get("target_reserved"):
        menu = {"scope":scope,"menu_kind":"verification","menu_token":uuid.uuid4().hex,
                "created_at":time.time(),"consumed":False,"binding":binding_key(binding),"plugin_version":version}
        save(menu_path(home, scope), menu)
    else:
        close_menu(home, scope)
    if binding.get("status") != "SELECTED":
        return {"status":"UNPAIRED","message":"尚未绑定，请先输入cwc bind选择Chat。",
                "verification_available":False,"host_sends":0}
    result = {**display_binding(home, scope, binding, menu),"verification_available":menu is not None}
    if binding.get("exchange"):
        result["hint"] = "已有未完成咨询，请用cwc resume恢复原请求，完成后再核验。"
    elif binding.get("target_reserved"):
        result["hint"] = "当前Chat正被未完成咨询占用，请待原咨询结束后再核验。"
    return result


def business_instructions():
    path = Path(__file__).resolve().parent.parent / "references/WORKFLOW.md"
    if path.stat().st_size > 256 * 1024:
        raise ValueError("Business workflow exceeds 256 KiB")
    text = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        raise ValueError("Business workflow is empty")
    return {"path":str(path), "sha256":hashlib.sha256(text.encode()).hexdigest(), "text":text}


def act(data, home):
    if not isinstance(data, dict):
        raise ValueError("Expected an entry object")
    version = _installed_version(data.get("expected_version"))
    scope = data["scope"]
    _scope(scope)
    action = data.get("action", "command")
    if action == "listed":
        options = candidates(data["result"])
        if not options:
            return {"status":"NO_CANDIDATES","message":"本次列表中没有可选Chat，请稍后重新获取。","host_sends":0}
        binding = snapshot(home, scope)
        if binding_key(binding) != data["expected_binding"]:
            raise ValueError("Binding changed while listing; reopen the menu")
        raw, _ = _unwrap(data["result"])
        task_title = next((item.get("title") for item in raw.get("pinnedThreads", []) + raw["threads"]
                           if item.get("id") == scope["thread_id"] and item.get("kind") == "codex"
                           and item.get("hostId", "local") == scope["host_id"]), None)
        menu = {"scope":scope,"menu_kind":"candidates","menu_token":uuid.uuid4().hex,
                "created_at":time.time(),"consumed":False,"binding":binding_key(binding),
                "listing":data["result"],"task_title":task_title,"plugin_version":version}
        save(menu_path(home, scope), menu)
        return {"status":"CHOOSE_TARGET","message":"请选择要绑定的Chat：", "menu_token":menu["menu_token"],
                "options":[{"value":i+1,"title":v["title"]} for i,v in enumerate(options)], "host_sends":0}
    if action == "selected":
        menu = menu_load(home, scope)
        if not menu or menu.get("menu_token") != data.get("menu_token") or menu.get("consumed") or menu["menu_kind"] != "candidates":
            raise ValueError("Candidate menu is no longer active")
        if menu.get("plugin_version") != version or time.time() - menu["created_at"] > 1800 or binding_key(snapshot(home, scope)) != menu["binding"]:
            raise ValueError("Candidate menu expired or binding changed")
        number = data["selection"]
        choices = candidates(menu["listing"])
        if type(number) is not int or not 1 <= number <= len(choices):
            raise ValueError("Choose a displayed candidate number")
        selected = choices[number-1]
        fresh = candidates(data["result"])
        match = [v for v in fresh if all(v[k] == selected[k] for k in ("host_id","kind","conversation_id"))]
        if len(match) != 1:
            raise ValueError("The selected Chat disappeared; reopen the candidate list")
        control({"action":"pair","scope":scope,"listing":data["result"],
                 "conversation_id":selected["conversation_id"],"target_host_id":selected["host_id"],
                 "task_title":menu["task_title"]}, home)
        binding = snapshot(home, scope)
        return offer_verification(home, scope, binding, version)
    if action != "command":
        raise ValueError("Unknown entry action")
    command = data.get("command")
    if not isinstance(command, str) or len(command) > 128*1024:
        raise ValueError("Entry command must be bounded text")
    normalized = " ".join(command.strip().lower().split())
    parsed = re.fullmatch(r"/?cwc(?: (\w+))?", normalized)
    name = (parsed.group(1) or "open") if parsed else None
    if name == "help":
        return {"status":"HELP","message":HELP,"host_sends":0}
    if name == "refresh":
        try:
            refreshed = refresh(scope, home, data.get("expected_version"), full_resources=data.get("full_resources", False),
                                known_hashes=data.get("known_hashes"))
            binding = snapshot(home, scope)
            if any(refreshed["binding"].get(k) != binding.get(k) for k in ("status","generation")):
                raise ValueError("Binding changed while refreshing; refresh again")
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
            close_menu(home, scope)
            raise
        offered = offer_verification(home, scope, binding, version)
        return {**refreshed, **offered, "status":"REFRESHED", "message":"本地说明已刷新。\n"+offered["message"]}
    menu = menu_load(home, scope)
    numeric = normalized.isdecimal() and len(normalized) < 5
    if (not parsed and numeric and menu and menu["menu_kind"] != "closed" and data.get("menu_token") is not None
            and data["menu_token"] != menu.get("menu_token")):
        return {"status":"MENU_EXPIRED","message":"该选项来自旧菜单，请使用最近一次菜单。","host_sends":0}
    if not parsed and numeric and menu and menu["menu_kind"] != "closed" and data.get("menu_token") == menu.get("menu_token"):
        binding = snapshot(home, scope)
        if menu.get("plugin_version") != version or time.time() - menu["created_at"] > 1800 or binding_key(binding) != menu["binding"]:
            return {"status":"MENU_EXPIRED","message":"原菜单已失效；已有请求用cwc resume恢复，否则重新输入cwc refresh或cwc bind。","host_sends":0}
        if menu["menu_kind"] == "candidates" and not menu["consumed"]:
            number = int(normalized)
            if not 1 <= number <= len(candidates(menu["listing"])):
                raise ValueError("Choose a displayed number")
            return {"status":"VALIDATE_SELECTION","selection":number,"menu_token":menu["menu_token"],"host_sends":0}
        if menu["menu_kind"] == "verification" and normalized == "1":
            # Persist the one menu grant before preparing; the derived key survives crashes/racing clicks.
            if not menu["consumed"]:
                menu.update(consumed=True, probe_key=menu["menu_token"])
                save(menu_path(home, scope), menu)
            if menu.get("probe_key"):
                prepared = control({"action":"prepare","scope":scope,"body":PROBE,
                                    "idempotency_key":menu["probe_key"]}, home)
                request_id = prepared["request_id"]
                path = menu_path(home, scope).with_name(request_id + "-resume.json")
                save(path, {"action":"resume","scope":scope,"request_id":request_id})
                return {"status":"VERIFY_REQUEST","request_id":request_id,"input_path":str(path),
                        "message":"绑定已保存，正在核验通信。","host_sends":0}
    if not parsed:
        return {"status":"BUSINESS","menu_closed":close_menu(home, scope),"operator_workflow":business_instructions(),"host_sends":0}
    if name not in {"open","status","bind","rebind","disconnect","resume","sync","stop","doctor","audit","progress"}:
        return {"status":"HELP","message":HELP,"host_sends":0}
    binding = snapshot(home, scope)
    if name in {"bind","rebind","disconnect"} and menu:
        save(menu_path(home, scope), {**menu,"consumed":True,"menu_kind":"closed"})
    if name == "disconnect":
        return control({"action":"disconnect","scope":scope}, home)
    if name in {"bind","rebind"} or (name == "open" and binding["status"] == "UNPAIRED"):
        if binding.get("exchange"):
            return {"status":"PENDING","message":"当前还有未完成咨询，请先恢复或明确停止。","host_sends":0}
        return {"status":"NEED_LIST","expected_binding":binding_key(binding),"host_sends":0}
    if name in {"open","status"}:
        if binding["status"] == "UNPAIRED":
            return {"status":"UNPAIRED","message":"尚未绑定，输入cwc bind选择Chat。","host_sends":0}
        return {**display_binding(home, scope, binding),"pending":binding.get("exchange")}
    return {"status":"CONTROL","action":name,"scope":scope,"binding":binding,"host_sends":0}


def load_runtime(home):
    command = shutil.which("codex")
    if not command:
        raise ValueError("Codex executable is unavailable")
    raw = subprocess.run([command,"plugin","list","--json"],
                         capture_output=True,text=True,encoding="utf-8",check=True,timeout=30)
    installed = [x for x in json.loads(raw.stdout)["installed"] if x.get("name")=="cwc" and x.get("installed") and x.get("enabled")]
    if len(installed) != 1:
        raise ValueError("Expected exactly one enabled CWC installation")
    version = installed[0]["version"]
    marketplace = installed[0].get("marketplaceName")
    if not isinstance(marketplace,str) or not re.fullmatch(r"[A-Za-z0-9_-]+",marketplace):
        raise ValueError("Official listing lacks a supported marketplace cache identity")
    if not isinstance(version,str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+_-]{0,127}",version):
        raise ValueError("Invalid installed version")
    # The cache key comes from the official enabled instance; never use source checkout paths or directory ordering.
    package = home / "plugins/cache" / marketplace / "cwc" / version
    manifest = json.loads((package / ".codex-plugin/plugin.json").read_text(encoding="utf-8-sig"))
    if manifest.get("name") != "cwc" or manifest.get("version") != version:
        raise ValueError("Installed manifest mismatch")
    scripts = package / "skills/cwc/scripts"
    scope = {"host_id":"local","thread_id":os.environ.get("CODEX_THREAD_ID"),"workspace":str(Path.cwd().resolve())}
    _scope(scope)
    return {"source":(scripts/"entry_relay.js").read_text(encoding="utf-8-sig"),
            "controller":str(scripts/"entry.py"),"transport":str(scripts/"cwc.py"),
            "transport_source_path":str(scripts/"host_relay.js"),
            "expectedVersion":version,"marketplace":marketplace,"scope":scope,"outputDir":str(home/"cwc/entry-evidence"/hashlib.sha256(_scope(scope).encode()).hexdigest())}


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--load",action="store_true")
    parser.add_argument("--input",type=Path)
    args=parser.parse_args()
    home=Path(os.environ.get("CODEX_HOME") or Path.home()/".codex").expanduser().resolve()
    try:
        if args.load:
            result=load_runtime(home)
        else:
            if args.input is None or args.input.stat().st_size>8*1024*1024:
                raise ValueError("A bounded JSON input file is required")
            result=act(json.loads(args.input.read_text(encoding="utf-8-sig")),home)
        print(json.dumps(result,ensure_ascii=False))
    except (OSError,ValueError,KeyError,TypeError,sqlite3.Error,subprocess.SubprocessError) as error:
        print(json.dumps({"status":"ERROR","error":str(error)},ensure_ascii=False))
        raise SystemExit(1)
