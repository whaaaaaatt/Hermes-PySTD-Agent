"""SQLite state store (stdlib ``sqlite3`` only).

Single connection-per-thread pattern, WAL mode when supported (with
graceful fallback for filesystems that refuse WAL — same problem the
upstream hermes_state.py dealt with).

Tables:
  - ``sessions``        — one row per conversation
  - ``messages``        — chat turns; tool calls stored as JSON
  - ``memory``          — agent key/value memory
  - ``usage``           — token usage ledger (one row per turn)
  - ``skills_state``    — disabled skills (the active set comes from disk)

We deliberately do NOT use FTS5 — the standard library's sqlite3 module
ships with FTS5 in modern Python builds, but enabling it varies by
distribution, and ``LIKE '%query%'`` is good enough for our scale.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

from .paths import get_state_db_path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA_VERSION = 3

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    id            TEXT PRIMARY KEY,
    title         TEXT NOT NULL DEFAULT '',
    model         TEXT NOT NULL DEFAULT '',
    provider      TEXT NOT NULL DEFAULT '',
    parent_id     TEXT,
    source        TEXT NOT NULL DEFAULT 'cli',
    system_prompt TEXT NOT NULL DEFAULT '',
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT NOT NULL,
    role          TEXT NOT NULL,        -- system|user|assistant|tool
    content       TEXT NOT NULL,        -- may be empty string
    tool_calls    TEXT,                 -- JSON list, assistant messages only
    tool_call_id  TEXT,                 -- tool messages: parent tool call id
    name          TEXT,                 -- tool name (for tool role)
    reasoning_content TEXT,             -- model thinking/reasoning content
    created_at    REAL NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);

CREATE TABLE IF NOT EXISTS memory (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    key           TEXT NOT NULL UNIQUE,
    value         TEXT NOT NULL,
    tags          TEXT NOT NULL DEFAULT '',
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memory_tags ON memory(tags);

CREATE TABLE IF NOT EXISTS usage (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT NOT NULL,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens      INTEGER NOT NULL DEFAULT 0,
    model         TEXT NOT NULL DEFAULT '',
    created_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_session ON usage(session_id, created_at);

CREATE TABLE IF NOT EXISTS skills_state (
    name          TEXT PRIMARY KEY,
    enabled       INTEGER NOT NULL DEFAULT 1,
    updated_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_meta (
    key           TEXT PRIMARY KEY,
    value         TEXT NOT NULL
);
"""


# ---------------------------------------------------------------------------
# WAL compatibility — same trick as hermes_state.py
# ---------------------------------------------------------------------------

_WAL_INCOMPAT_MARKERS = (
    "locking protocol",     # NFS / SMB
    "not authorized",       # some FUSE mounts
)


# ---------------------------------------------------------------------------
# Dataclasses (public API)
# ---------------------------------------------------------------------------

@dataclass
class Session:
    id: str
    title: str
    model: str
    provider: str
    parent_id: Optional[str] = None
    source: str = "cli"
    system_prompt: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


@dataclass
class Message:
    role: str
    content: str
    tool_calls: Optional[List[Dict[str, Any]]] = None
    tool_call_id: Optional[str] = None
    name: Optional[str] = None
    reasoning_content: Optional[str] = None
    id: Optional[int] = None
    session_id: Optional[str] = None
    created_at: float = field(default_factory=time.time)


@dataclass
class UsageRecord:
    session_id: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    model: str = ""
    created_at: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# StateStore
# ---------------------------------------------------------------------------

class StateStore:
    """Thread-safe SQLite wrapper.

    Each thread gets its own connection. Connections enable foreign keys,
    set ``row_factory=sqlite3.Row``, and use WAL mode when the filesystem
    supports it.

    Transaction model: ``isolation_level=None`` (autocommit). Callers
    that want a transaction wrap their work in :meth:`transaction`.
    """

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else get_state_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_lock = threading.Lock()
        self._initialized = False
        # Eagerly initialize so a bad filesystem is reported at startup,
        # not on the first message write.
        self._init_schema()

    # -- connection management ------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(
                self.path,
                detect_types=sqlite3.PARSE_DECLTYPES,
                check_same_thread=False,
                isolation_level=None,  # autocommit; we manage txns explicitly
            )
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA foreign_keys = ON")
            self._apply_wal(c)
            self._local.conn = c
        return c

    def _apply_wal(self, c: sqlite3.Connection) -> None:
        try:
            c.execute("PRAGMA journal_mode = WAL")
            c.execute("PRAGMA synchronous = NORMAL")
        except sqlite3.DatabaseError as exc:
            if any(m in str(exc).lower() for m in _WAL_INCOMPAT_MARKERS):
                logger.warning("state: WAL unavailable, using DELETE journal: %s", exc)
                try:
                    c.execute("PRAGMA journal_mode = DELETE")
                except sqlite3.DatabaseError:
                    pass
            else:
                raise

    def _init_schema(self) -> None:
        with self._init_lock:
            if self._initialized:
                return
            c = self._conn()
            # Check current schema version
            try:
                row = c.execute("SELECT value FROM schema_meta WHERE key = 'version'").fetchone()
                current_version = int(row[0]) if row else 0
            except sqlite3.OperationalError:
                current_version = 0
            
            # Apply migrations
            if current_version < 2:
                # Add reasoning_content column to messages table
                try:
                    c.execute("ALTER TABLE messages ADD COLUMN reasoning_content TEXT")
                except sqlite3.OperationalError:
                    pass  # Column already exists
            if current_version < 3:
                # Add system_prompt column to sessions table
                try:
                    c.execute("ALTER TABLE sessions ADD COLUMN system_prompt TEXT NOT NULL DEFAULT ''")
                except sqlite3.OperationalError:
                    pass  # Column already exists
            
            c.executescript(_SCHEMA_SQL)
            c.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES(?, ?)",
                ("version", str(SCHEMA_VERSION)),
            )
            self._initialized = True

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Begin a transaction, commit on clean exit, roll back on exception."""
        c = self._conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
            c.execute("COMMIT")
        except Exception:
            try:
                c.execute("ROLLBACK")
            except sqlite3.DatabaseError:
                pass
            raise

    def close(self) -> None:
        """Close the calling thread's connection. Safe to call from any
        thread; only affects the current thread's slot.
        """
        c = getattr(self._local, "conn", None)
        if c is not None:
            try:
                c.close()
            except Exception:
                pass
            self._local.conn = None

    # -- sessions -------------------------------------------------------------

    def create_session(
        self,
        *,
        title: str = "",
        model: str = "",
        provider: str = "",
        parent_id: Optional[str] = None,
        source: str = "cli",
        session_id: Optional[str] = None,
        system_prompt: str = "",
    ) -> Session:
        sid = session_id or uuid.uuid4().hex
        now = time.time()
        s = Session(
            id=sid,
            title=title,
            model=model,
            provider=provider,
            parent_id=parent_id,
            source=source,
            system_prompt=system_prompt,
            created_at=now,
            updated_at=now,
        )
        with self.transaction() as c:
            c.execute(
                "INSERT INTO sessions(id, title, model, provider, parent_id, source, system_prompt, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (s.id, s.title, s.model, s.provider, s.parent_id, s.source, s.system_prompt, s.created_at, s.updated_at),
            )
        return s

    def get_session(self, session_id: str) -> Optional[Session]:
        with self.transaction() as c:
            row = c.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return _row_to_session(row) if row else None

    def list_sessions(self, limit: int = 100) -> List[Session]:
        with self.transaction() as c:
            rows = c.execute(
                "SELECT * FROM sessions ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_session(r) for r in rows]

    def list_sessions_by_prefix(self, prefix: str, limit: int = 50) -> List[Session]:
        """Return sessions whose ID starts with ``prefix`` (e.g. 'cron_{job_id}_')."""
        with self.transaction() as c:
            rows = c.execute(
                "SELECT * FROM sessions WHERE id LIKE ? ORDER BY created_at DESC LIMIT ?",
                (prefix + "%", limit),
            ).fetchall()
        return [_row_to_session(r) for r in rows]

    def update_session(
        self,
        session_id: str,
        *,
        title: Optional[str] = None,
        model: Optional[str] = None,
        provider: Optional[str] = None,
        touch: bool = True,
    ) -> None:
        sets: List[str] = []
        params: List[Any] = []
        if title is not None:
            sets.append("title = ?")
            params.append(title)
        if model is not None:
            sets.append("model = ?")
            params.append(model)
        if provider is not None:
            sets.append("provider = ?")
            params.append(provider)
        if touch:
            sets.append("updated_at = ?")
            params.append(time.time())
        if not sets:
            return
        params.append(session_id)
        with self.transaction() as c:
            c.execute(f"UPDATE sessions SET {', '.join(sets)} WHERE id = ?", params)

    def delete_session(self, session_id: str) -> bool:
        with self.transaction() as c:
            cur = c.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            return cur.rowcount > 0

    def branch_session(self, session_id: str, new_title: str = "") -> str:
        """Branch current session. Copies all messages to a new session.

        Returns the new session ID.
        """
        import re as _re
        import uuid
        from datetime import datetime

        original = self.get_session(session_id)
        if original is None:
            raise ValueError(f"Session not found: {session_id}")

        messages = self.list_messages(session_id)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        new_id = f"{ts}_{uuid.uuid4().hex[:6]}"

        if not new_title:
            base = original.title or "session"
            base = _re.sub(r'\s*#\d+$', '', base)
            with self.transaction() as c:
                row = c.execute(
                    "SELECT COUNT(*) FROM sessions WHERE parent_id = ?",
                    (session_id,),
                ).fetchone()
            count = (row[0] if row else 0) + 1
            new_title = f"{base} #{count}"

        self.create_session(
            session_id=new_id,
            title=new_title,
            model=original.model,
            provider=original.provider,
            parent_id=session_id,
            source=original.source,
        )

        for msg in messages:
            self.add_message(new_id, Message(
                role=msg.role,
                content=msg.content,
                tool_calls=msg.tool_calls,
                tool_call_id=msg.tool_call_id,
                name=msg.name,
                reasoning_content=msg.reasoning_content,
            ))

        return new_id

    def get_session_system_prompt(self, session_id: str) -> Optional[str]:
        """Return the stored system prompt for a session, or None."""
        with self.transaction() as c:
            row = c.execute(
                "SELECT system_prompt FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        if row and row["system_prompt"]:
            return row["system_prompt"]
        return None

    def update_session_system_prompt(self, session_id: str, prompt: str) -> None:
        """Persist the system prompt for a session (for reuse on resumption)."""
        with self.transaction() as c:
            c.execute(
                "UPDATE sessions SET system_prompt = ? WHERE id = ?",
                (prompt, session_id),
            )

    # -- messages -------------------------------------------------------------

    def add_message(self, session_id: str, msg: Message) -> int:
        """Insert a message. Returns the new row id. Touches the session's
        ``updated_at`` so the session sorts to the top of the list.

        ``msg.content`` may be a plain string or a list of content parts
        (multimodal). Lists are JSON-serialized for SQLite storage.
        """
        tool_calls_json = json.dumps(msg.tool_calls, ensure_ascii=False) if msg.tool_calls else None
        content = msg.content
        if isinstance(content, list):
            content = json.dumps(content, ensure_ascii=False)
        with self.transaction() as c:
            cur = c.execute(
                "INSERT INTO messages(session_id, role, content, tool_calls, tool_call_id, name, reasoning_content, created_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (
                    session_id,
                    msg.role,
                    content,
                    tool_calls_json,
                    msg.tool_call_id,
                    msg.name,
                    msg.reasoning_content,
                    msg.created_at,
                ),
            )
            c.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (time.time(), session_id),
            )
        return int(cur.lastrowid)

    def list_messages(self, session_id: str) -> List[Message]:
        with self.transaction() as c:
            rows = c.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY id ASC",
                (session_id,),
            ).fetchall()
        return [_row_to_message(r) for r in rows]

    def delete_message(self, message_id: int) -> bool:
        with self.transaction() as c:
            cur = c.execute("DELETE FROM messages WHERE id = ?", (message_id,))
            return cur.rowcount > 0

    # -- memory ---------------------------------------------------------------

    def memory_set(self, key: str, value: str, tags: str = "") -> None:
        now = time.time()
        with self.transaction() as c:
            c.execute(
                "INSERT INTO memory(key, value, tags, created_at, updated_at) VALUES(?,?,?,?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value, tags=excluded.tags, updated_at=excluded.updated_at",
                (key, value, tags, now, now),
            )

    def memory_get(self, key: str) -> Optional[str]:
        with self.transaction() as c:
            row = c.execute("SELECT value FROM memory WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def memory_delete(self, key: str) -> bool:
        with self.transaction() as c:
            cur = c.execute("DELETE FROM memory WHERE key = ?", (key,))
            return cur.rowcount > 0

    def memory_list(self, limit: int = 200) -> List[Dict[str, Any]]:
        with self.transaction() as c:
            rows = c.execute(
                "SELECT key, value, tags, created_at, updated_at FROM memory"
                " ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def memory_search(self, query: str, limit: int = 50) -> List[Dict[str, Any]]:
        """Substring search across key, value, and tags.

        Supports multi-word queries: each word is matched independently
        (AND logic). ``LIKE`` is fine here — memory tables stay small
        (the cap is ``memory.max_entries``).
        """
        words = [w.strip() for w in query.split() if w.strip()]
        if not words:
            return []
        # Build WHERE clause: every word must match at least one column.
        conditions = []
        params: list = []
        for word in words:
            like = f"%{word}%"
            conditions.append("(key LIKE ? OR value LIKE ? OR tags LIKE ?)")
            params.extend([like, like, like])
        where = " AND ".join(conditions)
        params.append(limit)
        with self.transaction() as c:
            rows = c.execute(
                "SELECT key, value, tags, created_at, updated_at FROM memory"
                f" WHERE {where}"
                " ORDER BY updated_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [dict(r) for r in rows]

    def memory_count(self) -> int:
        """Return the total number of memory entries."""
        with self.transaction() as c:
            row = c.execute("SELECT COUNT(*) AS cnt FROM memory").fetchone()
        return int(row["cnt"] or 0) if row else 0

    # -- usage ----------------------------------------------------------------

    def record_usage(self, u: UsageRecord) -> None:
        with self.transaction() as c:
            c.execute(
                "INSERT INTO usage(session_id, prompt_tokens, completion_tokens, total_tokens, model, created_at)"
                " VALUES(?,?,?,?,?,?)",
                (u.session_id, u.prompt_tokens, u.completion_tokens, u.total_tokens, u.model, u.created_at),
            )

    def session_usage(self, session_id: str) -> Dict[str, int]:
        with self.transaction() as c:
            row = c.execute(
                "SELECT COALESCE(SUM(prompt_tokens),0) AS p, COALESCE(SUM(completion_tokens),0) AS c,"
                " COALESCE(SUM(total_tokens),0) AS t FROM usage WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return {
            "prompt_tokens": int(row["p"] or 0),
            "completion_tokens": int(row["c"] or 0),
            "total_tokens": int(row["t"] or 0),
        }

    def last_turn_usage(self, session_id: str) -> Dict[str, int]:
        """Return the most recent turn's token usage (not the sum).

        The progress bar needs prompt_tokens from the last turn (which
        represents the full context sent to the model), not the sum of
        all turns.
        """
        with self.transaction() as c:
            row = c.execute(
                "SELECT prompt_tokens, completion_tokens, total_tokens"
                " FROM usage WHERE session_id = ? ORDER BY id DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        if not row:
            return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        return {
            "prompt_tokens": int(row["prompt_tokens"] or 0),
            "completion_tokens": int(row["completion_tokens"] or 0),
            "total_tokens": int(row["total_tokens"] or 0),
        }

    def total_usage(self) -> Dict[str, int]:
        with self.transaction() as c:
            row = c.execute(
                "SELECT COALESCE(SUM(prompt_tokens),0) AS p, COALESCE(SUM(completion_tokens),0) AS c,"
                " COALESCE(SUM(total_tokens),0) AS t FROM usage"
            ).fetchone()
        return {
            "prompt_tokens": int(row["p"] or 0),
            "completion_tokens": int(row["c"] or 0),
            "total_tokens": int(row["t"] or 0),
        }

    # -- skills state ---------------------------------------------------------

    def set_skill_enabled(self, name: str, enabled: bool) -> None:
        with self.transaction() as c:
            c.execute(
                "INSERT INTO skills_state(name, enabled, updated_at) VALUES(?,?,?)"
                " ON CONFLICT(name) DO UPDATE SET enabled=excluded.enabled, updated_at=excluded.updated_at",
                (name, 1 if enabled else 0, time.time()),
            )

    def get_skill_enabled_map(self) -> Dict[str, bool]:
        with self.transaction() as c:
            rows = c.execute("SELECT name, enabled FROM skills_state").fetchall()
        return {r["name"]: bool(r["enabled"]) for r in rows}


# ---------------------------------------------------------------------------
# Row helpers
# ---------------------------------------------------------------------------

def _row_to_session(row: sqlite3.Row) -> Session:
    return Session(
        id=row["id"],
        title=row["title"],
        model=row["model"],
        provider=row["provider"],
        parent_id=row["parent_id"],
        source=row["source"],
        system_prompt=row["system_prompt"] if "system_prompt" in row.keys() else "",
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_message(row: sqlite3.Row) -> Message:
    tc_raw = row["tool_calls"]
    tc = json.loads(tc_raw) if tc_raw else None
    raw_content = row["content"]
    # Deserialise multimodal content: ONLY if the stored JSON array
    # looks like OpenAI content parts (each has a "type" field).
    # Tool results like [{"name": "Alice"}] must NOT be parsed as
    # multimodal — they are plain JSON arrays that should stay strings.
    content: Any = raw_content
    if raw_content and raw_content.startswith("["):
        try:
            parsed = json.loads(raw_content)
            if (isinstance(parsed, list)
                    and parsed
                    and isinstance(parsed[0], dict)
                    and "type" in parsed[0]):
                content = parsed
        except (json.JSONDecodeError, ValueError):
            pass
    return Message(
        id=row["id"],
        session_id=row["session_id"],
        role=row["role"],
        content=content,
        tool_calls=tc,
        tool_call_id=row["tool_call_id"],
        name=row["name"],
        reasoning_content=row["reasoning_content"],
        created_at=row["created_at"],
    )
