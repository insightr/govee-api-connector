"""LightService: the single entry point for the front ends and for phase 2.

It owns the device cache, groups, presets, validation and snapshot/restore.
It contains no command-line or web concerns.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import secrets
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from .client import GoveeClient
from .errors import AuthError, GoveeError, SnapshotError, TargetError, ValidationError
from .models import (
    BRIGHTNESS,
    KELVIN,
    LIGHT_SCENE,
    POWER,
    RGB,
    Device,
    DeviceResult,
    DeviceState,
    Snapshot,
    SnapshotEntry,
    int_to_rgb,
    rgb_to_int,
)
from .settings import AppConfig, Preset, Settings, load_config

log = logging.getLogger(__name__)

MAX_SNAPSHOTS = 20
Step = tuple[str, Any]  # (instance, value)


class LightService:
    def __init__(
        self,
        client: GoveeClient,
        config: AppConfig | None = None,
        snapshot_file: Path | str = "data/snapshots.json",
        *,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.client = client
        self.config = config or AppConfig()
        self.snapshot_file = Path(snapshot_file)
        self._clock = clock
        self._devices: list[Device] | None = None
        self._devices_lock = asyncio.Lock()
        self._state_cache: dict[str, tuple[float, DeviceState]] = {}
        self._last_preset: dict[str, tuple[str, Preset]] = {}
        self._scene_cache: dict[str, dict[str, tuple[str, Any]]] = {}

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> LightService:
        """Build a service from .env and config.yaml. Phase 2 can call this."""
        settings = settings or Settings()
        client = GoveeClient(settings.require_api_key(), settings.base_url)
        return cls(client, load_config(settings.config_file), settings.data_dir / "snapshots.json")

    async def aclose(self) -> None:
        await self.client.aclose()

    # ---- devices and targets -----------------------------------------------

    async def list_devices(self, refresh: bool = False) -> list[Device]:
        """Every light on the account. Cached until `refresh` is True."""
        async with self._devices_lock:
            if self._devices is None or refresh:
                everything = await self.client.list_devices()
                self._devices = [d for d in everything if d.is_light]
            return list(self._devices)

    async def resolve(self, target: str) -> list[Device]:
        """Turn a device ID, a group name or a light name into a list of lights."""
        devices = await self.list_devices()
        for device in devices:
            if device.device == target:
                return [device]

        key = target.strip().casefold()
        group_name = next((g for g in self.config.groups if g.casefold() == key), None)
        named = [d for d in devices if d.name.casefold() == key]

        if group_name is not None and named:
            raise TargetError(
                f"{target!r} is both a group in config.yaml and a light name. "
                "Rename the group, or use the light's device ID."
            )
        if group_name is not None:
            return self._group_members(group_name, devices)
        if len(named) > 1:
            ids = ", ".join(d.device for d in named)
            raise TargetError(f"{len(named)} lights are called {target!r}. Use a device ID instead: {ids}")
        if named:
            return named
        raise TargetError(
            f"No light or group is called {target!r}. Run `govee devices` to see lights and groups."
        )

    def _group_members(self, group_name: str, devices: list[Device]) -> list[Device]:
        spec = self.config.groups[group_name]
        patterns = [spec] if isinstance(spec, str) else list(spec)
        members: list[Device] = []
        for pattern in patterns:
            folded = pattern.casefold()
            matched = [
                d for d in devices
                if d.device == pattern or fnmatch.fnmatchcase(d.name.casefold(), folded)
            ]
            if not matched:
                log.warning("Group %r: %r matches no light", group_name, pattern)
            members.extend(d for d in matched if d not in members)
        if not members:
            raise TargetError(f"Group {group_name!r} matches no lights. Check config.yaml.")
        return members

    async def groups(self) -> dict[str, list[Device]]:
        """Every configured group with its current members (empty if it matches nothing)."""
        devices = await self.list_devices()
        result: dict[str, list[Device]] = {}
        for name in self.config.groups:
            try:
                result[name] = self._group_members(name, devices)
            except TargetError:
                result[name] = []
        return result

    # ---- state -------------------------------------------------------------

    async def get_state(self, target: str, max_age: float = 0.0) -> dict[str, DeviceState]:
        """State per device ID. A cached reading younger than `max_age` seconds is reused.

        An offline or unreachable light is reported, never raised, so one bad
        bulb does not hide the others.
        """
        devices = await self.resolve(target)
        states = await asyncio.gather(*(self._device_state(d, max_age) for d in devices))
        self._raise_if_auth(states)
        return {d.device: s for d, s in zip(devices, states, strict=True)}

    async def _device_state(self, device: Device, max_age: float) -> DeviceState | AuthError:
        cached = self._state_cache.get(device.device)
        if cached and max_age > 0 and self._clock() - cached[0] <= max_age:
            return cached[1]
        try:
            values = await self.client.get_state(device.sku, device.device)
        except AuthError as exc:
            return exc
        except GoveeError as exc:
            return DeviceState(device=device.device, name=device.name, online=False, error=str(exc))
        state = self._parse_state(device, values)
        self._state_cache[device.device] = (self._clock(), state)
        return state

    @staticmethod
    def _parse_state(device: Device, values: dict[str, Any]) -> DeviceState:
        state = DeviceState(device=device.device, name=device.name, online=values.get("online") is True)
        if not state.online:
            return state  # Govee returns stale or empty values for an offline device.

        def value(instance: str) -> int | None:
            raw = values.get(instance)
            return raw if isinstance(raw, int) and not isinstance(raw, bool) else None

        power, rgb = value(POWER), value(RGB)
        state.power = None if power is None else bool(power)
        state.brightness = value(BRIGHTNESS)
        state.rgb = None if rgb is None else int_to_rgb(rgb)
        state.kelvin = value(KELVIN)
        return state

    # ---- control -----------------------------------------------------------

    async def set_power(self, target: str, on: bool) -> list[DeviceResult]:
        devices = await self.resolve(target)
        self._require(devices, POWER, "power")
        return await self._send(devices, lambda d: [(POWER, 1 if on else 0)])

    async def set_brightness(self, target: str, percent: int) -> list[DeviceResult]:
        devices = await self.resolve(target)
        self._check_range(devices, BRIGHTNESS, percent, "Brightness")
        return await self._send(devices, lambda d: [(BRIGHTNESS, percent)])

    async def set_color(self, target: str, rgb: tuple[int, int, int]) -> list[DeviceResult]:
        devices = await self.resolve(target)
        value = self._check_rgb(devices, rgb)
        return await self._send(devices, lambda d: [(RGB, value)])

    async def set_color_temp(self, target: str, kelvin: int) -> list[DeviceResult]:
        devices = await self.resolve(target)
        self._check_range(devices, KELVIN, kelvin, "Colour temperature")
        return await self._send(devices, lambda d: [(KELVIN, kelvin)])

    async def apply_preset(self, target: str, preset: str, force: bool = False) -> list[DeviceResult]:
        """Turn the target on and set the preset's colour and brightness.

        Lights that this service last set to the same preset are skipped, so
        repeated calls are cheap. Changes made elsewhere (the Govee app, a wall
        switch) are invisible here; pass force=True to send regardless.
        """
        name, definition = self._preset(preset)
        devices = await self.resolve(target)
        steps = self._preset_steps(devices, definition)

        todo = [d for d in devices if force or self._last_preset.get(d.device) != (name, definition)]
        sent = {r.device: r for r in await self._send(todo, lambda d: steps)}
        results = []
        for device in devices:
            result = sent.get(device.device)
            if result is None:
                result = DeviceResult(
                    device=device.device, name=device.name, ok=True, skipped=True,
                    detail=f"already set to {name}",
                )
            elif result.ok:
                self._last_preset[device.device] = (name, definition)
            results.append(result)
        return results

    def _preset(self, preset: str) -> tuple[str, Preset]:
        key = preset.strip().casefold()
        for name, definition in self.config.presets.items():
            if name.casefold() == key:
                return name, definition
        known = ", ".join(self.config.presets) or "none defined"
        raise ValidationError(f"No preset is called {preset!r}. Presets in config.yaml: {known}.")

    def _preset_steps(self, devices: list[Device], preset: Preset) -> list[Step]:
        """Validate a preset for every device, then return the calls to make."""
        self._require(devices, POWER, "power")
        steps: list[Step] = [(POWER, 1)]
        if preset.rgb is not None:
            steps.append((RGB, self._check_rgb(devices, preset.rgb)))
        if preset.kelvin is not None:
            self._check_range(devices, KELVIN, preset.kelvin, "Colour temperature")
            steps.append((KELVIN, preset.kelvin))
        if preset.brightness is not None:
            self._check_range(devices, BRIGHTNESS, preset.brightness, "Brightness")
            steps.append((BRIGHTNESS, preset.brightness))
        return steps

    # ---- validation --------------------------------------------------------

    @staticmethod
    def _require(devices: list[Device], instance: str, label: str) -> None:
        for device in devices:
            if device.capability(instance) is None:
                raise ValidationError(f"{device.name} ({device.sku}) does not support {label}.")

    def _check_range(self, devices: list[Device], instance: str, value: int, label: str) -> None:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValidationError(f"{label} must be a whole number, not {value!r}.")
        self._require(devices, instance, label.lower())
        for device in devices:
            limits = device.capability(instance).range  # type: ignore[union-attr]
            if limits and not limits[0] <= value <= limits[1]:
                raise ValidationError(
                    f"{label} {value} is out of range for {device.name}: "
                    f"it accepts {limits[0]} to {limits[1]}."
                )

    def _check_rgb(self, devices: list[Device], rgb: tuple[int, int, int]) -> int:
        channels = tuple(rgb)
        if len(channels) != 3 or any(
            isinstance(c, bool) or not isinstance(c, int) or not 0 <= c <= 255 for c in channels
        ):
            raise ValidationError(
                f"Colour must be three whole numbers from 0 to 255 (red, green, blue), not {rgb!r}."
            )
        self._require(devices, RGB, "colour")
        return rgb_to_int(channels)  # type: ignore[arg-type]

    # ---- sending -----------------------------------------------------------

    async def _send(
        self, devices: list[Device], steps_for: Callable[[Device], list[Step]]
    ) -> list[DeviceResult]:
        """Send each device its steps; devices run concurrently."""
        outcomes = await asyncio.gather(*(self._send_one(d, steps_for(d)) for d in devices))
        self._raise_if_auth(outcomes)
        return list(outcomes)

    async def _send_one(self, device: Device, steps: list[Step]) -> DeviceResult | AuthError:
        # Whatever happens next, what we knew about this light is out of date.
        self._state_cache.pop(device.device, None)
        self._last_preset.pop(device.device, None)
        calls = [
            # Echo the type string the device itself reported.
            self.client.control(device.sku, device.device, capability.type, instance, value)
            for instance, value in steps
            if (capability := device.capability(instance)) is not None
        ]
        try:
            # Each call takes a second or more, so send a light's capabilities
            # together. Only a final "off" has to wait for the others (see restore).
            if steps and steps[-1] == (POWER, 0) and len(calls) > 1:
                await asyncio.gather(*calls[:-1])
                await calls[-1]
            else:
                await asyncio.gather(*calls)
        except AuthError as exc:
            return exc
        except GoveeError as exc:
            detail = "offline" if "offline" in str(exc).lower() else str(exc)
            return DeviceResult(device=device.device, name=device.name, ok=False, detail=detail)
        finally:
            for call in calls:
                call.close()  # no-op for calls that ran; silences "never awaited" for the rest
        return DeviceResult(device=device.device, name=device.name, ok=True)

    @staticmethod
    def _raise_if_auth(outcomes: list[Any]) -> None:
        for outcome in outcomes:
            if isinstance(outcome, AuthError):
                raise outcome

    # ---- snapshot and restore ----------------------------------------------

    async def snapshot(self, target: str) -> str:
        """Record what the target's lights are doing. Returns the snapshot ID."""
        devices = {d.device: d for d in await self.resolve(target)}
        states = await self.get_state(target)
        entries: list[SnapshotEntry] = []
        skipped: list[str] = []
        for device_id, state in states.items():
            if not state.online:
                log.warning("Snapshot: %s is offline and was left out", state.name)
                skipped.append(state.name)
                continue
            for field in ("power", "brightness"):
                if getattr(state, field) is None:
                    log.warning("Snapshot: %s did not report %s; it will not be restored", state.name, field)
            if not state.kelvin and state.rgb is None:
                log.warning("Snapshot: %s did not report a colour; it will not be restored", state.name)
            entries.append(
                SnapshotEntry(
                    device=device_id, sku=devices[device_id].sku, name=state.name,
                    power=state.power, brightness=state.brightness, rgb=state.rgb, kelvin=state.kelvin,
                )
            )
        if not entries:
            raise SnapshotError(f"Nothing to snapshot: every light in {target!r} is offline.")

        snapshot = Snapshot(
            id=f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}",
            created=datetime.now().astimezone().isoformat(timespec="seconds"),
            target=target, devices=entries, skipped=skipped,
        )
        store = self._load_store()
        store["snapshots"][snapshot.id] = snapshot.model_dump(mode="json")
        for old in list(store["snapshots"])[:-MAX_SNAPSHOTS]:
            del store["snapshots"][old]
        store["latest"] = snapshot.id
        self._save_store(store)
        return snapshot.id

    async def restore(self, snapshot_id: str = "latest") -> list[DeviceResult]:
        """Put lights back as a snapshot recorded them. `latest` means the newest one."""
        snapshot = self.get_snapshot(snapshot_id)
        known = {d.device: d for d in await self.list_devices()}
        present: list[Device] = []
        plans: dict[str, list[Step]] = {}
        results: list[DeviceResult] = []
        for entry in snapshot.devices:
            device = known.get(entry.device)
            if device is None:
                results.append(DeviceResult(
                    device=entry.device, name=entry.name, ok=False,
                    detail="no longer on the Govee account",
                ))
                continue
            present.append(device)
            plans[device.device] = self._restore_steps(device, entry)
        results.extend(await self._send(present, lambda d: plans[d.device]))
        return results

    @staticmethod
    def _restore_steps(device: Device, entry: SnapshotEntry) -> list[Step]:
        colour: list[Step] = []
        kelvin_cap = device.capability(KELVIN)
        if entry.kelvin and kelvin_cap is not None:
            low, high = kelvin_cap.range or (entry.kelvin, entry.kelvin)
            colour.append((KELVIN, max(low, min(high, entry.kelvin))))
        elif entry.rgb is not None:
            colour.append((RGB, rgb_to_int(entry.rgb)))
        if entry.brightness is not None:
            limits = device.capability(BRIGHTNESS).range if device.capability(BRIGHTNESS) else None
            low, high = limits or (1, 100)
            colour.append((BRIGHTNESS, max(low, min(high, entry.brightness))))

        if entry.power is False:
            # Put the colour back first so the light is right when next switched on.
            # Off goes last because setting brightness switches a bulb on.
            return [*colour, (POWER, 0)]
        if entry.power is True:
            return [(POWER, 1), *colour]
        return colour

    def list_snapshots(self) -> list[Snapshot]:
        return [Snapshot.model_validate(s) for s in self._load_store()["snapshots"].values()]

    def get_snapshot(self, snapshot_id: str = "latest") -> Snapshot:
        store = self._load_store()
        if snapshot_id == "latest":
            snapshot_id = store.get("latest") or ""
            if not snapshot_id:
                raise SnapshotError("There are no snapshots yet. Take one with `govee snapshot <target>`.")
        raw = store["snapshots"].get(snapshot_id)
        if raw is None:
            raise SnapshotError(f"No snapshot has the ID {snapshot_id!r}. Run `govee snapshots` to list them.")
        return Snapshot.model_validate(raw)

    def _load_store(self) -> dict[str, Any]:
        if not self.snapshot_file.exists():
            return {"latest": None, "snapshots": {}}
        try:
            store = json.loads(self.snapshot_file.read_text())
            store.setdefault("snapshots", {})
            return store
        except (ValueError, AttributeError):
            raise SnapshotError(f"{self.snapshot_file} is damaged. Delete it to start again.") from None

    def _save_store(self, store: dict[str, Any]) -> None:
        self.snapshot_file.parent.mkdir(parents=True, exist_ok=True)
        temp = self.snapshot_file.with_suffix(".tmp")
        temp.write_text(json.dumps(store, indent=2))
        temp.replace(self.snapshot_file)

    # ---- scenes (optional extra) -------------------------------------------

    async def list_scenes(self, target: str) -> list[str]:
        """Names of the Govee scenes that every light in the target supports."""
        devices = await self.resolve(target)
        tables = [await self._scenes_for(d) for d in devices]
        return [name for name in tables[0] if all(name in t for t in tables[1:])]

    async def apply_scene(self, target: str, scene: str) -> list[DeviceResult]:
        devices = await self.resolve(target)
        key = scene.strip().casefold()
        chosen: dict[str, tuple[str, Any]] = {}
        for device in devices:
            table = {name.casefold(): entry for name, entry in (await self._scenes_for(device)).items()}
            if key not in table:
                raise ValidationError(
                    f"{device.name} has no scene called {scene!r}. Run `govee scenes {target}` to list them."
                )
            chosen[device.device] = table[key]
        # Scene values are objects ({id, paramId}); send them back whole.
        return await self._send(devices, lambda d: [(LIGHT_SCENE, chosen[d.device][1])])

    async def _scenes_for(self, device: Device) -> dict[str, tuple[str, Any]]:
        """{scene name: (capability type, value)}; fetched once per model."""
        if device.sku not in self._scene_cache:
            table: dict[str, tuple[str, Any]] = {}
            for capability in await self.client.list_scenes(device.sku, device.device):
                if capability.instance != LIGHT_SCENE:
                    continue
                for option in (capability.parameters or {}).get("options") or []:
                    table[str(option.get("name"))] = (capability.type, option.get("value"))
            self._scene_cache[device.sku] = table
        return self._scene_cache[device.sku]
