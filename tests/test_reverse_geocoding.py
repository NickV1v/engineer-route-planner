import pytest

from dispatch.geocode import GeocodingError, PhotonClient
from dispatch.models import GeoPoint


def feature(lat=55.71, lon=37.61, house="7"):
    return {
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": {
            "countrycode": "ru",
            "city": "Москва",
            "street": "Тестовая улица",
            "housenumber": house,
        },
    }


@pytest.mark.parametrize(
    "features,expected",
    [
        ([feature(lat=55.7101)], "Москва, Тестовая улица, 7"),
        ([feature(lat=55.7101, house="")], "Москва, Тестовая улица"),
        ([feature(lat=55.73)], None),
        ([], None),
    ],
)
def test_reverse_uses_nearby_result_without_moving_input(monkeypatch, features, expected):
    client = PhotonClient(None, "https://photon.example")
    point = GeoPoint(lat=55.71, lon=37.61, label="Исходная точка", source="csv", precision="manual")
    before = point.model_dump()

    def fetch(address, url, params, **kwargs):
        assert url == "https://photon.example/reverse"
        assert params["lat"] == point.lat and params["lon"] == point.lon
        assert params["radius"] == 0.1
        return {"features": features}

    monkeypatch.setattr(client, "fetch", fetch)
    assert client.reverse(point) == expected
    assert point.model_dump() == before


def test_reverse_rejects_malformed_response(monkeypatch):
    client = PhotonClient(None, "https://photon.example")
    monkeypatch.setattr(client, "fetch", lambda *a, **kw: {"features": [{"geometry": None}]})
    with pytest.raises(GeocodingError):
        client.reverse(
            GeoPoint(lat=55.71, lon=37.61, label="Точка", source="csv", precision="manual")
        )
