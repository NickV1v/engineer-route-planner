import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import engineer, request, scenario
from pydantic import ValidationError

from dispatch.baseline import solve_baseline
from dispatch.checker import require_valid_plan
from dispatch.events import DayEvent, UrgentRequest, apply_event, replan
from dispatch.importer import location_id
from dispatch.models import GeoLocation, GeoPoint, Scenario
from dispatch.optimizer import SearchOptions, optimize
from dispatch.publication import PublishRequest, publish
from dispatch.routing.contracts import Coordinate, Journey, Leg, RoutingError, content_hash
from dispatch.routing.matrices import PreparedTransport, apply_transport, prepare_transport
from dispatch.routing.roads import RoadRouter, parse_route
from dispatch.routing.transit import Pattern, TransitNetwork, TransitRouter
from dispatch.store import PlanStore

FIXTURE = Path(__file__).parent / "fixtures/transit_network.json"


@pytest.fixture
def network():
    pytest.importorskip("networkx", reason="Установите requirements-routing.lock")
    return TransitNetwork.model_validate_json(FIXTURE.read_text())


def test_metro_then_bus_counts_waits_only_on_boarding(network):
    result = TransitRouter(network).route("office", "A")
    # 2 + 3 min access, 2 min wait, 5 + 4 min train, 4 min transfer,
    # 5 min wait, 7 min bus, 2 min egress = 34 min.
    assert result.duration_s == 2040
    assert result.distance_m == 9550
    assert result.report()["boarding_wait_s"] == 420
    assert result.report()["walking_s"] == 660
    assert [leg.mode for leg in result.legs] == [
        "walk",
        "walk",
        "wait",
        "metro",
        "metro",
        "walk",
        "wait",
        "bus",
        "walk",
    ]
    assert TransitRouter(network).route("A", "office").status == "unreachable"


def test_choose_faster_walking_and_reject_unknown_nodes(network):
    network.walking[-1].duration_s = 1000
    router = TransitRouter(network)
    assert [leg.mode for leg in router.route("office", "A").legs] == ["walk"]
    assert router.route("office", "office").duration_s == 0
    with pytest.raises(RoutingError, match="отсутствует"):
        router.route("outside", "A")


def test_severed_transfer_does_not_teleport_between_nearby_stops(network):
    network.walking = [leg for leg in network.walking if leg.from_id != "metro-b"]
    router = TransitRouter(network)
    assert router.route("office", "A").duration_s == 3600
    assert router.route("metro-b", "bus-b").status == "unreachable"


def test_same_location_different_station_ids_are_not_a_transfer(network):
    network.nodes["same-name-on-another-line"] = network.nodes["metro-b"]
    assert (
        TransitRouter(network).route("metro-b", "same-name-on-another-line").status == "unreachable"
    )


def make_pattern(network, id, stops, times, circular=False, line="M2"):
    return Pattern(
        id=id,
        line=line,
        mode="metro",
        headway_s=120,
        stops=stops,
        circular=circular,
        hops=[
            Leg(
                mode="metro",
                from_id=a,
                to_id=b,
                duration_s=t,
                distance_m=1000,
                line=line,
                quality="estimated",
                geometry=[network.nodes[a], network.nodes[b]],
            )
            for a, b, t in zip(stops, stops[1:], times)
        ],
    )


def test_two_transfers_require_two_additional_waits(network):
    # Replace the middle walk with a second metro line, retaining a positive transfer.
    network.patterns[0].hops = network.patterns[0].hops[:1]
    network.patterns[0].stops = network.patterns[0].stops[:2]
    network.nodes["m2-platform"] = network.nodes["metro-middle"].model_copy()
    network.walking.append(
        Leg(
            mode="walk",
            from_id="metro-middle",
            to_id="m2-platform",
            duration_s=60,
            distance_m=50,
            quality="estimated",
            geometry=[network.nodes["metro-middle"], network.nodes["m2-platform"]],
        )
    )
    network.patterns.append(make_pattern(network, "m2", ["m2-platform", "metro-b"], [240]))
    result = TransitRouter(network).route("office", "A")
    assert result.duration_s == 2160
    assert [(leg.line, leg.duration_s) for leg in result.legs if leg.mode == "wait"] == [
        ("M1", 120),
        ("M2", 60),
        ("B7", 300),
    ]


def test_branch_switch_requires_new_boarding_even_with_same_line_name(network):
    network.walking = []
    network.patterns = [
        make_pattern(network, "branch-a", ["office", "entrance", "metro-a"], [100, 100]),
        make_pattern(network, "branch-b", ["office", "entrance", "A"], [100, 150]),
    ]
    # Direct travel along a pattern pays once; separate directions are not reversible.
    assert TransitRouter(network).route("office", "A").duration_s == 310
    assert TransitRouter(network).route("metro-a", "A").status == "unreachable"
    network.patterns.append(make_pattern(network, "inbound-a", ["metro-a", "entrance"], [100]))
    result = TransitRouter(network).route("metro-a", "A")
    assert result.duration_s == 370
    assert sum(leg.mode == "wait" for leg in result.legs) == 2


def test_ring_continuation_does_not_reboard_at_arbitrary_sequence_start(network):
    network.walking = []
    network.patterns = [
        make_pattern(network, "ring", ["office", "entrance", "A", "office"], [100, 200, 300], True)
    ]
    assert TransitRouter(network).route("A", "entrance").duration_s == 460
    # An opposite direction is a distinct pattern; it may be the faster way around.
    network.patterns.append(
        make_pattern(
            network, "reverse", ["office", "A", "entrance", "office"], [300, 200, 100], True
        )
    )
    assert TransitRouter(network).route("A", "entrance").duration_s == 260


@pytest.mark.parametrize(
    "damage", ["zero_transfer", "wrong_geometry", "missing_node", "wrong_hop", "duplicate_pattern"]
)
def test_reject_malformed_transit_network(network, damage):
    if damage == "zero_transfer":
        network.walking[2].duration_s = 0
    elif damage == "wrong_geometry":
        network.walking[2].geometry[0] = Coordinate(lat=56.0, lon=38.0)
    elif damage == "missing_node":
        network.nodes.pop("metro-b")
    elif damage == "wrong_hop":
        network.patterns[0].hops[0].to_id = "metro-b"
    else:
        network.patterns.append(network.patterns[0])
    with pytest.raises(ValidationError):
        TransitRouter(network)


def test_router_copies_input_and_returned_legs(network):
    router = TransitRouter(network)
    result = router.route("office", "A")
    network.walking[0].duration_s = 9999
    result.legs[0].duration_s = 9999
    assert router.route("office", "A").duration_s == 2040
    assert TransitRouter(network).network_id != router.network_id


def transport_case(network):
    case = scenario(
        [request(window_start_s=32400, window_end_s=34440, service_s=600)],
        [engineer(profile="public_transport_approx")],
    )
    points = {key: network.nodes[key] for key in ("office", "A")}
    case.geography = {
        key: GeoLocation(
            location_id=key,
            address=key,
            status="manual",
            point=GeoPoint(**point.model_dump(), label=key, precision="manual", source="fixture"),
        )
        for key, point in points.items()
    }
    return Scenario.model_validate(case.model_dump()), points


def test_metro_changes_window_feasibility_for_both_solvers(network):
    case, points = transport_case(network)
    original_hash = case.fingerprint()
    for has_metro, expected in ((True, 1), (False, 0)):
        current = network.model_copy(deep=True)
        if not has_metro:
            current.patterns = [p for p in current.patterns if p.mode != "metro"]
        router = TransitRouter(current)
        package = prepare_transport(points, {"public_transport_approx": router.route})
        prepared = apply_transport(case, package)
        assert package.matrices["public_transport_approx"].duration_s[1][0] is None
        baseline = require_valid_plan(prepared, solve_baseline(prepared))
        optimized = optimize(prepared, SearchOptions(time_limit_ms=0, rounds=1))["plan"]
        assert baseline.metrics.assigned == optimized.metrics.assigned == expected
        require_valid_plan(prepared, optimized)
        if has_metro:
            assert baseline.routes[0].visits[0].arrival_s == 34440
            assert baseline.metrics.travel_s == 2040
    assert case.fingerprint() == original_hash


def test_matrices_reject_coordinate_changes_published_roster_and_missing_profile(network):
    case, points = transport_case(network)
    package = prepare_transport(points, {"public_transport_approx": TransitRouter(network).route})
    changed = case.model_copy(deep=True)
    changed.geography["A"].point.lat += 0.001
    with pytest.raises(ValueError, match="Координаты"):
        apply_transport(changed, package)
    changed = case.model_copy(update={"roster": ["E1"]}, deep=True)
    with pytest.raises(ValueError, match="новому планированию"):
        apply_transport(changed, package)
    changed = case.model_copy(deep=True)
    changed.engineers[0].profile = "car"
    with pytest.raises(ValueError, match="всех видов"):
        apply_transport(changed, package)


def test_provider_failure_aborts_matrix_preparation(network):
    _, points = transport_case(network)
    router = TransitRouter(network)

    def broken(origin, target):
        if target == "A":
            raise RoutingError("engine stopped")
        return router.route(origin, target)

    with pytest.raises(RoutingError, match="engine stopped"):
        prepare_transport(points, {"public_transport_approx": broken})


def test_matrix_package_cannot_claim_travel_from_unknown_coordinates(network):
    _, points = transport_case(network)
    package = prepare_transport(points, {"public_transport_approx": TransitRouter(network).route})
    raw = package.model_dump()
    raw["coordinates"]["A"] = None
    with pytest.raises(ValidationError, match="без подтверждённых"):
        PreparedTransport.model_validate(raw)
    with pytest.raises(RoutingError, match="без подтверждённых"):
        prepare_transport(
            {**points, "A": None}, {"public_transport_approx": TransitRouter(network).route}
        )


def test_network_cannot_change_during_matrix_preparation(network):
    _, points = transport_case(network)
    router = TransitRouter(network)

    def changing(origin, target):
        result = router.route(origin, target)
        result.network_id = target
        return result

    with pytest.raises(RoutingError, match="изменилась"):
        prepare_transport(points, {"public_transport_approx": changing})


def road_response():
    return {
        "trip": {
            "status": 0,
            "units": "kilometers",
            "legs": [
                {
                    "summary": {"time": 10.1, "length": 0.1501},
                    "shape": {"type": "LineString", "coordinates": [[37.6, 55.7], [37.601, 55.7]]},
                }
            ],
        }
    }


def test_road_units_coordinate_order_and_conservative_rounding():
    route = parse_route(road_response(), "walk", "a", "b", "test")
    assert route.duration_s == 11
    assert route.distance_m == 151
    assert route.legs[0].geometry[0] == Coordinate(lat=55.7, lon=37.6)


def test_native_valhalla_polyline6_response_regression():
    pytest.importorskip("valhalla", reason="Установите requirements-routing.lock")
    data = json.loads(FIXTURE.with_name("valhalla_route.json").read_text())
    result = parse_route(data, "car", "moscow_a", "moscow_b", "recorded")
    assert result.duration_s == 216
    assert result.distance_m == 2453
    assert len(result.legs[0].geometry) > 20
    assert result.legs[0].geometry[0].lat == pytest.approx(55.730210)
    assert result.legs[0].geometry[0].lon == pytest.approx(37.638719)


@pytest.mark.parametrize("value", [-1, float("inf"), float("nan"), True, "10"])
def test_bad_road_numbers_are_errors_not_unreachable_routes(value):
    data = road_response()
    data["trip"]["legs"][0]["summary"]["time"] = value
    with pytest.raises(RoutingError):
        parse_route(data, "walk", "a", "b", "test")


@pytest.fixture
def road(monkeypatch, tmp_path):
    class ProviderError(RuntimeError):
        def __init__(self, code):
            self.code, self.message = code, "test provider failure"

    fake = SimpleNamespace(response=road_response(), requests=[], error=None)

    def route(payload):
        fake.requests.append(payload)
        if fake.error:
            raise ProviderError(fake.error)
        return fake.response

    monkeypatch.setitem(
        sys.modules,
        "valhalla",
        SimpleNamespace(
            __version__="test",
            Actor=lambda config: SimpleNamespace(route=route),
            ValhallaError=ProviderError,
        ),
    )
    (tmp_path / "tiles").mkdir()
    (tmp_path / "tiles/1.gph").touch()
    (tmp_path / "valhalla.json").write_text("{}")
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "valhalla_static_v1",
                "engine": "test",
                "config_hash": content_hash({}),
                "assumptions": [],
            }
        )
    )
    return RoadRouter(tmp_path), fake


@pytest.mark.parametrize(
    "code,status", [(170, "unreachable"), (442, "unreachable"), (171, "unmatched")]
)
def test_known_road_failure_states(road, code, status):
    router, fake = road
    fake.error = code
    assert (
        router.route(
            "walk", "a", "b", Coordinate(lat=55.7, lon=37.6), Coordinate(lat=55.7, lon=37.601)
        ).status
        == status
    )


def test_relocated_graph_preserves_identity_and_original_config(road, tmp_path):
    original, _ = road
    config = {"mjolnir": {"tile_dir": "/another-machine/tiles", "tile_extract": ""}}
    config_path = tmp_path / "valhalla.json"
    manifest_path = tmp_path / "manifest.json"
    config_path.write_text(json.dumps(config))
    manifest = json.loads(manifest_path.read_text())
    manifest["config_hash"] = content_hash(config)
    manifest_path.write_text(json.dumps(manifest))
    original_files = config_path.read_bytes(), manifest_path.read_bytes()

    relocated = RoadRouter(tmp_path)
    assert relocated._config["mjolnir"]["tile_dir"] == str((tmp_path / "tiles").resolve())
    assert relocated.network_id == content_hash(manifest)
    assert relocated.network_id != original.network_id
    assert (config_path.read_bytes(), manifest_path.read_bytes()) == original_files
    config["mjolnir"]["tile_dir"] = "/tampered/tiles"
    config_path.write_text(json.dumps(config))
    with pytest.raises(RoutingError, match="Конфигурация графа изменена"):
        RoadRouter(tmp_path)


def test_missing_coordinates_do_not_call_engine_and_errors_are_not_hidden(road):
    router, fake = road
    a, b = Coordinate(lat=55.7, lon=37.6), Coordinate(lat=55.7, lon=37.601)
    assert router.route("walk", "a", "b", None, b).status == "missing_coordinates"
    assert fake.requests == []
    fake.error = 499
    with pytest.raises(RoutingError, match="499"):
        router.route("walk", "a", "b", a, b)
    fake.error = None
    assert router.route("car", "a", "b", a, b).duration_s == 11
    assert fake.requests[-1]["costing"] == "auto"
    with pytest.raises(RoutingError, match="пассажирские"):
        router.route("public_transport_approx", "a", "b", a, b)
    with pytest.raises(RoutingError, match="100 м"):
        router.route("walk", "a", "b", Coordinate(lat=56.0, lon=38.0), b)


def test_journey_must_be_continuous(network):
    legs = TransitRouter(network).route("office", "A").legs
    with pytest.raises(ValidationError, match="Разрыв"):
        Journey(from_id="office", to_id="A", status="ok", legs=legs[1:], network_id="test")


def test_started_transit_trip_and_stored_snapshot_survive_replanning(network, tmp_path):
    case, points = transport_case(network)
    package = prepare_transport(points, {"public_transport_approx": TransitRouter(network).route})
    prepared = apply_transport(case, package)
    baseline = require_valid_plan(prepared, solve_baseline(prepared))
    published, response = publish(
        prepared,
        baseline,
        PublishRequest(operation_id="publish", expected_version=1, engineer_ids=["E1"]),
    )
    store = PlanStore(tmp_path / "plans.sqlite")
    saved = store.create(published, response)
    original_hash = published.fingerprint()
    updated, result = replan(
        published,
        response["plan"],
        DayEvent(
            event_id="unavailable",
            expected_version=1,
            at_s=33000,
            type="engineer_unavailable",
            engineer_id="E1",
            search=SearchOptions(time_limit_ms=0),
        ),
    )
    assert updated.roster == ["E1"]
    assert updated.matrices == published.matrices
    assert result["plan"].routes[0].visits == baseline.routes[0].visits
    package.matrices["public_transport_approx"].duration_s[0][1] = 9999
    assert published.fingerprint() == original_hash
    restored, _ = store.get(saved["session_id"])
    assert restored.fingerprint() == original_hash
    require_valid_plan(restored, response["plan"])


def test_cli_prepares_a_reviewable_day_without_touching_original(
    network, tmp_path, monkeypatch, capsys
):
    from dispatch.routing.__main__ import main

    case, _ = transport_case(network)
    source = tmp_path / "input.json"
    source.write_text(case.model_dump_json())
    output = tmp_path / "prepared"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "routing",
            "prepare",
            "--scenario",
            str(source),
            "--network",
            str(FIXTURE),
            "--output",
            str(output),
            "--optimize",
        ],
    )
    main()
    assert "checker OK" in capsys.readouterr().out
    prepared = Scenario.model_validate_json((output / "scenario.json").read_text())
    assert prepared.matrices["public_transport_approx"].duration_s[0][1] == 2040
    assert json.loads((output / "optimized.json").read_text())["metrics"]["assigned"] == 1
    assert source.read_text() == case.model_dump_json()
    with pytest.raises(SystemExit, match="1"):
        main()
    assert "уже существует" in capsys.readouterr().err


def test_cancelled_address_keeps_coordinates_of_its_real_matrix_row():
    address = "Тестовый адрес A"
    key = location_id(address)
    case = scenario([request(location_id=key, address=address)])
    case.manifest = {
        "transport": "routing_static_v1",
        "policy": {"norm_mapping": {"Подключение": {"work": "install", "skill": "installation"}}},
        "norms": {"install": {"service_s": 4200}},
    }
    point = GeoPoint(lat=55.7, lon=37.6, label=address, precision="manual", source="fixture")
    case.geography[key] = GeoLocation(
        location_id=key, address=address, status="manual", point=point
    )
    published, response = publish(
        case,
        solve_baseline(case),
        PublishRequest(operation_id="publish", expected_version=1, engineer_ids=["E1"]),
    )
    cancelled, response = replan(
        published,
        response["plan"],
        DayEvent(event_id="cancel", expected_version=1, at_s=32000, type="cancel", request_id="A"),
    )
    assert cancelled.geography[key].point == point
    original_hash = cancelled.fingerprint()
    added = DayEvent(
        event_id="add",
        expected_version=2,
        at_s=32001,
        type="add_urgent",
        request=UrgentRequest(
            id="replacement",
            address=address,
            kind="Подключение",
            window_start_s=36000,
            window_end_s=43200,
            coordinates=point.model_copy(update={"lat": 55.8}),
        ),
    )
    with pytest.raises(ValueError, match="уже используется"):
        apply_event(cancelled, response["plan"], added)
    added.request.coordinates = point
    restored = apply_event(cancelled, response["plan"], added)
    assert restored.geography[key].point == point
    assert restored.matrices == cancelled.matrices
    assert cancelled.fingerprint() == original_hash
