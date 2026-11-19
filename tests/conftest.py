"""Shared fixtures. Every test runs against recorded responses; nothing touches the network."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from govee_lights.client import GoveeClient
from govee_lights.service import LightService
from govee_lights.settings import AppConfig

FIXTURES = Path(__file__).parent / "fixtures"
BASE = "https://openapi.api.govee.com"
API_KEY = "test-key-9f3b7c1e-do-not-leak"

DEVICES = f"{BASE}/router/api/v1/user/devices"
STATE = f"{BASE}/router/api/v1/device/state"
CONTROL = f"{BASE}/router/api/v1/device/control"
SCENES = f"{BASE}/router/api/v1/device/scenes"


def load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


class FakeTime:
    """A clock that only moves when something sleeps, so tests never wait."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def state_body(online=True, power=1, brightness=50, rgb=0, kelvin=3000) -> dict:
    def cap(type_, instance, value):
        return {"type": f"devices.capabilities.{type_}", "instance": instance, "state": {"value": value}}

    blank = not online
    return {
        "requestId": "x", "msg": "success", "code": 200,
        "payload": {"sku": "H600D", "device": "x", "capabilities": [
            cap("online", "online", online),
            cap("on_off", "powerSwitch", "" if blank else power),
            cap("range", "brightness", "" if blank else brightness),
            cap("color_setting", "colorRgb", "" if blank else rgb),
            cap("color_setting", "colorTemperatureK", "" if blank else kelvin),
            cap("dynamic_scene", "lightScene", ""),
        ]},
    }


def sent(route) -> list[dict]:
    """The JSON bodies sent to a mocked route."""
    return [json.loads(call.request.content) for call in route.calls]


@pytest.fixture
def fake_time() -> FakeTime:
    return FakeTime()


@pytest.fixture
def api():
    with respx.mock(assert_all_called=False) as router:
        router.get(DEVICES).mock(return_value=httpx.Response(200, json=load("devices")))
        router.post(CONTROL).mock(return_value=httpx.Response(200, json=load("control")))
        router.post(STATE).mock(return_value=httpx.Response(200, json=state_body()))
        router.post(SCENES).mock(return_value=httpx.Response(200, json=load("scenes")))
        yield router


@pytest.fixture
async def client(api, fake_time):
    async with GoveeClient(API_KEY, clock=fake_time.clock, sleep=fake_time.sleep) as c:
        yield c


@pytest.fixture
def config() -> AppConfig:
    return AppConfig.model_validate({
        "groups": {"all": "*", "cinema": ["Cinema Left", "Cinema Right"], "balcony": ["balcony *"]},
        "presets": {
            "red": {"rgb": [255, 0, 0], "brightness": 100},
            "white": {"kelvin": 4000, "brightness": 100},
            "too_cold": {"kelvin": 8000},
        },
    })


@pytest.fixture
def service(client, config, tmp_path, fake_time) -> LightService:
    return LightService(client, config, tmp_path / "snapshots.json", clock=fake_time.clock)
