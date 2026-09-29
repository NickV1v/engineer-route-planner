"""Calculation-local reuse and a separate immutable archive of completed inputs."""

import json
import sqlite3
import zlib
from contextlib import contextmanager
from pathlib import Path

from dispatch.routing.contracts import Journey, RoutingError, content_hash
from dispatch.routing.matrices import PreparedTransport


class CalculationCache:
    """No files, globals, or archive reads. Discarded when one calculation ends."""

    def __init__(self):
        self._journeys: dict[str, bytes] = {}

    def journey(self, key):
        raw = self._journeys.get(key)
        return Journey.model_validate(unpack(raw)) if raw is not None else None

    def save_journey(self, key, journey):
        self._journeys[key] = pack(journey.model_dump())

    def contains(self, key):
        return key in self._journeys

    def clear(self):
        self._journeys.clear()


def pack(value):
    return zlib.compress(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


def unpack(value):
    try:
        return json.loads(zlib.decompress(value))
    except (ValueError, zlib.error) as exc:
        raise RoutingError("Повреждены сохранённые транспортные данные") from exc


class RoutingCache:
    """Archive used by snapshots and offline graph preparation, never an OD lookup
    for a new web calculation. The name also preserves the preparation CLI API.
    """

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS journeys (id TEXT PRIMARY KEY, payload BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS packages (
                    id TEXT PRIMARY KEY, input_key TEXT UNIQUE NOT NULL,
                    payload BLOB NOT NULL, journeys BLOB NOT NULL
                );
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=60)
        try:
            with db:
                yield db
        finally:
            db.close()

    def journey(self, key):
        with self.connect() as db:
            row = db.execute("SELECT payload FROM journeys WHERE id=?", (key,)).fetchone()
        return Journey.model_validate(unpack(row[0])) if row else None

    def save_journey(self, key, journey):
        raw = pack(journey.model_dump())
        with self.connect() as db:
            self._save_journey(db, key, raw)

    @staticmethod
    def _save_journey(db, key, raw):
        existing = db.execute("SELECT payload FROM journeys WHERE id=?", (key,)).fetchone()
        if existing and existing[0] != raw:
            raise RoutingError("Расчёт изменился при прежней версии транспортной сети")
        db.execute("INSERT OR IGNORE INTO journeys VALUES (?,?)", (key, raw))

    def package(self, *, fingerprint=None, input_key=None):
        with self.connect() as db:
            row = db.execute(
                "SELECT id,payload,journeys FROM packages WHERE id=? OR input_key=?",
                (fingerprint, input_key),
            ).fetchone()
        if row is None:
            return None
        package = PreparedTransport.model_validate(unpack(row[1]))
        if package.fingerprint() != row[0]:
            raise RoutingError("Повреждён сохранённый транспортный пакет")
        return package, unpack(row[2])

    def save_package(self, input_key, package, refs):
        with self.connect() as db:
            self._save_package(db, input_key, package, refs)

    @staticmethod
    def _save_package(db, input_key, package, refs):
        for matrix_refs in refs.values():
            for row in matrix_refs:
                for key in row:
                    if db.execute("SELECT 1 FROM journeys WHERE id=?", (key,)).fetchone() is None:
                        raise RoutingError("Не сохранён один из маршрутов транспортного пакета")
        raw = (package.fingerprint(), input_key, pack(package.model_dump()), pack(refs))
        existing = db.execute(
            "SELECT payload,journeys FROM packages WHERE input_key=?", (input_key,)
        ).fetchone()
        if existing and existing != raw[2:]:
            raise RoutingError("Транспортный пакет с теми же входами изменился")
        db.execute("INSERT OR IGNORE INTO packages VALUES (?,?,?,?)", raw)

    def save_calculation(self, input_key, package, refs, cache, progress):
        """Archive validated, already packed results in one atomic transaction.

        This only writes the current calculation's data. Existing archive rows
        are compared for immutability, never used instead of a fresh search.
        """
        completed = 0
        total = sum(len(row) for matrix in refs.values() for row in matrix)
        with self.connect() as db:
            for matrix in refs.values():
                for row in matrix:
                    for key in row:
                        raw = cache._journeys.get(key)
                        if raw is None:
                            raise RoutingError("Не сохранилась поездка текущего расчёта")
                        self._save_journey(db, key, raw)
                        completed += 1
                    progress("Сохранение маршрутов", completed, total)
            self._save_package(db, input_key, package, refs)


def pair_key(network_id, profile, origin, target, a, b):
    return content_hash(
        {
            "network": network_id,
            "profile": profile,
            "from_id": origin,
            "to_id": target,
            "a": a.model_dump() if a else None,
            "b": b.model_dump() if b else None,
        }
    )
