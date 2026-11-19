"""Command line front end. No Govee logic lives here; it only calls LightService."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Annotated, TypeVar

import typer
from rich.console import Console
from rich.table import Table

from .errors import GoveeLightsError, ValidationError
from .models import BRIGHTNESS, KELVIN, DeviceResult
from .service import LightService
from .settings import Settings

app = typer.Typer(
    help="Control Govee lights. A TARGET is a light name, a device ID or a group from config.yaml.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
T = TypeVar("T")

Target = Annotated[str, typer.Argument(help="Light name, device ID or group name.")]


@app.callback()
def _main(verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show debug logging.")] = False) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.WARNING, format="%(levelname)s: %(message)s")
    # httpx logs request lines at INFO; keep it quiet unless asked.
    logging.getLogger("httpx").setLevel(logging.DEBUG if verbose else logging.WARNING)


def run(action: Callable[[LightService], Awaitable[T]]) -> T:
    """Build the service, run one async action, and turn known errors into clean messages."""

    async def go() -> T:
        service = LightService.from_settings(Settings())
        try:
            return await action(service)
        finally:
            await service.aclose()

    try:
        return asyncio.run(go())
    except GoveeLightsError as exc:
        console.print(f"[red]Error:[/red] {exc}", highlight=False)
        raise typer.Exit(1) from None


def report(results: list[DeviceResult]) -> None:
    """Print one line per light and exit non-zero if any failed."""
    for r in results:
        if r.skipped:
            console.print(f"[yellow]skipped[/yellow]  {r.name}  ({r.detail})", highlight=False)
        elif r.ok:
            console.print(f"[green]ok[/green]       {r.name}", highlight=False)
        else:
            console.print(f"[red]failed[/red]   {r.name}  ({r.detail})", highlight=False)
    if any(not r.ok for r in results):
        raise typer.Exit(1)


def parse_color(text: str) -> tuple[int, int, int]:
    """Accept '#ff8800', 'ff8800' or '255,136,0'."""
    cleaned = text.strip().lstrip("#")
    try:
        if "," in cleaned:
            parts = tuple(int(p) for p in cleaned.split(","))
            if len(parts) == 3:
                return parts  # type: ignore[return-value]
        elif len(cleaned) == 6:
            return tuple(int(cleaned[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    except ValueError:
        pass
    raise ValidationError(f"Could not read the colour {text!r}. Use R,G,B (such as 255,100,0) or hex (such as ff6400).")


@app.command()
def devices() -> None:
    """List every light with its model, ID and capabilities, then the groups."""

    async def action(service: LightService):
        return await service.list_devices(refresh=True), await service.groups()

    lights, groups = run(action)
    table = Table(title=f"{len(lights)} lights")
    for column in ("Name", "Model", "Device ID", "Capabilities"):
        table.add_column(column)
    for d in sorted(lights, key=lambda d: d.name.casefold()):
        caps = []
        for c in d.capabilities:
            limits = c.range if c.instance in (BRIGHTNESS, KELVIN) else None
            caps.append(f"{c.instance} {limits[0]}-{limits[1]}" if limits else c.instance)
        table.add_row(d.name, d.sku, d.device, ", ".join(caps))
    console.print(table)

    group_table = Table(title="Groups (config.yaml)")
    group_table.add_column("Group")
    group_table.add_column("Lights")
    for name, members in groups.items():
        group_table.add_row(name, ", ".join(m.name for m in members) or "[red]matches nothing[/red]")
    console.print(group_table)


@app.command()
def state(target: Target) -> None:
    """Show what each light is doing right now."""
    states = run(lambda s: s.get_state(target))
    table = Table()
    for column in ("Name", "Status", "Power", "Brightness", "Colour"):
        table.add_column(column)
    for st in states.values():
        if not st.online:
            table.add_row(st.name, f"[red]{'error: ' + st.error if st.error else 'offline'}[/red]", "-", "-", "-")
            continue
        power = "-" if st.power is None else ("on" if st.power else "off")
        brightness = "-" if st.brightness is None else f"{st.brightness}%"
        if st.kelvin:
            colour = f"{st.kelvin} K"
        elif st.rgb is not None:
            colour = "rgb {},{},{}".format(*st.rgb)
        else:
            colour = "-"
        table.add_row(st.name, "[green]online[/green]", power, brightness, colour)
    console.print(table)


@app.command()
def on(target: Target) -> None:
    """Turn lights on."""
    report(run(lambda s: s.set_power(target, True)))


@app.command()
def off(target: Target) -> None:
    """Turn lights off."""
    report(run(lambda s: s.set_power(target, False)))


@app.command()
def brightness(target: Target, percent: Annotated[int, typer.Argument(help="1 to 100.")]) -> None:
    """Set brightness as a percentage."""
    report(run(lambda s: s.set_brightness(target, percent)))


@app.command()
def color(target: Target, value: Annotated[str, typer.Argument(help="R,G,B such as 255,100,0, or hex such as ff6400.")]) -> None:
    """Set a colour."""

    async def action(service: LightService):
        return await service.set_color(target, parse_color(value))

    report(run(action))


@app.command()
def temp(target: Target, kelvin: Annotated[int, typer.Argument(help="White temperature in Kelvin, such as 4000.")]) -> None:
    """Set a white colour temperature."""
    report(run(lambda s: s.set_color_temp(target, kelvin)))


@app.command()
def preset(
    target: Target,
    name: Annotated[str, typer.Argument(help="Preset name from config.yaml.")],
    force: Annotated[bool, typer.Option("--force", help="Send even if the lights were last set to this preset.")] = False,
) -> None:
    """Apply a named preset from config.yaml."""
    report(run(lambda s: s.apply_preset(target, name, force=force)))


@app.command()
def presets() -> None:
    """List the presets defined in config.yaml."""

    async def action(service: LightService):
        return service.config.presets

    for name, p in run(action).items():
        colour = f"{p.kelvin} K" if p.kelvin else ("rgb {},{},{}".format(*p.rgb) if p.rgb else "-")
        console.print(f"{name:10} {colour:18} brightness {p.brightness if p.brightness is not None else '-'}", highlight=False)


@app.command()
def snapshot(target: Target) -> None:
    """Remember what the lights are doing now. Prints the snapshot ID."""

    async def action(service: LightService):
        snapshot_id = await service.snapshot(target)
        return service.get_snapshot(snapshot_id)

    snap = run(action)
    console.print(f"Saved snapshot [bold]{snap.id}[/bold] of {len(snap.devices)} lights.", highlight=False)
    if snap.skipped:
        console.print(f"[yellow]Left out (offline):[/yellow] {', '.join(snap.skipped)}", highlight=False)


@app.command()
def snapshots() -> None:
    """List saved snapshots, oldest first."""

    async def action(service: LightService):
        return service.list_snapshots()

    for snap in run(action):
        console.print(f"{snap.id}  {snap.created}  {snap.target}  ({len(snap.devices)} lights)", highlight=False)


@app.command()
def restore(snapshot_id: Annotated[str, typer.Argument(help="Snapshot ID, or 'latest'.")] = "latest") -> None:
    """Put lights back as a snapshot recorded them."""
    report(run(lambda s: s.restore(snapshot_id)))


@app.command()
def scenes(target: Target, apply: Annotated[str | None, typer.Option("--apply", help="Scene name to apply.")] = None) -> None:
    """List Govee scenes for the target, or apply one with --apply NAME."""
    if apply:
        report(run(lambda s: s.apply_scene(target, apply)))
        return
    names = run(lambda s: s.list_scenes(target))
    console.print(", ".join(names) if names else "No scenes.", highlight=False)


@app.command()
def web(
    host: Annotated[str | None, typer.Option(help="Address to listen on. Default 127.0.0.1 (this machine only).")] = None,
    port: Annotated[int | None, typer.Option(help="Port. Default 8000.")] = None,
    lan: Annotated[bool, typer.Option("--lan", help="Listen on all addresses so phones on the same Wi-Fi can connect.")] = False,
) -> None:
    """Start the local web page."""
    import uvicorn

    settings = Settings()
    try:
        settings.require_api_key()
    except GoveeLightsError as exc:
        console.print(f"[red]Error:[/red] {exc}", highlight=False)
        raise typer.Exit(1) from None
    bind = "0.0.0.0" if lan else (host or settings.web_host)
    uvicorn.run("govee_lights.web:app", host=bind, port=port or settings.web_port)


if __name__ == "__main__":
    app()
