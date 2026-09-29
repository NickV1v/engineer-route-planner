"""Add street transfers using the same local pedestrian router as address access."""

import argparse
import json
import time
from pathlib import Path

from dispatch.routing.cache import RoutingCache, pair_key
from dispatch.routing.contracts import Leg, content_hash
from dispatch.routing.public import PUBLIC_MODEL
from dispatch.routing.roads import RoadRouter
from dispatch.routing.transit import TransitNetwork
from dispatch.routing.walking import ACCESS_MODEL, SpatialIndex, door_journey


def connect(network_file, graph, cache_file, output):
    if output.exists():
        raise ValueError("Каталог результата уже существует")
    network = TransitNetwork.model_validate_json(network_file.read_bytes())
    source_hash = content_hash(network.model_dump())
    roads, cache = RoadRouter(graph), RoutingCache(cache_file)
    street_hash = content_hash({"road_network": roads.network_id, "access": ACCESS_MODEL})
    index = SpatialIndex({key: network.nodes[key] for key in network.surface})
    pairs = set()
    for origin in sorted(network.surface):
        for target in index.nearby(
            network.nodes[origin],
            PUBLIC_MODEL["transfer_m"],
            PUBLIC_MODEL["transfer_candidates"] + 1,
        ):
            if origin != target:
                pairs.add((origin, target))
                pairs.add((target, origin))
    print(f"Проверяем {len(pairs)} направленных переходов", flush=True)
    counts = {"ok": 0, "too_long": 0, "unreachable": 0}
    started = time.monotonic()
    for i, (origin, target) in enumerate(sorted(pairs)):
        a, b = network.nodes[origin], network.nodes[target]
        key = pair_key(street_hash, "walk", origin, target, a, b)
        journey = cache.journey(key)
        if journey is None:
            journey = door_journey(roads, "walk", origin, target, a, b)
            cache.save_journey(key, journey)
        if journey.status != "ok":
            counts["unreachable"] += 1
        elif journey.distance_m > PUBLIC_MODEL["transfer_m"]:
            counts["too_long"] += 1
        else:
            counts["ok"] += 1
            network.walking.append(
                Leg(
                    mode="walk",
                    from_id=origin,
                    to_id=target,
                    from_label=network.labels[origin],
                    to_label=network.labels[target],
                    duration_s=max(1, journey.duration_s),
                    distance_m=journey.distance_m,
                    geometry=[p for leg in journey.legs for p in leg.geometry],
                    quality="estimated",
                    geometry_quality="estimated"
                    if any(
                        leg.geometry_quality == "estimated" and leg.distance_m
                        for leg in journey.legs
                    )
                    else "network",
                )
            )
        if (i + 1) % 1000 == 0:
            print(
                f"{i + 1}/{len(pairs)} · {time.monotonic() - started:.0f} с · {counts}", flush=True
            )
    network.source = "connected:" + content_hash(
        {"transit": source_hash, "roads": street_hash, "model": PUBLIC_MODEL}
    )
    network.assumptions += [
        "Уличные пересадки: до 6 ближайших площадок/входов в радиусе 600 м; пеший путь проверен по дорожной сети."
    ]
    network = TransitNetwork.model_validate(network.model_dump())
    output.mkdir(parents=True)
    (output / "network.json").write_text(network.model_dump_json())
    (output / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "connected_transit_v1",
                "network_hash": content_hash(network.model_dump()),
                "road_network": street_hash,
                "source_network": source_hash,
                "transfers": counts,
                "model": PUBLIC_MODEL,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    print(json.dumps(counts), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("network", "graph", "cache", "output"):
        parser.add_argument(name, type=Path)
    args = parser.parse_args()
    connect(args.network, args.graph, args.cache, args.output)
