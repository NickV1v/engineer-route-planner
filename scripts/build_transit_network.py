"""Build a checked local OSM transit network and actual walking connections."""

import argparse
import json
from collections import Counter
from pathlib import Path

from dispatch.routing.import_osm import MODEL, NetworkBuilder


def build(rail_file, surface_file, output):
    if output.exists():
        raise ValueError("Каталог результата уже существует")
    builder = NetworkBuilder()
    rail = json.loads(rail_file.read_text())
    builder.rail(rail)
    print(f"Рельсовых вариантов: {len(builder.patterns)}", flush=True)
    surface = json.loads(surface_file.read_text())
    if rail["metadata"]["pbf_sha256"] != surface["metadata"]["pbf_sha256"]:
        raise ValueError("Рельсы и автобусы взяты из разных снимков")
    builder.surface_routes(surface)
    network = builder.finish(
        {
            "pbf_sha256": rail["metadata"]["pbf_sha256"],
            "validator_revision": rail["validator_revision"],
            "cities_sha256": rail["cities_sha256"],
        }
    )
    report = {
        "model": MODEL,
        "patterns": dict(Counter(p.mode for p in network.patterns)),
        "surface_nodes": len(network.surface),
        "missing_entrances": [
            {"id": key, "name": s["name"]} for key, s in rail["stops"].items() if not s["entrances"]
        ],
        "rejected": builder.rejected,
        "rejected_counts": dict(Counter(r["reason"] for r in builder.rejected)),
    }
    output.mkdir(parents=True)
    (output / "network.json").write_text(network.model_dump_json())
    (output / "audit.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(
        json.dumps(
            {k: v for k, v in report.items() if k not in {"model", "rejected"}}, ensure_ascii=False
        ),
        flush=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("rail", "surface", "output"):
        parser.add_argument(name, type=Path)
    args = parser.parse_args()
    build(args.rail, args.surface, args.output)
