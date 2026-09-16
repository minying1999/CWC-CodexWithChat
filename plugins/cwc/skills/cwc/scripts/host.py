"""Strict adapters for captured official Codex App tool results; no Host calls."""

import json
import re


class HostDataError(ValueError):
    """The Host result is erroneous, unsupported, ambiguous, or misrouted."""

    def __init__(self, message, code="HOST_DATA_INVALID"):
        super().__init__(message)
        self.code = code


def _unwrap(raw):
    frames = []
    for _ in range(6):
        if not isinstance(raw, dict):
            raise HostDataError("Host result must be a structured object")
        frames.append(raw)
        _check_failure(raw)
        if "structuredContent" in raw and raw["structuredContent"] is not None:
            raw = raw["structuredContent"]
        elif "content" in raw:
            content = raw["content"]
            if not isinstance(content, list) or len(content) != 1:
                raise HostDataError("Unsupported Host content envelope")
            block = content[0]
            if not isinstance(block, dict) or block.get("type") != "text":
                raise HostDataError("Host content must contain one JSON text block")
            _check_failure(block)
            frames.append(block)
            try:
                raw = json.loads(block["text"])
            except (KeyError, TypeError, ValueError) as error:
                raise HostDataError("Host text content is not structured JSON") from error
        else:
            return raw, frames
    raise HostDataError("Host envelope nesting is unsupported")


def _value(data, keys):
    values = [data[k] for k in keys if data.get(k) is not None]
    if any(not isinstance(v, str) or not v.strip() for v in values):
        raise HostDataError("Host identity fields must be nonempty strings")
    if len(set(values)) > 1:
        raise HostDataError("Conflicting Host identity fields")
    return values[0] if values else None


def _route(frames, target):
    for frame in frames:
        host = _value(frame, ("hostId", "host_id"))
        chat = _value(frame, ("threadId", "thread_id", "conversationId", "conversation_id"))
        if host is not None and host != target.get("host_id"):
            raise HostDataError("Host route does not match selected target", "HOST_ROUTE_MISMATCH")
        if chat is not None and chat != target["conversation_id"]:
            raise HostDataError("Conversation does not match selected target", "HOST_ROUTE_MISMATCH")
        if frame.get("kind") is not None and frame["kind"] != "chatgpt":
            raise HostDataError("Host result is not an ordinary ChatGPT conversation", "HOST_ROUTE_MISMATCH")
        if "thread" in frame:
            thread = frame["thread"]
            if not isinstance(thread, dict) or not thread.get("id"):
                raise HostDataError("Malformed Host thread identity")
            _route([{**thread, "threadId": thread["id"]}], target)


def _partial(frames):
    values = [f.get(k) for f in frames for k in ("truncated", "isTruncated", "partial", "isPartial")]
    if any(v is not None and not isinstance(v, bool) for v in values):
        raise HostDataError("Malformed Host completeness flag")
    return any(v is True for v in values)


def _status(data):
    value = data.get("status")
    if isinstance(value, dict):
        value = value.get("type")
    if value is not None and not isinstance(value, str):
        raise HostDataError("Malformed Host status")
    return value


def _check_failure(frame):
    for key in ("isError", "success", "accepted"):
        if key in frame and not isinstance(frame[key], bool):
            raise HostDataError("Malformed Host outcome flag")
    if (frame.get("isError") is True or frame.get("error")
            or frame.get("success") is False or frame.get("accepted") is False
            or _status(frame) in {"failed", "error", "rejected", "cancelled", "interrupted"}):
        raise HostDataError("Host reported an error; delivery is not confirmed", "HOST_REPORTED_ERROR")


def candidates(raw_listing):
    """Only explicitly identified ChatGPT entries are selectable; titles are labels."""
    data, frames = _unwrap(raw_listing)
    if _partial(frames):
        raise HostDataError("Host listing is truncated or partial", "HOST_PARTIAL")
    if not isinstance(data.get("threads"), list):
        raise HostDataError("Expected official threads listing")
    pinned = data.get("pinnedThreads", [])
    if not isinstance(pinned, list):
        raise HostDataError("Malformed pinnedThreads listing")
    outer_hosts = {_value(f, ("hostId", "host_id")) for f in frames}
    outer_hosts.discard(None)
    if len(outer_hosts) > 1:
        raise HostDataError("Conflicting listing Host routes")
    inherited_host = next(iter(outer_hosts), None)
    result = {}
    for item in pinned + data["threads"]:
        if not isinstance(item, dict):
            raise HostDataError("Malformed thread listing entry")
        if item.get("kind") != "chatgpt":
            continue
        chat = _value(item, ("id", "threadId", "conversationId", "conversation_id"))
        # The official global ChatGPT surface omits hostId; it is not host 'local'.
        host = _value(item, ("hostId", "host_id")) or inherited_host or "chatgpt"
        title = item.get("title")
        if chat is None or not isinstance(title, str) or not title.strip():
            raise HostDataError("Chat entry lacks an exact identity or title")
        target = {"host_id": host, "kind": "chatgpt", "conversation_id": chat, "title": title}
        key = (host, chat)
        if key in result and result[key] != target:
            raise HostDataError("Conflicting entries for one Chat target")
        result[key] = target
    return list(result.values())


def send_receipt(raw, target):
    """An unaccepted receipt means uncertain delivery, never permission to resend."""
    try:
        data, frames = _unwrap(raw)
        _route(frames, target)
        if _partial(frames):
            raise HostDataError("Partial Host send result", "HOST_PARTIAL")
        message_id = _value(data, ("message_ref", "message_id", "messageId", "outbound_message_id"))
        turn_id = _value(data, ("turnId", "turn_id"))
        status = _status(data)
        accepted = (data.get("success") is True or data.get("accepted") is True
                    or status in {"queued", "accepted", "sent", "success"})
        if not accepted:
            raise HostDataError("Unknown Host send receipt; acceptance is not confirmed")
        return {"accepted": True, "message_id": message_id, "turn_id": turn_id, "error": None}
    except HostDataError as error:
        return {"accepted": False, "message_id": None, "turn_id": None, "error": str(error)}


def read_health(raw_read, target):
    """Verify a captured read reached the selected Chat; do not accept any answer."""
    data, frames = _unwrap(raw_read)
    _route(frames, target)
    if _partial(frames):
        raise HostDataError("Host read envelope is truncated or partial", "HOST_PARTIAL")
    thread = data.get("thread")
    if not isinstance(thread, dict) or thread.get("id") != target["conversation_id"] or thread.get("kind") != "chatgpt":
        raise HostDataError("Host read lacks the selected Chat's identity", "HOST_ROUTE_MISMATCH")
    if _partial([thread]):
        raise HostDataError("Host thread metadata is truncated or partial", "HOST_PARTIAL")
    _check_failure(thread)
    return {"status": "READABLE", "conversation_status": _status(thread)}


def _response_header_variant(line, request_id):
    """Diagnose only an explicit first-line declaration; never accept this variant."""
    for wrapper in ("**", "__", "`"):
        if line.startswith(wrapper) and line.endswith(wrapper):
            line = line[len(wrapper):-len(wrapper)]
            break
    return re.fullmatch(r"CWC[ \t]+RESPONSE(?:[ \t]+|[ \t]*[:：][ \t]*)"
                        + request_id + r"[ \t]*[.:：。]?", line) is not None


def continuation_needed(answer):
    if not isinstance(answer, str) or not answer.strip():
        return None
    last = answer.rstrip().splitlines()[-1]
    marker = re.fullmatch(r"CWC CONTINUE:\s*(.+)", last)
    return marker.group(1) if marker else None


def reply(raw_read, target, request_id, diagnostics=None):
    """Return a complete correlated reply or None; optionally report the read reason."""
    if diagnostics is not None:
        if not isinstance(diagnostics, dict):
            raise HostDataError("diagnostics must be a dictionary")
        diagnostics["code"] = "NO_MATCH"
    if not isinstance(request_id, str) or not re.fullmatch(r"req_[0-9a-f]{32}", request_id):
        raise HostDataError("Invalid consultation request ID")
    data, frames = _unwrap(raw_read)
    _route(frames, target)
    thread = data.get("thread")
    if _partial(frames) or (isinstance(thread, dict) and _partial([thread])):
        raise HostDataError("Host read result is truncated or partial", "HOST_PARTIAL")
    if isinstance(data.get("messages"), list):
        groups = [(data, data["messages"])]
    elif isinstance(data.get("turns"), list):
        groups = []
        for turn in data["turns"]:
            if not isinstance(turn, dict) or not isinstance(turn.get("items"), list):
                raise HostDataError("Unsupported Host turn shape")
            groups.append((turn, turn["items"]))
    else:
        raise HostDataError("Host read has no structured messages or turns")
    found = {}
    pending = malformed = False
    for parent, messages in groups:
        _route([parent], target)
        for message in messages:
            if not isinstance(message, dict):
                raise HostDataError("Malformed Host message")
            role, kind = message.get("role"), message.get("type")
            if any(v is not None and not isinstance(v, str) for v in (role, kind)):
                raise HostDataError("Malformed message provenance")
            if role != "assistant" and kind not in {"agentMessage", "assistantMessage", "assistant_message"}:
                if role in {"user", "system", "tool"} or kind in {"userMessage", "toolCall", "toolResult", "reasoning"}:
                    continue
                raise HostDataError("Unknown or summary-only Host message shape")
            if role is not None and role != "assistant":
                raise HostDataError("Conflicting assistant message provenance")
            if kind is not None and kind not in {"agentMessage", "assistantMessage", "assistant_message"}:
                raise HostDataError("Conflicting assistant message type")
            _route([message], target)
            text = message.get("text")
            if not isinstance(text, str):
                raise HostDataError("Assistant message lacks full text")
            lines = text.strip().splitlines()
            if not lines:
                continue
            header = lines[0].rstrip(" \t")
            exact_header = header == "CWC RESPONSE " + request_id
            if not exact_header and not _response_header_variant(header, request_id):
                continue
            if len(text.encode("utf-16-le")) // 2 >= 20000:
                raise HostDataError("Correlated reply reaches the Host ceiling; completeness is ambiguous", "HOST_LENGTH_LIMIT")
            if _partial([parent, message]):
                raise HostDataError("Correlated reply is truncated or partial", "HOST_PARTIAL")
            for frame in (parent, message):
                _check_failure(frame)
            completion = [frame.get("complete") for frame in (parent, message)]
            if any(value is not None and not isinstance(value, bool) for value in completion):
                raise HostDataError("Malformed message completion flag")
            statuses = {_status(frame) for frame in (parent, message)}
            if False in completion or statuses & {"running", "in_progress", "pending", "streaming"}:
                pending = True
                continue
            if True not in completion and not statuses & {"completed", "complete", "done"}:
                raise HostDataError("Correlated reply has no completion evidence")
            if not exact_header:
                malformed = True
                continue
            answer = "\n".join(lines[1:]).strip()
            if not answer:
                raise HostDataError("Correlated reply has no answer")
            reply_id = _value(message, ("id", "messageId", "message_id"))
            if reply_id is None and parent is not data:
                reply_id = _value(parent, ("id", "turnId", "turn_id"))
            if reply_id is None:
                raise HostDataError("Correlated reply lacks a stable Host message or turn ID")
            if reply_id in found and found[reply_id] != answer:
                raise HostDataError("Conflicting content for one Host reply ID")
            found[reply_id] = answer
    if len(found) > 1:
        raise HostDataError("Multiple replies claim this request; review the Host result")
    if found:
        reply_id, answer = next(iter(found.items()))
        if diagnostics is not None:
            diagnostics["code"] = "REPLY_MATCHED"
        return {"reply_id": reply_id, "answer": answer}
    if diagnostics is not None:
        diagnostics["code"] = ("REPLY_PENDING" if pending else
                               "RESPONSE_FORMAT_MISMATCH" if malformed else "NO_MATCH")
    return None
