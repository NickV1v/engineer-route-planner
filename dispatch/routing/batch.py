"""Bounded parallel road searches, with a separate native actor in every thread."""

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from threading import local

from dispatch.routing.walking import door_journey


class RoadBatch:
    def __init__(self, workers):
        self.workers = workers
        self._local = local()
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="routing")

    def _route(self, roads, profile, item):
        if not hasattr(self._local, "roads"):
            self._local.roads = {}
        if roads not in self._local.roads:
            self._local.roads[roads] = roads.fork()
        key, origin, target, a, b = item
        return key, door_journey(self._local.roads[roads], profile, origin, target, a, b)

    def routes(self, roads, profile, items):
        # Consume whichever searches finish first: one long bicycle trip must
        # not leave the other workers idle. Pair keys restore matrix order later.
        # Cap outstanding geometry even if validation/serialization is slower.
        source = iter(items)
        pending = set()

        def submit():
            item = next(source, None)
            if item is not None:
                pending.add(self._pool.submit(self._route, roads, profile, item))

        try:
            for _ in range(self.workers * 2):
                submit()
            while pending:
                finished, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in finished:
                    pending.remove(future)
                    result = future.result()
                    submit()
                    yield result
        finally:
            for future in pending:
                future.cancel()

    def close(self):
        # Running native calls finish before the calculation releases its lock.
        # Neither actors nor address results survive into the next calculation.
        self._pool.shutdown(wait=True, cancel_futures=True)
