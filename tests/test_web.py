from __future__ import annotations

import httpx
import pytest

from govee_lights.web import create_app

from .conftest import API_KEY, CONTROL, load, sent


@pytest.fixture
async def web(service):
    app = create_app(service)
    app.state.service = service  # the ASGI transport does not run lifespan events
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as browser:
        yield browser


@pytest.fixture(autouse=True)
def _let_test_host_through(api):
    api.route(host="test").pass_through()


async def test_page_and_overview(web):
    page = await web.get("/")
    assert page.status_code == 200 and "Govee lights" in page.text

    overview = (await web.get("/api/overview")).json()
    assert len(overview["devices"]) == 20
    assert set(overview["groups"]) == {"all", "cinema", "balcony"}
    assert overview["presets"]["red"] == {"rgb": [255, 0, 0], "brightness": 100}
    bulb = next(d for d in overview["devices"] if d["name"] == "Cinema Left")
    assert bulb["brightness"] == [1, 100] and bulb["kelvin"] == [2700, 6500] and bulb["color"]


async def test_state_route(web):
    states = (await web.get("/api/state", params={"target": "cinema"})).json()
    assert len(states) == 2
    assert all(s["online"] and s["brightness"] == 50 for s in states.values())


async def test_control_routes(web, api):
    for path, body in [
        ("/api/power", {"target": "Cinema Left", "on": True}),
        ("/api/brightness", {"target": "Cinema Left", "percent": 40}),
        ("/api/color", {"target": "Cinema Left", "rgb": [0, 0, 255]}),
        ("/api/temp", {"target": "Cinema Left", "kelvin": 4000}),
    ]:
        response = await web.post(path, json=body)
        assert response.status_code == 200, path
        assert response.json()["results"][0]["ok"]
    values = [b["payload"]["capability"]["value"] for b in sent(api.post(CONTROL))]
    assert values == [1, 40, 255, 4000]


async def test_preset_snapshot_restore_routes(web, api):
    snapshot = (await web.post("/api/snapshot", json={"target": "cinema"})).json()
    assert len(snapshot["devices"]) == 2
    results = (await web.post("/api/preset", json={"target": "cinema", "preset": "red"})).json()["results"]
    assert len(results) == 2 and all(r["ok"] for r in results)
    assert len((await web.get("/api/snapshots")).json()) == 1
    restored = await web.post("/api/restore", json={"snapshot_id": snapshot["id"]})
    assert all(r["ok"] for r in restored.json()["results"])


async def test_errors_become_clear_json(web, api):
    response = await web.post("/api/brightness", json={"target": "cinema", "percent": 500})
    assert response.status_code == 422 and "out of range" in response.json()["error"]

    response = await web.post("/api/power", json={"target": "kitchen", "on": True})
    assert response.status_code == 404 and "No light or group" in response.json()["error"]

    api.post(CONTROL).mock(return_value=httpx.Response(401))
    response = await web.post("/api/power", json={"target": "cinema", "on": True})
    assert response.status_code == 500 and "GOVEE_API_KEY" in response.json()["error"]

    api.post(CONTROL).mock(return_value=httpx.Response(200, json=load("error_offline")))
    response = await web.post("/api/power", json={"target": "cinema", "on": True})
    assert response.status_code == 200
    assert [r["detail"] for r in response.json()["results"]] == ["offline", "offline"]


async def test_api_key_never_reaches_the_browser(web):
    for path in ("/", "/api/overview", "/api/devices", "/api/state?target=cinema", "/api/snapshots"):
        assert API_KEY not in (await web.get(path)).text
