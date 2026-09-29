"""Validate a local OSM extract with a pinned, separately downloaded subways tool.

This exports identifiers and geometry, not upstream travel-time assumptions.
The importer rejects incomplete track geometries and unlinked entrances.
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path

UPSTREAM_REVISION = "2329b6aa8be4864d2dfa10073c7fd0756e83bb03"


def export(source: Path, cities_file: Path, upstream: Path, output: Path):
    if output.exists():
        raise ValueError("Каталог результата уже существует")
    if upstream.name != "subways-" + UPSTREAM_REVISION:
        raise ValueError("Нужна закреплённая ревизия валидатора, указанная в инструкции")
    sys.path.insert(0, str(upstream.resolve()))
    from subways.osm_element import el_center
    from subways.structure.city import find_transfers
    from subways.validation import (
        add_osm_elements_to_cities,
        calculate_centers,
        prepare_cities,
        validate_cities,
    )

    data = json.loads(source.read_text())
    # The upstream rail validator expects an Overpass rail extract, not bus
    # route masters. Keep all infrastructure but exclude other route relations.
    elements = [
        e
        for e in data["elements"]
        if e["type"] != "relation"
        or (not e.get("tags", {}).get("route") and not e.get("tags", {}).get("route_master"))
        or e.get("tags", {}).get("route", e.get("tags", {}).get("route_master"))
        in {"subway", "train", "light_rail"}
    ]
    cities = [c for c in prepare_cities(cities_file.resolve().as_uri()) if c.name == "Moscow"]
    if len(cities) != 1:
        raise ValueError("Нет единственной записи Moscow в справочнике")
    calculate_centers(elements)
    add_osm_elements_to_cities(elements, cities)
    good = validate_cities(cities)
    output.mkdir(parents=True)
    (output / "validation.json").write_text(
        json.dumps([c.get_validation_result() for c in cities], ensure_ascii=False, indent=2)
    )
    if not good:
        raise ValueError("Сеть метро не прошла структурную проверку; см. validation.json")
    city = good[0]
    stops, routes = {}, []
    for master in city:
        for route in master:
            routes.append(
                {
                    "id": route.id,
                    "line": master.ref,
                    "name": master.name,
                    "mode": master.mode,
                    "circular": route.is_circular,
                    "tracks_complete": route.are_tracks_complete(),
                    "geometry": route.get_tracks_geometry(),
                    "stops": [{"id": s.stoparea.id, "position": s.stop} for s in route.stops],
                }
            )
            for rs in route.stops:
                area = rs.stoparea
                # Do not inherit the validator's proximity fallback for stations
                # lacking a stop_area. Only explicit OSM relation membership counts.
                explicit = {m["type"][0] + str(m["ref"]) for m in area.element.get("members", [])}
                entrances = []
                for key in sorted((area.entrances | area.exits) & explicit):
                    element = city.elements[key]
                    tags = element.get("tags", {})
                    if tags.get("access") in {"no", "private"} or tags.get("entrance") == "no":
                        continue
                    entrances.append(
                        {
                            "id": key,
                            "position": el_center(element),
                            "name": tags.get("ref") or tags.get("name") or "Вход/выход",
                            "entry": key in area.entrances,
                            "exit": key in area.exits,
                        }
                    )
                stops[area.id] = {
                    "name": area.station.name,
                    "position": area.center,
                    "entrances": entrances,
                }
    result = {
        "schema_version": "rail_osm_v1",
        "metadata": data["metadata"],
        "validator_revision": UPSTREAM_REVISION,
        "cities_sha256": hashlib.sha256(cities_file.read_bytes()).hexdigest(),
        "stops": stops,
        "routes": routes,
        "transfers": [sorted(group) for group in find_transfers(elements, good)],
    }
    (output / "rail.json").write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    print(
        json.dumps(
            {
                "routes": len(routes),
                "stop_areas": len(stops),
                "incomplete": [r["id"] for r in routes if not r["tracks_complete"]],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "cities", "upstream", "output"):
        parser.add_argument(name, type=Path)
    args = parser.parse_args()
    export(args.source, args.cities, args.upstream, args.output)
