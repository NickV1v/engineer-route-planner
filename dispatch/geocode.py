"""Explicit, cached preparation of synthetic addresses through a Photon-compatible service."""

import argparse
import hashlib
import json
import math
import re
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from dispatch.geography import GeographyStore
from dispatch.importer import import_sources, location_id, read_policy
from dispatch.models import GeoLocation, GeoPoint
from dispatch.store import PlanStore, VersionConflict


class GeocodingError(RuntimeError):
    pass


PIPELINE_PREFIX = "photon-search-v1:"
STREET_TYPES = {
    "улица",
    "проспект",
    "бульвар",
    "проезд",
    "переулок",
    "шоссе",
    "набережная",
    "площадь",
    "квартал",
}


def house_match(address: str):
    """Retain the complete house identifier; a slash is never a corpus separator."""
    return re.search(r"\bд(?:ом)?\.?\s*((?:\d|к\s*\d)[^,;]*)$", address, re.I) or re.search(
        r",\s*((?:\d|к\s*\d)[^,;]*)$", address, re.I
    )


def normalize_query(address: str) -> str:
    value = re.sub(r"\bг\.?\s*Город\s+", "", address, flags=re.I)
    value = re.sub(r"\b(?:г\.\s*|г\s+|город\s+)", "", value, flags=re.I)
    value = re.sub(r"^МО[.,\s]+", "Московская область, ", value, flags=re.I)
    value = re.sub(r"\bобл\.\s*(?=Московская область)", "", value, flags=re.I)
    value = re.sub(r"\bул(?:\.\s*|\s+)", "улица ", value, flags=re.I)
    value = re.sub(r"\bпр-?к?т(?:\.\s*|\s+)", "проспект ", value, flags=re.I)
    value = re.sub(r"\b(?:пр-зд|пр-д|прзд)(?:\.\s*|\s+)", "проезд ", value, flags=re.I)
    value = re.sub(r"\bпер(?:\.\s*|\s+)", "переулок ", value, flags=re.I)
    value = re.sub(r"\bб-р\.?\s*", "бульвар ", value, flags=re.I)
    for short, full in (("наб", "набережная"), ("пл", "площадь"), ("ш", "шоссе")):
        value = re.sub(rf"\b{short}(?:\.\s*|\s+)", full + " ", value, flags=re.I)
    value = re.sub(r"\b(проезд|улица|переулок|проспект|бульвар)\.\s*", r"\1 ", value, flags=re.I)
    house = house_match(value)
    if house:
        # Photon distinguishes `10 к2` from `10к2` and from `10 к 2`.
        number = re.sub(r"(?<=[\dа-яa-z])([кс])(?=\d)", r" \1", house_token(house[1]))
        value = value[: house.start()].rstrip(" ,") + ", " + number
    return re.sub(r"\s+", " ", value).strip(" ,")


def house_token(value: str) -> str:
    value = re.sub(r"(?:корпус|корп)\.?\s*", "к", value.lower())
    value = re.sub(r"(?:строение|стр)\.?\s*", "с", value)
    return re.sub(r"[\s.]", "", value)


def address_words(value: str | None) -> list[str]:
    value = (value or "").lower().replace("ё", "е")
    # Calendar street names use both `8 Марта` and `8-го Марта`.
    value = re.sub(
        r"\b(\d+)-(?:го|е)(?=\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)\b)",
        r"\1",
        value,
    )
    return [w for w in re.findall(r"\d+-[йяе]|\d+|[а-яa-z]+", value) if w not in STREET_TYPES]


def canonical_words(words: list[str]) -> tuple:
    # Only ordinal placement is flexible; other words and digits retain their order.
    ordinal = [w.split("-")[0] for w in words if re.fullmatch(r"\d+-[йяе]", w)]
    return [w for w in words if not re.fullmatch(r"\d+-[йяе]", w)], sorted(ordinal)


def query_context(address: str, street: str) -> list[str]:
    query = normalize_query(address)
    house = house_match(query)
    words = address_words(query[: house.start()] if house else query)
    return words[: -len(address_words(street))]


def auto_match(address: str, properties: dict) -> bool:
    number = properties.get("housenumber", "")
    street = properties.get("street", "")
    city = properties.get("city", "")
    if not number or not street or not city:
        return False
    query = normalize_query(address).lower().replace("ё", "е")
    query_house = house_match(query)
    query_words = address_words(query[: query_house.start()] if query_house else query)
    street_words = address_words(street)
    context = query_words[: -len(street_words)] if street_words else []
    city_words = address_words(city)
    state_words = address_words(properties.get("state", ""))
    district_words = address_words(properties.get("district", ""))
    contexts = [city_words, state_words + city_words]
    contexts += [c + district_words for c in contexts]
    # Match the entire street suffix, never a name nested inside another street.
    if (
        not street_words
        or canonical_words(query_words[-len(street_words) :]) != canonical_words(street_words)
        or context not in contexts
    ):
        return False
    query_types = set(re.findall(r"[а-я]+", query)) & STREET_TYPES
    result_types = set(re.findall(r"[а-я]+", street.lower())) & STREET_TYPES
    if query_types and result_types and query_types != result_types:
        return False
    # House numbers must match completely: 12 is not 12к1 or 120.
    house = house_match(address)
    return house is not None and house_token(house[1]) == house_token(str(number))


def fallback_queries(address: str, payload: dict) -> list[tuple[str, dict]]:
    """Use provider spellings only after validating city/street against the input."""
    house = house_match(address)
    if not house:
        return []
    number = house_token(house[1])
    names = {}
    for feature in payload["features"][:20]:
        props = feature["properties"]
        # A street result exposes its own name, while an address exposes `street`.
        if not props.get("street") and (
            props.get("type") == "street" or props.get("osm_key") == "highway"
        ):
            props = {**props, "street": props.get("name")}
        if str(props.get("countrycode", "")).lower() == "ru" and auto_match(
            address, {**props, "housenumber": number}
        ):
            names.setdefault((props["city"], props["street"]), props)
    # Several street interpretations require review, not a randomly chosen spelling.
    if len(names) != 1:
        return []
    city, street = next(iter(names))
    props = names[city, street]
    context = query_context(address, street)
    city_words = address_words(city)
    state_words = address_words(props.get("state", ""))
    # Reconstruct only fully understood context; no locality/district gets dropped.
    if context not in (city_words, state_words + city_words):
        return []
    common = {"limit": 10, "bbox": "35,53,41,57.5", "countrycode": "RU"}
    state = props.get("state") if context != city_words else None
    if state:
        common["state"] = state
    queries = []
    # The public server errors on slash/space house expressions in /structured.
    if re.fullmatch(r"\d+[а-яa-z]?", number):
        queries.append(
            ("structured", {**common, "city": city, "street": street, "housenumber": number})
        )
    formatted = re.sub(r"(?<=[\dа-яa-z])([кс])(?=\d)", r" \1", number)
    # Keep the full compound number; layer=house keeps streets out of the top results.
    queries.append(
        (
            "api/",
            {
                **{k: v for k, v in common.items() if k != "state"},
                "q": ", ".join(p for p in (state, city, street, formatted) if p),
                "lang": "default",
                "layer": "house",
            },
        )
    )
    return queries


def house_rank(address: str, props: dict) -> tuple:
    """Rank review suggestions by unchanged street and base house, never auto-correct."""
    house = house_match(address)
    number = str(props.get("housenumber") or "")
    same_street = bool(house) and auto_match(address, {**props, "housenumber": house[1]})
    original_base = re.match(r"\d+", house_token(house[1])) if house else None
    other_base = re.match(r"\d+", house_token(number))
    same_base = original_base and other_base and original_base[0] == other_base[0]
    return (not same_street, not same_base, not bool(number), props.get("osm_key") != "building")


def near_building(point: GeoPoint, building: GeoPoint, properties: dict) -> bool:
    """Accept nearby POIs only within the building's extent (+20 m), or 50 m without one."""
    lat_scale = 111_320
    lon_scale = lat_scale * math.cos(math.radians(building.lat))
    extent = properties.get("extent")
    if extent is not None:
        if not isinstance(extent, list) or len(extent) != 4:
            return False
        if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in extent):
            return False
        west, north, east, south = extent
        if not (west <= building.lon <= east and south <= building.lat <= north):
            return False
        # An implausibly large building extent must not merge distant address matches.
        if math.hypot((east - west) * lon_scale, (north - south) * lat_scale) > 1000:
            return False
        return (
            west - 20 / lon_scale <= point.lon <= east + 20 / lon_scale
            and south - 20 / lat_scale <= point.lat <= north + 20 / lat_scale
        )
    return (
        math.hypot((point.lon - building.lon) * lon_scale, (point.lat - building.lat) * lat_scale)
        <= 50
    )


def choose_match(matches: list[tuple[GeoPoint, dict]]) -> GeoPoint | None:
    if len(matches) == 1:
        return matches[0][0]
    buildings = [(p, props) for p, props in matches if props.get("osm_key") == "building"]
    if len(buildings) == 1:
        building, props = buildings[0]
        if all(near_building(p, building, props) for p, _ in matches):
            return building
    return None


def parse_photon(address: str, payload: dict, query: str) -> GeoLocation:
    if not isinstance(payload, dict) or not isinstance(payload.get("features"), list):
        raise GeocodingError("Геокодер вернул некорректный ответ")
    candidates, matches, properties = {}, {}, {}
    for feature in payload["features"][:60]:
        try:
            props = feature["properties"]
            geometry = feature["geometry"]
            if geometry.get("type") != "Point" or str(props.get("countrycode", "")).lower() != "ru":
                continue
            lon, lat = geometry["coordinates"]
            # The source dataset covers Moscow and its wider southern region.
            if not 35 <= lon <= 41 or not 53 <= lat <= 57.5:
                continue
            key = (round(lat, 6), round(lon, 6))
            house, street = props.get("housenumber"), props.get("street")
            label = ", ".join(
                str(props[k])
                for k in ("city", "district", "street", "housenumber", "name")
                if props.get(k)
            )
            point = GeoPoint(
                lat=lat,
                lon=lon,
                label=label or query,
                precision="house"
                if house and street
                else "street"
                if street
                or props.get("osm_value") in {"residential", "primary", "secondary", "tertiary"}
                else "locality",
                source=f"OpenStreetMap:{props.get('osm_type', '')}{props.get('osm_id', '')}",
            )
            candidates.setdefault(key, point)
            properties.setdefault(key, props)
            if auto_match(address, props):
                # A POI at the same coordinates must not hide the exact building match.
                if key not in matches or props.get("osm_key") == "building":
                    matches[key] = (point, props)
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise GeocodingError("Геокодер вернул повреждённую точку") from exc
    exact = list(matches.values())
    selected = choose_match(exact)
    # Show exact address candidates first, even when the provider ranked them below POIs.
    ordered = {
        **{k: p for k, (p, _) in matches.items()},
        **{
            k: candidates[k]
            for k in sorted(candidates, key=lambda k: house_rank(address, properties[k]))
            if k not in matches
        },
    }
    points = list(ordered.values())
    note = (
        "Выбрано здание среди объектов с тем же адресом"
        if selected and len(exact) > 1
        else "Совпали населённый пункт, улица и полный номер дома"
        if selected
        else "Найдены разные точки с одинаковым адресом; проверьте нужное здание"
        if exact
        else "Есть варианты, но населённый пункт, улица или полный номер дома не совпали"
        if points
        else "По этому запросу не получено подходящих координат"
    )
    if not exact and (house := house_match(address)):
        alternatives = list(
            dict.fromkeys(
                str(properties[k]["housenumber"])
                for k in ordered
                if not house_rank(address, properties[k])[0] and properties[k].get("housenumber")
            )
        )[:3]
        if alternatives:
            note = (
                f"В заявке дом «{house[1]}»; на карте варианты: {', '.join(alternatives)}. "
                "Полный номер не совпал — подтвердите дом и корпус."
            )[:500]
    return GeoLocation(
        location_id=location_id(address),
        address=address,
        query=query,
        status="matched" if selected else "review" if points else "missing",
        point=selected,
        candidates=([selected] + [c for c in points if c != selected])[:5]
        if selected
        else points[:5],
        note=note,
    )


class PhotonClient:
    def __init__(self, catalog: GeographyStore, url: str):
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Укажите URL сервиса без ключей, параметров и паролей")
        self.catalog = catalog
        self.base_url = url.rstrip("/") + "/"
        self.url = self.base_url + "api/"
        self.last_request = 0.0

    def lookup(
        self,
        address: str,
        *,
        cached_only: bool = False,
        query: str | None = None,
        expand: bool = False,
    ) -> GeoLocation:
        query = query or normalize_query(address)
        if query.startswith(PIPELINE_PREFIX):
            saved = self.catalog.cache_get(query)
            if not saved or saved.get("address") != address or saved.get("url") != self.url:
                raise GeocodingError("Для повторной проверки нет сохранённых вариантов поиска")
            return parse_photon(address, saved["payload"], query)
        params = {
            "q": query,
            "limit": 5,
            "lang": "default",
            "lat": 55.6,
            "lon": 37.6,
            "bbox": "35,53,41,57.5",
        }
        payload = self.fetch(address, self.url, params, cached_only=cached_only)
        result = parse_photon(address, payload, query)
        if not expand or result.status == "matched":
            return result
        queries = fallback_queries(address, payload)
        if not queries:
            return result
        features = list(payload["features"][:20])
        trace = [{"url": self.url, "params": params}]
        for endpoint, extra_params in queries:
            url = self.base_url + endpoint
            extra = self.fetch(address, url, extra_params, cached_only=cached_only)
            features.extend(extra["features"][:20])
            trace.append({"url": url, "params": extra_params})
        # Repeated responses may describe one object with slightly different centroids.
        # Retain conflicts and let the existing building-extent rule resolve nearby POIs.
        combined = {"features": features}
        saved = {"address": address, "url": self.url, "requests": trace, "payload": combined}
        key = (
            PIPELINE_PREFIX + hashlib.sha256(json.dumps(saved, sort_keys=True).encode()).hexdigest()
        )
        self.catalog.cache_put(key, saved)
        return parse_photon(address, combined, key)

    def reverse(self, point: GeoPoint) -> str | None:
        """A nearby address for display only; never move the supplied coordinate."""
        query = f"Координаты {point.lat}, {point.lon}"
        payload = self.fetch(
            query,
            self.base_url + "reverse",
            {"lat": point.lat, "lon": point.lon, "radius": 0.1, "limit": 5, "lang": "default"},
            cached_only=False,
        )
        candidates = parse_photon(query, payload, query).candidates
        nearby = []
        for candidate in candidates:
            distance = math.hypot(
                (candidate.lat - point.lat) * 111_320,
                (candidate.lon - point.lon) * 111_320 * math.cos(math.radians(point.lat)),
            )
            if candidate.precision in {"house", "street"} and distance <= 100:
                nearby.append((candidate.precision != "house", distance, candidate.label))
        return min(nearby)[2] if nearby else None

    def fetch(self, address: str, url: str, params: dict, *, cached_only: bool) -> dict:
        key = hashlib.sha256(json.dumps([url, params], sort_keys=True).encode()).hexdigest()
        payload = self.catalog.cache_get(key)
        if payload is None:
            if cached_only:
                raise GeocodingError("Для повторной проверки нет сохранённого ответа")
            time.sleep(max(0, 1.1 - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            request = Request(
                url + "?" + urlencode(params),
                headers={
                    "User-Agent": "VyezdDispatchPrototype/0.5 (one-time synthetic-address preparation)",
                    "Accept": "application/json",
                },
            )
            try:
                with urlopen(request, timeout=15) as response:
                    raw = response.read(1_000_001)
                if len(raw) > 1_000_000:
                    raise GeocodingError("Слишком большой ответ геокодера")
                payload = json.loads(raw)
                parse_photon(address, payload, str(params))
            except (HTTPError, URLError, TimeoutError, ValueError) as exc:
                raise GeocodingError(
                    "Сервис координат недоступен; подготовка остановлена, сохранённые данные не изменены"
                ) from exc
            self.catalog.cache_put(key, payload)
        else:
            parse_photon(address, payload, str(params))
        return payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url", required=True, help="Explicitly selected Photon-compatible service URL"
    )
    parser.add_argument("--database", type=Path, default=Path("artifacts/dispatch.sqlite"))
    parser.add_argument("--zone", choices=["Восток", "Юго-восток", "Югоцентр"])
    parser.add_argument("--limit", type=int, default=250)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--recheck-cache",
        action="store_true",
        help="Reclassify saved responses without network calls; retain manual edits",
    )
    mode.add_argument(
        "--retry-unresolved",
        action="store_true",
        help="Retry unresolved addresses using corrected queries; retain matched and manual coordinates",
    )
    mode.add_argument(
        "--improve-unresolved",
        action="store_true",
        help="Try at most two extra searches, preserve house identity and merge all candidates",
    )
    args = parser.parse_args()
    if not 1 <= args.limit <= 250:
        parser.error("limit must be between 1 and 250")
    root = Path(__file__).resolve().parents[1]
    catalog = GeographyStore(PlanStore(args.database))
    client = PhotonClient(catalog, args.url)
    items = import_sources(
        root / "Обезличивание.zip",
        root / "Нормативы.xlsx",
        read_policy(root / "config/scenario.json"),
    )
    addresses = list(
        dict.fromkeys(
            a
            for item in items
            if not args.zone or item["id"] == args.zone
            for a in [item["office_address"], *[r.address for r in item["requests"]]]
        )
    )
    processed = 0
    try:
        for address in addresses:
            current = catalog.get(address)
            if current.status == "manual":
                continue
            retry = args.retry_unresolved or args.improve_unresolved
            if retry and current.status == "matched":
                continue
            if current.revision and not (args.recheck_cache or retry):
                continue
            if args.recheck_cache and not current.revision:
                continue
            record = client.lookup(
                address,
                cached_only=args.recheck_cache,
                query=current.query if args.recheck_cache else None,
                expand=args.improve_unresolved,
            )
            try:
                catalog.save(record, current.revision)
            except VersionConflict:
                print(
                    json.dumps(
                        {"status": "concurrent_edit_preserved", "address": address},
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                continue
            processed += 1
            print(
                json.dumps(
                    {"processed": processed, "status": record.status, "address": address},
                    ensure_ascii=False,
                ),
                flush=True,
            )
            if processed >= args.limit:
                break
    except GeocodingError as exc:
        parser.exit(1, str(exc) + "\n")
    print(json.dumps({"saved": processed, "unique_addresses": len(addresses)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
