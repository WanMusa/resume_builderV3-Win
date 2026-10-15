"""Thread-local SQLite persistence and explicit, durable queue state."""

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import threading
import uuid

from .models import Job


STATUSES = frozenset({
    "ready", "queued", "generating", "rendering", "completed",
    "failed", "interrupted", "invalid",
})
ACTIVE = ("queued", "generating", "rendering")
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _check_snapshot(value):
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if any(word in normalized for word in (
                "apikey", "secret", "password", "credential", "authorization",
                "accesstoken", "refreshtoken", "bearertoken",
            )) or normalized in {"token", "auth", "passwd"}:
                raise ValueError("Snapshots must not contain credential fields")
            _check_snapshot(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _check_snapshot(child)


class Store:
    VERSION = 1

    def __init__(self, db_path):
        self.db_path = str(db_path)
        self._local = threading.local()
        self._uri = self.db_path == ":memory:"
        self._target = f"file:resume-{uuid.uuid4()}?mode=memory&cache=shared" if self._uri else self.db_path
        self._existing = not self._uri and Path(self.db_path).exists() and Path(self.db_path).stat().st_size > 0
        if not self._uri:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        key = self._target if self._uri else str(Path(self.db_path).resolve())
        with _LOCKS_GUARD:
            self._write_lock = _LOCKS.setdefault(key, threading.RLock())
        with self._write_lock:
            try:
                self._initialize()
            except BaseException:
                self.close()
                raise

    def _connection(self):
        connection = getattr(self._local, "connection", None)
        if connection is None:
            connection = sqlite3.connect(self._target, timeout=10, isolation_level=None, uri=self._uri)
            connection.row_factory = sqlite3.Row
            try:
                connection.execute("PRAGMA busy_timeout=10000")
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA journal_mode=WAL")
            except BaseException:
                connection.close()
                raise
            self._local.connection = connection
        return connection

    def _initialize(self):
        connection = self._connection()
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version > self.VERSION:
            raise RuntimeError("Database was created by a newer application")
        if version == self.VERSION:
            return
        tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        if self._existing:
            self.backup(self.db_path + f".pre-migration-v{version}.bak")
        # Unknown legacy schemas must never be silently replaced.
        if tables:
            raise RuntimeError("Unsupported legacy database; a SQLite backup was saved")
        with self._transaction() as conn:
            conn.execute("""
                CREATE TABLE jobs (
                    id TEXT PRIMARY KEY, source_id TEXT, canonical_hash TEXT NOT NULL,
                    revision INTEGER NOT NULL, filename TEXT NOT NULL, source_path TEXT NOT NULL,
                    input_json TEXT, snapshot_json TEXT,
                    checkpoints_json TEXT NOT NULL DEFAULT '{}',
                    artifacts_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL, archived INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, queued_at TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE import_ledger (
                    kind TEXT NOT NULL, source_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    PRIMARY KEY(kind, source_id, fingerprint)
                )
            """)
            conn.execute("CREATE INDEX jobs_queue ON jobs(status, queued_at)")
            conn.execute("CREATE INDEX jobs_source ON jobs(source_id)")
            conn.execute(f"PRAGMA user_version={self.VERSION}")

    @contextmanager
    def _transaction(self):
        with self._write_lock:
            connection = self._connection()
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    @staticmethod
    def _row(row):
        result = dict(row)
        for field, column, default in (
            ("job", "input_json", None), ("snapshot", "snapshot_json", None),
            ("checkpoints", "checkpoints_json", {}), ("artifacts", "artifacts_json", []),
        ):
            result[field] = json.loads(result[column]) if result[column] is not None else default
        result["archived"] = bool(result["archived"])
        return result

    def list_jobs(self, include_archived=False):
        query = "SELECT * FROM jobs" + ("" if include_archived else " WHERE archived=0")
        return [self._row(row) for row in self._connection().execute(query + " ORDER BY created_at, id")]

    def get(self, id):
        row = self._connection().execute("SELECT * FROM jobs WHERE id=?", (id,)).fetchone()
        if row is None:
            raise KeyError(id)
        return self._row(row)

    def import_job(self, job: dict, source_path: str) -> bool:
        job = Job.model_validate(job).model_dump(mode="json")
        encoded = _json(job)
        fingerprint = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        with self._transaction() as conn:
            inserted = conn.execute(
                "INSERT OR IGNORE INTO import_ledger VALUES ('job',?,?)", (job["id"], fingerprint)
            ).rowcount
            if not inserted:
                return False
            revision = conn.execute(
                "SELECT COUNT(*) FROM import_ledger WHERE kind IN ('job','regeneration') AND source_id=?",
                (job["id"],),
            ).fetchone()[0]
            revision = max(revision, conn.execute(
                "SELECT COALESCE(MAX(revision),0)+1 FROM jobs WHERE source_id=?", (job["id"],)
            ).fetchone()[0])
            timestamp = _now()
            conn.execute("""
                INSERT INTO jobs (id,source_id,canonical_hash,revision,filename,source_path,
                                  input_json,status,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,'ready',?,?)
            """, (str(uuid.uuid4()), job["id"], fingerprint, revision,
                  Path(source_path).name, str(source_path), encoded, timestamp, timestamp))
        return True

    def import_invalid(self, source_path: str, fingerprint: str, error: str) -> bool:
        with self._transaction() as conn:
            inserted = conn.execute(
                "INSERT OR IGNORE INTO import_ledger VALUES ('invalid',?,?)",
                (str(source_path), fingerprint),
            ).rowcount
            if not inserted:
                return False
            timestamp = _now()
            conn.execute("""
                INSERT INTO jobs (id,canonical_hash,revision,filename,source_path,status,error,
                                  created_at,updated_at)
                VALUES (?,?,1,?,?,'invalid',?,?,?)
            """, (str(uuid.uuid4()), fingerprint, Path(source_path).name,
                  str(source_path), error, timestamp, timestamp))
        return True

    def enqueue(self, id, snapshot: dict) -> bool:
        if not isinstance(snapshot, dict):
            raise ValueError("Snapshot must be an object")
        _check_snapshot(snapshot)
        encoded = _json(snapshot)
        with self._transaction() as conn:
            previous = conn.execute(
                "SELECT status,snapshot_json,checkpoints_json FROM jobs WHERE id=?", (id,)
            ).fetchone()
            if previous is None or previous["status"] not in ("ready", "failed", "interrupted"):
                return False
            if previous["snapshot_json"] is not None:
                encoded = previous["snapshot_json"]
            return bool(conn.execute("""
                UPDATE jobs SET status='queued',snapshot_json=?,error='',queued_at=?,updated_at=?
                WHERE id=? AND status IN ('ready','failed','interrupted')
            """, (encoded, _now(), _now(), id)).rowcount)

    def regenerate(self, id, snapshot: dict) -> str:
        """Queue an independent revision without changing previous outputs or checkpoints."""
        if not isinstance(snapshot, dict):
            raise ValueError("Snapshot must be an object")
        _check_snapshot(snapshot)
        _json(snapshot)
        with self._transaction() as conn:
            original = conn.execute("SELECT * FROM jobs WHERE id=?", (id,)).fetchone()
            if original is None:
                raise KeyError(id)
            if original["status"] in ACTIVE or original["input_json"] is None:
                raise ValueError("Only inactive, valid jobs can be regenerated")
            return self._clone_revision(conn, original, snapshot, {})

    def rerender(self, id, output_folder: str) -> str:
        """Relocate saved generation into an independent render-only queue revision."""
        if not isinstance(output_folder, str) or not output_folder.strip():
            raise ValueError("An output folder is required")
        with self._transaction() as conn:
            original = conn.execute("SELECT * FROM jobs WHERE id=?", (id,)).fetchone()
            if original is None:
                raise KeyError(id)
            if original["status"] in ACTIVE or original["input_json"] is None:
                raise ValueError("Only inactive, valid jobs can be rendered again")
            checkpoints = json.loads(original["checkpoints_json"])
            if original["snapshot_json"] is None or not {"resume", "letter"} <= checkpoints.keys():
                raise ValueError("A frozen snapshot and both resume and letter checkpoints are required")
            snapshot = json.loads(original["snapshot_json"])
            snapshot["output_folder"] = output_folder
            return self._clone_revision(conn, original, snapshot, checkpoints)

    def _clone_revision(self, conn, original, snapshot, checkpoints):
        new_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO import_ledger VALUES ('regeneration',?,?)", (original["source_id"], new_id)
        )
        ledger_count = conn.execute(
            "SELECT COUNT(*) FROM import_ledger WHERE kind IN ('job','regeneration') AND source_id=?",
            (original["source_id"],),
        ).fetchone()[0]
        next_revision = conn.execute(
            "SELECT COALESCE(MAX(revision),0)+1 FROM jobs WHERE source_id=?", (original["source_id"],)
        ).fetchone()[0]
        revision = max(ledger_count, next_revision)
        encoded = _json({**snapshot, "revision": revision})
        timestamp = _now()
        conn.execute("""
            INSERT INTO jobs (id,source_id,canonical_hash,revision,filename,source_path,
                              input_json,snapshot_json,checkpoints_json,status,created_at,updated_at,queued_at)
            VALUES (?,?,?,?,?,?,?,?,?,'queued',?,?,?)
        """, (new_id, original["source_id"], original["canonical_hash"],
              revision, original["filename"], original["source_path"],
              original["input_json"], encoded, _json(checkpoints), timestamp, timestamp, timestamp))
        return new_id

    def recover(self):
        with self._transaction() as conn:
            return conn.execute("""
                UPDATE jobs SET status='interrupted',error='Interrupted before completion',updated_at=?
                WHERE status IN ('queued','generating','rendering')
            """, (_now(),)).rowcount

    def claim_next(self):
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT id FROM jobs WHERE status='queued' ORDER BY queued_at,id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            conn.execute("UPDATE jobs SET status='generating',updated_at=? WHERE id=?", (_now(), row["id"]))
            return self._row(conn.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())

    def set_status(self, id, status, error=""):
        if status not in STATUSES:
            raise ValueError("Unknown job status")
        with self._transaction() as conn:
            if not conn.execute(
                "UPDATE jobs SET status=?,error=?,updated_at=? WHERE id=?", (status, error, _now(), id)
            ).rowcount:
                raise KeyError(id)

    def save_checkpoint(self, id, kind, value: dict):
        if not isinstance(value, dict):
            raise ValueError("Checkpoint must be an object")
        with self._transaction() as conn:
            row = conn.execute("SELECT checkpoints_json FROM jobs WHERE id=?", (id,)).fetchone()
            if row is None:
                raise KeyError(id)
            checkpoints = json.loads(row[0])
            checkpoints[kind] = value
            conn.execute(
                "UPDATE jobs SET checkpoints_json=?,updated_at=? WHERE id=?", (_json(checkpoints), _now(), id)
            )

    def save_artifacts(self, id, paths: list[str]):
        if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
            raise ValueError("Artifacts must be a list of paths")
        with self._transaction() as conn:
            if not conn.execute(
                "UPDATE jobs SET artifacts_json=?,updated_at=? WHERE id=?", (_json(paths), _now(), id)
            ).rowcount:
                raise KeyError(id)

    def archive(self, ids, archived=True):
        with self._transaction() as conn:
            conn.executemany(
                "UPDATE jobs SET archived=?,updated_at=? WHERE id=?",
                [(bool(archived), _now(), id) for id in ids],
            )

    def cleanup(self, cutoff_iso, statuses: list[str], preview=True, clear_ledger=False) -> int:
        """Select old matching statuses or archived history, always protecting active jobs."""
        cutoff = datetime.fromisoformat(cutoff_iso.replace("Z", "+00:00"))
        if cutoff.tzinfo is None:
            raise ValueError("Cutoff must include a timezone")
        if any(status not in STATUSES for status in statuses):
            raise ValueError("Unknown job status")
        eligible = [status for status in statuses if status not in ACTIVE]
        placeholders = ",".join("?" for _ in eligible)
        selected = f"(status IN ({placeholders}) OR archived=1)" if eligible else "archived=1"
        with self._transaction() as conn:
            rows = conn.execute(
                f"""SELECT id,created_at,source_id,source_path,canonical_hash,status FROM jobs
                    WHERE {selected} AND status NOT IN ('queued','generating','rendering')""",
                eligible,
            ).fetchall()
            rows = [row for row in rows if datetime.fromisoformat(row["created_at"]) < cutoff]
            if not preview:
                for row in rows:
                    if clear_ledger:
                        kind = "invalid" if row["status"] == "invalid" else "job"
                        source = row["source_path"] if kind == "invalid" else row["source_id"]
                    conn.execute("DELETE FROM jobs WHERE id=?", (row["id"],))
                    if clear_ledger:
                        conn.execute(
                            "DELETE FROM import_ledger WHERE kind='regeneration' AND fingerprint=?", (row["id"],)
                        )
                        if kind == "invalid" or not conn.execute(
                            "SELECT 1 FROM jobs WHERE source_id IS ? AND canonical_hash=? LIMIT 1",
                            (row["source_id"], row["canonical_hash"]),
                        ).fetchone():
                            conn.execute("DELETE FROM import_ledger WHERE kind=? AND source_id=? AND fingerprint=?",
                                         (kind, source, row["canonical_hash"]))
            return len(rows)

    def reset_history(self, clear_ledger=False):
        with self._transaction() as conn:
            count = conn.execute(
                "DELETE FROM jobs WHERE status NOT IN ('queued','generating','rendering')"
            ).rowcount
            if clear_ledger:
                # Preserve identities of protected jobs, even during an explicit reset.
                conn.execute("""
                    DELETE FROM import_ledger WHERE NOT (
                        (kind='job' AND EXISTS (
                            SELECT 1 FROM jobs WHERE jobs.source_id=import_ledger.source_id
                            AND jobs.canonical_hash=import_ledger.fingerprint
                        )) OR (kind='regeneration' AND EXISTS (
                            SELECT 1 FROM jobs WHERE jobs.id=import_ledger.fingerprint
                        ))
                    )
                """)
            return count

    def backup(self, path):
        destination = Path(path)
        if not self._uri and destination.resolve() == Path(self.db_path).resolve():
            raise ValueError("Backup destination must differ from the database")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self._write_lock:
            target = sqlite3.connect(str(destination), timeout=10)
            try:
                self._connection().backup(target)
            finally:
                target.close()

    def close(self):
        connection = getattr(self._local, "connection", None)
        if connection is not None:
            connection.close()
            del self._local.connection
