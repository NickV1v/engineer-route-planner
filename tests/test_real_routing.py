import random
from pathlib import Path

import pytest
from conftest import engineer, request, scenario
from fastapi.testclient import TestClient

from dispatch.api import create_app
from dispatch.baseline import solve_baseline
from dispatch.checker import require_valid_plan
from dispatch.events import DayEvent, replan
from dispatch.models import GeoLocation, GeoPoint
from dispatch.routing.cache import RoutingCache
from dispatch.routing.contracts import Coordinate, Journey, Leg, RoutingError, content_hash
from dispatch.routing.geometry import slice_stops
from dispatch.routing.import_osm import NetworkBuilder, join_ways
from dispatch.routing.native import NativeGraph, library
from dispatch.routing.public import PublicRouter
from dispatch.routing.service import RoutingService
from dispatch.routing.transit import TransitNetwork, TransitRouter
from dispatch.routing.walking import ACCESS_MODEL, door_journey

FIXTURE = Path(__file__).parent / "fixtures/transit_network.json"


def movement(a, b, origin, target, duration=120, distance=150, mode="walk"):
    return Leg(
        mode=mode,
        from_id=origin,
        to_id=target,
        duration_s=duration,
        distance_m=distance,
        geometry=[a, b],
        quality="estimated",
    )


@pytest.fixture
def public_case():
    network = TransitNetwork.model_validate_json(FIXTURE.read_text())
    coordinates = {key: network.nodes.pop(key) for key in ("office", "A")}
    network.surface = {"entrance": "entrance", "bus-c": "stop"}
    network.walking = [
        leg
        for leg in network.walking
        if leg.from_id not in coordinates and leg.to_id not in coordinates
    ]

    def walking(origin, target, a, b):
        allowed = {("office", "entrance"), ("bus-c", "A")}
        return Journey(
            from_id=origin,
            to_id=target,
            network_id="fixture-walk",
            status="ok" if (origin, target) in allowed else "unreachable",
            legs=[movement(a, b, origin, target)] if (origin, target) in allowed else [],
        )

    return network, coordinates, walking


def test_address_to_address_metro_bus_is_directed_and_counts_every_leg(public_case):
    network, coordinates, walking = public_case
    router = PublicRouter(network, "fixture-roads", walking)
    result = router.many("office", coordinates)["A"]
    assert result.duration_s == 2040
    assert result.distance_m == 9550
    assert result.report()["walking_s"] == 660
    assert result.report()["boarding_wait_s"] == 420
    assert [leg.mode for leg in result.legs if leg.mode not in {"walk", "wait"}] == [
        "metro",
        "metro",
        "bus",
    ]
    assert router.many("A", coordinates)["office"].status == "unreachable"
    assert (
        router.many("office", {**coordinates, "missing": None})["missing"].status
        == "missing_coordinates"
    )
    result.legs[0].duration_s = 1
    assert router.many("office", coordinates)["A"].duration_s == 2040


def test_station_is_not_accessible_without_its_entrance(public_case):
    network, coordinates, walking = public_case
    network.walking = [leg for leg in network.walking if leg.from_id != "entrance"]
    router = PublicRouter(network, "fixture-roads", walking)
    assert router.many("office", coordinates)["A"].status == "unreachable"


def test_walking_budget_keeps_slower_usable_label(public_case):
    network, coordinates, walking = public_case
    # Fast access to metro-b spends almost the whole walking budget; it must
    # not dominate the slower train path that still permits reaching the bus.
    network.walking.append(
        movement(
            network.nodes["entrance"],
            network.nodes["metro-b"],
            "entrance",
            "metro-b",
            duration=1,
            distance=4800,
        )
    )
    result = PublicRouter(network, "fixture-roads", walking).many("office", coordinates)["A"]
    assert result.duration_s == 2040
    assert sum(leg.distance_m for leg in result.legs if leg.mode == "walk") == 650
    network.patterns = [p for p in network.patterns if p.mode != "metro"]
    assert (
        PublicRouter(network, "fixture-roads", walking).many("office", coordinates)["A"].status
        == "unreachable"
    )


def test_short_walk_competes_with_transit_but_long_walk_is_not_fallback(public_case):
    network, _, _ = public_case
    a, b = Coordinate(lat=55.7, lon=37.6), Coordinate(lat=55.7001, lon=37.6)

    def walking(origin, target, start, end):
        return Journey(
            from_id=origin,
            to_id=target,
            status="ok",
            network_id="walk",
            legs=[movement(start, end, origin, target, distance=3000 if target == "long" else 100)],
        )

    empty = network.model_copy(update={"surface": {}})
    router = PublicRouter(empty, "roads", walking)
    paths = router.many("office", {"office": a, "near": b, "long": b})
    assert paths["near"].duration_s == 120
    assert paths["long"].status == "unreachable"


def test_boarding_and_alighting_permissions(public_case):
    network, _, _ = public_case
    network.patterns[0].boarding = [False, True, True]
    assert TransitRouter(network).route("metro-a", "metro-b").status == "unreachable"
    network.patterns[0].boarding = None
    network.patterns[0].alighting = [True, False, True]
    assert TransitRouter(network).route("metro-a", "metro-middle").status == "unreachable"
    assert TransitRouter(network).route("metro-a", "metro-b").duration_s == 660


def test_geometry_keeps_bend_direction_and_rejects_unordered_stops():
    points = [
        Coordinate(lat=55.7, lon=37.6),
        Coordinate(lat=55.71, lon=37.6),
        Coordinate(lat=55.71, lon=37.61),
    ]
    assert slice_stops(points, [points[0], points[-1]]) == [points]
    with pytest.raises(ValueError):
        slice_stops(points, [points[-1], points[0]])
    assert join_ways([[1, 2], [3, 2], [3, 4]]) == [1, 2, 3, 4]
    with pytest.raises(ValueError, match="Разрыв"):
        join_ways([[1, 2], [3, 4]])


def test_bus_import_uses_route_ways_and_explicit_platforms():
    nodes = [
        {"type": "node", "id": i, "lat": 55.7 + dy, "lon": 37.6 + dx, "tags": {"name": f"stop{i}"}}
        for i, dx, dy in [
            (1, 0, 0),
            (2, 0, 0.01),
            (3, 0.01, 0.01),
            (4, 0, 0.0001),
            (5, 0.01, 0.0101),
        ]
    ]
    relation = {
        "type": "relation",
        "id": 9,
        "tags": {"route": "bus", "public_transport:version": "2", "ref": "7"},
        "members": [
            {"type": "node", "ref": 1, "role": "stop_entry_only"},
            {"type": "node", "ref": 4, "role": "platform_entry_only"},
            {"type": "node", "ref": 3, "role": "stop_exit_only"},
            {"type": "node", "ref": 5, "role": "platform_exit_only"},
            {"type": "way", "ref": 6, "role": ""},
        ],
    }
    data = {"elements": [*nodes, {"type": "way", "id": 6, "nodes": [1, 2, 3]}, relation]}
    builder = NetworkBuilder()
    builder.surface_routes(data)
    network = builder.finish({"test": True})
    assert len(network.patterns) == 1
    assert len(network.patterns[0].hops[0].geometry) == 3
    assert network.patterns[0].boarding == [True, False]
    assert network.patterns[0].alighting == [False, True]
    assert network.surface == {"surface:n4": "stop", "surface:n5": "stop"}
    router = TransitRouter(network)
    assert router.route("surface:n4", "surface:n5").status == "ok"
    assert router.route("surface:n5", "surface:n4").status == "unreachable"


class FakeRoads:
    network_id = "test-roads"

    def route(self, profile, origin, target, a, b):
        if a is None or b is None:
            return Journey(
                from_id=origin,
                to_id=target,
                status="missing_coordinates",
                network_id=self.network_id,
            )
        return Journey(
            from_id=origin,
            to_id=target,
            status="ok",
            network_id=self.network_id,
            legs=[movement(a, b, origin, target, duration=600, distance=1000, mode=profile)],
        )


def service(tmp_path):
    result = RoutingService(tmp_path, mode="real")
    result.roads = FakeRoads()
    result.street_hash = content_hash(
        {"road_network": result.roads.network_id, "access": ACCESS_MODEL}
    )
    result.public = PublicRouter(
        TransitNetwork.model_validate_json(FIXTURE.read_text()), result.street_hash, None
    )
    return result


def geo(key, lat=55.7):
    return GeoLocation(
        location_id=key,
        address=key,
        status="manual",
        point=GeoPoint(lat=lat, lon=37.6, label=key, precision="manual", source="fixture"),
    )


def test_route_cache_and_atomic_extension_preserve_old_day(tmp_path, policy):
    router = service(tmp_path)
    case = scenario([request(window_end_s=57600)], [engineer()])
    case.geography = {"office": geo("office"), "A": geo("A", 55.71)}
    case.manifest.update({"policy": policy, "norms": {"Подключение интернета": {"service_s": 600}}})
    rule = policy["norm_mapping"]["Подключение"]["work"]
    case.manifest["norms"][rule] = {"service_s": 600}
    prepared = router.new_day(case)
    prepared.roster = ["E1"]
    plan = require_valid_plan(prepared, solve_baseline(prepared))
    before = prepared.model_dump_json()
    old_journeys = router.journeys(prepared, plan)
    event = DayEvent(
        event_id="new",
        expected_version=1,
        at_s=32401,
        type="add_urgent",
        request={
            "id": "new",
            "kind": "Подключение",
            "address": "Новый адрес 7",
            "window_start_s": 43200,
            "window_end_s": 57600,
            "coordinates": geo("new", 55.72).point,
        },
        search={"time_limit_ms": 0, "rounds": 1},
    )
    updated, response = replan(prepared, plan, event, transport_extension=router.extend)
    require_valid_plan(updated, response["plan"])
    assert updated.roster == prepared.roster
    assert updated.planning_state.frozen_routes["E1"] == plan.routes[0].visits[:1]
    assert prepared.model_dump_json() == before
    assert [row[:2] for row in updated.matrices["car"].duration_s[:2]] == prepared.matrices[
        "car"
    ].duration_s
    assert router.journeys(updated, response["plan"])["E1"][0] == old_journeys["E1"][0]
    assert RoutingService(tmp_path, mode="real").journeys(prepared, plan) == old_journeys
    # Changing the road source must not silently recalculate a published day.
    router.street_hash = "different-source"
    with pytest.raises(RoutingError, match="прежний"):
        replan(prepared, plan, event, transport_extension=router.extend)
    assert prepared.model_dump_json() == before


def test_cache_is_immutable_and_rejects_missing_geometry(tmp_path):
    cache = RoutingCache(tmp_path / "cache.sqlite")
    journey = Journey(from_id="a", to_id="a", status="ok", network_id="fixture")
    cache.save_journey("key", journey)
    with pytest.raises(RoutingError, match="изменился"):
        cache.save_journey("key", journey.model_copy(update={"network_id": "changed"}))


def test_door_access_accounts_for_snaps_and_identity():
    a = Coordinate(lat=55.7, lon=37.6)
    b = Coordinate(lat=55.71, lon=37.6)
    roads = FakeRoads()
    original = roads.route

    def snapped(profile, origin, target, a, b):
        return original(profile, origin, target, Coordinate(lat=a.lat + 0.0001, lon=a.lon), b)

    roads.route = snapped
    journey = door_journey(roads, "car", "a", "b", a, b)
    assert [leg.mode for leg in journey.legs] == ["walk", "car", "walk"]
    assert journey.duration_s > 600 and journey.distance_m > 1000
    assert journey.legs[0].geometry_quality == "estimated"
    assert door_journey(roads, "car", "a", "a", None, None).duration_s == 0


def test_bicycle_graph_is_independent_and_old_geometry_remains_readable(tmp_path):
    router = service(tmp_path)
    car_network = router.road_network("car")
    public_network = router.public.network_id
    original_bicycle = router.road_network("bicycle")
    router.bicycle_roads = FakeRoads()
    router.bicycle_roads.network_id = "dismount-graph-v1"
    bicycle_calls = []
    original_route = router.bicycle_roads.route

    def counted(*args):
        bicycle_calls.append(args[0])
        return original_route(*args)

    router.bicycle_roads.route = counted
    case = scenario([request()], [engineer(profile="bicycle")])
    case.geography = {"office": geo("office"), "A": geo("A", 55.71)}
    prepared = router.new_day(case)
    assert bicycle_calls == ["bicycle", "bicycle"]
    assert prepared.manifest["routing"]["networks"]["bicycle"] != original_bicycle
    assert router.road_network("car") == car_network
    assert router.public.network_id == public_network
    plan = require_valid_plan(prepared, solve_baseline(prepared))
    archived = router.journeys(prepared, plan)
    assert archived["E1"][0]["duration_s"] == plan.routes[0].visits[0].travel_s
    router.extend(prepared, None)
    assert bicycle_calls == ["bicycle"] * 4  # New calculation searches again.
    router.bicycle_roads.network_id = "dismount-graph-v2"
    with pytest.raises(RoutingError, match="прежний"):
        router.extend(prepared, None)
    assert router.journeys(prepared, plan) == archived
    new_day = router.new_day(case)
    assert (
        new_day.manifest["routing"]["package_hash"] != prepared.manifest["routing"]["package_hash"]
    )


def test_api_requires_real_network_and_never_falls_back(monkeypatch, tmp_path):
    monkeypatch.setenv("DISPATCH_ROUTING_MODE", "real")
    app = create_app(storage_path=tmp_path / "api.sqlite")

    def fail():
        raise RoutingError("Сеть недоступна")

    monkeypatch.setattr(app.state.routing, "ensure", fail)
    with TestClient(app) as client:
        response = client.post("/api/scenarios/Восток/baseline", json={"engineer_count": 1})
        assert response.status_code == 503
        assert response.json()["detail"] == "Сеть недоступна"


def small_day():
    case = scenario([request(window_end_s=57600)], [engineer()])
    case.geography = {"office": geo("office"), "A": geo("A", 55.71)}
    return case


def test_each_calculation_rebuilds_routes_despite_completed_archive(monkeypatch, tmp_path):
    router = service(tmp_path)
    calls = []
    original = router.roads.route

    def counted(*args):
        calls.append(args[:3])
        return original(*args)

    monkeypatch.setattr(router.roads, "route", counted)
    previous = None
    for expected in (2, 4):
        with router.calculation() as calculation:
            prepared = calculation.new_day(small_day())
            assert len(calls) == expected
            # Both solvers and repeated OD queries inside this same calculation
            # are allowed to reuse its newly built inputs.
            assert calculation.new_day(small_day()) == prepared
            assert len(calls) == expected
            assert calculation.report()["road_searches"] == 2
            assert calculation.report()["cache_scope"] == "calculation"
            assert calculation.report()["cache_hits"] > 0
        assert not calculation.cache._journeys
        with pytest.raises(RoutingError, match="завершён"):
            calculation.new_day(small_day())
        if previous is not None:
            assert prepared == previous
        previous = prepared
    # Reading history does not execute the router or fill the next run's cache.
    stored = router.journeys(prepared, solve_baseline(prepared))
    assert stored["E1"][0]["duration_s"] == 600
    assert len(calls) == 4


def test_failure_discards_partly_built_routes(monkeypatch, tmp_path):
    router = service(tmp_path)
    original = router.roads.route
    calls = []

    def fail_second(*args):
        calls.append(args[:3])
        if len(calls) == 2:
            raise RoutingError("Тестовый сбой маршрутизации")
        return original(*args)

    monkeypatch.setattr(router.roads, "route", fail_second)
    with pytest.raises(RoutingError, match="Тестовый сбой"):
        with router.calculation() as calculation:
            calculation.new_day(small_day())
    assert calculation.closed and not calculation.cache._journeys
    router.new_day(small_day())
    assert len(calls) == 4
    assert calls[:2] == calls[2:]


def test_access_cache_is_local_to_calculation(public_case):
    network, coordinates, walking = public_case
    calls = []

    def counted(*args):
        calls.append(args[:2])
        return walking(*args)

    shared = PublicRouter(network, "roads", None)
    one, two = shared.for_calculation(counted), shared.for_calculation(counted)
    one.many("office", coordinates)
    before = len(calls)
    assert before > 0
    one.many("office", coordinates)
    assert len(calls) == before
    two.many("office", coordinates)
    assert len(calls) == 2 * before
    assert shared._access == {}
    assert one.router is two.router  # Only immutable topology is shared.


def test_fast_search_matches_full_resource_search(public_case):
    network, coordinates, walking = public_case
    randomizer = random.Random(217)
    for _ in range(30):
        current = network.model_copy(deep=True)
        for pattern in current.patterns:
            pattern.headway_s = randomizer.randint(60, 1200)
            for hop in pattern.hops:
                hop.duration_s = randomizer.randint(60, 1800)
        current.walking.append(
            movement(
                current.nodes["entrance"],
                current.nodes["bus-c"],
                "entrance",
                "bus-c",
                duration=randomizer.randint(1, 6000),
                distance=randomizer.randint(100, 6000),
            )
        )
        fast = PublicRouter(current, "roads", walking)
        reference = PublicRouter(current, "roads", walking)
        full_search = reference.labels_from
        reference.labels_from = lambda access, **kwargs: full_search(access, constrained=True)
        actual = fast.many("office", coordinates)["A"]
        expected = reference.many("office", coordinates)["A"]
        assert actual.status == expected.status
        assert actual.duration_s == expected.duration_s
        if actual.status == "ok":
            on_transport = any(leg.mode in {"metro", "bus"} for leg in actual.legs)
            walked = sum(leg.distance_m for leg in actual.legs if leg.mode == "walk")
            assert walked <= (5000 if on_transport else 2500)


def test_native_search_matches_python_labels_and_ties_on_random_graphs(public_case):
    native = library()
    if native is None:
        pytest.skip("make solver builds the native transit search")
    import networkx as nx

    network, coordinates, walking = public_case
    router = PublicRouter(network, "roads", walking)
    rng = random.Random(903)
    point = coordinates["office"]
    for _ in range(50):
        graph = nx.DiGraph()
        nodes = [("stop", str(i)) for i in range(12)]
        graph.add_nodes_from(nodes)
        for a in nodes:
            for b in nodes:
                if rng.random() < 0.3:
                    mode = rng.choice(["walk", "bus", None])
                    leg = (
                        movement(
                            point, point, a[1], b[1], rng.randint(1, 500), rng.randint(0, 4000)
                        )
                        if mode
                        else None
                    )
                    if mode == "bus":
                        leg.mode, leg.line = "bus", "test"
                    graph.add_edge(a, b, weight=leg.duration_s if leg else 0, leg=leg)
        router.router._graph = graph
        router._native = NativeGraph(graph, native)
        access = [
            (node[1], (movement(point, point, "origin", node[1], rng.randint(1, 100), 30),))
            for node in nodes[:2]
        ]
        for constrained in (False, True):
            actual, fronts = router.labels_from(access, constrained=constrained)
            expected, reference = router.python_labels_from(access, constrained=constrained)
            assert len(actual) == len(expected)
            assert [actual[i] for i in range(len(actual))] == expected
            for node in nodes:
                for boarded in (False, True):
                    key = (node, boarded)
                    assert fronts.get(key, []) == reference.get(key, [])


def test_native_and_python_public_journeys_are_identical_and_isolated(public_case):
    if library() is None:
        pytest.skip("make solver builds the native transit search")
    network, coordinates, walking = public_case
    router = PublicRouter(network, "roads", walking)
    reference = PublicRouter(network, "roads", walking)
    reference._native = None
    # Force the Pareto search: the fastest path walks too far to be feasible.
    leg = movement(network.nodes["entrance"], network.nodes["bus-c"], "entrance", "bus-c", 1, 4999)
    for current in (router, reference):
        current.router._graph.add_edge(("stop", "entrance"), ("stop", "bus-c"), weight=1, leg=leg)
    router._native = NativeGraph(router.router._graph, library())
    for _ in range(3):
        for origin in coordinates:
            actual = router.many(origin, coordinates)
            expected = reference.many(origin, coordinates)
            assert actual == expected
            for journey in actual.values():
                if journey.legs:
                    journey.legs[0].duration_s += 100
                    journey.legs[0].geometry[0].lat += 0.001


def test_api_replans_fresh_after_cancellation_and_keeps_idempotent_acceptance(
    monkeypatch, tmp_path
):
    app = create_app(storage_path=tmp_path / "api.sqlite")
    routing = app.state.routing
    fake = service(tmp_path / "routing")
    for field in ("roads", "public", "street_hash", "archive", "mode"):
        setattr(routing, field, getattr(fake, field))
    monkeypatch.setattr("dispatch.api.make_scenario", lambda *args: small_day())
    calls = []
    original = routing.roads.route

    def counted(*args):
        calls.append(args[:3])
        return original(*args)

    monkeypatch.setattr(routing.roads, "route", counted)
    with TestClient(app) as client:
        first = client.post("/api/scenarios/Восток/baseline", json={"engineer_count": 1})
        assert first.status_code == 200, first.text
        first = first.json()
        assert first["performance"]["road_searches"] == 2
        session = first["session_id"]
        second = client.post("/api/scenarios/Восток/baseline", json={"engineer_count": 1}).json()
        assert second["performance"]["road_searches"] == 2
        assert len(calls) == 4
        assert second["plan"] == first["plan"]
        assert second["session_id"] != session
        assert first["staffing"]["engineer_ids"] == ["E1"]
        event = {
            "event_id": "cancel-1",
            "expected_version": 1,
            "at_s": 32400,
            "type": "cancel",
            "request_id": "A",
            "search": {"time_limit_ms": 0},
        }
        url = f"/api/sessions/{session}/events/preview"
        preview = client.post(url, json=event)
        assert preview.status_code == 200, preview.text
        assert preview.json()["result"]["performance"]["road_searches"] == 2
        assert len(calls) == 6
        assert client.get(f"/api/sessions/{session}").json()["version"] == 1
        # A transport retry is the same operation; a new event ID runs afresh.
        assert client.post(url, json=event).json() == preview.json()
        assert len(calls) == 6
        event["event_id"] = "cancel-2"
        repeated = client.post(url, json=event)
        assert repeated.status_code == 200, repeated.text
        assert len(calls) == 8
        committed = client.post(
            f"/api/sessions/{session}/events/commit",
            json={"preview_id": repeated.json()["preview_id"]},
        )
        assert committed.status_code == 200, committed.text
        assert len(calls) == 8
        assert client.get(f"/api/sessions/{session}?version=1").json() == first
