# Delivery

Dispatches charging plans to chargers and vehicles. `deliver.py` reads the
`deliveries:` block inside each charging profile in `config.yaml`, resolves
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

Deliveries are configured inside each charging profile in `config.yaml`.

```yaml
entsoe:
  timezone: "Europe/Helsinki"

charging:
  - name: overnight
    continuous_only: true
    ...
    deliveries:
      - handler: chargeamps
        charge_point_id: CHARGER_ID_1
        connector_id: 1
        max_charging_rate: 16.0
        restore_mode: false

      - handler: myskoda
        charge_point_id: SKODA_VIN
        api_key_env: SKODA_API_KEY
        profile_name: "Koti"
        set_charge_mode: true
```

`timezone` is set once in the `entsoe:` block and passed to all handlers automatically.

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
| `charge_point_id` | — | **Required.** Env var whose value is the Charge Amps charger ID. Accepts a string or list. |
| `connector_id` | `1` | Connector index on the charger |
| `max_charging_rate` | `16.0` | Maximum current in amps (A) |
| `restore_mode` | `false` | Restore connector mode after delivery if it was not already `Schedule` |

**Environment variables:**

| Variable | Description |
|---|---|
| `CHARGER_USERNAME` | `my.charge.space` login email |
| `CHARGER_PASSWORD` | `my.charge.space` login password |
| `CHARGER_ID_<N>` | Charger ID — referenced by `charge_point_id` |

---

### `deliver_easee.py`

> ⚠ Untested — based on official Easee API documentation. Has not been verified
> against a real Easee charger.

Delivers via the official Easee REST API (`api.easee.com`).

**Config keys:**

| Key | Default | Description |
|---|---|---|
| `charge_point_id` | — | **Required.** Env var whose value is the Easee charger serial number. |
| `max_charging_rate` | `16` | Maximum current in amps (A), integer |

**Environment variables:**

| Variable | Description |
|---|---|
| `CHARGER_USERNAME` | Easee account username (email or phone number with country code) |
| `CHARGER_PASSWORD` | Easee account password |
| `CHARGER_ID_<N>` | Easee charger serial number — referenced by `charge_point_id` |

---

### `deliver_myskoda.py`

Delivers via the official MyŠkoda Public API (`public.api.connect.skoda-auto.cz`).
Fetches the vehicle's current charging profile, updates preferred charging time
slot 4 with the planned window, disables slots 1–3 (times preserved), and sets
the charge mode to `PREFERRED_CHARGING_TIMES`.

**Only compatible with `continuous_only: true` profiles** — the MyŠkoda API
accepts a single time window per slot. Plans with multiple windows are rejected.

API keys are created in the MyŠkoda app at `go.skoda.eu/api-keys`. Keys expire —
check `X-API-Key-Expires-At` in responses and rotate before expiry. Rate limit:
20 requests/hour per VIN; delivery uses 3 requests (GET profiles + PUT profile +
PUT charge mode).

Tested against a real Škoda Enyaq. First delivery confirmed correct in the
MyŠkoda app (slot 4 updated, slot 1 disabled, charge mode set).

**Config keys:**

| Key | Default | Description |
|---|---|---|
| `charge_point_id` | — | **Required.** Env var whose value is the VIN (17 characters). |
| `api_key_env` | `SKODA_API_KEY` | Env var holding the MyŠkoda API key |
| `profile_name` | first profile | Charging profile name to update (e.g. `"Koti"`, `"Home"`) |
| `set_charge_mode` | `true` | Set charge mode to `PREFERRED_CHARGING_TIMES` after profile update |

**Environment variables:**

| Variable | Description |
|---|---|
| `SKODA_VIN` | Vehicle Identification Number (17 characters) — referenced by `charge_point_id` |
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
    timezone:         IANA timezone name from the entsoe: block

    Returns True on success, False on failure.
    """
```

3. Add a delivery entry in `config.yaml` with `handler: <name>` and handler-specific keys
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
