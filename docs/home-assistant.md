# Govee bulbs and F1 lighting in Home Assistant

Two parts: get the bulbs into Home Assistant as light entities, then install
the F1 lighting package. Both use what you already have: your Govee API key
and the F1 Sensor integration.

## Part 1: bulbs into Home Assistant

The routes that failed before (Matter, the local LAN integration) do not use
Govee's cloud API. The two below do, with the same key the `govee` command
uses, so the H600D bulbs should appear. Try them in order and stop at the
first one that works.

### Option A: Govee2MQTT app (try first)

Recent Home Assistant versions call add-ons "apps". If your menus still say
"Add-ons", the steps are the same.

1. **Mosquitto broker.** Settings > Apps > Install app, install
   *Mosquitto broker*, start it.
2. **MQTT integration.** Settings > Devices & Services. Home Assistant should
   offer *MQTT* as discovered; click Configure and accept. If it is not
   offered, use Add Integration and search for MQTT.
3. **Add the repository.** Settings > Apps > Install app, then the three-dot
   menu (top right) > Repositories. Add `https://github.com/wez/govee2mqtt`.
   "Govee to MQTT Bridge" appears in the list.
4. **Go back to the store** with the back arrow and refresh the browser page.
   A *Govee2MQTT* card is now at the bottom of the store (or search for
   `govee`). Open it and click Install.
5. **Configure.** On the app's Configuration tab, tick *Show unused optional
   configuration options* and enter **only the API key**. Leave the Govee
   email and password empty. Save.
6. **Start** it from the Info tab and watch the Log tab.
7. Within a minute, Settings > Devices & Services > MQTT should list your
   Govee devices.

**Why no email and password:** Govee now asks for an emailed verification
code when an account logs in from a new client. Govee2MQTT cannot enter one,
so with the email and password set it stops at start-up with
`login failed with code 454` and restarts in a loop. The API key alone is
enough to control the bulbs. The only loss is that changes made in the Govee
app reach Home Assistant by polling, so they show a little later.

### Option B: GoveeLife (if option A does not show the H600Ds)

1. HACS > three-dot menu > Custom repositories, add
   `https://github.com/disforw/goveelife` with type *Integration*.
2. Install *GoveeLife* from HACS and restart Home Assistant.
3. Settings > Devices & Services > Add Integration > GoveeLife, and enter the
   API key. Set the poll interval to 60 seconds or more: with 18 bulbs, faster
   polling will hit Govee's rate limits.

Do not run both options at once; they would double the API usage.

### Check

Settings > Devices & Services > MQTT lists a device per bulb. Each has four
entities, and nothing is hidden:

| Entity | What it is |
| --- | --- |
| The one with the bulb's own name (`light.cinema_left`) | The light itself. Brightness, colour, white temperature and Govee scenes (as "effects") are all controls on this one entity. Click it to open them. |
| Power Switch | A plain on/off duplicate of the light. |
| Request Platform API State | A button that forces a refresh from Govee. |
| Status | A diagnostic sensor holding the raw Govee data. |

Open one light, change its colour, and confirm the real bulb follows.

Devices named after groups in the Govee app (for example "Theatre all") also
appear, with an unknown state. Govee's API only offers on/off for them;
ignore them.

To list every light entity ID, go to Developer Tools > Template and paste:

```
{{ states.light | map(attribute='entity_id') | join('\n') }}
```

If neither option shows the H600D bulbs, stop here: the fallback is to build
our own integration from this repository's client, which is known to work
with them.

## Part 2: F1 lighting package

`homeassistant/packages/f1_lights.yaml` contains everything: the light group,
a dashboard switch, a delay setting and the automations.

### What it does

- When a race, sprint, qualifying or sprint qualifying session goes live and
  the switch is on, it saves what the lights were doing and shows the track
  status.
- Each change of track status changes the lights:

  | Track status | Lights |
  | --- | --- |
  | CLEAR | green |
  | YELLOW | yellow |
  | VSC, SC | orange |
  | RED | red |

- When the session ends, the lights go white for a minute and then return to
  what they were doing before.
- Turning the switch off mid-session restores the lights immediately.
- Practice sessions are ignored.

### Install

1. **Enable packages**, once. In `configuration.yaml` add (or extend your
   existing `homeassistant:` section):

   ```yaml
   homeassistant:
     packages: !include_dir_named packages
   ```

2. **Copy the file** to `/config/packages/f1_lights.yaml` (create the
   `packages` folder). The *File editor* or *Studio Code Server* add-on is
   the easiest way.
3. **Check the entity IDs.** The list under `light:` at the top of the file
   assumes names like `light.cinema_left`. Compare with Developer Tools >
   States and correct any that differ. Also confirm you have
   `sensor.f1_track_status`, `sensor.f1_session_status` and
   `sensor.f1_current_session`; if not, enable live data in the F1 Sensor
   integration's options.
4. **Validate and restart.** Developer Tools > YAML > Check configuration,
   then Restart.
5. **Add to a dashboard**: `input_boolean.f1_lights_enabled`,
   `input_number.f1_lights_delay`, `binary_sensor.f1_lights_active` and
   `light.f1_lights`.

### Test without a race

Developer Tools > Actions, run `script.f1_lights_test`. It saves the current
lighting, shows green, yellow, orange, red and green for five seconds each,
then puts the lighting back. Bulbs that are switched off at the wall will not
take part.

To show one status by hand, run `script.f1_lights_apply` with
`status: YELLOW`.

### Tuning

- **Colours and brightness** are in `script.f1_lights_apply` in the file,
  one `rgb_color` and `brightness_pct` per track status, marked `EDIT`. After editing, Developer Tools > YAML > reload
  Scripts.
- **Spoilers.** Live timing runs ahead of TV and streaming pictures, so the
  lights can change before you see the incident. Prefer the F1 Sensor
  integration's own *Live Delay* setting, which delays all its sensors. The
  package's *F1 lights extra delay* adds further seconds for the lights only;
  leave it at 0 if Live Delay is set.
- **Which sessions** is one line in the `F1 lights active` template, marked
  `EDIT`.

### Limits

- Commands go through Govee's cloud, so expect one to two seconds per change
  and nothing without internet.
- A bulb that was off before the session is turned off again afterwards; when
  you next switch it on it will show the white from the end of the session
  until you change it.
- A bulb that was running a Govee scene comes back with its colour and
  brightness, not the scene.
