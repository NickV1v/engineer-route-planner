"""Synthetic client-device requirements and exact morning issuance, without a reserve."""

from collections.abc import Iterable

from dispatch.models import Equipment, Request

EQUIPMENT_POLICY = "client_devices_v1"


def equipment_required(requests: Iterable[Request]) -> Equipment:
    routers = boxes = 0
    for request in requests:
        routers += request.kind == "Подключение"
        boxes += request.kind == "Дозаказ"
    return Equipment(routers=routers, set_top_boxes=boxes)
