"""Offline audit of recorded geocoding searches; never accepts points or writes a database."""

import argparse
import hashlib
import json
import math
import re
from collections import Counter
from pathlib import Path

from dispatch.geocode import auto_match, house_token

# Explicit regional translations, not a fuzzy transliteration of arbitrary place names.
PLACE_NAMES = {
    "Moscow": "Москва",
    "Moscow City": "Москва",
    "Moscow Oblast": "Московская область",
    "Domodedovo": "Домодедово",
    "Kashira": "Кашира",
    "Stupino": "Ступино",
    "Moskovskiy": "Московский",
}


def provider_house(value: str) -> str:
    """Normalize OpenAddresses' labelled parts without changing their meaning."""
    value = re.sub(r"^дом\s+", "", value.strip(), flags=re.I)
    return house_token(value.replace(",", " "))


def distance_m(first: dict, second: dict) -> float:
    lat1, lat2 = map(math.radians, (first["lat"], second["lat"]))
    dlon = math.radians(second["lon"] - first["lon"])
    a = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6_371_008.8 * 2 * math.asin(math.sqrt(min(1, max(0, a))))


def audit_candidate(address: str, result: dict) -> dict:
    """Separate text identity, provider precision and geometry consistency."""
    props = {
        "city": PLACE_NAMES.get(result.get("city"), result.get("city")),
        "state": PLACE_NAMES.get(result.get("state"), result.get("state")),
        "district": result.get("suburb"),
        "street": result.get("street"),
        "housenumber": provider_house(str(result.get("housenumber") or "")),
    }
    country_ok = str(result.get("country_code", "")).lower() == "ru"
    identity = country_ok and auto_match(address, props)
    flags = []
    if not country_ok:
        flags.append("outside_country")
    if not identity:
        flags.append("address_mismatch")
    if result.get("result_type") != "building":
        flags.append("not_building_level")
    rank = result.get("rank") or {}
    if rank.get("confidence_building_level") == 0:
        flags.append("building_position_not_found")
    elif rank.get("confidence_building_level") is None:
        flags.append("building_confidence_unknown")
    try:
        lat, lon = float(result["lat"]), float(result["lon"])
        if not (
            math.isfinite(lat) and math.isfinite(lon) and 53 <= lat <= 57.5 and 35 <= lon <= 41
        ):
            raise ValueError("outside study region")
        point = {"lat": lat, "lon": lon}
    except (KeyError, TypeError, ValueError, OverflowError):
        point = None
        flags.append("invalid_or_outside_region_point")
    bbox = result.get("bbox")
    if bbox is not None:
        try:
            west, south, east, north = [float(bbox[k]) for k in ("lon1", "lat1", "lon2", "lat2")]
            if not all(math.isfinite(v) for v in (west, south, east, north)) or not (
                -180 <= west <= east <= 180 and -90 <= south <= north <= 90
            ):
                raise ValueError("invalid bounds")
            if point and not (west <= lon <= east and south <= lat <= north):
                flags.append("point_outside_bbox")
            if distance_m({"lat": south, "lon": west}, {"lat": north, "lon": east}) > 1000:
                flags.append("bbox_larger_than_building")
        except (KeyError, TypeError, ValueError, OverflowError):
            flags.append("invalid_bbox")
    source = (result.get("datasource") or {}).get("sourcename", "unknown")
    if source != "openstreetmap":
        # In this sample the Moscow OA source is dated 2017; freshness needs a separate check.
        flags.append("source_freshness_not_verified")
    return {
        "label": result.get("formatted", ""),
        "source": source,
        "point": point,
        "components": props,
        "address_match": identity,
        "rank": rank,
        "flags": flags,
        "passes_screening": not flags,
    }


def audit_dadata_candidate(address: str, suggestion: dict) -> dict:
    data = suggestion["data"]
    number = str(data.get("house") or "")
    if data.get("block"):
        number += " " + str(data.get("block_type_full") or data.get("block_type") or "?")
        number += " " + str(data["block"])
    state = " ".join(str(data.get(k) or "") for k in ("region", "region_type_full"))
    props = {
        "city": data.get("city"),
        "state": state,
        "district": data.get("settlement_with_type"),
        "street": " ".join(str(data.get(k) or "") for k in ("street_type_full", "street")),
        "housenumber": provider_house(number),
    }
    identity = data.get("country_iso_code") == "RU" and auto_match(address, props)
    flags = []
    if not identity:
        flags.append("address_mismatch")
    if str(data.get("qc_geo")) != "0":
        flags.append("not_exact_house_coordinates")
    if str(data.get("fias_level")) not in {"8", "9"} or not data.get("house_fias_id"):
        flags.append("house_not_in_registry")
    if str(data.get("fias_actuality_state")) != "0":
        flags.append("registry_address_not_current")
    if data.get("flat") or data.get("room"):
        flags.append("unspecified_apartment")
    try:
        lat, lon = float(data["geo_lat"]), float(data["geo_lon"])
        if not (
            math.isfinite(lat) and math.isfinite(lon) and 53 <= lat <= 57.5 and 35 <= lon <= 41
        ):
            raise ValueError("outside study region")
        point = {"lat": lat, "lon": lon}
    except (KeyError, TypeError, ValueError, OverflowError):
        point = None
        flags.append("invalid_or_outside_region_point")
    return {
        "label": suggestion["value"],
        "source": "DaData",
        "point": point,
        "components": props,
        # Property type (дом/владение) remains visible for review even when the number matches.
        "house_type": data.get("house_type_full"),
        "house_fias_id": data.get("house_fias_id"),
        "address_match": identity,
        "rank": {k: data.get(k) for k in ("qc_geo", "fias_level", "fias_actuality_state")},
        "flags": flags,
        "passes_screening": not flags,
    }


def audit(baseline: list[dict], searches: list[dict], provider: str = "geoapify") -> dict:
    adapters = {
        "geoapify": ("results", audit_candidate),
        "dadata": ("suggestions", audit_dadata_candidate),
    }
    results_key, adapter = adapters[provider]
    references = {row["location_id"]: row for row in baseline}
    if len(references) != len(baseline):
        raise ValueError("Duplicate baseline location_id")
    seen, rows = set(), []
    for search in searches:
        key = search["location_id"]
        if key in seen:
            raise ValueError("Duplicate search location_id; variants must be reported separately")
        seen.add(key)
        reference = references[key]
        if search["address"] != reference["address"]:
            raise ValueError("Search address differs from baseline")
        candidates = [adapter(search["address"], r) for r in search["payload"][results_key]]
        screened = [c for c in candidates if c["passes_screening"]]
        distinct = {(c["point"]["lat"], c["point"]["lon"]) for c in screened}
        reference_point = reference.get("point")
        distances = (
            [distance_m(c["point"], reference_point) for c in screened] if reference_point else []
        )
        rows.append(
            {
                "location_id": key,
                "address": search["address"],
                "baseline_status": reference["status"],
                "candidate_count": len(candidates),
                "exact_candidate_count": sum(c["address_match"] for c in candidates),
                "screened_distinct_points": len(distinct),
                "top_candidate_matches": bool(candidates and candidates[0]["address_match"]),
                "nearest_reference_distance_m": round(min(distances), 1) if distances else None,
                "candidates": candidates,
            }
        )

    def summarize(group: list[dict]) -> dict:
        return {
            "attempted": len(group),
            "any_candidate": sum(bool(r["candidate_count"]) for r in group),
            "exact_address_candidate": sum(bool(r["exact_candidate_count"]) for r in group),
            "screened_candidate": sum(bool(r["screened_distinct_points"]) for r in group),
            "one_screened_point": sum(r["screened_distinct_points"] == 1 for r in group),
            "top_candidate_matches": sum(r["top_candidate_matches"] for r in group),
            "nearest_over_100m": sum(
                r["nearest_reference_distance_m"] is not None
                and r["nearest_reference_distance_m"] > 100
                for r in group
            ),
        }

    return {
        "schema": "geocode-audit-v1",
        "provider": provider,
        "warning": "Screening is not ground truth or permission to accept a point. Preserve conflicts from other sources.",
        "overall": summarize(rows),
        "unresolved": summarize([r for r in rows if r["baseline_status"] in {"missing", "review"}]),
        "accepted_control": summarize(
            [r for r in rows if r["baseline_status"] in {"matched", "manual"}]
        ),
        "candidate_flags": dict(
            Counter(f for r in rows for c in r["candidates"] for f in c["flags"])
        ),
        "rows": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, type=Path)
    provider_args = parser.add_mutually_exclusive_group(required=True)
    provider_args.add_argument("--geoapify", type=Path)
    provider_args.add_argument("--dadata", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    provider = "geoapify" if args.geoapify else "dadata"
    search_path = args.geoapify or args.dadata
    if args.output.resolve() in {args.baseline.resolve(), search_path.resolve()}:
        parser.error("Output must not overwrite an input file")
    baseline, searches = args.baseline.read_bytes(), search_path.read_bytes()
    report = audit(json.loads(baseline), json.loads(searches), provider)
    report["inputs_sha256"] = {
        "baseline": hashlib.sha256(baseline).hexdigest(),
        provider: hashlib.sha256(searches).hexdigest(),
    }
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps({k: v for k, v in report.items() if k != "rows"}, ensure_ascii=False, indent=2)
    )


if __name__ == "__main__":
    main()
