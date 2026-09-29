"""Interactive address lookup. No bulk geocoding and no implicit coordinate acceptance."""

import json
import math
import os
import secrets
import threading
import time
from collections import OrderedDict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pydantic import Field

from dispatch.geocode import STREET_TYPES, auto_match, house_match, house_token, normalize_query
from dispatch.geography import GeographyStore
from dispatch.models import GeoLocation, GeoPoint, Model

DADATA_URL = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/suggest/address"


class SuggestionError(RuntimeError):
    pass


class SuggestionSearch(Model):
    query: str = Field(min_length=3, max_length=300)
    original_address: str = Field(default="", max_length=500)
    auto_resolve: bool = False


class SuggestionResolve(Model):
    suggestion_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class AddressSuggestion(Model):
    id: str
    label: str = Field(max_length=1000)
    provider: Literal["catalog", "dadata"]
    precision: Literal["house", "street", "locality", "unknown"]
    house: str = ""
    notice: str = ""
    point: GeoPoint | None = None


class Suggestions(Model):
    items: list[AddressSuggestion]
    live_available: bool
    notice: str = ""
    automatic: AddressSuggestion | None = None


def same_dadata_address(address: str, raw: dict, item: AddressSuggestion) -> bool:
    """Exact component identity only; in particular, 55/2 never means 55к2."""
    data = raw["data"]
    for key in ("city", "region", "region_type_full", "settlement_with_type", "street"):
        if data.get(key) is not None and not isinstance(data[key], str):
            raise ValueError("Malformed address component")
    if (
        item.precision != "house"
        or data.get("country_iso_code") != "RU"
        or data.get("house_type_full") != "дом"
        or data.get("street_type_full") not in STREET_TYPES
    ):
        return False
    return auto_match(
        address,
        {
            "city": data.get("city"),
            "state": " ".join(str(data.get(k) or "") for k in ("region", "region_type_full")),
            "district": data.get("settlement_with_type"),
            "street": f"{data['street_type_full']} {data.get('street') or ''}",
            "housenumber": item.house,
        },
    )


def dadata_suggestion(raw: dict) -> AddressSuggestion:
    if not isinstance(raw, dict) or not isinstance(raw.get("data"), dict):
        raise ValueError("Malformed address suggestion")
    data = raw["data"]
    house = str(data.get("house") or "")
    if data.get("block"):
        house += " " + str(data.get("block_type_full") or data.get("block_type") or "?")
        house += " " + str(data["block"])
    registered = (
        bool(data.get("house"))
        and str(data.get("fias_level")) == "8"
        and bool(data.get("house_fias_id"))
        and str(data.get("fias_actuality_state")) == "0"
        and not data.get("flat")
        and not data.get("room")
    )
    precision = "house" if registered else "street" if data.get("street") else "locality"
    point = None
    try:
        lat, lon = float(data["geo_lat"]), float(data["geo_lon"])
        if (
            registered
            and str(data.get("qc_geo")) == "0"
            and data.get("country_iso_code") == "RU"
            and math.isfinite(lat)
            and math.isfinite(lon)
            and 53 <= lat <= 57.5
            and 35 <= lon <= 41
        ):
            point = GeoPoint(
                lat=lat,
                lon=lon,
                label=raw["value"],
                precision="house",
                source=f"DaData:{data['house_fias_id']}; OpenStreetMap",
            )
    except (KeyError, TypeError, ValueError, OverflowError):
        pass
    if not registered:
        notice = "Дом не подтверждён в реестре. Уточните номер; координаты дома не выбраны."
    elif not point:
        notice = "Дом найден в реестре. Точные координаты уточнятся при выборе."
    else:
        notice = "Найден дом. Проверьте адрес и точку перед подтверждением."
    return AddressSuggestion(
        id=secrets.token_hex(16),
        label=raw["value"],
        provider="dadata",
        precision=precision,
        house=house,
        notice=notice,
        point=point,
    )


class AddressSuggestions:
    def __init__(self, geography: GeographyStore, config_file: Path | None = None):
        self.geography = geography
        self.config_file = config_file
        self._issued: OrderedDict[str, tuple[float, AddressSuggestion, dict | None]] = OrderedDict()
        self._lock = threading.Lock()
        self._network_lock = threading.Lock()
        self._next_call = 0.0

    @property
    def live_available(self) -> bool:
        return bool(self._api_key())

    def _api_key(self) -> str:
        # Explicit empty environment value disables live calls, including in tests.
        token = os.environ.get("DADATA_API_KEY")
        if token is None and self.config_file:
            try:
                with self.config_file.open(encoding="utf-8") as handle:
                    content = handle.read(8192)
                for line in content.splitlines():
                    name, separator, value = line.partition("=")
                    if separator and name.strip() == "DADATA_API_KEY":
                        token = value.strip().strip("\"'")
                        break
            except (OSError, UnicodeError):
                return ""
        token = (token or "").strip()
        # Only a credential value is read; no shell expansion or execution of the file.
        return token if token.isascii() and token.isalnum() and len(token) <= 512 else ""

    def _remember(self, suggestion: AddressSuggestion, raw: dict | None = None):
        with self._lock:
            now = time.monotonic()
            self._issued[suggestion.id] = (now + 900, suggestion, raw)
            while self._issued and (
                len(self._issued) > 1024 or next(iter(self._issued.values()))[0] < now
            ):
                self._issued.popitem(last=False)
        return suggestion

    def _call(self, query: str, count: int) -> list[dict]:
        token = self._api_key()
        if not token:
            raise SuggestionError("Поиск DaData не подключён. Доступен локальный справочник.")
        with self._network_lock:
            time.sleep(max(0, self._next_call - time.monotonic()))
            self._next_call = time.monotonic() + 1.1
            request = Request(
                DADATA_URL,
                data=json.dumps(
                    {
                        "query": query,
                        "count": count,
                        "language": "ru",
                        "to_bound": {"value": "house"},
                    },
                    ensure_ascii=False,
                ).encode(),
                headers={
                    "Authorization": "Token " + token,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": "BeelineDispatch/0.6 interactive-address-suggestions",
                },
                method="POST",
            )
            try:
                with urlopen(request, timeout=8) as response:
                    body = response.read(1_000_001)
                if len(body) > 1_000_000:
                    raise SuggestionError("Сервис адресов вернул слишком большой ответ.")
                payload = json.loads(body)
                if not isinstance(payload, dict) or not isinstance(
                    payload.get("suggestions"), list
                ):
                    raise ValueError("Malformed response")
                return payload["suggestions"][:count]
            except HTTPError as exc:
                message = (
                    "Лимит поиска адресов исчерпан. Доступен локальный справочник."
                    if exc.code == 429
                    else "Поиск DaData временно недоступен. Доступен локальный справочник."
                )
                raise SuggestionError(message) from None
            except (URLError, OSError, ValueError):
                raise SuggestionError(
                    "Не удалось связаться с сервисом адресов. Повторите поиск."
                ) from None

    def _local(self, query: str, original: str) -> list[AddressSuggestion]:
        text = normalize_query(query).casefold().replace("ё", "е")
        with self.geography.store.connection() as db:
            records = [
                GeoLocation.model_validate_json(r[0])
                for r in db.execute("SELECT data FROM geography")
            ]
        found = []
        for record in records:
            for point in ([record.point] if record.point else []) + record.candidates:
                house = house_match(point.label)
                # A street centroid or an unconfirmed manual candidate is not a house.
                if point.precision != "house" and not (
                    point == record.point and point.precision == "manual" and house
                ):
                    continue
                label = normalize_query(point.label).casefold().replace("ё", "е")
                raw = normalize_query(record.address).casefold().replace("ё", "е")
                score = max(
                    SequenceMatcher(None, text, label).ratio(),
                    SequenceMatcher(None, text, raw).ratio(),
                )
                if text in label or text in raw:
                    score = max(score, 0.9)
                # Known candidates are useful when opening an unresolved address unchanged.
                if original == record.address and text == normalize_query(
                    original
                ).casefold().replace("ё", "е"):
                    score = 1.0
                if score < 0.6:
                    continue
                item = AddressSuggestion(
                    id=secrets.token_hex(16),
                    label=point.label,
                    provider="catalog",
                    precision="house",
                    house=house[1] if house else "",
                    point=point,
                    notice="Из локального справочника. Подтвердите соответствие адресу.",
                )
                found.append((score, bool(record.point), item))
        found.sort(key=lambda x: (-x[0], -x[1], x[2].label))
        unique = {}
        for _, _, item in found:
            identity = (
                item.label,
                item.point.lat if item.point else None,
                item.point.lon if item.point else None,
            )
            unique.setdefault(identity, item)
        return list(unique.values())[:5]

    def search(self, request: SuggestionSearch) -> Suggestions:
        query = request.query.strip()
        if len(query) < 3:
            raise ValueError("Введите минимум три символа адреса")
        items = self._local(query, request.original_address)
        notice = "Поиск по сохранённым адресам. Расширенный поиск пока не подключён."
        automatic = None
        # An interactive fallback must not replace an accepted Photon/manual point.
        existing = self.geography.get(request.original_address or query)
        fallback = request.auto_resolve and existing.point is None
        if request.auto_resolve and existing.point is not None:
            for item in items:
                self._remember(item)
            return Suggestions(
                items=items,
                live_available=self.live_available,
                notice="Координаты уже есть в справочнике. Автоматическая замена не требуется.",
            )
        if self.live_available:
            try:
                live = []
                exact = []
                malformed = False
                results = self._call(query, 20 if fallback else 5)
                for raw in results:
                    try:
                        item = dadata_suggestion(raw)
                        if raw["data"].get("country_iso_code") != "RU" or item.precision != "house":
                            continue
                        live.append(self._remember(item, raw))
                        if fallback and same_dadata_address(query, raw, item):
                            if not request.original_address or same_dadata_address(
                                request.original_address, raw, item
                            ):
                                exact.append(item)
                    except (KeyError, TypeError, ValueError):
                        malformed = True
                        continue
                notice = (
                    "" if live else "В сервисе нет подходящих адресов. Уточните город, улицу и дом."
                )
                items = live + items
                if fallback:
                    # A full page or malformed rows can conceal another matching house.
                    if len(exact) == 1 and len(results) < 20 and not malformed:
                        automatic = self.resolve(exact[0].id, expected_address=query)
                        notice = (
                            "Адрес совпал полностью. Координаты дома подставлены автоматически."
                        )
                    else:
                        notice = "Однозначное точное совпадение не подтверждено. Выберите и проверьте адрес."
            except SuggestionError as exc:
                notice = str(exc)
        for item in items:
            if item.provider == "catalog":
                self._remember(item)
        return Suggestions(
            items=items, live_available=self.live_available, notice=notice, automatic=automatic
        )

    def resolve(self, key: str, *, expected_address: str | None = None) -> AddressSuggestion:
        with self._lock:
            entry = self._issued.get(key)
        if entry is None or entry[0] < time.monotonic():
            raise SuggestionError("Варианты устарели. Повторите поиск и выберите адрес.")
        _, item, raw = entry
        if raw is None or item.precision != "house":
            return item
        # Resolve the previously offered unrestricted_value and revalidate its identity.
        query = raw.get("unrestricted_value")
        if not isinstance(query, str) or not 3 <= len(query) <= 300:
            raise SuggestionError("Не удалось уточнить выбранный адрес. Повторите поиск.")
        results = self._call(query, 1)
        if not results:
            raise SuggestionError("Выбранный дом больше не находится. Повторите поиск.")
        try:
            resolved = dadata_suggestion(results[0])
            same = results[0]["data"].get("house_fias_id") == raw["data"].get(
                "house_fias_id"
            ) and house_token(resolved.house) == house_token(item.house)
            if not same or not resolved.point:
                raise ValueError("House changed or no precise point")
            if expected_address and not same_dadata_address(expected_address, results[0], resolved):
                raise ValueError("Address changed during automatic resolution")
        except (KeyError, TypeError, ValueError):
            raise SuggestionError(
                "Точные координаты выбранного дома не подтверждены. Укажите точку вручную."
            ) from None
        resolved.id = key
        self._remember(resolved)
        return resolved
