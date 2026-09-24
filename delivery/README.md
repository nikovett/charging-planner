# Delivery

Dispatches charging plans to chargers and vehicles. `deliver.py` reads the
`delivery:` block inside each charging profile in `config.yaml`, resolves
charger IDs from environment variables, and calls the correct handler module.

---

## Files

| File | Description |
|---|---|
| `deliver.py` | Dispatcher — reads config, resolves IDs, calls handler modules |
| `deliver_chargeamps.py` | Charge Amps handler — tested and supported |
| `deliver_easee.py` | Easee handler — untested, based on official API documentation |
| `deliver_myskoda.py` | MyŠkoda handler — tested against real vehicle |

---

## Configuration

Delivery targets are configured inside each charging profile in `config.yaml`, one list entry per target.

```yaml
area: FI
timezone: Europe/Helsinki

profiles:
  - name: overnight
    schedule:
      mon-sun: { window: 21:00-06:30, required: 4 }
    max_windows: 1
    delivery:
      - chargeamps: { charger: CHARGER_ID_1, connector: 1, max_amps: 16.0 }
      - myskoda:    { vin: SKODA_VIN, api_key_env: SKODA_API_KEY,
                       # profile_name: Koti                       # optional — omit if the vehicle has only one charging profile
                       set_charge_mode: PREFERRED_CHARGING_TIMES } # or false to skip, or any other valid mode
```

`timezone` is set once at the top level and passed to all handlers automatically. Each `delivery:` entry is `{ handler_name: { params... } }` — the handler name is the key, not a field inside it, so the same handler can appear more than once in one profile (e.g. two Charge Amps chargers on different connectors) as two separate entries.

**Every handler needs a way to identify the charge point** — the env var name (not the ID itself) holding the charger ID or VIN, under a handler-specific key: `charger` for Charge Amps and Easee, `vin` for MyŠkoda. Accepts a string or a list of env var names.

To stop delivering to a target without losing its settings, comment the entry out. A profile with no `delivery:` still plans; the plan just isn't sent anywhere.

Every other key is handler-specific and optional — see each handler's table below. Only set a key when you want something other than its default.

---

## Handler reference

### `deliver_chargeamps.py`

Delivers via the `my.charge.space` portal API using the user's own credentials.
Converts plan windows to weekly `schedulePeriods` anchored to Monday 00:00 local
time. Logs in once per process and caches the token for reuse across charger IDs.

Always reads the connector state before delivery. If the car is actively charging,
schedule override is activated after delivery so the current session is not
interrupted — the override expires automatically when the cable is disconnected.

Periods where `to` would exceed the 604800s weekly limit (windows ending on Monday)
are handled automatically: crossing-midnight windows wrap to `from=0`, fully-Monday
windows shift by -604800s.

**Config keys:**

| Key | Default | Description |
|---|---|---|
| `charger` | — | **Required.** Env var whose value is the Charge Amps charger ID. Accepts a string or list. |
| `connector` | `1` | Connector index on the charger |
| `max_amps` | `16.0` | Maximum current in amps (A) |
| `restore_mode` | `false` | Restore connector mode after delivery if it was not already `Schedule` |

**Environment variables:**

| Variable | Description |
|---|---|
| `CHARGER_USERNAME` | `my.charge.space` login email |
| `CHARGER_PASSWORD` | `my.charge.space` login password |
| `CHARGER_ID_<N>` | Charger ID — referenced by `charger` |

---

### `deliver_easee.py`

> ⚠ Untested — based on official Easee API documentation. Has not been verified
> against a real Easee charger.

Delivers via the official Easee REST API (`api.easee.com`).

**Config keys:**

| Key | Default | Description |
|---|---|---|
| `charger` | — | **Required.** Env var whose value is the Easee charger serial number. |
| `max_amps` | `16` | Maximum current in amps (A), integer |

**Environment variables:**

| Variable | Description |
|---|---|
| `CHARGER_USERNAME` | Easee account username (email or phone number with country code) |
| `CHARGER_PASSWORD` | Easee account password |
| `CHARGER_ID_<N>` | Easee charger serial number — referenced by `charger` |

---

### `deliver_myskoda.py`

Delivers via the official MyŠkoda Public API (`public.api.connect.skoda-auto.cz`).
Fetches the vehicle's current charging profile, writes each plan window into a
preferred charging time slot (window 1 → slot 1, window 2 → slot 2, and so
on), disables any unused slots (times preserved), and sets the charge mode to
`PREFERRED_CHARGING_TIMES`.

**Active-session protection.** If the vehicle is actively charging in
PREFERRED_CHARGING_TIMES mode, the API doesn't say which slot is driving the
session — the handler infers it by checking which enabled slot's time window
contains the current local time (charging stops right at the window end, so
this is a reliable signal). If exactly one slot matches, plan windows are
routed around it — that slot is left completely untouched (not even its
enabled flag) while every other slot is managed normally. If the active slot
can't be identified (no match, or more than one enabled slot's window
overlaps "now"), or the plan needs all 4 slots so there's no room to route
around it, delivery is skipped entirely for that run rather than risk
interrupting the session. Charging in any other mode (MANUAL, TIMER,
TIMER_CHARGING_WITH_CLIMATISATION) doesn't go through preferredChargingTimes
slots at all, so all 4 are managed freely — only the charge mode itself is
left unchanged, to avoid disturbing whatever's driving that session.

**Requires `max_windows` set to a value between 1 and 4** — the vehicle has
exactly 4 preferred charging time slots. `max_windows: null` (unlimited) is
rejected even if a given day's plan happens to produce 4 or fewer windows,
since an unbounded profile could produce more on a different day. Plans that
somehow still end up with more than 4 windows are also rejected as a
defensive runtime check.

API keys are created in the MyŠkoda app at `go.skoda.eu/api-keys`. Keys expire —
check `X-API-Key-Expires-At` in responses and rotate before expiry. Rate limit:
20 requests/hour per VIN; delivery uses 3 requests (GET profiles + PUT profile +
PUT charge mode).

Tested against a real Škoda Enyaq with a single-window plan. First delivery
confirmed correct in the MyŠkoda app (slot 1 updated, other slots disabled,
charge mode set). Multi-window slot routing and active-slot detection are
covered by unit tests but not yet verified against real hardware.

**Config keys:**

| Key | Default | Description |
|---|---|---|
| `vin` | — | **Required.** Env var whose value is the VIN (17 characters). |
| `api_key_env` | `SKODA_API_KEY` | Env var holding the MyŠkoda API key |
| `profile_name` | — | Charging profile name to update (e.g. `"Koti"`, `"Home"`). Optional when the vehicle has only one charging profile — omit it and the single profile is used automatically. Required when there are multiple profiles. |
| `set_charge_mode` | `PREFERRED_CHARGING_TIMES` | Charge mode to set after profile update. Any valid MyŠkoda charge mode string (`MANUAL`, `TIMER`, `TIMER_CHARGING_WITH_CLIMATISATION`, `PREFERRED_CHARGING_TIMES`, `ONLY_OWN_CURRENT`, `IMMEDIATE_DISCHARGING`, `HOME_STORAGE_CHARGING`), or `false` to skip. New modes added by Škoda are passed through as-is. |

**Environment variables:**

| Variable | Description |
|---|---|
| `SKODA_VIN` | Vehicle Identification Number (17 characters) — referenced by `vin` |
| `SKODA_API_KEY` | API key from the MyŠkoda app |

**GitHub Actions secrets required:**

Add to the delivery step's `env:` block in `schedule.yml`:

```yaml
SKODA_VIN:     ${{ secrets.SKODA_VIN }}
SKODA_API_KEY: ${{ secrets.SKODA_API_KEY }}
```

---

## Adding a new handler

1. Create `delivery/deliver_<name>.py`
2. Implement the public interface:

```python
def deliver(plan: dict, charge_point_id: str, entry: dict, timezone: str) -> bool:
    """
    plan:             Plan dict from charging_planner.py
    charge_point_id:  Resolved charger/vehicle ID (env var already read by dispatcher)
    entry:            Delivery config entry from config.yaml
    timezone:         IANA timezone name from the top-level timezone: key

    Returns True on success, False on failure.
    """
```

3. Add a delivery entry in `config.yaml`: `- <name>: { charge_point_id: ENV_VAR_NAME, ...other keys }`. Optionally add friendlier key names for that handler to `_DELIVERY_KEY_ALIASES` in `charging_planner.py` — without one, config authors use the internal key names directly, which still works.
4. Add the relevant env vars to `.github/workflows/schedule.yml`

The dispatcher finds and loads the script automatically — no changes to `deliver.py` needed.

---

## Running

```bash
# Dispatch all plans using config.yaml
python delivery/deliver.py plan-*.json --config config.yaml

# Debug logging
python delivery/deliver.py plan-*.json --debug
```
