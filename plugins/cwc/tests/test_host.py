"""Host adapter checks use captured shapes and local dictionaries, never Host calls."""

import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "cwc" / "scripts"))
from host import HostDataError, candidates, reply, send_receipt


REQUEST = "req_" + "a" * 32
TARGET = {"host_id": "chatgpt", "kind": "chatgpt", "conversation_id": "chat-1", "title": "Test"}


def snapshot():
    return {
        "schemaVersion": 1,
        "thread": {"id": "chat-1", "kind": "chatgpt", "status": {"type": "idle"}},
        "page": {"hasMore": False, "nextCursor": None},
        "turns": [{"id": "turn-physical-1", "status": "completed", "error": None,
                   "items": [{"type": "agentMessage", "id": "reply-physical-1",
                              "text": "CWC RESPONSE " + REQUEST + "\nThe answer."}]}],
    }


class HostChecks(unittest.TestCase):
    def test_reply_diagnostics_distinguish_unmatched_pending_and_matched(self):
        cases = [
            ("CWC RESPONSE " + REQUEST + "  ", "completed", "REPLY_MATCHED"),
            ("CWC RESPONSE " + REQUEST, "running", "REPLY_PENDING"),
            ("**CWC RESPONSE " + REQUEST + "**", "running", "REPLY_PENDING"),
            ("CWC RESPONSE req_" + "b" * 32, "completed", "NO_MATCH"),
        ]
        for header, status, expected in cases:
            with self.subTest(header=header, status=status):
                data, diagnostics = snapshot(), {"code": "stale"}
                data["turns"][0]["status"] = status
                data["turns"][0]["items"][0]["text"] = header + "\nThe answer."
                result = reply(data, TARGET, REQUEST, diagnostics)
                self.assertEqual(diagnostics["code"], expected)
                self.assertEqual(result is not None, expected == "REPLY_MATCHED")

    def test_header_variants_are_diagnosed_but_never_accepted(self):
        declaration = "CWC RESPONSE " + REQUEST
        for header in (f"**{declaration}**", f"__{declaration}__", f"`{declaration}`",
                       f"CWC RESPONSE: {REQUEST}", f"CWC\tRESPONSE  {REQUEST}", declaration + ":"):
            with self.subTest(header=header):
                data, diagnostics = snapshot(), {}
                data["turns"][0]["items"][0]["text"] = header + "\nThe answer."
                self.assertIsNone(reply(data, TARGET, REQUEST, diagnostics))
                self.assertEqual(diagnostics["code"], "RESPONSE_FORMAT_MISMATCH")
                self.assertIsNone(reply(data, TARGET, REQUEST))
        for text in ("An example follows:\n" + declaration, "> " + declaration,
                     declaration + "x\nThe answer.", declaration + " refers to an earlier request.",
                     "```text\n" + declaration + "\n```", "The request is " + REQUEST):
            with self.subTest(text=text):
                data, diagnostics = snapshot(), {}
                data["turns"][0]["items"][0]["text"] = text
                self.assertIsNone(reply(data, TARGET, REQUEST, diagnostics))
                self.assertEqual(diagnostics["code"], "NO_MATCH")

    def test_diagnostics_preserve_route_source_and_completeness_gates(self):
        for variant in (False, True):
            for level, change, code in (
                    ("data", {"threadId": "other-chat"}, "HOST_ROUTE_MISMATCH"),
                    ("thread", {"kind": "codex"}, "HOST_ROUTE_MISMATCH"),
                    ("data", {"truncated": True}, "HOST_PARTIAL"),
                    ("thread", {"partial": True}, "HOST_PARTIAL"),
                    ("turn", {"truncated": True}, "HOST_PARTIAL"),
                    ("message", {"partial": True}, "HOST_PARTIAL"),
                    ("message", {"role": "user"}, "HOST_DATA_INVALID"),
                    ("turn", {"status": None}, "HOST_DATA_INVALID"),
                    ("message", {"isError": True}, "HOST_REPORTED_ERROR")):
                with self.subTest(variant=variant, level=level, change=change):
                    data, diagnostics = snapshot(), {}
                    turn = data["turns"][0]
                    message = turn["items"][0]
                    if variant:
                        message["text"] = f"**CWC RESPONSE {REQUEST}**\nThe answer."
                    {"data": data, "thread": data["thread"], "turn": turn, "message": message}[level].update(change)
                    with self.assertRaises(HostDataError) as raised:
                        reply(data, TARGET, REQUEST, diagnostics)
                    self.assertEqual(raised.exception.code, code)

    def test_response_header_allows_only_trailing_markdown_spacing(self):
        for spacing in ("  ", "\t"):
            data = snapshot()
            data["turns"][0]["items"][0]["text"] = f"CWC RESPONSE {REQUEST}{spacing}\nThe answer."
            self.assertEqual(reply(data, TARGET, REQUEST)["answer"], "The answer.")
        for header in (f"CWC RESPONSE {REQUEST} extra  ", f"CWC RESPONSE {REQUEST}x  "):
            data = snapshot()
            data["turns"][0]["items"][0]["text"] = header + "\nThe answer."
            self.assertIsNone(reply(data, TARGET, REQUEST))

    def test_candidates_preserve_exact_host_route_and_deduplicate(self):
        chat = {"id": "chat-1", "kind": "chatgpt", "title": "Test"}
        data = {"schemaVersion": 4, "pinnedThreads": [chat], "threads": [chat,
                {"id": "other", "kind": "codex", "title": "Test"},
                {"id": "unknown", "kind": "chat", "title": "Test"}]}
        self.assertEqual(candidates(data), [TARGET])
        chat["hostId"] = "remote-1"
        self.assertEqual(candidates(data)[0]["host_id"], "remote-1")
        chat["threadId"] = "different-id"
        with self.assertRaises(HostDataError):
            candidates(data)

    def test_error_envelope_cannot_be_hidden_by_structured_success(self):
        for failure in ({"isError": True}, {"success": False}, {"accepted": False},
                        {"status": "failed"}, {"success": "false"}, {"accepted": 0}):
            for envelope in ("outer", "block", "data"):
                with self.subTest(failure=failure, envelope=envelope):
                    data = {"accepted": True, "messageId": "remote-id"}
                    if envelope == "data":
                        data.update(failure)
                    block = {"type": "text", "text": json.dumps(data)}
                    if envelope == "block":
                        block.update(failure)
                    raw = {"content": [block]}
                    if envelope == "outer":
                        raw = {**failure, "structuredContent": data}
                    self.assertFalse(send_receipt(raw, TARGET)["accepted"])
                    with self.assertRaises(HostDataError):
                        reply(raw, TARGET, REQUEST)

    def test_send_requires_explicit_acceptance_and_uses_no_request_fallback(self):
        self.assertFalse(send_receipt({"threadId": "chat-1"}, TARGET)["accepted"])
        self.assertFalse(send_receipt({"status": "error"}, TARGET)["accepted"])
        queued = send_receipt({"status": "queued", "threadId": "chat-1"}, TARGET)
        self.assertTrue(queued["accepted"])
        self.assertIsNone(queued["message_id"])
        accepted = send_receipt({"accepted": True, "messageId": "physical-7"}, TARGET)
        self.assertEqual(accepted["message_id"], "physical-7")
        self.assertNotEqual(accepted["message_id"], REQUEST)

    def test_current_read_shape_and_duplicate_snapshot(self):
        data = snapshot()
        expected = {"reply_id": "reply-physical-1", "answer": "The answer."}
        wrapped = {"content": [{"type": "text", "text": json.dumps(data)}]}
        self.assertEqual(reply(wrapped, TARGET, REQUEST), expected)
        self.assertEqual(reply(wrapped, TARGET, REQUEST), expected)
        data["turns"].append(copy.deepcopy(data["turns"][0]))
        self.assertEqual(reply(data, TARGET, REQUEST), expected)
        data["turns"][1]["items"][0]["text"] += " Conflicting answer."
        with self.assertRaises(HostDataError):
            reply(data, TARGET, REQUEST)

    def test_completed_turn_does_not_override_partial_message(self):
        data = snapshot()
        message = data["turns"][0]["items"][0]
        message["complete"] = False
        self.assertIsNone(reply(data, TARGET, REQUEST))
        message["complete"] = True
        data["turns"][0]["status"] = "running"
        self.assertIsNone(reply(data, TARGET, REQUEST))

    def test_correlated_reply_cannot_hide_parent_or_message_failure(self):
        for level in ("turn", "message"):
            for evidence in ({"status": "running"}, {"complete": False},
                             {"status": "failed"}, {"isError": True},
                             {"success": False}, {"complete": "true"}):
                with self.subTest(level=level, evidence=evidence):
                    data = snapshot()
                    turn = data["turns"][0]
                    message = turn["items"][0]
                    message.update({"complete": True, "status": "completed"})
                    (turn if level == "turn" else message).update(evidence)
                    if evidence in ({"status": "running"}, {"complete": False}):
                        self.assertIsNone(reply(data, TARGET, REQUEST))
                    else:
                        with self.assertRaises(HostDataError):
                            reply(data, TARGET, REQUEST)

    def test_truncation_at_each_envelope_level_fails(self):
        for level in ("outer", "block", "turn", "message"):
            with self.subTest(level=level):
                data = snapshot()
                if level == "turn":
                    data["turns"][0]["truncated"] = True
                if level == "message":
                    data["turns"][0]["items"][0]["truncated"] = True
                block = {"type": "text", "text": json.dumps(data), "truncated": level == "block"}
                wrapped = {"content": [block], "truncated": level == "outer"}
                with self.assertRaises(HostDataError):
                    reply(wrapped, TARGET, REQUEST)

    def test_wrong_conversation_or_host_is_rejected(self):
        for change in ({"threadId": "other-chat"}, {"hostId": "other-host"}):
            data = snapshot() | change
            with self.assertRaises(HostDataError):
                reply(data, TARGET, REQUEST)
            self.assertFalse(send_receipt({"accepted": True} | change, TARGET)["accepted"])
        data = snapshot()
        data["thread"]["id"] = "other-chat"
        with self.assertRaises(HostDataError):
            reply(data, TARGET, REQUEST)

    def test_wrong_correlation_plain_text_and_summary_are_not_answers(self):
        data = snapshot()
        self.assertIsNone(reply(data, TARGET, "req_" + "b" * 32))
        for raw in ("CWC RESPONSE " + REQUEST + "\nAnswer", {"summary": "Answer"},
                    {"messages": [{"type": "summary", "text": "Answer"}]}):
            with self.subTest(raw=raw), self.assertRaises(HostDataError):
                reply(raw, TARGET, REQUEST)
        data["turns"][0]["items"][0]["role"] = "user"
        with self.assertRaises(HostDataError):
            reply(data, TARGET, REQUEST)


if __name__ == "__main__":
    unittest.main()
