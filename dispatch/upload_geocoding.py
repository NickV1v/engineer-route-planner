"""Resolve new upload addresses during calculation, never during CSV validation."""

import os
from threading import Lock

from dispatch.geocode import GeocodingError, PhotonClient
from dispatch.store import VersionConflict


def coordinate_request(request):
    return request.address.startswith("Координаты ") and not request.raw.get("Адрес", "").strip()


class UploadGeocoder:
    def __init__(self, catalog):
        self.catalog = catalog
        self.client = PhotonClient(
            catalog, os.environ.get("DISPATCH_PHOTON_URL", "https://photon.komoot.io")
        )
        # One shared client preserves the provider's request interval across runs.
        self._lock = Lock()

    def prepare(self, scenario, report):
        pending = [
            record
            for record in scenario.geography.values()
            if record.point is None and not record.query
        ]
        reverse = list(
            dict.fromkeys(
                r.location_id
                for r in scenario.requests
                if coordinate_request(r)
                and scenario.geography[r.location_id].point
                and scenario.geography[r.location_id].point.label == r.address
            )
        )
        total = len(pending) + len(reverse)
        report("Уточнение адресов", 0, total)
        for i, record in enumerate(pending):
            with self._lock:
                current = self.catalog.get(record.address)
                if not current.point and not current.query:
                    try:
                        resolved = self.client.lookup(record.address, expand=True)
                    except GeocodingError as exc:
                        raise GeocodingError(
                            "Не удалось определить координаты: сервис адресов недоступен. "
                            "Повторите расчёт позже или укажите координаты в CSV."
                        ) from exc
                    try:
                        current = self.catalog.save(resolved, current.revision)
                    except VersionConflict:
                        # A dispatcher edit made during the lookup takes precedence.
                        current = self.catalog.get(record.address)
                scenario.geography[record.location_id] = current
            report("Уточнение адресов", i + 1, total)
        available = True
        for i, key in enumerate(reverse):
            record = scenario.geography[key]
            if available:
                try:
                    with self._lock:
                        label = self.client.reverse(record.point)
                    if label:
                        record.point = record.point.model_copy(update={"label": "Рядом: " + label})
                except GeocodingError:
                    # Reverse lookup is optional. Keep coordinates and avoid repeated timeouts.
                    available = False
            report("Уточнение адресов", len(pending) + i + 1, total)
