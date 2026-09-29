"""Per-operation progress. Percentages count completed stages, not elapsed time."""

from contextlib import contextmanager
from threading import Lock
from time import monotonic

from dispatch.store import SessionNotFound, VersionConflict


class CalculationProgress:
    def __init__(self):
        self._lock = Lock()
        self._records = {}

    def get(self, key):
        with self._lock:
            if key not in self._records:
                raise SessionNotFound("Расчёт ещё не начался или больше не доступен.")
            return {k: v for k, v in self._records[key].items() if k != "updated"}

    def update(self, key, stage, percent, status="running"):
        if key is None:
            return
        with self._lock:
            record = self._records[key]
            record.update(
                stage=stage,
                percent=max(record["percent"], percent),
                status=status,
                updated=monotonic(),
            )

    def routing(self, key):
        def changed(stage, completed, total):
            if stage in {"Определение координат", "Уточнение адресов"}:
                percent = int(10 * completed / max(1, total))
            elif stage == "Загрузка дорожной и транспортной сети":
                percent = 10
            elif stage == "Сохранение маршрутов":
                percent = 90 + int(5 * completed / max(1, total))
            else:
                percent = 10 + int(80 * completed / max(1, total))
            self.update(key, stage, min(95, percent))

        return changed

    @contextmanager
    def track(self, key):
        if key is not None:
            with self._lock:
                # Keep active requests, bound completed entries and reject reused IDs.
                now = monotonic()
                self._records = {
                    k: v
                    for k, v in self._records.items()
                    if v["status"] == "running" or now - v["updated"] < 3600
                }
                if key in self._records:
                    raise VersionConflict("Этот идентификатор расчёта уже использован.")
                if len(self._records) >= 256:
                    finished = [k for k, v in self._records.items() if v["status"] != "running"]
                    if not finished:
                        raise VersionConflict(
                            "Слишком много расчётов. Дождитесь завершения текущих."
                        )
                    del self._records[finished[0]]
                self._records[key] = {
                    "id": key,
                    "stage": "Ожидание расчёта",
                    "percent": 0,
                    "status": "running",
                    "updated": now,
                }
        try:
            yield self.routing(key)
        except Exception:
            self.update(key, "Расчёт не завершён", 0, "failed")
            raise
        else:
            if key is None or self.get(key)["status"] == "running":
                self.update(key, "Готово", 100, "completed")
