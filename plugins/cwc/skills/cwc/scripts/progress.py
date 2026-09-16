"""Read-only completion check over the caller's existing project checklist."""
from datetime import datetime, timezone


def check_progress(checklist, pending=None, review_received=False):
    if not isinstance(checklist, dict) or not isinstance(checklist.get("goal"), str) or not checklist["goal"].strip():
        raise ValueError("progress requires the current user goal")
    items = checklist.get("required")
    for key in ("user_stopped", "budget_exhausted", "review_required", "review_passed"):
        if key in checklist and type(checklist[key]) is not bool:
            raise ValueError(f"{key} must be a boolean from the user's instruction or observed budget")
    if not isinstance(items, list) or not items or len(items) > 200:
        raise ValueError("progress requires the existing nonempty required-item checklist")
    stalled = False
    if "last_progress_at" in checklist:
        value = checklist["last_progress_at"]
        evidence = checklist.get("last_progress_evidence")
        if not isinstance(value, str) or not isinstance(evidence, str) or not evidence.strip():
            raise ValueError("last progress requires an actual UTC timestamp and evidence reference")
        progress_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if progress_at.tzinfo is None:
            raise ValueError("last_progress_at requires a timezone")
        stalled = (datetime.now(timezone.utc) - progress_at).total_seconds() >= 1800
    ids, remaining = set(), []
    for item in items:
        if (not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"].strip()
                or item["id"] in ids or not isinstance(item.get("title"), str) or not item["title"].strip()
                or type(item.get("passed")) is not bool):
            raise ValueError("each required item needs a unique id, title and passed boolean")
        ids.add(item["id"])
        for key in ("next_action", "blocked_by", "question"):
            if key in item and (not isinstance(item[key], str) or not item[key].strip()):
                raise ValueError(f"{key} must contain a concrete explanation")
        if "authorized" in item and type(item["authorized"]) is not bool:
            raise ValueError("authorized must be a boolean")
        if "human_action_required" in item and type(item["human_action_required"]) is not bool:
            raise ValueError("human_action_required must be a boolean")
        proof = item.get("evidence", [])
        if not isinstance(proof, list) or any(not isinstance(p, str) or not p.strip() for p in proof):
            raise ValueError("evidence must contain checked, attributable evidence references")
        if item["passed"] and not proof:
            raise ValueError("a passed item requires actual verification evidence")
        if not item["passed"]:
            remaining.append(item)
    human_required = any(i.get("blocked_by") and i.get("human_action_required") is True for i in remaining)
    def outcome(status, reason, item=None):
        if stalled and status in {"CONTINUE_EXECUTION", "NEED_CONSULTATION", "WAITING", "PAUSED", "BLOCKED"}:
            status, reason = "NO_PROGRESS", "No material progress for 30 minutes; preserve state and request the needed decision."
        return {"status": status, "project_complete": status == "COMPLETE", "reason": reason,
                "human_action_required":human_required,
                "notify_user": status in {"COMPLETE", "NO_PROGRESS"} or (status == "BLOCKED" and human_required),
                "remaining_items": [i["id"] for i in remaining], "next_required_item": item,
                "basis": "Caller-supplied checklist and evidence; the Executor must verify their truth."}
    if checklist.get("user_stopped") is True:
        return outcome("STOPPED", "The user explicitly stopped this work.")
    if not remaining:
        if pending:
            return outcome("PAUSED" if pending.get("wait_expired") else "WAITING",
                           "A consultation still has an unresolved result; resolve or explicitly stop it.")
        if checklist.get("review_required", True) is not False:
            if not review_received:
                return outcome("NEED_CONSULTATION", "Submit actual implementation evidence for the required final review.")
            if checklist.get("review_passed") is not True:
                return outcome("CONTINUE_EXECUTION", "Read the received review, add its unresolved findings to required items, and verify their resolution before marking review_passed true.")
        return outcome("COMPLETE", "All declared required items have verification evidence and the required review is accepted.")
    if checklist.get("budget_exhausted") is True:
        return outcome("PAUSED", "The explicitly specified execution budget is exhausted; preserve the resume point.")
    for item in remaining:
        if item.get("authorized") is True and item.get("next_action") and not item.get("blocked_by") and not item.get("question"):
            return outcome("CONTINUE_EXECUTION", item["next_action"], item["id"])
    for item in remaining:
        if item.get("question") and not item.get("blocked_by"):
            state = ("PAUSED" if pending.get("wait_expired") else "WAITING") if pending else "NEED_CONSULTATION"
            return outcome(state, item["question"], item["id"])
    if pending:
        return outcome("PAUSED" if pending.get("wait_expired") else "WAITING",
                       "Preserve the existing request; do not send it again.")
    blockers = [i for i in remaining if i.get("blocked_by")]
    if len(blockers) == len(remaining):
        return outcome("BLOCKED", "; ".join(str(i["blocked_by"]) for i in blockers), remaining[0]["id"])
    return outcome("NEED_CONSULTATION", "Required work remains, but the next authorized action or missing evidence is not specified.", remaining[0]["id"])
