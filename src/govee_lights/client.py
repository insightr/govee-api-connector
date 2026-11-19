"""GoveeClient: raw calls to the Govee cloud API, with rate limiting and retries.

Notes on the API that differ from Govee's reference pages:
- The device list answers with {code, message, data}; every other endpoint
  answers with {requestId, msg, code, payload}.
- Errors usually arrive as HTTP 200 with the real status in the body's `code`.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from .errors import (
    AuthError,
    BadRequestError,
    GoveeConnectionError,
    GoveeError,
    NotFoundError,
    RateLimitedError,
)
from .models import Capability, Device
from .ratelimit import TokenBucket

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://openapi.api.govee.com"
MAX_BACKOFF_SECONDS = 30.0


class GoveeClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout: float = 15.0,
        max_retries: int = 3,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._http = httpx.AsyncClient(
            base_url=base_url,
            headers={"Govee-API-Key": api_key, "Content-Type": "application/json"},
            timeout=timeout,
        )
        self._max_retries = max_retries
        self._clock = clock
        self._sleep = sleep
        # Govee's published limits.
        self._devices_bucket = self._bucket(30 / 60, 30)  # device list, per account
        self._control_bucket = self._bucket(12, 80)  # control, per account
        self._device_control: dict[str, TokenBucket] = {}  # 2/s, burst 6, per device
        self._device_state: dict[str, TokenBucket] = {}  # 30/min per device
        self._device_scenes: dict[str, TokenBucket] = {}  # 30/min per device

    def _bucket(self, rate: float, capacity: float) -> TokenBucket:
        return TokenBucket(rate, capacity, clock=self._clock, sleep=self._sleep)

    def _per_device(self, table: dict[str, TokenBucket], device: str, rate: float, cap: float) -> TokenBucket:
        if device not in table:
            table[device] = self._bucket(rate, cap)
        return table[device]

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> GoveeClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ---- endpoints ---------------------------------------------------------

    async def list_devices_raw(self) -> list[dict[str, Any]]:
        body = await self._request("GET", "/router/api/v1/user/devices", None, [self._devices_bucket])
        return body.get("data") or []

    async def list_devices(self) -> list[Device]:
        return [Device.model_validate(item) for item in await self.list_devices_raw()]

    async def get_state(self, sku: str, device: str) -> dict[str, Any]:
        """Return {instance: value} for one device. Empty values stay as ''."""
        bucket = self._per_device(self._device_state, device, 30 / 60, 30)
        body = await self._request(
            "POST", "/router/api/v1/device/state", self._body(sku, device), [bucket]
        )
        capabilities = (body.get("payload") or {}).get("capabilities") or []
        return {c["instance"]: (c.get("state") or {}).get("value") for c in capabilities}

    async def control(self, sku: str, device: str, type: str, instance: str, value: Any) -> None:
        """Send one capability to one device. `type` must be the string the device reported."""
        buckets = [self._control_bucket, self._per_device(self._device_control, device, 2, 6)]
        payload = self._body(sku, device)
        payload["payload"]["capability"] = {"type": type, "instance": instance, "value": value}
        await self._request("POST", "/router/api/v1/device/control", payload, buckets)

    async def list_scenes(self, sku: str, device: str) -> list[Capability]:
        return await self._scenes("/router/api/v1/device/scenes", sku, device)

    async def list_diy_scenes(self, sku: str, device: str) -> list[Capability]:
        return await self._scenes("/router/api/v1/device/diy-scenes", sku, device)

    async def _scenes(self, path: str, sku: str, device: str) -> list[Capability]:
        bucket = self._per_device(self._device_scenes, device, 30 / 60, 30)
        body = await self._request("POST", path, self._body(sku, device), [bucket])
        capabilities = (body.get("payload") or {}).get("capabilities") or []
        return [Capability.model_validate(c) for c in capabilities]

    # ---- plumbing ----------------------------------------------------------

    @staticmethod
    def _body(sku: str, device: str) -> dict[str, Any]:
        return {"requestId": str(uuid.uuid4()), "payload": {"sku": sku, "device": device}}

    async def _request(
        self, method: str, path: str, json: dict[str, Any] | None, buckets: list[TokenBucket]
    ) -> dict[str, Any]:
        attempt = 0
        while True:
            for bucket in buckets:
                await bucket.acquire()
            try:
                response = await self._http.request(method, path, json=json)
            except httpx.HTTPError as exc:
                raise GoveeConnectionError(
                    f"Could not reach the Govee cloud ({type(exc).__name__}). "
                    "Check the internet connection and try again."
                ) from None

            body = self._parse(response)
            code = self._code(response, body)
            if code == 200:
                return body
            message = str(body.get("msg") or body.get("message") or response.reason_phrase or "")

            if code == 429:
                if attempt >= self._max_retries:
                    raise RateLimitedError(
                        f"Govee is still rate limiting after {self._max_retries} retries. "
                        "Wait a minute and try again.",
                        429,
                    )
                delay = self._retry_delay(response, attempt)
                attempt += 1
                log.warning("Rate limited by Govee; retry %d in %.1fs", attempt, delay)
                await self._sleep(delay)
                continue

            raise self._error(code, message)

    @staticmethod
    def _parse(response: httpx.Response) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}

    @staticmethod
    def _code(response: httpx.Response, body: dict[str, Any]) -> int:
        if response.status_code != 200:
            return response.status_code
        try:
            return int(body.get("code", 200))
        except (TypeError, ValueError):
            return 200

    @staticmethod
    def _retry_delay(response: httpx.Response, attempt: int) -> float:
        try:
            delay = float(response.headers.get("Retry-After", ""))
        except ValueError:
            delay = (2**attempt) + random.uniform(0, 0.5)
        return max(0.0, min(delay, MAX_BACKOFF_SECONDS))

    @staticmethod
    def _error(code: int, message: str) -> GoveeError:
        if code == 401:
            return AuthError(
                "Govee rejected the API key (401). Check GOVEE_API_KEY in your .env file.", 401
            )
        if code == 404:
            return NotFoundError(f"Govee could not find that device or capability (404): {message}", 404)
        if code == 400:
            return BadRequestError(f"Govee rejected the request (400): {message}", 400)
        return GoveeError(f"Govee returned an error ({code}): {message}", code)
