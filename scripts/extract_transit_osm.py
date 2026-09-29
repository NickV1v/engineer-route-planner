"""Extract public-transport relations and their complete references from a local PBF."""

import argparse
import hashlib
import json
from pathlib import Path

import osmium

MODES = {"subway", "train", "light_rail", "tram", "bus", "trolleybus"}
TYPES = {"n": "node", "w": "way", "r": "relation"}


def extract(pbf: Path, output: Path):
    if output.exists():
        raise ValueError("Файл уже существует")
    relations = {}
    wanted = set()
    for relation in osmium.FileProcessor(pbf, osmium.osm.RELATION):
        tags = dict(relation.tags)
        relations[relation.id] = {
            "type": "relation",
            "id": relation.id,
            "tags": tags,
            "members": [
                {"type": TYPES[m.type], "ref": m.ref, "role": m.role} for m in relation.members
            ],
        }
        if (
            tags.get("route") in MODES
            or tags.get("route_master") in MODES
            or tags.get("public_transport") in {"stop_area", "stop_area_group"}
        ):
            wanted.add(relation.id)
    pending = list(wanted)
    while pending:
        for member in relations.get(pending.pop(), {}).get("members", []):
            if member["type"] == "relation" and member["ref"] not in wanted:
                wanted.add(member["ref"])
                pending.append(member["ref"])
    selected = [relations[key] for key in sorted(wanted) if key in relations]
    missing_relations = sorted(wanted - relations.keys())
    del relations
    ways = {m["ref"] for r in selected for m in r["members"] if m["type"] == "way"}
    nodes = {m["ref"] for r in selected for m in r["members"] if m["type"] == "node"}
    way_data = []
    for way in osmium.FileProcessor(pbf, osmium.osm.WAY).with_filter(osmium.filter.IdFilter(ways)):
        refs = [n.ref for n in way.nodes]
        nodes.update(refs)
        way_data.append({"type": "way", "id": way.id, "tags": dict(way.tags), "nodes": refs})
    print(
        f"Отношений: {len(selected)}, путей: {len(way_data)}, запрошено узлов: {len(nodes)}",
        flush=True,
    )
    node_data = []
    for node in osmium.FileProcessor(pbf, osmium.osm.NODE).with_filter(
        osmium.filter.IdFilter(nodes)
    ):
        node_data.append(
            {
                "type": "node",
                "id": node.id,
                "tags": dict(node.tags),
                "lat": node.location.lat,
                "lon": node.location.lon,
            }
        )
    with pbf.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    data = {
        "elements": [*node_data, *way_data, *selected],
        "metadata": {
            "pbf_sha256": digest,
            "missing_relations": missing_relations,
            "missing_ways": sorted(ways - {w["id"] for w in way_data}),
            "missing_nodes": sorted(nodes - {n["id"] for n in node_data}),
            "attribution": "© OpenStreetMap contributors, ODbL",
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    print(f"Сохранено {len(node_data)} узлов: {output}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pbf", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    extract(args.pbf, args.output)
