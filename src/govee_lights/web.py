"""Web front end: JSON routes that mirror LightService, plus one static page.

The API key never leaves the server: no route returns settings.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from .errors import (
    AuthError,
    ConfigError,
    GoveeError,
    GoveeLightsError,
    SnapshotError,
    TargetError,
    ValidationError,
)
from .models import BRIGHTNESS, KELVIN, POWER, RGB
from .service import LightService

STATIC = Path(__file__).parent / "static"


class PowerBody(BaseModel):
    target: str
    on: bool


class BrightnessBody(BaseModel):
    target: str
    percent: int


class ColorBody(BaseModel):
    target: str
    rgb: tuple[int, int, int]


class TempBody(BaseModel):
    target: str
    kelvin: int


class PresetBody(BaseModel):
    target: str
    preset: str
    force: bool = False


class SnapshotBody(BaseModel):
    target: str


class RestoreBody(BaseModel):
    snapshot_id: str = "latest"


def _status_for(exc: GoveeLightsError) -> int:
    if isinstance(exc, ValidationError):
        return 422
    if isinstance(exc, (TargetError, SnapshotError)):
        return 404
    if isinstance(exc, (AuthError, ConfigError)):
        return 500
    if isinstance(exc, GoveeError):
        return 502
    return 500


def create_app(service: LightService | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.service = service or LightService.from_settings()
        yield
        if service is None:
            await app.state.service.aclose()

    app = FastAPI(title="Govee lights", lifespan=lifespan, docs_url=None, redoc_url=None)

    @app.exception_handler(GoveeLightsError)
    async def _known_error(request: Request, exc: GoveeLightsError) -> JSONResponse:
        return JSONResponse({"error": str(exc)}, status_code=_status_for(exc))

    def svc(request: Request) -> LightService:
        return request.app.state.service

    def dump(results):
        return {"results": [r.model_dump() for r in results]}

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/api/overview")
    async def overview(request: Request, refresh: bool = False):
        """Lights, groups and presets: everything the page needs to draw itself."""
        service = svc(request)
        lights = await service.list_devices(refresh=refresh)
        groups = await service.groups()

        def describe(device):
            brightness, kelvin = device.capability(BRIGHTNESS), device.capability(KELVIN)
            return {
                "device": device.device,
                "name": device.name,
                "sku": device.sku,
                "power": device.capability(POWER) is not None,
                "color": device.capability(RGB) is not None,
                "brightness": brightness.range if brightness else None,
                "kelvin": kelvin.range if kelvin else None,
            }

        return {
            "poll_seconds": service.config.poll_seconds,
            "devices": [describe(d) for d in sorted(lights, key=lambda d: d.name.casefold())],
            "groups": {name: [m.device for m in members] for name, members in groups.items()},
            "presets": {n: p.model_dump(exclude_none=True) for n, p in service.config.presets.items()},
        }

    @app.get("/api/devices")
    async def devices(request: Request, refresh: bool = False):
        return [d.model_dump() for d in await svc(request).list_devices(refresh=refresh)]

    @app.get("/api/state")
    async def state(request: Request, target: str = "all", fresh: bool = False):
        service = svc(request)
        # Browsers share one cached reading so several open pages cost one poll.
        max_age = 0 if fresh else max(service.config.poll_seconds - 2, 0)
        states = await service.get_state(target, max_age=max_age)
        return {device: s.model_dump() for device, s in states.items()}

    @app.post("/api/power")
    async def power(request: Request, body: PowerBody):
        return dump(await svc(request).set_power(body.target, body.on))

    @app.post("/api/brightness")
    async def brightness(request: Request, body: BrightnessBody):
        return dump(await svc(request).set_brightness(body.target, body.percent))

    @app.post("/api/color")
    async def color(request: Request, body: ColorBody):
        return dump(await svc(request).set_color(body.target, body.rgb))

    @app.post("/api/temp")
    async def temp(request: Request, body: TempBody):
        return dump(await svc(request).set_color_temp(body.target, body.kelvin))

    @app.post("/api/preset")
    async def preset(request: Request, body: PresetBody):
        return dump(await svc(request).apply_preset(body.target, body.preset, force=body.force))

    @app.post("/api/snapshot")
    async def snapshot(request: Request, body: SnapshotBody):
        service = svc(request)
        snapshot_id = await service.snapshot(body.target)
        return service.get_snapshot(snapshot_id).model_dump()

    @app.get("/api/snapshots")
    async def snapshots(request: Request):
        return [s.model_dump() for s in svc(request).list_snapshots()]

    @app.post("/api/restore")
    async def restore(request: Request, body: RestoreBody):
        return dump(await svc(request).restore(body.snapshot_id))

    return app


app = create_app()
