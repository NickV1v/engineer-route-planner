"""Immutable plan snapshots and atomic, idempotent event commits."""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from fastapi.encoders import jsonable_encoder

from dispatch.models import Scenario


class SessionNotFound(KeyError):
    pass


class VersionConflict(ValueError):
    pass


class PlanStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS snapshots (
                    session_id TEXT NOT NULL REFERENCES sessions(id), version INTEGER NOT NULL,
                    scenario TEXT NOT NULL, response TEXT NOT NULL,
                    PRIMARY KEY (session_id, version)
                );
                CREATE TABLE IF NOT EXISTS events (
                    session_id TEXT NOT NULL REFERENCES sessions(id), id TEXT NOT NULL,
                    payload_hash TEXT NOT NULL, version INTEGER NOT NULL,
                    PRIMARY KEY (session_id, id)
                );
                CREATE TABLE IF NOT EXISTS previews (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
                    base_version INTEGER NOT NULL, event_id TEXT NOT NULL,
                    payload_hash TEXT NOT NULL, scenario TEXT NOT NULL, response TEXT NOT NULL,
                    UNIQUE(session_id, event_id)
                );
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def write_snapshot(
        db, session_id: str, version: int, scenario: Scenario, response: dict
    ) -> dict:
        encoded = jsonable_encoder({**response, "session_id": session_id, "version": version})
        db.execute(
            "INSERT INTO snapshots VALUES (?,?,?,?)",
            (
                session_id,
                version,
                scenario.model_dump_json(),
                json.dumps(encoded, ensure_ascii=False, separators=(",", ":")),
            ),
        )
        return encoded

    def create(self, scenario: Scenario, response: dict) -> dict:
        session_id = uuid4().hex
        with self.connection() as db:
            db.execute("INSERT INTO sessions VALUES (?,1)", (session_id,))
            return self.write_snapshot(db, session_id, 1, scenario, response)

    def get(self, session_id: str, version: int | None = None) -> tuple[Scenario, dict]:
        with self.connection() as db:
            if version is None:
                row = db.execute(
                    "SELECT version FROM sessions WHERE id=?", (session_id,)
                ).fetchone()
                if row is None:
                    raise SessionNotFound("План не найден")
                version = row["version"]
            row = db.execute(
                "SELECT scenario,response FROM snapshots WHERE session_id=? AND version=?",
                (session_id, version),
            ).fetchone()
            if row is None:
                raise SessionNotFound("Версия плана не найдена")
            return Scenario.model_validate_json(row["scenario"]), json.loads(row["response"])

    @staticmethod
    def existing_event(db, session_id: str, event_id: str, payload_hash: str) -> dict | None:
        row = db.execute(
            "SELECT payload_hash,version FROM events WHERE session_id=? AND id=?",
            (session_id, event_id),
        ).fetchone()
        if row is None:
            return None
        if row["payload_hash"] != payload_hash:
            raise VersionConflict("Этот ID события уже использован с другими параметрами")
        saved = db.execute(
            "SELECT response FROM snapshots WHERE session_id=? AND version=?",
            (session_id, row["version"]),
        ).fetchone()
        return json.loads(saved["response"])

    def find_event(self, session_id: str, event_id: str, payload_hash: str) -> dict | None:
        with self.connection() as db:
            return self.existing_event(db, session_id, event_id, payload_hash)

    def commit_event(
        self,
        session_id: str,
        expected_version: int,
        event_id: str,
        payload_hash: str,
        scenario: Scenario,
        response: dict,
    ) -> dict:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = self.existing_event(db, session_id, event_id, payload_hash)
            if existing is not None:
                return existing
            current = db.execute(
                "SELECT version FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
            if current is None:
                raise SessionNotFound("План не найден")
            if current["version"] != expected_version:
                raise VersionConflict(
                    "План уже изменился. Откройте текущую версию и повторите событие."
                )
            version = expected_version + 1
            saved = self.write_snapshot(db, session_id, version, scenario, response)
            db.execute(
                "INSERT INTO events VALUES (?,?,?,?)", (session_id, event_id, payload_hash, version)
            )
            db.execute("UPDATE sessions SET version=? WHERE id=?", (version, session_id))
            return saved

    def find_preview(self, session_id: str, event_id: str, payload_hash: str) -> dict | None:
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM previews WHERE session_id=? AND event_id=?", (session_id, event_id)
            ).fetchone()
            if row is None:
                return None
            if row["payload_hash"] != payload_hash:
                raise VersionConflict("Этот ID события уже использован с другими параметрами")
            return self.preview_response(row)

    @staticmethod
    def preview_response(row) -> dict:
        return {
            "preview_id": row["id"],
            "base_version": row["base_version"],
            "result": json.loads(row["response"]),
        }

    def save_preview(
        self,
        session_id: str,
        base_version: int,
        event_id: str,
        payload_hash: str,
        scenario: Scenario,
        response: dict,
    ) -> dict:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM previews WHERE session_id=? AND event_id=?", (session_id, event_id)
            ).fetchone()
            if row is not None:
                if row["payload_hash"] != payload_hash:
                    raise VersionConflict("Этот ID события уже использован с другими параметрами")
                return self.preview_response(row)
            current = db.execute(
                "SELECT version FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
            if current is None:
                raise SessionNotFound("План не найден")
            if current["version"] != base_version:
                raise VersionConflict("План уже изменился. Рассчитайте вариант заново.")
            preview_id = uuid4().hex
            result = jsonable_encoder(
                {**response, "session_id": session_id, "version": base_version + 1}
            )
            db.execute(
                "INSERT INTO previews VALUES (?,?,?,?,?,?,?)",
                (
                    preview_id,
                    session_id,
                    base_version,
                    event_id,
                    payload_hash,
                    scenario.model_dump_json(),
                    json.dumps(result, ensure_ascii=False),
                ),
            )
            return {"preview_id": preview_id, "base_version": base_version, "result": result}

    def commit_preview(self, session_id: str, preview_id: str) -> dict:
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM previews WHERE session_id=? AND id=?", (session_id, preview_id)
            ).fetchone()
            if row is None:
                raise SessionNotFound("Вариант не найден")
            scenario = Scenario.model_validate_json(row["scenario"])
            response = json.loads(row["response"])
        return self.commit_event(
            session_id,
            row["base_version"],
            row["event_id"],
            row["payload_hash"],
            scenario,
            response,
        )
