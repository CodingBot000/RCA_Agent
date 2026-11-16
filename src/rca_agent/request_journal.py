"""Durable metadata-only request journal. No prompts, credentials, or automatic recovery sends."""

import fcntl
import hashlib
import json
import os
import sqlite3
import stat
import time
from contextlib import contextmanager
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path


class JournalBlocked(ValueError):
    pass


def readonly_summary(path):
    """Inspect a trusted journal without creating it or converting recovery states."""
    path = Path(path)
    directory = os.lstat(path.parent)
    if (
        not stat.S_ISDIR(directory.st_mode)
        or directory.st_uid != os.getuid()
        or stat.S_IMODE(directory.st_mode) != 0o700
    ):
        raise JournalBlocked("unsafe journal directory")
    if not os.path.lexists(path):
        return {"completed": 0, "manual_review": 0, "sent": 0}
    info = os.lstat(path)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
    ):
        raise JournalBlocked("unsafe journal file")
    db = sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=1)
    try:
        counts = dict(db.execute("SELECT state,COUNT(*) FROM requests GROUP BY state"))
        if set(counts) - {"COMPLETED", "MANUAL_REVIEW", "SENT"}:
            raise JournalBlocked("unknown journal state")
        return {
            "completed": counts.get("COMPLETED", 0),
            "manual_review": counts.get("MANUAL_REVIEW", 0),
            "sent": counts.get("SENT", 0),
        }
    finally:
        db.close()


def fingerprint(prepared):
    return hashlib.sha256(
        json.dumps(asdict(prepared), sort_keys=True, allow_nan=False).encode()
    ).hexdigest()


class RequestJournal:
    def __init__(self, path):
        self.path = Path(path)
        directory = self.path.parent
        mode = os.lstat(directory)
        if (
            not stat.S_ISDIR(mode.st_mode)
            or stat.S_IMODE(mode.st_mode) != 0o700
            or mode.st_uid != os.getuid()
        ):
            raise JournalBlocked("unsafe journal directory")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        try:
            fd = os.open(self.path, flags, 0o600)
            os.close(fd)
        except FileExistsError:
            pass
        metadata = os.lstat(self.path)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise JournalBlocked("unsafe journal file")
        lock_path = self.path.with_suffix(self.path.suffix + ".owner")
        self.fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        lock_info = os.fstat(self.fd)
        if (
            not stat.S_ISREG(lock_info.st_mode)
            or lock_info.st_uid != os.getuid()
            or lock_info.st_nlink != 1
            or stat.S_IMODE(lock_info.st_mode) != 0o600
        ):
            os.close(self.fd)
            raise JournalBlocked("unsafe journal owner lock")
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.fd)
            raise JournalBlocked("journal already owned") from None
        try:
            with self.connection() as db:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS requests (digest TEXT PRIMARY KEY, request_id TEXT UNIQUE, state TEXT, input_count INTEGER, max_output INTEGER, usage TEXT, created REAL, updated REAL)"
                )
                columns = {row[1] for row in db.execute("PRAGMA table_info(requests)")}
                for name in ("model", "response_id"):
                    if name not in columns:
                        db.execute("ALTER TABLE requests ADD COLUMN " + name + " TEXT")
                # A durable SENT is never assumed to have failed just because the process disappeared.
                db.execute(
                    "UPDATE requests SET state='MANUAL_REVIEW',updated=? WHERE state='SENT'",
                    (time.time(),),
                )

        except BaseException:
            self.close()
            raise

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=5, isolation_level="IMMEDIATE")
        try:
            db.execute("PRAGMA synchronous=EXTRA")
            db.execute("PRAGMA journal_mode=DELETE")
            with db:
                yield db
        finally:
            db.close()

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def sent(self, prepared, request_id, input_count):
        if (
            not isinstance(request_id, str)
            or len(request_id) != 32
            or any(c not in "0123456789abcdef" for c in request_id)
            or type(input_count) is not int
            or input_count < 0
        ):
            raise JournalBlocked("invalid request metadata")
        digest = fingerprint(prepared)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT 1 FROM requests WHERE digest=? OR request_id=?", (digest, request_id)
            ).fetchone():
                raise JournalBlocked("request already recorded; manual reconciliation required")
            now = time.time()
            db.execute(
                "INSERT INTO requests(digest,request_id,state,input_count,max_output,usage,created,updated,model) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    digest,
                    request_id,
                    "SENT",
                    input_count,
                    prepared.max_output_tokens,
                    None,
                    now,
                    now,
                    prepared.model,
                ),
            )
        # sqlite FULL commit precedes returning to the network caller.
        return digest

    def completed(self, digest, usage, response_id=None):
        if set(usage) != {"input_tokens", "output_tokens", "cost_usd"} or any(
            type(usage[k]) is not int or usage[k] < 0 for k in ("input_tokens", "output_tokens")
        ):
            raise JournalBlocked("invalid usage")
        cost = Decimal(usage["cost_usd"])
        if not cost.is_finite() or cost < 0:
            raise JournalBlocked("invalid cost")
        if response_id is not None and (
            not isinstance(response_id, str)
            or not response_id.startswith("resp_")
            or len(response_id) > 160
            or any(
                c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
                for c in response_id
            )
        ):
            raise JournalBlocked("invalid provider response ID")
        usage = {
            **usage,
            "cost_usd": str(cost),
            "cost_basis": "token_pricing_estimate",
            "billing_status": "UNRECONCILED",
        }
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT state,input_count,max_output FROM requests WHERE digest=?", (digest,)
            ).fetchone()
            if row is None or row[0] != "SENT":
                raise JournalBlocked("invalid settlement state")
            state = (
                "COMPLETED"
                if usage["input_tokens"] <= row[1] and usage["output_tokens"] <= row[2]
                else "MANUAL_REVIEW"
            )
            db.execute(
                "UPDATE requests SET state=?,usage=?,updated=?,response_id=? WHERE digest=?",
                (state, json.dumps(usage), time.time(), response_id, digest),
            )

    def uncertain(self, digest):
        with self.connection() as db:
            db.execute(
                "UPDATE requests SET state='MANUAL_REVIEW',updated=? WHERE digest=? AND state='SENT'",
                (time.time(), digest),
            )

    def summary(self):
        with self.connection() as db:
            counts = dict(db.execute("SELECT state,COUNT(*) FROM requests GROUP BY state"))
        return {
            "completed": counts.get("COMPLETED", 0),
            "manual_review": counts.get("MANUAL_REVIEW", 0),
            "sent": counts.get("SENT", 0),
        }
