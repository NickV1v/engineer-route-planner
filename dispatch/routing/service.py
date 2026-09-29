"""Routing integration for new days, append-only day events, and snapshot geometry."""

import json
import os
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from time import perf_counter

from dispatch.routing.batch import RoadBatch
from dispatch.routing.cache import CalculationCache, RoutingCache, pair_key
from dispatch.routing.contracts import Coordinate, RoutingError, content_hash
from dispatch.routing.matrices import apply_transport, prepare_transport
from dispatch.routing.public import PUBLIC_MODEL, PublicRouter
from dispatch.routing.roads import RoadRouter
from dispatch.routing.transit import TransitNetwork
from dispatch.routing.walking import ACCESS_MODEL, door_journey, street_network_id


class RoutingService:
    def __init__(self, root: Path, mode=None, *, archive_path=None):
        self.root = root
        self.mode = mode or os.environ.get("DISPATCH_ROUTING_MODE", "real")
        if self.mode not in {"real", "synthetic"}:
            raise ValueError("DISPATCH_ROUTING_MODE должен быть real или synthetic")
        config_path = root / "config/routing.json"
        self.config = json.loads(config_path.read_text()) if config_path.exists() else {}
        self.road_workers = self.config.get(
            "road_workers", min(8, max(1, (os.cpu_count() or 2) - 2))
        )
        if type(self.road_workers) is not int or not 1 <= self.road_workers <= 16:
            raise ValueError("road_workers должен быть целым числом от 1 до 16")
        self.archive = RoutingCache(
            archive_path or root / self.config.get("cache", "artifacts/routing-cache.sqlite")
        )
        self._lock = Lock()
        self.roads = self.public = None
        self.bicycle_roads = None
        self.progress = None

    def status(self):
        ready = all(
            (self.root / self.config.get(key, "__missing__") / "manifest.json").exists()
            for key in (
                "roads",
                "transit",
                *(["bicycle_roads"] if "bicycle_roads" in self.config else []),
            )
        )
        return {
            "mode": self.mode,
            "ready": ready if self.mode == "real" else True,
            "progress": self.progress,
            "cache_scope": "calculation",
            "description": "Локальные дороги, метро, МЦК/МЦД и автобусы; статическая оценка времени",
        }

    def ensure(self):
        if self.roads is not None:
            return
        self.progress = {
            "stage": "Загрузка дорожной и транспортной сети",
            "completed": 0,
            "total": 1,
        }
        try:
            roads = RoadRouter(self.root / self.config["roads"])
            bicycle_roads = (
                RoadRouter(self.root / self.config["bicycle_roads"])
                if "bicycle_roads" in self.config
                else roads
            )
            path = self.root / self.config["transit"]
            network = TransitNetwork.model_validate_json((path / "network.json").read_bytes())
            manifest = json.loads((path / "manifest.json").read_text())
            street_hash = content_hash({"road_network": roads.network_id, "access": ACCESS_MODEL})
            if (
                manifest["schema_version"] != "connected_transit_v1"
                or manifest["network_hash"] != content_hash(network.model_dump())
                or manifest["road_network"] != street_hash
                or manifest["model"] != PUBLIC_MODEL
            ):
                raise RoutingError(
                    "Дороги, пешеходные связи и транспортная сеть имеют разные версии"
                )
            if not any(p.mode == "metro" for p in network.patterns) or not any(
                p.mode == "bus" for p in network.patterns
            ):
                raise RoutingError("В сети должны быть метро и автобусы")
            self.street_hash = street_hash
            self.roads = roads
            self.bicycle_roads = bicycle_roads
            self.public = PublicRouter(network, street_hash, None)
        except RoutingError:
            self.roads = self.public = None
            self.bicycle_roads = None
            raise
        except (OSError, KeyError, ValueError) as exc:
            self.roads = self.public = None
            self.bicycle_roads = None
            raise RoutingError(
                "Транспортная сеть не подготовлена или повреждена. Подготовьте графы маршрутизации и проверьте config/routing.json."
            ) from exc

    def road_router(self, profile):
        return (self.bicycle_roads or self.roads) if profile == "bicycle" else self.roads

    def road_network(self, profile):
        return (
            street_network_id(self.road_router(profile), profile)
            if profile == "bicycle"
            else self.street_hash
        )

    @contextmanager
    def calculation(self, progress=None):
        """One user calculation owns all reusable OD results, even on failure."""
        waiting = perf_counter()
        with self._lock:
            run = RoutingCalculation(self, progress)
            run.timings["queue_wait_ms"] = (perf_counter() - waiting) * 1000
            try:
                yield run
            finally:
                run.close()
                self.progress = None

    def new_day(self, scenario):
        with self.calculation() as run:
            return run.new_day(scenario)

    def extend(self, scenario, location):
        with self.calculation() as run:
            run.refresh(scenario, location)

    def journeys(self, scenario, plan):
        if scenario.manifest.get("transport") != "routing_static_v1":
            return {}
        stored = self.archive.package(fingerprint=scenario.manifest["routing"]["package_hash"])
        if stored is None:
            raise RoutingError(
                "Геометрия транспортного пакета отсутствует; план не будет опубликован"
            )
        package, refs = stored
        if package.matrices != scenario.matrices:
            raise RoutingError("Матрицы плана отличаются от сохранённого транспортного пакета")
        jobs = {r.id: r for r in scenario.requests}
        engineers = {e.id: e for e in scenario.engineers}
        result = {}
        for route in plan.routes:
            engineer = engineers[route.engineer_id]
            matrix = package.matrices[engineer.profile]
            origin = engineer.start_location_id
            result[engineer.id] = []
            for visit in route.visits:
                target = jobs[visit.request_id].location_id
                key = refs[engineer.profile][matrix.locations.index(origin)][
                    matrix.locations.index(target)
                ]
                journey = self.archive.journey(key)
                if (
                    journey is None
                    or journey.from_id != origin
                    or journey.to_id != target
                    or journey.network_id != package.networks[engineer.profile]
                    or journey.duration_s != visit.travel_s
                    or journey.distance_m != visit.distance_m
                ):
                    raise RoutingError(
                        "Геометрия поездки не соответствует времени и расстоянию плана"
                    )
                result[engineer.id].append({"request_id": visit.request_id, **journey.report()})
                origin = target
        return result


class RoutingCalculation:
    def __init__(self, service, progress=None):
        self.service = service
        self._progress = progress
        self.cache = CalculationCache()
        self.public = None
        self.closed = False
        self._road_batch = None
        self.profile_timings = {}
        self.timings = {
            "network_load_ms": 0.0,
            "routing_ms": 0.0,
            "archive_write_ms": 0.0,
            "solver_ms": 0.0,
            "queue_wait_ms": 0.0,
        }
        self.counts = {"road_searches": 0, "public_source_searches": 0, "cache_hits": 0}

    def close(self):
        if self._road_batch is not None:
            self._road_batch.close()
            self._road_batch = None
        self.cache.clear()
        if self.public:
            self.public.clear_calculation()
        self.public = None
        self.closed = True

    def report(self):
        return {
            "cache_scope": "calculation",
            **self.counts,
            **{key: round(value, 3) for key, value in self.timings.items()},
            "routing_profiles_ms": {k: round(v, 3) for k, v in self.profile_timings.items()},
        }

    def ensure(self):
        if self.closed:
            raise RoutingError("Расчёт уже завершён; начните новый запуск")
        if self.public is None:
            started = perf_counter()
            self.progress("Загрузка дорожной и транспортной сети", 0, 1)
            self.service.ensure()
            self.public = self.service.public.for_calculation(
                lambda *args: self.road("walk", *args)
            )
            self.timings["network_load_ms"] += (perf_counter() - started) * 1000

    def progress(self, stage, completed, total):
        self.service.progress = {"stage": stage, "completed": completed, "total": total}
        if self._progress:
            self._progress(stage, completed, total)

    def road(self, profile, origin, target, a, b):
        if self.closed:
            raise RoutingError("Расчёт уже завершён; начните новый запуск")
        key = pair_key(self.service.road_network(profile), profile, origin, target, a, b)
        journey = self.cache.journey(key)
        if journey is None:
            if origin != target and a is not None and b is not None and a != b:
                self.counts["road_searches"] += 1
            journey = door_journey(self.service.road_router(profile), profile, origin, target, a, b)
            self.cache.save_journey(key, journey)
        else:
            self.counts["cache_hits"] += 1
        return journey

    @staticmethod
    def coordinates(scenario, new_location=None):
        if not scenario.matrices:
            raise RoutingError("Нет транспортных профилей для расчёта")
        locations = next(iter(scenario.matrices.values())).locations.copy()
        if new_location and new_location not in locations:
            locations.append(new_location)
        return {
            key: Coordinate(lat=record.point.lat, lon=record.point.lon)
            if (record := scenario.geography.get(key)) and record.point
            else None
            for key in locations
        }

    def prepare(self, scenario, new_location=None, *, preserve=False):
        self.ensure()
        started = perf_counter()
        coordinates = self.coordinates(scenario, new_location)
        profiles = list(scenario.matrices)
        networks = {
            p: self.public.network_id
            if p == "public_transport_approx"
            else self.service.road_network(p)
            for p in profiles
        }
        input_key = content_hash(
            {
                "coordinates": {k: v.model_dump() if v else None for k, v in coordinates.items()},
                "order": list(coordinates),
                "networks": networks,
            }
        )
        old = None
        if preserve:
            # Read accepted inputs only to validate invariants. Every OD result
            # below is freshly calculated in this operation, including old pairs.
            old = self.service.archive.package(
                fingerprint=scenario.manifest["routing"]["package_hash"]
            )
            if old is None or old[0].networks != networks:
                raise RoutingError(
                    "Для перепланирования нужен прежний транспортный набор. Создайте новый план дня для смены сети."
                )
            if any(coordinates[k] != point for k, point in old[0].coordinates.items()):
                raise RoutingError("Координаты существующих матриц изменены")
            if old[0].matrices != scenario.matrices:
                raise RoutingError("Существующие матрицы отличаются от сохранённого пакета")

        refs = {profile: [] for profile in profiles}
        completed = 0
        total = len(coordinates) ** 2 * len(profiles)
        prepared_profiles = set()
        reported = 0

        def report_progress(stage, count):
            nonlocal reported
            if count >= reported:
                self.progress(stage, count, total)
                reported = count

        def road_result(profile, origin, target, key, a, b):
            roads = self.service.road_router(profile)
            if self.service.road_workers == 1 or not isinstance(roads, RoadRouter):
                return self.road(profile, origin, target, a, b)
            if self._road_batch is None:
                self._road_batch = RoadBatch(self.service.road_workers)
            if profile not in prepared_profiles:
                # Freeze the missing inputs before workers start; only the main
                # thread reads/writes CalculationCache and progress records.
                pending = []
                for start, point in coordinates.items():
                    for end, other in coordinates.items():
                        pair = pair_key(networks[profile], profile, start, end, point, other)
                        if not self.cache.contains(pair):
                            pending.append((pair, start, end, point, other))
                searched = {
                    item[0]
                    for item in pending
                    if item[1] != item[2]
                    and item[3] is not None
                    and item[4] is not None
                    and item[3] != item[4]
                }
                ready = completed + len(coordinates) ** 2 - len(pending)
                self.counts["cache_hits"] += len(coordinates) ** 2 - len(pending)
                iterator = self._road_batch.routes(roads, profile, pending)
                try:
                    for result_key, journey in iterator:
                        self.cache.save_journey(result_key, journey)
                        self.counts["road_searches"] += result_key in searched
                        ready += 1
                        report_progress("Матрицы переездов", ready)
                finally:
                    iterator.close()
                prepared_profiles.add(profile)
            journey = self.cache.journey(key)
            if journey is None:
                raise RoutingError("Параллельный расчёт не вернул запрошенную поездку")
            return journey

        def get(profile, origin, target):
            nonlocal completed
            profile_started = perf_counter()
            a, b = coordinates[origin], coordinates[target]
            key = pair_key(networks[profile], profile, origin, target, a, b)
            if profile == "public_transport_approx":
                journey = self.cache.journey(key)
                if journey is None:
                    report_progress("Общественный транспорт", completed)
                    if a is not None:
                        self.counts["public_source_searches"] += 1
                    for destination, value in self.public.many(origin, coordinates).items():
                        current_key = pair_key(
                            networks[profile],
                            profile,
                            origin,
                            destination,
                            a,
                            coordinates[destination],
                        )
                        self.cache.save_journey(current_key, value)
                    journey = self.cache.journey(key)
                    if journey is None:
                        raise RoutingError("Маршрутизатор не вернул запрошенную пару")
                else:
                    self.counts["cache_hits"] += 1
            else:
                journey = road_result(profile, origin, target, key, a, b)
            self.profile_timings[profile] = (
                self.profile_timings.get(profile, 0) + (perf_counter() - profile_started) * 1000
            )
            if not refs[profile] or len(refs[profile][-1]) == len(coordinates):
                refs[profile].append([])
            refs[profile][-1].append(key)
            completed += 1
            report_progress("Матрицы переездов", completed)
            return journey

        package = prepare_transport(
            coordinates, {p: lambda a, b, p=p: get(p, a, b) for p in profiles}
        )
        if old:
            for profile, matrix in old[0].matrices.items():
                n = len(matrix.locations)
                updated = package.matrices[profile]
                if [row[:n] for row in updated.duration_s[:n]] != matrix.duration_s or [
                    row[:n] for row in updated.distance_m[:n]
                ] != matrix.distance_m:
                    raise RoutingError(
                        "Повторный расчёт изменил принятые поездки; создайте новый план дня"
                    )
        self.timings["routing_ms"] += (perf_counter() - started) * 1000
        started = perf_counter()
        # Archive only complete address-to-address results. Access-query caches
        # remain local and disappear with the operation.
        self.service.archive.save_calculation(input_key, package, refs, self.cache, self.progress)
        self.timings["archive_write_ms"] += (perf_counter() - started) * 1000
        return package

    def new_day(self, scenario):
        if self.service.mode == "synthetic":
            return scenario
        return apply_transport(scenario, self.prepare(scenario))

    def refresh(self, scenario, new_location=None):
        if scenario.manifest.get("transport") != "routing_static_v1":
            if new_location:
                from dispatch.scenarios import extend_matrices

                extend_matrices(scenario, new_location)
            return
        if new_location and any(
            new_location not in m.locations for m in scenario.matrices.values()
        ):
            record = scenario.geography.get(new_location)
            if not record or not record.point:
                raise ValueError("Для новой заявки выберите подтверждённые координаты дома")
        package = self.prepare(scenario, new_location, preserve=True)
        scenario.matrices = package.matrices
        scenario.manifest["routing"] = {
            "package_hash": package.fingerprint(),
            "networks": package.networks,
            "statuses": package.statuses,
        }
