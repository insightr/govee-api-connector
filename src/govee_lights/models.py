"""Data models shared by the client, the service and the front ends."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

LIGHT_TYPE = "devices.types.light"

POWER = "powerSwitch"
BRIGHTNESS = "brightness"
RGB = "colorRgb"
KELVIN = "colorTemperatureK"
LIGHT_SCENE = "lightScene"


def rgb_to_int(rgb: tuple[int, int, int]) -> int:
    r, g, b = rgb
    return r * 65536 + g * 256 + b


def int_to_rgb(value: int) -> tuple[int, int, int]:
    return (value >> 16) & 255, (value >> 8) & 255, value & 255


class Capability(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: str
    instance: str
    parameters: dict[str, Any] | None = None

    @property
    def range(self) -> tuple[int, int] | None:
        rng = (self.parameters or {}).get("range")
        if not rng or "min" not in rng or "max" not in rng:
            return None
        return int(rng["min"]), int(rng["max"])


class Device(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    sku: str
    device: str
    name: str = Field(default="", alias="deviceName")
    type: str | None = None
    capabilities: list[Capability] = []

    @property
    def is_light(self) -> bool:
        return self.type == LIGHT_TYPE

    def capability(self, instance: str) -> Capability | None:
        return next((c for c in self.capabilities if c.instance == instance), None)


class DeviceState(BaseModel):
    """What one light is doing. Fields are None when Govee cannot report them."""

    device: str
    name: str
    online: bool
    power: bool | None = None
    brightness: int | None = None
    rgb: tuple[int, int, int] | None = None
    kelvin: int | None = None
    error: str | None = None


class DeviceResult(BaseModel):
    """The outcome of one command on one light."""

    device: str
    name: str
    ok: bool
    skipped: bool = False
    detail: str = ""


class SnapshotEntry(BaseModel):
    device: str
    sku: str
    name: str
    power: bool | None = None
    brightness: int | None = None
    rgb: tuple[int, int, int] | None = None
    kelvin: int | None = None


class Snapshot(BaseModel):
    id: str
    created: str
    target: str
    devices: list[SnapshotEntry]
    skipped: list[str] = []
