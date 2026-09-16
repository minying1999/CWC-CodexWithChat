"""File-backed local controller; only Codex invokes the official Host tools."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

from host import candidates, read_health, reply, send_receipt, continuation_needed
from legacy import check_legacy_pending
from store import Store, validate_stop
from progress import check_progress


def _installed_version(expected_version=None):
    skill_dir = Path(__file__).resolve().parent.parent
    manifest = json.loads((skill_dir.parents[1] / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8-sig"))
    if not isinstance(manifest, dict) or manifest.get("name") != "cwc":
        raise ValueError("Not a CWC plugin manifest")
    version = manifest.get("version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("CWC plugin version is missing")
    if expected_version is not None and version != expected_version:
        raise ValueError("CWC version differs from the selected installation; resolve the installed path again")
    return version


def _local_resources(expected_version: str | None = None) -> tuple[str, dict]:
    skill_dir = Path(__file__).resolve().parent.parent
    version = _installed_version(expected_version)
    resources = {}
    for name, relative in (("skill", "SKILL.md"), ("reasoner", "REASONER.md"),
                           ("host_workflow", "references/HOST_WORKFLOW.md"),
                           ("host_relay", "scripts/host_relay.js"),
                           ("contract", "CWC_AGENT_CONTRACT.md"),
                           ("workflow", "references/WORKFLOW.md"),
                           ("continuation", "references/CONTINUATION.md"),
                           ("entry", "scripts/entry.py"), ("entry_relay", "scripts/entry_relay.js")):
        path = skill_dir / relative
        if path.stat().st_size > 256 * 1024:
            raise ValueError(f"CWC {name} resource exceeds 256 KiB")
        raw = path.read_bytes()
        text = raw.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
        if not text.strip():
            raise ValueError(f"CWC {name} resource is empty")
        resources[name] = {"path": str(path), "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "text": text}
    frontmatter = resources["skill"]["text"].replace("\r\n", "\n").split("---", 2)
    if (len(frontmatter) != 3 or frontmatter[0].strip()
            or not re.search(r"(?m)^name:[ \t]*['\"]?cwc['\"]?[ \t]*$", frontmatter[1])):
        raise ValueError("CWC Skill frontmatter is invalid")
    if not re.search(r"(?m)^# CWC Agent Contract v[0-9]+\r?$", resources["contract"]["text"]):
        raise ValueError("CWC Agent Contract header is invalid")
    if re.search(r"(?im)(?:[A-Z]:\\|/Users/|/home/|\\\\[^\\]+\\)", resources["contract"]["text"]):
        raise ValueError("CWC Agent Contract must not contain machine paths")
    return version, resources


def _contract_meta(resources: dict) -> tuple[str, str, dict[str, str]]:
    text = resources["contract"]["text"]
    match = re.search(r"(?m)^# CWC Agent Contract v([0-9]+)$", text)
    if not match:
        raise ValueError("CWC Agent Contract version is missing")
    hashes = {name: item["sha256"] for name, item in resources.items()}
    return f"{match.group(1)}", resources["contract"]["sha256"], hashes


def refresh(scope: dict, codex_home: Path, expected_version: str | None = None, task_title=None,
            full_resources=False, known_hashes=None) -> dict:
    version, resources = _local_resources(expected_version)
    if type(full_resources) is not bool or (known_hashes is not None and
            (not isinstance(known_hashes, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in known_hashes.items()))):
        raise ValueError("Invalid refresh options")
    changed = [name for name, item in resources.items() if (known_hashes or {}).get(name) != item["sha256"]]
    return {"status": "REFRESHED", "plugin_version": version,
            "resources": resources if full_resources else {name: {k:v for k,v in item.items() if k != "text"} for name,item in resources.items()},
            "operator_instructions": resources["skill"]["text"], "changed_resources": changed,
            "reference_policy": "Use these entry instructions now. Read the current substantive workflow when entering business work; never infer behavior from hashes.",
            "binding": Store.read_status(codex_home / "cwc" / "state" / "cwc.sqlite3", scope, task_title=task_title),
            "session_reload": "NOT_PERFORMED", "host_actions": 0}


def route(target: dict) -> dict:
    args = {"threadId": target["conversation_id"]}
    if target["host_id"] != "chatgpt":
        args["hostId"] = target["host_id"]
    return args


def check_call(target: dict, called_with: dict, payload: str | None = None) -> None:
    if not isinstance(called_with, dict):
        raise ValueError("Pass the actual Host call arguments unchanged")
    expected = route(target)
    if any(called_with.get(key) != expected.get(key) for key in ("threadId", "hostId")):
        raise ValueError("Host call target does not match the frozen request target")
    if payload is not None and called_with.get("prompt") != payload:
        raise ValueError("Host send payload differs from the frozen request")


def validate_send_arguments(arguments: dict, request_id: str) -> None:
    if (not isinstance(arguments, dict) or set(arguments) - {"threadId", "prompt", "hostId"}
            or not {"threadId", "prompt"} <= set(arguments)):
        raise ValueError("Invalid CWC Host send argument fields")
    for key in ("threadId", "hostId"):
        if key in arguments and (not isinstance(arguments[key], str) or not arguments[key].strip()
                or any(ord(c) < 32 for c in arguments[key])):
            raise ValueError(f"Invalid CWC send {key}")
    prompt = arguments["prompt"]
    if not isinstance(prompt, str) or not prompt.startswith(f"CWC REQUEST {request_id}\n"):
        raise ValueError("CWC send prompt must be the nonempty frozen request")
    if not prompt.split("\n", 1)[1].strip():
        raise ValueError("CWC send prompt has no content")


def doctor(scope: dict, codex_home: Path, available_tools=None, read_probe=None, expected_version=None, task_title=None) -> dict:
    required = ("mcp__codex_app__list_threads", "mcp__codex_app__send_message_to_thread",
                "mcp__codex_app__read_thread", "exec_command", "apply_patch")
    if available_tools is not None and (not isinstance(available_tools, list)
            or any(not isinstance(name, str) for name in available_tools)):
        raise ValueError("available_tools must be the caller's actual tool-name list")
    tools = {name: "NOT_CHECKED" if available_tools is None else
             "LISTED" if name in available_tools else "MISSING" for name in required}
    result = {"status": "OK", "plugin_version": None, "binding": None, "tools": tools,
              "host_read": {"status": "NOT_CHECKED"}, "issues": [], "host_actions": 0}
    issues = result["issues"]
    if available_tools is None:
        issues.append({"code": "TOOLS_UNCHECKED", "detail": "Tool inventory was not supplied; presence is unknown."})
    elif "MISSING" in tools.values():
        issues.append({"code": "MISSING_TOOLS", "detail": "Required Host tools are absent from the supplied inventory."})
    try:
        result["plugin_version"], resources = _local_resources(expected_version)
        result["resources"] = {name: {"sha256": item["sha256"]} for name, item in resources.items()}
    except (OSError, ValueError, TypeError) as error:
        issues.append({"code": "LOCAL_RESOURCE_ERROR", "detail": str(error)})
    try:
        binding = Store.read_status(codex_home / "cwc" / "state" / "cwc.sqlite3", scope, details=True, task_title=task_title)
        target = binding.pop("target", None)
        result["binding"] = binding
    except (OSError, ValueError, sqlite3.Error) as error:
        binding, target = {}, None
        issues.append({"code": "STATE_READ_ERROR", "detail": str(error)})
    state = (binding.get("exchange") or {}).get("state")
    result["workflow"] = {"PREPARED": "NOT_SENT", "UNKNOWN": "DELIVERY_UNKNOWN",
                          "WAITING": "WAITING_REPLY", "READY": "READY_TO_APPLY"}.get(
                              state, "IDLE" if binding.get("status") == "SELECTED" else "UNKNOWN")
    if binding.get("status") == "UNPAIRED":
        result["workflow"] = "UNPAIRED"
        issues.append({"code": "UNPAIRED", "detail": "Select an existing Chat before consulting."})
    if binding.get("target_reserved"):
        issues.append({"code": "TARGET_RESERVED", "detail": "This Chat still has an unresolved request; use another target or reconcile it."})
    if state == "UNKNOWN":
        issues.append({"code": "DELIVERY_UNKNOWN", "detail": "Read the existing request or explicitly stop waiting; do not resend."})
    if read_probe is not None:
        try:
            if target is None or not isinstance(read_probe, dict):
                raise ValueError("A read observation requires a selected target and captured call/result")
            check_call(target, read_probe["called_with"])
            result["host_read"] = read_health(read_probe["result"], target)
        except (KeyError, TypeError, ValueError) as error:
            result["host_read"] = {"status": "READ_FAILED", "detail": str(error)}
            issues.append({"code": "READ_FAILED", "detail": str(error)})
    elif target is not None and tools[required[2]] == "LISTED":
        result["probe"] = {"tool": required[2], "arguments": {**route(target), "turnLimit": 1,
                           "includeOutputs": False, "maxOutputCharsPerItem": 1000}}
    if issues:
        result["status"] = "ATTENTION"
    elif result["workflow"] in {"NOT_SENT", "WAITING_REPLY"}:
        result["status"] = "WAITING"
    return result


def execute(data: dict, codex_home: Path) -> dict:
    result = _execute(data, codex_home)
    if result.get("request_id") and data.get("scope"):
        result.update(Store(codex_home / "cwc/state/cwc.sqlite3").display(data["scope"], result["request_id"]))
        result.setdefault("project_complete", False)
        if result["status"] in {"CONSUMED", "RECOVERED"}:
            result["next_step"] = "Check remaining acceptance items; execute, consult with new evidence, or record a real blocker."
            more = continuation_needed(result.get("answer"))
            result.update(answer_complete=more is None, required_continuation=more)
            if more:
                result["next_step"] = "NEED_CONSULTATION: obtain the named remaining sections with a new linked follow-up before execution."
    return result


def _execute(data: dict, codex_home: Path) -> dict:
    if not isinstance(data, dict):
        raise ValueError("Expected one JSON object")
    action = data.get("action")
    current_resources = None
    if data.get("expected_version") is not None and action not in {"refresh", "doctor"}:
        _installed_version(data["expected_version"])
    if action in {"pair", "prepare", "consult"}:
        _, current_resources = _local_resources()
    if action == "list":
        return {"status": "CHOOSE_TARGET", "candidates": candidates(data["result"])}
    if action == "refresh":
        return refresh(data["scope"], codex_home, data.get("expected_version"), data.get("task_title"),
                       data.get("full_resources", False), data.get("known_hashes"))
    if action == "doctor":
        return doctor(data["scope"], codex_home, data.get("available_tools"), data.get("read_probe"), data.get("expected_version"), data.get("task_title"))
    if action == "check-progress":
        snapshot = Store.read_status(codex_home / "cwc/state/cwc.sqlite3", data["scope"])
        checklist = data["checklist"]
        review_received = False
        review_id = checklist.get("review_request_id") if isinstance(checklist, dict) else None
        if review_id:
            # Read an existing accepted review; never create/migrate state for this check.
            from contextlib import closing
            from store import _scope, _request_id
            with closing(sqlite3.connect((codex_home / "cwc/state/cwc.sqlite3").as_uri() + "?mode=ro", uri=True)) as db:
                row = db.execute("""SELECT e.state,e.mode,e.answer FROM exchanges e JOIN pairs p ON e.scope=p.scope
                                 AND e.generation=p.generation AND e.target_host=p.target_host
                                 AND e.target_kind=p.target_kind AND e.target_id=p.target_id
                                 WHERE e.scope=? AND e.request_id=?""",
                                 (_scope(data["scope"]), _request_id(review_id))).fetchone()
            review_received = (row is not None and tuple(row[:2]) == ("CONSUMED", "review") and not continuation_needed(row[2]) and
                        (snapshot.get("last_exchange") or {}).get("request_id") == review_id)
        return check_progress(checklist, snapshot.get("exchange"), review_received)
    if action == "resume" and not data.get("request_id"):
        snapshot = Store.read_status(codex_home / "cwc/state/cwc.sqlite3", data["scope"])
        current = snapshot.get("exchange") or snapshot.get("last_exchange")
        if current is None:
            return {"status": "IDLE", "host_actions": 0}
        data = {**data, "request_id": current["request_id"]}
    if action in {"stop", "abandon"}:
        validate_stop(data.get("scope"), data.get("request_id"), data.get("reason"), data.get("confirmed", False))
        if not (codex_home / "cwc" / "state" / "cwc.sqlite3").is_file():
            raise ValueError("No local CWC request exists to stop")
    store = Store(codex_home / "cwc" / "state" / "cwc.sqlite3")
    scope = data["scope"]
    if action == "pair":
        choices = candidates(data["listing"])
        selected = [item for item in choices
                    if item["conversation_id"] == data["conversation_id"]
                    and item["host_id"] == data["target_host_id"]]
        if len(selected) != 1:
            raise ValueError("Choose exactly one target from the official Host listing")
        check_legacy_pending(codex_home, Path(scope["workspace"]), selected[0])
        contract_version, contract_hash, _ = _contract_meta(current_resources)
        return store.pair(scope, selected[0], data.get("task_title"),
                          contract_version, contract_hash)
    if action == "status":
        return store.status(scope)
    if action == "disconnect":
        return store.disconnect(scope)
    if action in {"prepare", "consult"}:
        contract_version, contract_hash, resource_hashes = _contract_meta(current_resources)
        prepared = store.prepare(scope, data["body"], data.get("mode", "consult"),
                                 data.get("task_title"), contract_version, contract_hash,
                                 resource_hashes, current_resources["contract"]["text"],
                                 data.get("parent_request_id"), data.get("waiting"),
                                 current_resources["reasoner"]["text"], data.get("idempotency_key"))
        if action == "prepare":
            return prepared
        request_id = prepared["request_id"]
        action = "claim"
    else:
        request_id = data["request_id"]
    exchange = store.get_exchange(scope, request_id)
    target = exchange["target"]
    if action == "resume":
        state = exchange["state"]
        if state == "READY":
            delivered = store.consume(scope, request_id)
            if delivered["status"] != "ALREADY_CONSUMED":
                return delivered
            state = "CONSUMED"
        if state == "CONSUMED":
            return {"status": "RECOVERED", "state": state, "request_id": request_id,
                    "reply_id": exchange["reply_id"], "answer": exchange["answer"],
                    "effect_check_required": True,
                    "notice": "Previously delivered answer: inspect actual work before continuing; do not replay effects."}
        # Resume never claims a send. The relay may claim PREPARED only in explicit send mode.
        return execute({**data, "action": "read", "late_probe": True}, codex_home)
    if action in {"stop", "abandon"}:
        return store.abandon(scope, request_id, data["reason"], data.get("confirmed", False))
    if action == "claim":
        if exchange["state"] == "PREPARED":
            _, current_resources = _local_resources()
            contract_version, contract_hash, hashes = _contract_meta(current_resources)
            store.verify_contract(scope, request_id, contract_version, contract_hash, hashes)
            validate_send_arguments({**route(target), "prompt": exchange["payload"]}, request_id)
        check_legacy_pending(codex_home, Path(scope["workspace"]), target)
        claimed = store.claim(scope, request_id)
        if claimed["status"] != "SEND_ONCE":
            return claimed
        return {"status": "SEND_ONCE", "request_id": request_id,
                "tool": "mcp__codex_app__send_message_to_thread",
                "arguments": {**route(target), "prompt": claimed["payload"]}}
    if action == "record-send":
        check_call(target, data["called_with"], exchange["payload"])
        receipt = store.record_send(scope, request_id, send_receipt(data["result"], target))
        if receipt["state"] in {"UNKNOWN", "WAITING"}:
            receipt["next"] = execute({"action": "read", "scope": scope, "request_id": request_id}, codex_home)
        return receipt
    if action in {"read", "read-start"}:
        if "late_probe" in data and type(data["late_probe"]) is not bool:
            raise ValueError("late_probe must be a boolean")
        schedule = store.schedule_read(scope, request_id, reserve=action == "read-start", allow_late=data.get("late_probe", False))
        if schedule["status"] != "READ_PENDING":
            return schedule
        args = {**route(target), "turnLimit": 10, "includeOutputs": True,
                "maxOutputCharsPerItem": 20000}
        if data.get("cursor") is not None:
            if not isinstance(data["cursor"], str) or len(data["cursor"]) > 4096:
                raise ValueError("Invalid Host page cursor")
            args["cursor"] = data["cursor"]
        return {**schedule,
                "tool": "mcp__codex_app__read_thread", "arguments": args}
    if action in {"receive", "accept"}:
        check_call(target, data["called_with"])
        diagnostics = {}
        answer = reply(data["result"], target, request_id, diagnostics)
        store.observe(scope, request_id, diagnostics.get("code", "NO_MATCH"))
        if answer is None:
            return {"status": "NO_MATCH", "request_id": request_id,
                    "state": exchange["state"], "reason": diagnostics.get("code", "NO_MATCH")}
        received = store.receive(scope, request_id, answer["reply_id"], answer["answer"])
        return store.consume(scope, request_id) if action == "accept" and received["state"] in {"READY", "CONSUMED"} else received
    if action == "consume":
        return store.consume(scope, request_id)
    if action == "cancel":
        return store.cancel(scope, request_id)
    if action == "inspect":
        return {"status": "READ_ONLY_RECOVERY", "request_id": request_id,
                "state": exchange["state"], "answer": exchange.get("answer"),
                "stop_reason": exchange.get("stop_reason"), "stopped_at": exchange.get("stopped_at"),
                "notice": "Inspect actual work before continuing; do not replay side effects."}
    raise ValueError("Unknown CWC action")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=["refresh", "doctor", "stop", "resume"], help="Refresh, diagnose, resume, or explicitly stop waiting")
    parser.add_argument("--input", type=Path, help="UTF-8 JSON file; default is stdin")
    parser.add_argument("--codex-home", type=Path, help="Isolated state root for tests")
    parser.add_argument("--thread-id", help="Refresh context; defaults to CODEX_THREAD_ID")
    parser.add_argument("--host-id", default="local", help="Refresh context for this local desktop host")
    parser.add_argument("--workspace", type=Path, default=Path.cwd(), help="Refresh context; defaults to the working directory")
    parser.add_argument("--task-title", help="Current task title for the manual naming suggestion; not identity")
    parser.add_argument("--request-id", help="Exact request to stop waiting for")
    parser.add_argument("--reason", help="User's reason for stopping")
    parser.add_argument("--confirm", action="store_true", help="The user explicitly chose to stop this request")
    parser.add_argument("--expected-version", help="Reject a stale plugin before any state operation")
    args = parser.parse_args()
    if args.command and args.input:
        parser.error("Use a command or --input, not both")
    home = (args.codex_home or Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")).expanduser().resolve()
    try:
        if args.command:
            data = {"action": args.command, "scope": {"host_id": args.host_id,
                    "thread_id": args.thread_id or os.environ.get("CODEX_THREAD_ID"),
                    "workspace": str(args.workspace.resolve())}}
            if args.command == "stop":
                data.update(request_id=args.request_id, reason=args.reason, confirmed=args.confirm)
            elif args.command == "resume" and args.request_id:
                data["request_id"] = args.request_id
            if args.expected_version:
                data["expected_version"] = args.expected_version
            if args.task_title is not None:
                data["task_title"] = args.task_title
            print(json.dumps(execute(data, home), ensure_ascii=False))
            return 0
        if args.input and args.input.stat().st_size > 8 * 1024 * 1024:
            raise ValueError("CWC input exceeds 8 MiB")
        raw = args.input.read_text(encoding="utf-8-sig") if args.input else sys.stdin.read(8 * 1024 * 1024 + 1)
        if len(raw.encode("utf-8")) > 8 * 1024 * 1024:
            raise ValueError("CWC input exceeds 8 MiB")
        data = json.loads(raw)
        if args.expected_version:
            if not isinstance(data, dict) or data.get("expected_version", args.expected_version) != args.expected_version:
                raise ValueError("Conflicting expected plugin versions")
            data["expected_version"] = args.expected_version
        result = execute(data, home)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
        print(json.dumps({"status": "ERROR", "error": str(error),
                          "error_code": getattr(error, "code", "LOCAL_ERROR")}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdin.reconfigure(encoding="utf-8")
    raise SystemExit(main())
