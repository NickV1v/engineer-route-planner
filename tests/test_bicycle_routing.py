import copy
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from dispatch.routing.batch import RoadBatch
from dispatch.routing.bicycle import dismount_tags, prepare_dismount_pbf
from dispatch.routing.contracts import Coordinate, RoutingError
from dispatch.routing.roads import RoadRouter, parse_route
from dispatch.routing.walking import door_journey


@pytest.mark.parametrize("highway", ["footway", "pedestrian"])
def test_public_footpaths_allow_pushing_without_mutating_input(highway):
    tags = {"highway": highway, "footway": "crossing", "surface": "asphalt"}
    assert dismount_tags(tags) == {**tags, "bicycle": "dismount"}
    assert "bicycle" not in tags


@pytest.mark.parametrize(
    "rule",
    [
        {"bicycle": "no"},
        {"bicycle": "yes"},
        {"bicycle": "dismount"},
        {"bicycle:forward": "no"},
        {"bicycle:conditional": "no @ (08:00-10:00)"},
        {"access": "private"},
        {"access": "no"},
        {"foot": "no"},
        {"foot": "private"},
        {"vehicle": "no"},
        {"access:conditional": "yes @ (08:00-20:00)"},
        {"foot:forward": "no"},
        {"indoor": "yes"},
        {"conveying": "forward"},
        {"construction": "yes"},
        {"opening_hours": "08:00-20:00"},
        {"highway": "motorway"},
        {"highway": "steps"},
        {"highway": "path"},
    ],
)
def test_dismount_policy_preserves_explicit_rules_and_other_ways(rule):
    tags = {"highway": "footway", **rule}
    assert dismount_tags(tags) == tags


def test_pbf_preparation_keeps_nodes_restrictions_and_metadata(tmp_path):
    osmium = pytest.importorskip("osmium")
    source, target = tmp_path / "source.osm.pbf", tmp_path / "prepared.osm.pbf"
    with osmium.SimpleWriter(str(source)) as writer:
        for i in range(1, 4):
            writer.add_node(
                osmium.osm.mutable.Node(
                    id=i,
                    version=3,
                    location=(37.6 + i * 0.001, 55.7),
                    tags={"barrier": "gate", "bicycle": "no"} if i == 2 else {},
                )
            )
        writer.add_way(
            osmium.osm.mutable.Way(id=1, version=2, nodes=[1, 2], tags={"highway": "footway"})
        )
        writer.add_way(
            osmium.osm.mutable.Way(
                id=2, version=4, nodes=[2, 3], tags={"highway": "residential", "oneway": "yes"}
            )
        )
        writer.add_relation(
            osmium.osm.mutable.Relation(
                id=1,
                version=5,
                members=[("w", 1, "from"), ("n", 2, "via"), ("w", 2, "to")],
                tags={"type": "restriction", "restriction": "no_right_turn"},
            )
        )

    def read(path):
        result = []
        for obj in osmium.FileProcessor(str(path)):
            values = {"id": obj.id, "version": obj.version, "tags": dict(obj.tags)}
            if isinstance(obj, osmium.osm.Node):
                values["location"] = (obj.location.lon, obj.location.lat)
            elif isinstance(obj, osmium.osm.Way):
                values["nodes"] = [n.ref for n in obj.nodes]
            else:
                values["members"] = [(m.type, m.ref, m.role) for m in obj.members]
            result.append(values)
        return result

    original = read(source)
    assert prepare_dismount_pbf(source, target) == 1
    expected = copy.deepcopy(original)
    expected[3]["tags"]["bicycle"] = "dismount"
    assert read(target) == expected
    assert read(source) == original
    with pytest.raises((OSError, RuntimeError)):
        prepare_dismount_pbf(source, target)
    assert read(target) == expected


def bicycle_response():
    return {
        "trip": {
            "status": 0,
            "units": "kilometers",
            "legs": [
                {
                    "summary": {"time": 31.2, "length": 0.100003},
                    "shape": {
                        "type": "LineString",
                        "coordinates": [[37.6 + i * 0.0001, 55.7] for i in range(5)],
                    },
                    "maneuvers": [
                        dict(
                            begin_shape_index=a,
                            end_shape_index=b,
                            time=t,
                            length=d,
                            travel_mode=mode,
                        )
                        for a, b, t, d, mode in [
                            (0, 1, 5.2, 0.015, "bicycle"),
                            (1, 2, 5.2, 0.015001, "bicycle"),
                            (2, 3, 12.4, 0.050001, "pedestrian"),
                            (3, 4, 8.4, 0.020001, "bicycle"),
                            (4, 4, 0, 0, "bicycle"),
                        ]
                    ],
                }
            ],
        }
    }


def test_dismount_boundaries_keep_totals_geometry_and_door_access():
    result = parse_route(bicycle_response(), "bicycle", "a", "b", "recorded")
    assert [leg.mode for leg in result.legs] == ["bicycle", "walk", "bicycle"]
    assert [len(leg.geometry) for leg in result.legs] == [3, 2, 2]
    assert result.duration_s == 32 and result.distance_m == 101
    assert result.report()["walking_s"] == 13
    assert all(a.geometry[-1] == b.geometry[0] for a, b in zip(result.legs, result.legs[1:]))
    original = result.model_dump()
    roads = SimpleNamespace(network_id="graph", route=lambda *args: result)
    wrapped = door_journey(
        roads, "bicycle", "a", "b", result.legs[0].geometry[0], result.legs[-1].geometry[-1]
    )
    assert [leg.mode for leg in wrapped.legs] == ["walk", "bicycle", "walk", "bicycle", "walk"]
    assert wrapped.duration_s == 32 and wrapped.distance_m == 101
    assert result.model_dump() == original


@pytest.mark.parametrize(
    "damage", ["gap", "overlap", "mode", "missing", "time", "distance", "bool"]
)
def test_corrupt_bicycle_maneuvers_fail_closed(damage):
    data = bicycle_response()
    raw = data["trip"]["legs"][0]
    m = raw["maneuvers"][2]
    if damage == "missing":
        del raw["maneuvers"]
    else:
        key, value = {
            "gap": ("begin_shape_index", 3),
            "overlap": ("begin_shape_index", 1),
            "mode": ("travel_mode", "drive"),
            "time": ("time", 100),
            "distance": ("length", 1),
            "bool": ("end_shape_index", True),
        }[damage]
        m[key] = value
    with pytest.raises(RoutingError):
        parse_route(data, "bicycle", "a", "b", "recorded")


def test_recorded_east_trip_has_real_dismount_and_unchanged_totals():
    pytest.importorskip("valhalla")
    data = json.loads((Path(__file__).parent / "fixtures/valhalla_bicycle.json").read_text())
    original = copy.deepcopy(data)
    result = parse_route(data, "bicycle", "saratovskaya", "trofimova", "recorded")
    assert result.duration_s == 1568 and result.distance_m == 7315
    assert [leg.mode for leg in result.legs] == ["bicycle", "walk", "bicycle"]
    assert result.legs[1].distance_m == 7
    assert result.legs[1].duration_s == 25
    assert data == original


def test_recorded_corrected_east_trip_keeps_crossing_and_stairs_as_walking():
    pytest.importorskip("valhalla")
    data = json.loads(
        (Path(__file__).parent / "fixtures/valhalla_bicycle_dismount.json").read_text()
    )
    result = parse_route(data, "bicycle", "saratovskaya", "trofimova", "recorded-new")
    assert result.duration_s == 1319 and result.distance_m == 5975
    assert [leg.mode for leg in result.legs] == [
        "walk",
        "bicycle",
        "walk",
        "bicycle",
        "walk",
        "bicycle",
    ]
    walks = [leg for leg in result.legs if leg.mode == "walk"]
    assert [leg.distance_m for leg in walks] == [13, 35, 7]
    assert [leg.duration_s for leg in walks] == [10, 31, 25]
    assert all(a.geometry[-1] == b.geometry[0] for a, b in zip(result.legs, result.legs[1:]))


@pytest.fixture(scope="module")
def tiny_graphs(tmp_path_factory):
    osmium = pytest.importorskip("osmium")
    pytest.importorskip("valhalla")
    root = tmp_path_factory.mktemp("bicycle-graph")
    # Four disconnected U-shaped public roads, each closed by a short footway.
    # An inferred push-bike crossing is faster than riding all the way around.
    with osmium.SimpleWriter(str(root / "source.osm.pbf")) as writer:
        for case in range(4):
            y = 55.7 + case * 0.03
            for i, lon, lat in [
                (1, 37.6, y),
                (2, 37.6, y + 0.005),
                (3, 37.603, y + 0.005),
                (4, 37.603, y),
            ]:
                writer.add_node(osmium.osm.mutable.Node(id=case * 10 + i, location=(lon, lat)))
        for case, extra in enumerate(
            [{}, {"bicycle": "no"}, {"access": "private"}, {"bicycle": "yes"}]
        ):
            writer.add_way(
                osmium.osm.mutable.Way(
                    id=case * 10 + 1,
                    nodes=[case * 10 + i for i in (1, 2, 3, 4)],
                    tags={"highway": "residential"},
                )
            )
            writer.add_way(
                osmium.osm.mutable.Way(
                    id=case * 10 + 2,
                    nodes=[case * 10 + 1, case * 10 + 4],
                    tags={"highway": "footway", "footway": "crossing", **extra},
                )
            )
    for name, flags in [("original", []), ("dismount", ["--bicycle-dismount"])]:
        subprocess.run(
            [
                sys.executable,
                "scripts/build_routing_graph.py",
                str(root / "source.osm.pbf"),
                str(root / name),
                "--threads",
                "1",
                *flags,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    return RoadRouter(root / "original"), RoadRouter(root / "dismount")


def test_native_bicycle_search_uses_pushing_but_respects_prohibitions(tiny_graphs):
    old, new = tiny_graphs
    for case in range(4):
        a = Coordinate(lat=55.7 + case * 0.03, lon=37.6)
        b = a.model_copy(update={"lon": 37.603})
        before = old.route("bicycle", "a", "b", a, b)
        after = new.route("bicycle", "a", "b", a, b)
        assert before.status == after.status == "ok"
        if case == 0:
            assert before.distance_m > 1000 and after.distance_m < 200
            assert after.duration_s < before.duration_s
            assert all(leg.mode == "walk" for leg in after.legs)
            assert after.duration_s > 100  # No cycling speed on the crossing.
        elif case in (1, 2):
            assert after.distance_m == before.distance_m > 1000
        else:
            assert after.distance_m == before.distance_m < 200
            assert all(leg.mode == "bicycle" for leg in after.legs)
        for mode in ("car", "walk"):
            original = old.route(mode, "a", "b", a, b)
            updated = new.route(mode, "a", "b", a, b)
            assert (updated.duration_s, updated.distance_m, updated.legs) == (
                original.duration_s,
                original.distance_m,
                original.legs,
            )


def test_native_parallel_routes_match_serial_for_every_profile(tiny_graphs):
    _, roads = tiny_graphs
    batch = RoadBatch(3)
    try:
        for mode in ("car", "walk", "bicycle"):
            pairs = []
            for case in range(4):
                a = Coordinate(lat=55.7 + case * 0.03, lon=37.6)
                b = a.model_copy(update={"lon": 37.603})
                pairs.extend([(f"{case}-out", "a", "b", a, b), (f"{case}-back", "b", "a", b, a)])
            pairs.append(("missing", "a", "missing", a, None))
            actual = dict(batch.routes(roads, mode, pairs))
            expected = {
                key: door_journey(roads, mode, origin, target, a, b)
                for key, origin, target, a, b in pairs
            }
            assert actual == expected
    finally:
        batch.close()
