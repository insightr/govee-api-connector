# Govee lights

Control Govee lights through the Govee cloud API, from a command line and a
local web page. Built for H600D bulbs, which cannot be added to Home Assistant
directly, but it works with any light the Govee API lists.

This is phase 1. Phase 2 will drive the same lights from live Formula 1 track
status by calling `LightService` (see [Using it from code](#using-it-from-code)).

## Setup

Needs Python 3.11 or later and a Govee API key (Govee Home app: Profile,
Settings, Apply for API Key).

```bash
python -m venv .venv
```

```bash
source .venv/bin/activate
```

```bash
pip install -e ".[dev]"
```

```bash
cp .env.example .env
```

Open `.env` and paste your key after `GOVEE_API_KEY=`. That file is
git-ignored; the key must not go anywhere else.

Check it works:

```bash
govee devices
```

You should see every light with its model, ID and capabilities, followed by
the groups from `config.yaml`. Run commands from the project folder, because
`.env`, `config.yaml` and `data/` are found relative to where you run them.

## Configuration

`config.yaml` holds groups and presets. Edit it freely; no code changes needed.

```yaml
poll_seconds: 60          # how often the web page re-reads state

groups:
  all: "*"                              # every light on the account
  garden: ["Garden *"]                  # * is a wildcard; names ignore case
  cinema: ["Cinema Left", "Cinema Right"]

presets:
  red:   {rgb: [255, 0, 0], brightness: 100}
  white: {kelvin: 4000,     brightness: 100}
```

- A group member is a light name (with optional `*` wildcard) or a device ID.
- A preset has `rgb` or `kelvin`, and optionally `brightness`. Applying a
  preset also turns the lights on.
- Groups made in the Govee Home app are not used; define groups here.

Optional settings, as environment variables or lines in `.env`:

| Variable | Default | Purpose |
| --- | --- | --- |
| `GOVEE_API_KEY` | none | Your Govee API key (required) |
| `GOVEE_CONFIG_FILE` | `config.yaml` | Path to the groups and presets file |
| `GOVEE_DATA_DIR` | `data` | Where snapshots are stored |
| `GOVEE_WEB_HOST` | `127.0.0.1` | Address the web page listens on |
| `GOVEE_WEB_PORT` | `8000` | Port the web page listens on |

## Commands

A `TARGET` is a light name, a device ID, or a group name. Quote names that
contain spaces. Every command prints one line per light and exits with an
error code if any light failed.

| Command | What it does |
| --- | --- |
| `govee devices` | List lights, capabilities and groups |
| `govee state TARGET` | Show power, brightness and colour; offline lights are marked |
| `govee on TARGET` / `govee off TARGET` | Turn on or off |
| `govee brightness TARGET 40` | Set brightness, 1 to 100 |
| `govee color TARGET 255,100,0` | Set a colour, as R,G,B or hex (`ff6400`) |
| `govee temp TARGET 4000` | Set a white temperature in Kelvin |
| `govee preset TARGET red` | Apply a preset; add `--force` to resend |
| `govee presets` | List presets |
| `govee snapshot TARGET` | Remember what the lights are doing; prints an ID |
| `govee snapshots` | List saved snapshots |
| `govee restore [ID]` | Put lights back; without an ID, uses the latest snapshot |
| `govee scenes TARGET` | List Govee scenes; `--apply "Sunset Glow"` applies one |
| `govee web` | Start the web page |

Examples:

```bash
govee preset balcony red
```

```bash
govee brightness "Cinema Left" 40
```

Values are checked against what each light reports before anything is sent.
For example `govee temp cinema 9000` is refused locally because H600D bulbs
accept 2700 to 6500 K.

## Web page

```bash
govee web --lan
```

Then open `http://<this computer's IP address>:8000` on a phone on the same
Wi-Fi. Without `--lan` the page is only reachable from this computer, at
<http://127.0.0.1:8000>.

The page shows a card per group and per light with on/off, brightness, white
temperature, a colour picker and preset buttons, plus Snapshot all and Restore
latest. There is no login, so only use `--lan` on a network you trust.

## Things to know

- **Everything goes through Govee's cloud.** A command takes about a second,
  and nothing works without internet.
- **State is polled, not pushed.** Changes made in the Govee app or at a wall
  switch show up on the next poll. The page polls only while it is on screen;
  press Refresh to read immediately.
- **Requests are limited.** Each poll costs one request per light, and Govee
  limits requests per minute and per day. With 20 lights, keep `poll_seconds`
  at 60 or more. Rate-limit responses are retried automatically.
- **Repeated presets are skipped.** If this app last set a light to `red`,
  applying `red` again sends nothing. It cannot see changes made elsewhere,
  so use `--force` if a light was changed in the Govee app in between.
- **Offline lights do not block the rest.** They are reported as `offline`
  and left out of snapshots.
- **Snapshots cannot capture scenes.** Govee does not report which scene is
  running, so a light in a scene is restored to its power and brightness only.
- **Restoring a light that was off** puts its colour and brightness back and
  then turns it off, so it may light briefly.

## Using it from code

`LightService` is the only entry point; the command line and the web page are
thin wrappers around it.

```python
import asyncio
from govee_lights import LightService

async def main():
    lights = LightService.from_settings()      # reads .env and config.yaml
    try:
        snapshot_id = await lights.snapshot("garden")
        await lights.apply_preset("garden", "yellow")
        await lights.restore(snapshot_id)
    finally:
        await lights.aclose()

asyncio.run(main())
```

Control methods return one `DeviceResult` per light (`ok`, `skipped`,
`detail`). Errors derive from `govee_lights.errors.GoveeLightsError`.

## Tests

```bash
pytest
```

Tests run against recorded responses in `tests/fixtures/` and never touch the
network. Device IDs in the fixtures are masked.

## Docker

```bash
docker build -t govee-lights .
```

```bash
docker run -d --name govee-lights --env-file .env -p 8000:8000 -v govee-data:/app/data govee-lights
```

The key is passed at run time and is not baked into the image. To change
groups or presets without rebuilding, add `-v "$PWD/config.yaml:/app/config.yaml:ro"`.
Run a command in the container with `docker exec govee-lights govee state all`.

## Layout

```
src/govee_lights/
  client.py      GoveeClient: the five API endpoints, rate limiting, retries
  service.py     LightService: groups, presets, validation, snapshot/restore
  cli.py         the `govee` command
  web.py         FastAPI routes and the page
  static/index.html
  settings.py  models.py  errors.py  ratelimit.py
tests/           client, service and web tests with recorded fixtures
config.yaml      groups and presets
```
