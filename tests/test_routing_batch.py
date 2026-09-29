from threading import Event, get_ident

import pytest
from conftest import engineer, request, scenario
from test_real_routing import FakeRoads, geo, service

from dispatch.routing.batch import RoadBatch
from dispatch.routing.cache import CalculationCache, RoutingCache
from dispatch.routing.contracts import Coordinate, Journey, Leg, RoutingError
from dispatch.routing.roads import RoadRouter


def test_batch_uses_independent_actors_and_bounded_keyed_results():
    second_finished = Event()
    instances, consumed = [], []

    class Roads:
        network_id = "parallel-fixture"

        def fork(self):
            actor = Roads()
            actor.owner = get_ident()
            instances.append(actor)
            return actor

        def route(self, profile, origin, target, a, b):
            assert self.owner == get_ident()
            if origin == "0":
                assert second_finished.wait(5)
            elif origin == "1":
                second_finished.set()
            return Journey(
                from_id=origin,
                to_id=target,
                network_id=self.network_id,
                status="ok",
                legs=[
                    Leg(
                        mode=profile,
                        from_id=origin,
                        to_id=target,
                        duration_s=60,
                        distance_m=100,
                        geometry=[a, b],
                        quality="estimated",
                    )
                ],
            )

    a, b = Coordinate(lat=55.7, lon=37.6), Coordinate(lat=55.71, lon=37.6)

    def inputs():
        for i in range(20):
            consumed.append(i)
            yield str(i), str(i), "end", a, b

    batch = RoadBatch(2)
    try:
        results = batch.routes(Roads(), "car", inputs())
        first = next(results)
        assert first[1].from_id == first[0]
        assert len(consumed) <= 2 * batch.workers + 1
        values = [first, *results]
        assert {key for key, _ in values} == set(map(str, range(20)))
        assert len(values) == 20
        assert all(journey.duration_s == 60 for _, journey in values)
        assert len(instances) == 2
    finally:
        batch.close()


class ParallelFixtureRoads(FakeRoads, RoadRouter):
    def __init__(self, calls, instances, fail=False):
        self.calls, self.instances, self.fail = calls, instances, fail

    def fork(self):
        result = ParallelFixtureRoads(self.calls, self.instances, self.fail)
        self.instances.append(result)
        return result

    def route(self, profile, origin, target, a, b):
        self.calls.append((profile, origin, target))
        if self.fail:
            raise RoutingError("Ошибка рабочего потока")
        return super().route(profile, origin, target, a, b)


def test_parallel_calculations_are_fresh_and_match_serial(tmp_path):
    router = service(tmp_path)
    calls, instances = [], []
    router.roads = ParallelFixtureRoads(calls, instances)
    router.road_workers = 2
    case = scenario([request()], [engineer()])
    case.geography = {"office": geo("office"), "A": geo("A", 55.71)}
    previous = None
    for count in (2, 4):
        with router.calculation() as calculation:
            prepared = calculation.new_day(case)
            assert len(calls) == count
            assert calculation.new_day(case) == prepared
            assert len(calls) == count
            assert calculation.report()["road_searches"] == 2
            assert calculation.report()["routing_profiles_ms"]["car"] >= 0
        assert calculation._road_batch is None
        assert not calculation.cache._journeys
        if previous:
            assert previous == prepared
        previous = prepared
    router.road_workers = 1
    assert router.new_day(case) == previous
    assert len(calls) == 6
    assert len(instances) >= 2


def test_worker_failure_discards_results_and_allows_fresh_retry(tmp_path):
    router = service(tmp_path)
    router.roads = ParallelFixtureRoads([], [], fail=True)
    router.road_workers = 2
    case = scenario([request()], [engineer()])
    case.geography = {"office": geo("office"), "A": geo("A", 55.71)}
    with pytest.raises(RoutingError, match="рабочего потока"):
        with router.calculation() as calculation:
            calculation.new_day(case)
    assert calculation.closed and calculation._road_batch is None
    assert not calculation.cache._journeys
    with router.archive.connect() as db:
        assert db.execute("SELECT count(*) FROM packages").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM journeys").fetchone()[0] == 0
    router.roads.fail = False
    router.new_day(case)


def test_bulk_archive_is_atomic_and_preserves_existing_journeys(tmp_path):
    router = service(tmp_path / "source")
    case = scenario([request()], [engineer()])
    case.geography = {"office": geo("office"), "A": geo("A", 55.71)}
    target = RoutingCache(tmp_path / "target.sqlite")
    progress = []
    with router.calculation() as calculation:
        package = calculation.prepare(case)
        _, refs = router.archive.package(fingerprint=package.fingerprint())
        last_key = refs["car"][-1][-1]
        last = calculation.cache._journeys.pop(last_key)
        with pytest.raises(RoutingError, match="Не сохранилась"):
            target.save_calculation("input", package, refs, calculation.cache, lambda *x: None)
        with target.connect() as db:
            assert db.execute("SELECT count(*) FROM journeys").fetchone()[0] == 0
        calculation.cache._journeys[last_key] = last
        target.save_calculation(
            "input", package, refs, calculation.cache, lambda *x: progress.append(x)
        )
        assert progress[-1] == ("Сохранение маршрутов", 4, 4)
        assert target.package(input_key="input") == (package, refs)
        changed = CalculationCache()
        changed._journeys = calculation.cache._journeys.copy()
        key = refs["car"][0][1]
        journey = changed.journey(key)
        journey.legs[1].duration_s += 1
        changed.save_journey(key, journey)
        with pytest.raises(RoutingError, match="изменился"):
            target.save_calculation("input", package, refs, changed, lambda *x: None)
        assert target.journey(key) == calculation.cache.journey(key)


@pytest.mark.parametrize("workers", [0, 17, True, "4"])
def test_worker_configuration_is_bounded(tmp_path, workers):
    import json

    from dispatch.routing.service import RoutingService

    (tmp_path / "config").mkdir()
    (tmp_path / "config/routing.json").write_text(json.dumps({"road_workers": workers}))
    with pytest.raises(ValueError, match="road_workers"):
        RoutingService(tmp_path)
