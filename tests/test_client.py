from __future__ import annotations

import logging

import httpx
import pytest

from govee_lights.errors import (
    AuthError,
    BadRequestError,
    GoveeConnectionError,
    GoveeError,
    NotFoundError,
    RateLimitedError,
)
from govee_lights.ratelimit import TokenBucket

from .conftest import API_KEY, CONTROL, DEVICES, STATE, load, sent

SKU, DEV = "H600D", "AA:BB:CC:DD:EE:FF:00:17"
POWER = ("devices.capabilities.on_off", "powerSwitch")


async def test_list_devices_reads_the_data_envelope(client, api):
    devices = await client.list_devices()
    bulbs = [d for d in devices if d.sku == "H600D"]
    assert len(bulbs) == 18
    assert all(d.is_light for d in bulbs)
    bulb = bulbs[0]
    assert {"powerSwitch", "brightness", "colorRgb", "colorTemperatureK"} <= {c.instance for c in bulb.capabilities}
    assert bulb.capability("brightness").range == (1, 100)
    assert bulb.capability("colorTemperatureK").range == (2700, 6500)
    # Thermometers and app-side groups are on the account but are not lights.
    assert any(not d.is_light for d in devices)


async def test_every_request_carries_the_key_header(client, api):
    await client.list_devices()
    request = api.calls.last.request
    assert request.headers["Govee-API-Key"] == API_KEY
    assert request.url.host == "openapi.api.govee.com"


async def test_state_reads_the_payload_envelope(client, api):
    api.post(STATE).mock(return_value=httpx.Response(200, json=load("state_online")))
    values = await client.get_state("H6076", "AA:BB:CC:DD:EE:FF:00:0F")
    assert values["online"] is True
    assert values["brightness"] == 77
    assert values["colorTemperatureK"] == 3200
    assert values["lightScene"] == ""  # cannot be queried


async def test_offline_state_has_empty_values(client, api):
    api.post(STATE).mock(return_value=httpx.Response(200, json=load("state_offline")))
    values = await client.get_state(SKU, DEV)
    assert values["online"] is False
    assert values["powerSwitch"] == ""


async def test_control_body_shape(client, api):
    await client.control(SKU, DEV, "devices.capabilities.range", "brightness", 40)
    body = sent(api.post(CONTROL))[0]
    assert len(body["requestId"]) == 36
    assert body["payload"] == {
        "sku": SKU, "device": DEV,
        "capability": {"type": "devices.capabilities.range", "instance": "brightness", "value": 40},
    }


async def test_scenes_keep_object_values(client, api):
    capabilities = await client.list_scenes(SKU, DEV)
    option = capabilities[0].parameters["options"][0]
    assert option["name"] == "Dusk"
    assert set(option["value"]) == {"id", "paramId"}


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (httpx.Response(401, json={"message": "Unauthorized"}), AuthError),
        (httpx.Response(200, json={"code": 401, "msg": "invalid key"}), AuthError),
        (httpx.Response(404, json={"msg": "not found"}), NotFoundError),
        (httpx.Response(400, json={"msg": "bad"}), BadRequestError),
        (httpx.Response(200, json=load("error_not_found")), BadRequestError),  # error inside a 200
        (httpx.Response(500, text="oops"), GoveeError),
    ],
)
async def test_typed_errors(client, api, response, error):
    api.post(CONTROL).mock(return_value=response)
    with pytest.raises(error):
        await client.control(SKU, DEV, *POWER, 1)


async def test_auth_error_tells_the_user_what_to_check(client, api):
    api.get(DEVICES).mock(return_value=httpx.Response(401, json={"message": "Unauthorized"}))
    with pytest.raises(AuthError, match="Check GOVEE_API_KEY in your .env file"):
        await client.list_devices()


async def test_429_is_retried_then_succeeds(client, api, fake_time):
    api.post(CONTROL).mock(side_effect=[
        httpx.Response(429, headers={"Retry-After": "2"}, json={"msg": "slow down"}),
        httpx.Response(200, json={"code": 429, "msg": "slow down"}),  # 429 inside a 200
        httpx.Response(200, json=load("control")),
    ])
    await client.control(SKU, DEV, *POWER, 1)
    assert api.post(CONTROL).call_count == 3
    assert fake_time.sleeps[0] == 2  # honoured Retry-After
    assert 2 <= fake_time.sleeps[1] <= 2.5  # then exponential backoff


async def test_429_gives_up_with_a_typed_error(client, api, fake_time):
    api.post(CONTROL).mock(return_value=httpx.Response(429, json={"msg": "slow down"}))
    with pytest.raises(RateLimitedError):
        await client.control(SKU, DEV, *POWER, 1)
    assert api.post(CONTROL).call_count == 4  # first try plus three retries


async def test_network_failure_is_a_clear_error(client, api):
    api.get(DEVICES).mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(GoveeConnectionError, match="Could not reach the Govee cloud"):
        await client.list_devices()


async def test_per_device_limit_spaces_out_a_burst(client, api, fake_time):
    """20 commands to one bulb: 6 go at once, the rest are held to 2 per second."""
    for _ in range(20):
        await client.control(SKU, DEV, *POWER, 1)
    assert api.post(CONTROL).call_count == 20
    assert fake_time.now == pytest.approx(7.0)  # 14 waits of half a second


async def test_burst_of_20_survives_govee_429s(client, api, fake_time):
    """Even if Govee pushes back mid-burst, every command completes."""
    replies = iter([429 if i in (5, 6, 12) else 200 for i in range(23)])
    api.post(CONTROL).mock(
        side_effect=lambda request: httpx.Response(200, json=load("control"))
        if next(replies) == 200 else httpx.Response(429, json={"msg": "slow down"})
    )
    for _ in range(20):
        await client.control(SKU, DEV, *POWER, 1)
    assert api.post(CONTROL).call_count == 23


async def test_token_bucket(fake_time):
    bucket = TokenBucket(2, 3, clock=fake_time.clock, sleep=fake_time.sleep)
    for _ in range(3):
        await bucket.acquire()
    assert fake_time.sleeps == []
    await bucket.acquire()
    assert fake_time.sleeps == [0.5]
    fake_time.now += 10  # refills, but never past capacity
    for _ in range(3):
        await bucket.acquire()
    assert fake_time.sleeps == [0.5]


async def test_api_key_never_appears_in_errors_or_logs(client, api, caplog):
    caplog.set_level(logging.DEBUG)
    messages = []
    for response in (httpx.Response(401), httpx.Response(429), httpx.Response(500, text="x"), httpx.ConnectError("x")):
        api.get(DEVICES).mock(side_effect=[response] * 4)
        with pytest.raises(GoveeError) as caught:
            await client.list_devices()
        messages.append(str(caught.value) + repr(caught.value))
    assert API_KEY not in "".join(messages)
    assert API_KEY not in caplog.text
    assert API_KEY not in repr(client.__dict__)
