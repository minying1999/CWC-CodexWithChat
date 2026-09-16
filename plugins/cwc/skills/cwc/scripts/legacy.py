"""Read-only upgrade guard. Old requests are never migrated or resent."""

import hashlib
import json
from pathlib import Path


def _document(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        result = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise ValueError(f"Cannot verify old CWC state: {path}") from error
    if not isinstance(result, dict):
        raise ValueError(f"Malformed old CWC state: {path}")
    return result


def _records(document: dict, key: str) -> dict:
    values = document.get(key, {})
    if not isinstance(values, dict) or any(not isinstance(v, dict) for v in values.values()):
        raise ValueError(f"Malformed old CWC {key}")
    return values


def check_legacy_pending(codex_home: Path, workspace: Path, target: dict) -> None:
    """Reject a target still used by a known old pending exchange, without writes."""
    package = Path(__file__).resolve().parents[3]
    canonical = codex_home / "cwc" / "state"
    # ponytail: inspect known CWC state locations only; custom old roots need explicit reconciliation.
    roots = [canonical, package / "state"]
    cache = codex_home / "plugins" / "cache"
    if cache.exists():
        roots.extend(path / "state" for path in cache.glob("*/cwc/*") if path.is_dir())
    roots = list(dict.fromkeys(path.resolve() for path in roots))
    authoritative = _records(_document(canonical / "bindings.json"), "bindings")
    records = list(authoritative.values())
    for root in roots[1:]:
        records.extend(value for key, value in _records(_document(root / "bindings.json"), "bindings").items()
                       if key not in authoritative)
    chat = target["conversation_id"]
    owners = set()
    for record in records:
        if record.get("pending_binding_id") and record.get("pending_physical_conversation_ref") == chat:
            raise ValueError("LEGACY_PENDING: finish the old binding request in its original version")
        if record.get("physical_chat_conversation_ref") == chat and record.get("codex_thread_ref"):
            owners.add(record["codex_thread_ref"])
    for root in roots:
        old = _document(root / "state.json")
        pending = _records(old, "pending_exchanges")
        covered = {(item.get("task_id"), item.get("business_id")) for item in pending.values()}
        for record in pending.values():
            if record.get("physical_conversation_ref") == chat and record.get("state") != "CONSUMED":
                raise ValueError("LEGACY_PENDING: finish the old consultation in its original version")
        for record in _records(old, "consults").values():
            if (record.get("physical_conversation_ref") == chat
                    and (record.get("task_id"), record.get("consult_id")) not in covered
                    and record.get("status") not in {"consumed", "resolved"}):
                raise ValueError("LEGACY_PENDING: old consultation delivery needs reconciliation")
    project_paths = [workspace / ".cwc" / "projects" / "registry.json"]
    project_paths.extend(root.parent / ".cwc" / "projects" / "registry.json" for root in roots[1:])
    for path in dict.fromkeys(project_paths):
        projects = _records(_document(path), "projects")
        old_workspace = path.parents[2]
        normalized = str(old_workspace.resolve()).replace("\\", "/").casefold()
        for owner in owners:
            fingerprint = hashlib.sha256("\0".join((normalized, owner, chat)).encode("utf-8")).hexdigest()
            project = projects.get(fingerprint, {})
            if project.get("reasoner_contract_sync_message_id"):
                raise ValueError("LEGACY_PENDING: finish the old contract sync in its original version")
