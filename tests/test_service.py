from __future__ import annotations

import json
import logging

import httpx
import pytest

from govee_lights.errors import AuthError, SnapshotError, TargetError, ValidationError
from govee_lights.service import LightService

from .conftest import CONTROL, DEVICES, STATE, load, sent, state_body


def commands(api) -> list[tuple[str, str, object]]:
    """(device id, instance, value) for every control call made so far."""
    return [
        (b["payload"]["device"], b["payload"]["capability"]["instance"], b["payload"]["capability"]["value"])
        for b in sent(api.post(CONTROL))
    ]


async def ids(service: LightService, target: str) -> list[str]:
    return [d.device for d in await service.resolve(target)]


def per_device(states: dict[str, dict]):
    """A state mock that answers differently per device; default is on, 50%, 3000 K."""

    def reply(request: httpx.Request) -> httpx.Response:
        device = json.loads(request.content)["payload"]["device"]
        return httpx.Response(200, json=states.get(device, state_body()))

    return reply


# ---- devices and targets ----------------------------------------------------


async def test_list_devices_keeps_only_lights_and_caches(service, api):
    lights = await service.list_devices()
    assert len(lights) == 20
    assert sum(d.sku == "H600D" for d in lights) == 18
    await service.list_devices()
    assert api.get(DEVICES).call_count == 1
    await service.list_devices(refresh=True)
    assert api.get(DEVICES).call_count == 2


async def test_resolve_by_id_name_and_group(service):
    left = (await service.resolve("cinema left"))[0]  # names are not case-sensitive
    assert left.name == "Cinema Left"
    assert await service.resolve(left.device) == [left]
    assert {d.name for d in await service.resolve("cinema")} == {"Cinema Left", "Cinema Right"}
    assert len(await service.resolve("balcony")) == 6  # wildcard group
    assert len(await service.resolve("all")) == 20


async def test_resolve_errors_are_clear(service):
    with pytest.raises(TargetError, match="No light or group is called 'kitchen'"):
        await service.resolve("kitchen")
    service.config.groups["Cinema Left"] = ["Cinema Left"]
    with pytest.raises(TargetError, match="both a group in config.yaml and a light name"):
        await service.resolve("Cinema Left")
    service.config.groups["empty"] = ["Nothing *"]
    with pytest.raises(TargetError, match="matches no lights"):
        await service.resolve("empty")


# ---- control and validation ---------------------------------------------------


async def test_basic_controls_send_the_right_values(service, api):
    device = (await ids(service, "Cinema Left"))[0]
    await service.set_power("Cinema Left", True)
    await service.set_power("Cinema Left", False)
    await service.set_brightness("Cinema Left", 40)
    await service.set_color("Cinema Left", (255, 100, 0))
    await service.set_color_temp("Cinema Left", 4000)
    assert commands(api) == [
        (device, "powerSwitch", 1),
        (device, "powerSwitch", 0),
        (device, "brightness", 40),
        (device, "colorRgb", 255 * 65536 + 100 * 256),
        (device, "colorTemperatureK", 4000),
    ]
    types = [b["payload"]["capability"]["type"] for b in sent(api.post(CONTROL))]
    assert types[2] == "devices.capabilities.range"
    assert types[3] == types[4] == "devices.capabilities.color_setting"


@pytest.mark.parametrize(
    ("call", "message"),
    [
        (lambda s: s.set_brightness("cinema", 0), "Brightness 0 is out of range for Cinema Left: it accepts 1 to 100"),
        (lambda s: s.set_brightness("cinema", 101), "out of range"),
        (lambda s: s.set_brightness("cinema", 50.5), "whole number"),
        (lambda s: s.set_color_temp("cinema", 2000), "accepts 2700 to 6500"),
        (lambda s: s.set_color_temp("cinema", 9000), "accepts 2700 to 6500"),
        (lambda s: s.set_color("cinema", (256, 0, 0)), "0 to 255"),
        (lambda s: s.set_color("cinema", (0, 0)), "0 to 255"),
        (lambda s: s.apply_preset("cinema", "purple"), "No preset is called 'purple'"),
        (lambda s: s.apply_preset("cinema", "too_cold"), "accepts 2700 to 6500"),
    ],
)
async def test_bad_values_are_rejected_without_calling_the_api(service, api, call, message):
    with pytest.raises(ValidationError, match=message):
        await call(service)
    assert api.post(CONTROL).call_count == 0


async def test_range_comes_from_each_device(service, api):
    # The Apple TV backlight reports 2000-9000 K, unlike the bulbs.
    await service.set_color_temp("Apple TV Backlight", 8000)
    assert api.post(CONTROL).call_count == 1


async def test_offline_bulb_does_not_block_the_others(service, api):
    left, right = await ids(service, "Cinema Left"), await ids(service, "Cinema Right")

    def reply(request: httpx.Request) -> httpx.Response:
        device = json.loads(request.content)["payload"]["device"]
        return httpx.Response(200, json=load("error_offline" if device == right[0] else "control"))

    api.post(CONTROL).mock(side_effect=reply)
    results = {r.name: r for r in await service.set_power("cinema", True)}
    assert results["Cinema Left"].ok
    assert not results["Cinema Right"].ok
    assert results["Cinema Right"].detail == "offline"
    assert {c[0] for c in commands(api)} == {left[0], right[0]}


async def test_auth_failure_is_raised_not_buried(service, api):
    api.post(CONTROL).mock(return_value=httpx.Response(401))
    with pytest.raises(AuthError, match="GOVEE_API_KEY"):
        await service.set_power("cinema", True)


# ---- state ------------------------------------------------------------------


async def test_state_parses_values_and_reports_offline(service, api):
    left, right = (await ids(service, "Cinema Left"))[0], (await ids(service, "Cinema Right"))[0]
    api.post(STATE).mock(side_effect=per_device({
        left: state_body(power=1, brightness=40, rgb=255, kelvin=0),
        right: load("state_offline"),
    }))
    states = await service.get_state("cinema")
    assert states[left].online and states[left].power is True
    assert states[left].brightness == 40 and states[left].rgb == (0, 0, 255)
    assert states[right].online is False
    assert states[right].power is None and states[right].brightness is None  # stale values not trusted


async def test_state_error_on_one_bulb_is_reported_per_bulb(service, api):
    left, right = (await ids(service, "Cinema Left"))[0], (await ids(service, "Cinema Right"))[0]
    api.post(STATE).mock(side_effect=per_device({right: load("error_not_found")}))
    states = await service.get_state("cinema")
    assert states[left].online
    assert not states[right].online and "400" in states[right].error


async def test_state_cache_is_shared_and_cleared_by_commands(service, api, fake_time):
    await service.get_state("Cinema Left", max_age=30)
    await service.get_state("Cinema Left", max_age=30)
    assert api.post(STATE).call_count == 1
    await service.get_state("Cinema Left")  # max_age 0 always asks
    assert api.post(STATE).call_count == 2
    fake_time.now += 31
    await service.get_state("Cinema Left", max_age=30)
    assert api.post(STATE).call_count == 3
    await service.set_power("Cinema Left", True)
    await service.get_state("Cinema Left", max_age=30)
    assert api.post(STATE).call_count == 4


# ---- presets ----------------------------------------------------------------


async def test_preset_turns_on_and_sets_colour_and_brightness(service, api):
    await service.apply_preset("cinema", "red")
    calls = commands(api)
    assert len(calls) == 6
    for device in await ids(service, "cinema"):
        assert {c[1:] for c in calls if c[0] == device} == {
            ("powerSwitch", 1), ("colorRgb", 0xFF0000), ("brightness", 100),
        }


async def test_repeated_preset_is_skipped(service, api):
    await service.apply_preset("cinema", "red")
    results = await service.apply_preset("cinema", "RED")
    assert all(r.ok and r.skipped for r in results)
    assert api.post(CONTROL).call_count == 6

    await service.apply_preset("cinema", "red", force=True)
    assert api.post(CONTROL).call_count == 12


async def test_preset_skip_is_forgotten_after_other_commands(service, api):
    await service.apply_preset("cinema", "red")
    await service.set_brightness("Cinema Left", 20)  # 7 calls so far
    results = {r.name: r for r in await service.apply_preset("cinema", "red")}
    assert not results["Cinema Left"].skipped
    assert results["Cinema Right"].skipped
    assert api.post(CONTROL).call_count == 10

    await service.apply_preset("cinema", "white")  # a different preset is never skipped
    assert api.post(CONTROL).call_count == 16


async def test_failed_preset_is_not_remembered(service, api):
    api.post(CONTROL).mock(return_value=httpx.Response(200, json=load("error_offline")))
    assert not any(r.ok for r in await service.apply_preset("cinema", "red"))
    api.post(CONTROL).mock(return_value=httpx.Response(200, json=load("control")))
    assert not any(r.skipped for r in await service.apply_preset("cinema", "red"))


# ---- snapshot and restore -----------------------------------------------------


async def test_snapshot_then_restore(service, api, tmp_path):
    left, right = (await ids(service, "Cinema Left"))[0], (await ids(service, "Cinema Right"))[0]
    api.post(STATE).mock(side_effect=per_device({
        left: state_body(power=1, brightness=30, rgb=0x00FFFF, kelvin=0),  # on, in colour
        right: state_body(power=0, brightness=80, rgb=0, kelvin=3300),  # off, in white
    }))
    snapshot_id = await service.snapshot("cinema")
    await service.apply_preset("cinema", "red")
    before = len(commands(api))

    # A new service reading the same file: snapshots survive a restart.
    fresh = LightService(service.client, service.config, tmp_path / "snapshots.json")
    assert all(r.ok for r in await fresh.restore(snapshot_id))

    calls = commands(api)[before:]
    assert {c[1:] for c in calls if c[0] == left} == {("powerSwitch", 1), ("colorRgb", 0x00FFFF), ("brightness", 30)}
    right_calls = [c[1:] for c in calls if c[0] == right]
    assert set(right_calls) == {("colorTemperatureK", 3300), ("brightness", 80), ("powerSwitch", 0)}
    assert right_calls[-1] == ("powerSwitch", 0)  # off goes last: brightness switches a bulb on


async def test_restore_latest_and_unknown_id(service, api):
    with pytest.raises(SnapshotError, match="no snapshots yet"):
        await service.restore()
    first = await service.snapshot("Cinema Left")
    second = await service.snapshot("Cinema Right")
    assert first != second
    assert service.get_snapshot("latest").id == second
    assert [s.id for s in service.list_snapshots()] == [first, second]
    with pytest.raises(SnapshotError, match="No snapshot has the ID"):
        await service.restore("nope")


async def test_snapshot_skips_offline_and_empty_values(service, api, caplog):
    left, right = (await ids(service, "Cinema Left"))[0], (await ids(service, "Cinema Right"))[0]
    api.post(STATE).mock(side_effect=per_device({
        left: state_body(power=1, brightness="", rgb="", kelvin=""),  # e.g. running a scene
        right: load("state_offline"),
    }))
    with caplog.at_level(logging.WARNING):
        snapshot = service.get_snapshot(await service.snapshot("cinema"))
    assert [e.name for e in snapshot.devices] == ["Cinema Left"]
    assert snapshot.skipped == ["Cinema Right"]
    assert "Cinema Right is offline" in caplog.text
    assert "did not report brightness" in caplog.text
    assert "did not report a colour" in caplog.text

    await service.restore()
    assert commands(api) == [(left, "powerSwitch", 1)]  # only what was known


async def test_snapshot_of_only_offline_lights_fails_clearly(service, api):
    api.post(STATE).mock(return_value=httpx.Response(200, json=load("state_offline")))
    with pytest.raises(SnapshotError, match="every light in 'cinema' is offline"):
        await service.snapshot("cinema")


# ---- scenes -----------------------------------------------------------------


async def test_scenes_list_and_apply_send_the_whole_value(service, api):
    assert "Dusk" in await service.list_scenes("cinema")
    await service.apply_scene("Cinema Left", "dusk")
    body = sent(api.post(CONTROL))[0]["payload"]["capability"]
    assert body["type"] == "devices.capabilities.dynamic_scene"
    assert body["instance"] == "lightScene"
    assert set(body["value"]) == {"id", "paramId"}
    with pytest.raises(ValidationError, match="no scene called 'Nope'"):
        await service.apply_scene("Cinema Left", "Nope")
