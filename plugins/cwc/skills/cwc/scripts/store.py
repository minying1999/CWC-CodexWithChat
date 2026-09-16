"""Transactional local CWC pair and exchange records. This module never calls Host."""

from __future__ import annotations

from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from typing import Any
import uuid


class StoreError(ValueError):
    pass


ACTIVE_STATES = ("PREPARED", "UNKNOWN", "WAITING", "READY")
REQUEST_ID = re.compile(r"req_[0-9a-f]{32}\Z")
CONTRACT_HASH = re.compile(r"[0-9a-f]{64}\Z")


def wait_policy(value=None):
    policy = {"initial_delay_ms": 60000, "poll_interval_ms": 30000,
              "wait_budget_ms": 600000, "max_wait_ms": 1800000}
    if value is not None:
        if not isinstance(value, dict) or set(value) - set(policy):
            raise StoreError("unknown wait policy fields")
        policy.update(value)
    if any(type(v) is not int for v in policy.values()):
        raise StoreError("wait policy values must be integer milliseconds")
    if not (0 <= policy["initial_delay_ms"] <= 300000 and
            0 <= policy["poll_interval_ms"] <= 300000 and
            max(1, policy["initial_delay_ms"]) <= policy["wait_budget_ms"] <= policy["max_wait_ms"] <= 1800000):
        raise StoreError("invalid bounded wait policy")
    return policy


def _text(value: Any, name: str, limit: int, *, empty: bool = False) -> str:
    if not isinstance(value, str) or "\x00" in value:
        raise StoreError(f"{name} must be text without NUL")
    try:
        length = len(value.encode("utf-8"))
    except UnicodeError as error:
        raise StoreError(f"{name} must be valid UTF-8 text") from error
    if length > limit or (not empty and not value.strip()):
        raise StoreError(f"{name} must contain 1 to {limit} UTF-8 bytes")
    return value


def _identity(value: Any, name: str) -> str:
    result = _text(value, name, 2048).strip()
    if any(ord(char) < 32 or ord(char) == 127 for char in result):
        raise StoreError(f"{name} must not contain control characters")
    return result


def _scope(value: Any) -> str:
    if not isinstance(value, dict) or set(value) != {"host_id", "thread_id", "workspace"}:
        raise StoreError("scope requires host_id, thread_id and workspace")
    workspace = Path(_text(value["workspace"], "workspace", 32768)).expanduser()
    if not workspace.is_absolute():
        raise StoreError("workspace must be an absolute path")
    normalized = [
        _identity(value["host_id"], "scope host_id"),
        _identity(value["thread_id"], "thread_id"),
        os.path.normcase(str(workspace.resolve())),
    ]
    return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))


def _target(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) - {"host_id", "kind", "conversation_id", "title"}:
        raise StoreError("target requires host_id, kind and conversation_id; title is optional")
    if not {"host_id", "kind", "conversation_id"} <= set(value):
        raise StoreError("target requires host_id, kind and conversation_id")
    kind = _identity(value["kind"], "target kind").casefold()
    if kind != "chatgpt":
        raise StoreError("target kind must be chatgpt")
    return {
        "host_id": _identity(value["host_id"], "target host_id"),
        "kind": kind,
        "conversation_id": _identity(value["conversation_id"], "conversation_id"),
        "title": _text(value.get("title", ""), "title", 4096, empty=True),
    }


def _request_id(value: Any) -> str:
    if not isinstance(value, str) or not REQUEST_ID.fullmatch(value):
        raise StoreError("invalid request_id")
    return value


def _contract(version: Any = "", digest: Any = "") -> tuple[str, str]:
    version = _text(version, "contract version", 128, empty=True).strip()
    digest = _text(digest, "contract hash", 64, empty=True).strip().lower()
    if digest and not CONTRACT_HASH.fullmatch(digest):
        raise StoreError("contract hash must be a SHA-256 hex digest")
    return version, digest


def _suggested_title(scope_key: str, target: dict, task_title: str = "") -> str:
    workspace = json.loads(scope_key)[2]
    project = "".join(char for char in Path(workspace).name
                      if char != "|" and char.isprintable()).strip()[:32] or "项目"
    task = "".join(char for char in task_title
                   if char != "|" and char.isprintable()).strip()[:80] or "未命名任务"
    identity = [scope_key, target["host_id"], target["kind"], target["conversation_id"]]
    tag = hashlib.sha256(json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()[:8].upper()
    return f"CWC|{project}-{task}|{tag}"


def validate_stop(scope: dict, request_id: str, reason: str, confirmed: bool = False) -> tuple[str, str, str]:
    if confirmed is not True:
        raise StoreError("stopping a request requires explicit confirmation")
    return _scope(scope), _request_id(request_id), _text(reason, "stop reason", 4096).strip()


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2, 3, 4):
                raise StoreError(f"unsupported CWC schema version: {version}")
            if version == 0:
                if db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
                    raise StoreError("refusing to adopt an unversioned existing database")
                db.execute("""CREATE TABLE pairs (
                    scope TEXT PRIMARY KEY, target_host TEXT, target_kind TEXT,
                    target_id TEXT, title TEXT NOT NULL, generation INTEGER NOT NULL,
                    task_title TEXT NOT NULL DEFAULT '',
                    contract_version TEXT NOT NULL DEFAULT '',
                    contract_hash TEXT NOT NULL DEFAULT ''
                )""")
            if version in (0, 1):
                if version == 1:
                    db.execute("ALTER TABLE pairs ADD COLUMN task_title TEXT NOT NULL DEFAULT ''")
                    db.execute("ALTER TABLE exchanges RENAME TO exchanges_v1")
                    db.execute("DROP INDEX one_pending_pair")
                    db.execute("DROP INDEX one_pending_target")
                db.execute("""CREATE TABLE exchanges (
                    request_id TEXT PRIMARY KEY, scope TEXT NOT NULL REFERENCES pairs(scope),
                    generation INTEGER NOT NULL, target_host TEXT NOT NULL,
                    target_kind TEXT NOT NULL, target_id TEXT NOT NULL,
                    mode TEXT NOT NULL, format_version INTEGER NOT NULL,
                    payload TEXT NOT NULL, state TEXT NOT NULL CHECK
                        (state IN ('PREPARED','UNKNOWN','WAITING','READY','CONSUMED','CANCELLED',
                                   'ABANDONED','DISCARDED')),
                    receipt TEXT, reply_id TEXT, answer TEXT, answer_hash TEXT,
                    stop_reason TEXT, stopped_at TEXT,
                    contract_version TEXT NOT NULL DEFAULT '',
                    contract_hash TEXT NOT NULL DEFAULT ''
                )""")
                if version == 1:
                    columns = ("request_id,scope,generation,target_host,target_kind,target_id,"
                               "mode,format_version,payload,state,receipt,reply_id,answer,answer_hash")
                    db.execute(f"INSERT INTO exchanges(rowid,{columns}) SELECT rowid,{columns} FROM exchanges_v1")
                    db.execute("DROP TABLE exchanges_v1")
                    db.execute("ALTER TABLE pairs ADD COLUMN contract_version TEXT NOT NULL DEFAULT ''")
                    db.execute("ALTER TABLE pairs ADD COLUMN contract_hash TEXT NOT NULL DEFAULT ''")
                db.execute("""CREATE UNIQUE INDEX one_pending_pair ON exchanges(scope)
                    WHERE state IN ('PREPARED','UNKNOWN','WAITING','READY')""")
                db.execute("""CREATE UNIQUE INDEX one_pending_target
                    ON exchanges(target_host,target_kind,target_id)
                    WHERE state IN ('PREPARED','UNKNOWN','WAITING','READY','ABANDONED')""")
                db.execute("PRAGMA user_version=3")
            if version == 2:
                db.execute("ALTER TABLE pairs ADD COLUMN contract_version TEXT NOT NULL DEFAULT ''")
                db.execute("ALTER TABLE pairs ADD COLUMN contract_hash TEXT NOT NULL DEFAULT ''")
                db.execute("ALTER TABLE exchanges ADD COLUMN contract_version TEXT NOT NULL DEFAULT ''")
                db.execute("ALTER TABLE exchanges ADD COLUMN contract_hash TEXT NOT NULL DEFAULT ''")
                db.execute("PRAGMA user_version=3")
            if version < 4:
                db.execute("ALTER TABLE exchanges ADD COLUMN display_id TEXT")
                db.execute("ALTER TABLE exchanges ADD COLUMN parent_request_id TEXT")
                db.execute("ALTER TABLE exchanges ADD COLUMN timing TEXT NOT NULL DEFAULT '{}'")
                db.execute("ALTER TABLE exchanges ADD COLUMN resource_hashes TEXT NOT NULL DEFAULT '{}'")
                db.execute("ALTER TABLE exchanges ADD COLUMN body_hash TEXT NOT NULL DEFAULT ''")
                db.execute("CREATE UNIQUE INDEX exchange_display_id ON exchanges(display_id)")
                for old in db.execute("SELECT request_id FROM exchanges ORDER BY rowid").fetchall():
                    db.execute("UPDATE exchanges SET display_id=? WHERE request_id=?",
                               (self._allocate_display(db, old[0]), old[0]))
                db.execute("PRAGMA user_version=4")

    @staticmethod
    def _allocate_display(db, request_id):
        # Extend on collisions; previously assigned short identifiers never change.
        for length in range(8, 33, 2):
            label = request_id[4:4 + length].upper()
            if not db.execute("SELECT 1 FROM exchanges WHERE display_id=?", (label,)).fetchone():
                return label
        raise StoreError("request identity already exists")

    @contextmanager
    def _transaction(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _pending(db: sqlite3.Connection, scope: str):
        return db.execute("""SELECT request_id,state,mode FROM exchanges WHERE scope=?
            AND state IN ('PREPARED','UNKNOWN','WAITING','READY')""", (scope,)).fetchone()

    @staticmethod
    def _pair(db: sqlite3.Connection, scope: str):
        record = db.execute("SELECT * FROM pairs WHERE scope=?", (scope,)).fetchone()
        if not record or record["target_id"] is None:
            raise StoreError("no selected CWC target")
        return record

    @staticmethod
    def _exchange(db: sqlite3.Connection, scope: str, request_id: str):
        record = db.execute("SELECT * FROM exchanges WHERE request_id=? AND scope=?",
                            (_request_id(request_id), scope)).fetchone()
        if not record:
            raise StoreError("exchange does not belong to this scope")
        return record

    @staticmethod
    def _current(db: sqlite3.Connection, record: sqlite3.Row) -> None:
        pair = Store._pair(db, record["scope"])
        if any(pair[key] != record[key] for key in
               ("generation", "target_host", "target_kind", "target_id")):
            raise StoreError("exchange belongs to an old target or pair generation")

    @staticmethod
    def _summary(record: sqlite3.Row) -> dict[str, Any]:
        return {key: record[key] for key in ("request_id", "state", "mode")}

    def display(self, scope, request_id):
        with self._transaction() as db:
            record = self._exchange(db, _scope(scope), request_id)
            label = record["display_id"]
            parent = record["parent_request_id"]
            parent_label = self._exchange(db, record["scope"], parent)["display_id"] if parent else None
        return {"question_id": "Q-" + label, "answer_id": "A-" + label,
                "parent_request_id": parent, "parent_answer_id": "A-" + parent_label if parent_label else None}

    def pair(self, scope: dict, target: dict, task_title: str | None = None,
             contract_version: str = "", contract_hash: str = "") -> dict[str, Any]:
        key, target = _scope(scope), _target(target)
        contract_version, contract_hash = _contract(contract_version, contract_hash)
        if task_title is not None:
            task_title = _text(task_title, "task title", 4096, empty=True)
        identity = (target["host_id"], target["kind"], target["conversation_id"])
        with self._transaction() as db:
            current = db.execute("SELECT * FROM pairs WHERE scope=?", (key,)).fetchone()
            same = current and tuple(current[name] for name in
                                     ("target_host", "target_kind", "target_id")) == identity
            if current and not same and self._pending(db, key):
                raise StoreError("finish the pending exchange before changing target")
            if task_title is None:
                task_title = current["task_title"] if current else ""
            generation = current["generation"] + (not same) if current else 1
            db.execute("""INSERT INTO pairs
                (scope,target_host,target_kind,target_id,title,generation,task_title,contract_version,contract_hash)
                VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(scope) DO UPDATE SET
                target_host=excluded.target_host,target_kind=excluded.target_kind,
                target_id=excluded.target_id,title=excluded.title,generation=excluded.generation,
                task_title=excluded.task_title,contract_version=excluded.contract_version,
                contract_hash=excluded.contract_hash""",
                        (key, *identity, target["title"], generation, task_title,
                         contract_version, contract_hash))
        return {"status": "SELECTED", "title": target["title"], "generation": generation,
                "task_title": task_title, "contract_version": contract_version,
                "contract_hash": contract_hash,
                "suggested_title": _suggested_title(key, target, task_title)}

    def status(self, scope: dict) -> dict[str, Any]:
        key = _scope(scope)
        with self._transaction() as db:
            return self._status_snapshot(db, key)

    @staticmethod
    def _status_snapshot(db: sqlite3.Connection, key: str, details: bool = False,
                         task_title: str | None = None) -> dict[str, Any]:
        pair = db.execute("SELECT * FROM pairs WHERE scope=?", (key,)).fetchone()
        latest = db.execute("SELECT request_id,state FROM exchanges WHERE scope=? ORDER BY rowid DESC LIMIT 1",
                            (key,)).fetchone()
        last_exchange = dict(latest) if latest else None
        if not pair or pair["target_id"] is None:
            result = {"status": "UNPAIRED", "last_exchange": last_exchange}
        else:
            pending = Store._pending(db, key)
            if task_title is None:
                task_title = pair["task_title"] if "task_title" in pair.keys() else ""
            target = {"host_id": pair["target_host"], "kind": pair["target_kind"],
                      "conversation_id": pair["target_id"]}
            result = {"status": "SELECTED", "title": pair["title"],
                      "generation": pair["generation"],
                      "task_title": task_title, "suggested_title": _suggested_title(key, target, task_title),
                      "exchange": Store._summary(pending) if pending else None,
                      "last_exchange": last_exchange}
            if pending and db.execute("PRAGMA user_version").fetchone()[0] == 4:
                details_row = db.execute("SELECT display_id,timing FROM exchanges WHERE request_id=?", (pending["request_id"],)).fetchone()
                timing = json.loads(details_row["timing"])
                result["exchange"].update(question_id="Q-" + details_row["display_id"],
                    answer_id="A-" + details_row["display_id"],
                    wait_expired=("deadline" in timing and max(int(time.time() * 1000), timing.get("last_clock_at", 0)) > timing["deadline"])
                                 or timing.get("read_count", 0) >= 120)
        if details:
            target = ({"host_id": pair["target_host"], "kind": pair["target_kind"],
                       "conversation_id": pair["target_id"]} if pair and pair["target_id"] else None)
            stopped, stopped_count = [], 0
            if db.execute("PRAGMA user_version").fetchone()[0] in (2, 3, 4):
                stopped = [dict(row) for row in db.execute("""SELECT request_id,state,mode,stop_reason,stopped_at
                    FROM exchanges WHERE scope=? AND state='ABANDONED' ORDER BY rowid DESC LIMIT 10""", (key,))]
                stopped_count = db.execute("SELECT COUNT(*) FROM exchanges WHERE scope=? AND state='ABANDONED'",
                                           (key,)).fetchone()[0]
            reserved = target and db.execute("""SELECT 1 FROM exchanges
                WHERE target_host=? AND target_kind=? AND target_id=? AND (state='ABANDONED'
                OR (scope!=? AND state IN ('PREPARED','UNKNOWN','WAITING','READY'))) LIMIT 1""",
                (target["host_id"], target["kind"], target["conversation_id"], key)).fetchone()
            result.update(target=target, stopped_exchanges=stopped, stopped_count=stopped_count,
                          target_reserved=bool(reserved))
        return result

    @staticmethod
    def read_status(path: str | Path, scope: dict, details: bool = False,
                    task_title: str | None = None) -> dict[str, Any]:
        key, path = _scope(scope), Path(path).expanduser().resolve()
        if task_title is not None:
            task_title = _text(task_title, "task title", 4096, empty=True)
        if not path.exists():
            result = {"status": "UNPAIRED", "last_exchange": None}
            if details:
                result.update(target=None, stopped_exchanges=[], stopped_count=0, target_reserved=False)
            return result
        # Even a read-only SQLite WAL connection may create sidecars; do not open it here.
        with path.open("rb") as file:
            if file.read(20)[18:20] != b"\x01\x01":
                raise StoreError("refresh requires the default rollback journal; WAL/unknown format is left untouched")
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN")
            if db.execute("PRAGMA user_version").fetchone()[0] not in (1, 2, 3, 4):
                raise StoreError("unsupported CWC schema version; refresh performs no migration")
            return Store._status_snapshot(db, key, details, task_title)

    def disconnect(self, scope: dict) -> dict[str, Any]:
        key = _scope(scope)
        with self._transaction() as db:
            if self._pending(db, key):
                raise StoreError("finish the pending exchange before disconnecting")
            db.execute("""UPDATE pairs SET target_host=NULL,target_kind=NULL,target_id=NULL,
                title='',generation=generation+1 WHERE scope=? AND target_id IS NOT NULL""", (key,))
        return {"status": "UNPAIRED"}

    def prepare(self, scope: dict, body: str, mode: str = "consult",
                task_title: str | None = None, contract_version: str = "",
                contract_hash: str = "", resource_hashes: dict | None = None,
                contract_text: str = "", parent_request_id=None, waiting=None,
                reasoner_text: str | None = None, idempotency_key=None) -> dict[str, Any]:
        key = _scope(scope)
        body = _text(body, "body", 128 * 1024)
        contract_version, contract_hash = _contract(contract_version, contract_hash)
        resource_hashes = resource_hashes or {}
        policy = wait_policy(waiting)
        contract_text = _text(contract_text, "contract text", 32 * 1024, empty=True)
        if contract_hash and hashlib.sha256(contract_text.encode("utf-8")).hexdigest() != contract_hash:
            raise StoreError("contract text and hash disagree")
        if not isinstance(resource_hashes, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                                        or not CONTRACT_HASH.fullmatch(v) for k, v in resource_hashes.items()):
            raise StoreError("resource hashes must be SHA-256 hex digests")
        if task_title is not None:
            task_title = _text(task_title, "task title", 4096, empty=True)
        if not isinstance(mode, str) or mode not in ("consult", "review"):
            raise StoreError("mode must be consult or review")
        rules_path = Path(__file__).resolve().parent.parent / "REASONER.md"
        rules = _text(reasoner_text if reasoner_text is not None else rules_path.read_text(encoding="utf-8"),
                      "Reasoner rules", 32 * 1024)
        if idempotency_key is not None and (not isinstance(idempotency_key, str) or not re.fullmatch(r"[0-9a-f]{32}", idempotency_key)):
            raise StoreError("invalid menu idempotency key")
        request_id = "req_" + (uuid.uuid5(uuid.NAMESPACE_URL, key + ":verification:" + idempotency_key).hex
                               if idempotency_key is not None else uuid.uuid4().hex)
        with self._transaction() as db:
            pair = self._pair(db, key)
            previous = db.execute("SELECT * FROM exchanges WHERE request_id=?", (request_id,)).fetchone()
            if previous:
                self._current(db, previous)
                if (previous["scope"] != key or previous["mode"] != mode or previous["parent_request_id"] != parent_request_id
                        or previous["body_hash"] != hashlib.sha256(body.encode("utf-8")).hexdigest()):
                    raise StoreError("menu request conflicts with its frozen question")
                return {"status": previous["state"], **self._summary(previous)}
            parent_label = None
            if parent_request_id is not None:
                parent = self._exchange(db, key, parent_request_id)
                if parent["state"] != "CONSUMED":
                    raise StoreError("follow-up requires a consumed parent reply")
                self._current(db, parent)
                parent_label = parent["display_id"]
                if parent["body_hash"] == hashlib.sha256(body.encode("utf-8")).hexdigest():
                    raise StoreError("follow-up repeats the same question without new evidence")
            display_id = self._allocate_display(db, request_id)
            if task_title is None:
                task_title = pair["task_title"]
            else:
                db.execute("UPDATE pairs SET task_title=? WHERE scope=?", (task_title, key))
            target = {"host_id": pair["target_host"], "kind": pair["target_kind"],
                      "conversation_id": pair["target_id"]}
            contract_line = (f"CWC CONTRACT v{contract_version} sha256={contract_hash}\n"
                             if contract_hash else "CWC CONTRACT legacy\n")
            resource_line = "CWC RESOURCES " + " ".join(
                f"{name}={resource_hashes[name]}" for name in sorted(resource_hashes)) + "\n"
            payload = (f"CWC REQUEST {request_id}\n建议对话名称：{_suggested_title(key, target, task_title)}\n"
                       f"交互编号：Q-{display_id} / A-{display_id}\n"
                       + (f"承接回复：A-{parent_label}\n" if parent_label else "") +
                       f"{contract_line}{resource_line}"
                       f"{contract_text}\n"
                       f"{rules.strip()}\n\nMode: {mode}\n\n{body}\n\n"
                       f"Reply with this first line, followed by your answer:\nCWC RESPONSE {request_id}")
            if self._pending(db, key):
                raise StoreError("this pair already has a pending exchange")
            try:
                db.execute("""INSERT INTO exchanges
                    (request_id,scope,generation,target_host,target_kind,target_id,
                     mode,format_version,payload,state,contract_version,contract_hash,
                     display_id,parent_request_id,timing,resource_hashes,body_hash)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                           (request_id, key, pair["generation"], pair["target_host"],
                            pair["target_kind"], pair["target_id"], mode, 2, payload,
                            "PREPARED", contract_version, contract_hash, display_id,
                            parent_request_id, json.dumps(policy), json.dumps(resource_hashes),
                            hashlib.sha256(body.encode("utf-8")).hexdigest()))
            except sqlite3.IntegrityError as error:
                raise StoreError("this target already has a pending exchange") from error
        return {"status": "PREPARED", "request_id": request_id, "state": "PREPARED", "mode": mode,
                **self.display(scope, request_id)}

    def get_exchange(self, scope: dict, request_id: str) -> dict[str, Any]:
        with self._transaction() as db:
            record = dict(self._exchange(db, _scope(scope), request_id))
        record["target"] = {"host_id": record["target_host"], "kind": record["target_kind"],
                            "conversation_id": record["target_id"]}
        record["receipt"] = json.loads(record["receipt"]) if record["receipt"] else None
        return record

    def verify_contract(self, scope: dict, request_id: str,
                        contract_version: str, contract_hash: str, resource_hashes=None) -> None:
        contract_version, contract_hash = _contract(contract_version, contract_hash)
        with self._transaction() as db:
            record = self._exchange(db, _scope(scope), request_id)
            if record["state"] != "PREPARED":
                return
            saved_hash = record["contract_hash"] if "contract_hash" in record.keys() else ""
            saved_version = record["contract_version"] if "contract_version" in record.keys() else ""
            if saved_hash and (saved_hash != contract_hash or saved_version != contract_version):
                raise StoreError("frozen CWC contract differs from the installed contract")
            frozen = json.loads(record["resource_hashes"])
            if frozen and frozen != resource_hashes:
                raise StoreError("frozen CWC resources differ from the installed resources")

    def claim(self, scope: dict, request_id: str) -> dict[str, Any]:
        with self._transaction() as db:
            record = self._exchange(db, _scope(scope), request_id)
            if record["state"] in ("ABANDONED", "DISCARDED"):
                return {"status": record["state"], **self._summary(record)}
            if record["state"] == "CANCELLED":
                return {"status": "ALREADY_CLAIMED", **self._summary(record)}
            self._current(db, record)
            if record["state"] != "PREPARED":
                return {"status": "ALREADY_CLAIMED", **self._summary(record)}
            now = int(time.time() * 1000)
            timing = wait_policy(json.loads(record["timing"]))
            timing.update(claimed_at=now, next_read_at=now + timing["initial_delay_ms"],
                          deadline=now + timing["wait_budget_ms"],
                          max_deadline=now + timing["max_wait_ms"], read_count=0)
            db.execute("UPDATE exchanges SET state='UNKNOWN',timing=? WHERE request_id=?",
                       (json.dumps(timing), request_id))
            result = {"status": "SEND_ONCE", "request_id": request_id, "state": "UNKNOWN",
                      "target": {"host_id": record["target_host"], "kind": record["target_kind"],
                                 "conversation_id": record["target_id"]}, "payload": record["payload"]}
        return result

    def schedule_read(self, scope, request_id, reserve=False, allow_late=False):
        with self._transaction() as db:
            record = self._exchange(db, _scope(scope), request_id)
            if record["state"] not in ("UNKNOWN", "WAITING", "ABANDONED"):
                return {"status": record["state"], "request_id": request_id}
            now = int(time.time() * 1000)
            timing = json.loads(record["timing"])
            if "claimed_at" not in timing:
                # Old records have no send timestamp: anchor once at first resumed read.
                timing = wait_policy()
                timing.update(claimed_at=now, next_read_at=now, deadline=now + timing["wait_budget_ms"],
                              max_deadline=now + timing["max_wait_ms"], read_count=0, legacy_anchor=True)
                db.execute("UPDATE exchanges SET timing=? WHERE request_id=?", (json.dumps(timing), request_id))
            now = max(now, timing.get("last_clock_at", 0), timing["claimed_at"])
            timing["last_clock_at"] = now
            db.execute("UPDATE exchanges SET timing=? WHERE request_id=?", (json.dumps(timing), request_id))
            expired = now > timing["deadline"] or timing["read_count"] >= 120
            if expired and (not allow_late or now < timing["next_read_at"]):
                return {"status": "WAIT_EXPIRED", "state": record["state"], "request_id": request_id,
                        "elapsed_ms": max(0, now - timing["claimed_at"]), "project_complete": False,
                        "notice": "Observation budget ended; preserve the request and check remaining work."}
            delay = max(0, timing["next_read_at"] - now)
            if reserve and delay == 0:
                timing["read_count"] += 1
                timing["next_read_at"] = now + (max(30000, timing["poll_interval_ms"]) if expired else timing["poll_interval_ms"])
                db.execute("UPDATE exchanges SET timing=? WHERE request_id=?", (json.dumps(timing), request_id))
            return {"status": "READ_PENDING", "request_id": request_id, "wait_ms": delay,
                    "late_probe": expired,
                    "elapsed_ms": max(0, now - timing["claimed_at"]),
                    "remaining_ms": max(0, timing["deadline"] - now), "read_count": timing["read_count"]}

    def observe(self, scope, request_id, reason):
        with self._transaction() as db:
            record = self._exchange(db, _scope(scope), request_id)
            timing = json.loads(record["timing"])
            if "claimed_at" not in timing or record["state"] not in ("UNKNOWN", "WAITING", "ABANDONED"):
                return
            now = max(int(time.time() * 1000), timing.get("last_clock_at", 0), timing["claimed_at"])
            timing["last_clock_at"] = now
            timing["last_reason"] = reason
            timing["last_observed_at"] = now
            if reason == "REPLY_PENDING" and now >= timing["deadline"] - timing["poll_interval_ms"]:
                timing["deadline"] = min(timing["max_deadline"], timing["deadline"] + 300000)
            db.execute("UPDATE exchanges SET timing=? WHERE request_id=?", (json.dumps(timing), request_id))

    def record_send(self, scope: dict, request_id: str, receipt: dict) -> dict[str, Any]:
        allowed = {"accepted", "message_id", "turn_id", "error"}
        if not isinstance(receipt, dict) or set(receipt) - allowed or type(receipt.get("accepted")) is not bool:
            raise StoreError("receipt requires accepted bool and optional message_id, turn_id, error")
        receipt = {"accepted": receipt["accepted"], **{key: receipt.get(key) for key in allowed - {"accepted"}}}
        for name in ("message_id", "turn_id", "error"):
            if receipt[name] is not None:
                receipt[name] = (_identity(receipt[name], name) if name != "error"
                                 else _text(receipt[name], "error", 8192, empty=True))
        accepted = receipt["accepted"]
        with self._transaction() as db:
            record = self._exchange(db, _scope(scope), request_id)
            if record["state"] in ("READY", "CONSUMED", "ABANDONED", "DISCARDED", "CANCELLED"):
                return {"status": record["state"], **self._summary(record)}
            self._current(db, record)
            if record["state"] not in ("UNKNOWN", "WAITING"):
                raise StoreError("send receipt requires a claimed request")
            previous = json.loads(record["receipt"]) if record["receipt"] else None
            if previous and previous["accepted"]:
                if any(previous[name] and receipt[name] and previous[name] != receipt[name]
                       for name in ("message_id", "turn_id")):
                    raise StoreError("conflicting Host receipt for this request")
                return {"status": record["state"], **self._summary(record)}
            state = "WAITING" if accepted else "UNKNOWN"
            timing = json.loads(record["timing"])
            if "claimed_at" in timing and "send_returned_at" not in timing:
                timing["send_returned_at"] = int(time.time() * 1000)
                timing["next_read_at"] = max(timing["next_read_at"], timing["send_returned_at"] + timing["initial_delay_ms"])
            db.execute("UPDATE exchanges SET state=?,receipt=?,timing=? WHERE request_id=?",
                       (state, json.dumps(receipt, ensure_ascii=False), json.dumps(timing), request_id))
        return {"status": state, "state": state, "request_id": request_id}

    def receive(self, scope: dict, request_id: str, reply_id: str, answer: str) -> dict[str, Any]:
        reply_id = _identity(reply_id, "reply_id")
        answer = _text(answer, "answer", 256 * 1024)
        digest = hashlib.sha256(answer.encode("utf-8")).hexdigest()
        with self._transaction() as db:
            record = self._exchange(db, _scope(scope), request_id)
            if record["state"] == "DISCARDED":
                if record["answer_hash"] != digest:
                    raise StoreError("conflicting reply content for this request")
                return {"status": "DISCARDED", **self._summary(record)}
            if record["state"] == "ABANDONED":
                db.execute("""UPDATE exchanges SET state='DISCARDED',reply_id=?,answer=?,answer_hash=?
                    WHERE request_id=?""", (reply_id, answer, digest, request_id))
                return {"status": "DISCARDED", "request_id": request_id, "state": "DISCARDED"}
            self._current(db, record)
            if record["state"] in ("READY", "CONSUMED"):
                if record["answer_hash"] != digest:
                    raise StoreError("conflicting reply content for this request")
                return {"status": "DUPLICATE", **self._summary(record)}
            if record["state"] not in ("UNKNOWN", "WAITING"):
                raise StoreError("reply requires a claimed pending request")
            db.execute("""UPDATE exchanges SET state='READY',reply_id=?,answer=?,answer_hash=?
                WHERE request_id=?""", (reply_id, answer, digest, request_id))
        return {"status": "READY", "request_id": request_id, "state": "READY"}

    def consume(self, scope: dict, request_id: str) -> dict[str, Any]:
        with self._transaction() as db:
            record = self._exchange(db, _scope(scope), request_id)
            if record["state"] == "CONSUMED":
                return {"status": "ALREADY_CONSUMED", **self._summary(record)}
            if record["state"] in ("ABANDONED", "DISCARDED"):
                return {"status": record["state"], **self._summary(record)}
            self._current(db, record)
            if record["state"] != "READY":
                raise StoreError("only a READY reply can be consumed")
            db.execute("UPDATE exchanges SET state='CONSUMED' WHERE request_id=?", (request_id,))
            result = {"status": "CONSUMED", "request_id": request_id, "state": "CONSUMED",
                      "reply_id": record["reply_id"], "answer": record["answer"]}
        return result

    def abandon(self, scope: dict, request_id: str, reason: str, confirmed: bool = False) -> dict[str, Any]:
        key, request_id, reason = validate_stop(scope, request_id, reason, confirmed)
        with self._transaction() as db:
            record = self._exchange(db, key, request_id)
            if record["state"] == "CONSUMED":
                return {"status": "ALREADY_CONSUMED", **self._summary(record)}
            if record["state"] not in ("ABANDONED", "DISCARDED", "CANCELLED"):
                self._current(db, record)
                state = {"PREPARED": "CANCELLED", "UNKNOWN": "ABANDONED",
                         "WAITING": "ABANDONED", "READY": "DISCARDED"}[record["state"]]
                db.execute("""UPDATE exchanges SET state=?,stop_reason=?,
                    stopped_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE request_id=?""",
                    (state, reason, request_id))
                record = self._exchange(db, record["scope"], request_id)
            return {"status": record["state"], **self._summary(record),
                    "stop_reason": record["stop_reason"], "stopped_at": record["stopped_at"]}

    def cancel(self, scope: dict, request_id: str) -> dict[str, Any]:
        with self._transaction() as db:
            record = self._exchange(db, _scope(scope), request_id)
            if record["state"] == "CANCELLED":
                return {"status": "CANCELLED", **self._summary(record)}
            self._current(db, record)
            if record["state"] != "PREPARED":
                raise StoreError("only a request never claimed for sending can be cancelled")
            db.execute("UPDATE exchanges SET state='CANCELLED' WHERE request_id=?", (request_id,))
        return {"status": "CANCELLED", "request_id": request_id, "state": "CANCELLED"}
