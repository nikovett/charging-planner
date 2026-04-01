# delivery/

Delivery handlers for pushing charging plans to EV chargers.

Each handler is a self-contained Python script that receives a plan produced by `charging_planner.py` and delivers it to a specific charger. `deliver.py` acts as the dispatcher — it reads `config.yaml`, groups deliveries by handler, and calls the correct script automatically.

---

## How it works

```
charging_planner.py  →  plan-{name}.json  →  delivery/deliver.py  →  deliver_chargeamps.py
                                                                   →  deliver_easee.py
                                                                   →  deliver_<your_handler>.py
```

`deliver.py` is the only script called directly. It reads the `deliveries:` block nested inside each charging profile in `config.yaml`, resolves charger IDs from environment variables, and dispatches each delivery to the correct handler. The handler's `deliver()` function is called once per charger ID.

---

## Files

| File | Description |
|---|---|
| `deliver.py` | Dispatcher — reads config, resolves IDs, calls handler modules |
| `deliver_chargeamps.py` | Charge Amps handler — tested and supported |
| `deliver_easee.py` | Easee handler — untested, based on official API documentation |

---

## Configuration

Deliveries are configured inside each charging profile in `config.yaml`. The `handler` field determines which script is used. `charge_point_id` accepts a single env var name or a list — when a list is given, the same plan is delivered to every charger ID independently.

```yaml
entsoe:
  timezone: "Europe/Helsinki"

charging:
  - name: topup
    ...
    deliveries:
      - handler: chargeamps
        charge_point_id: CHARGER_ID_1
        connector_id: 1
        max_charging_rate: 16.0
        restore_mode: false

      - handler: chargeamps
        charge_point_id:          # deliver to two chargers
          - CHARGER_ID_1
          - CHARGER_ID_2
        connector_id: 1
        max_charging_rate: 16.0
```

`timezone` is set once in the `entsoe:` block and passed to all handlers automatically.

---

## Handler reference

### `deliver_chargeamps.py`

Delivers via the `my.charge.space` API using the user's own credentials. Converts plan windows to weekly `schedulePeriods` anchored to Monday 00:00 local time. Logs in once per process and caches the token for reuse across multiple charger IDs.

**Mode restore** — controlled by `restore_mode` (default `false`). When enabled, reads the connector's current mode (`On`, `Off`, or `Schedule`) before delivery and restores it afterwards if it was not already `Schedule`. Prevents the delivery from silently switching the charger into schedule mode when the user had it set to `On` or `Off`. If mode restore fails the schedule was still delivered — a warning is logged but delivery is reported as succeeded.

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
| `CHARGER_USERNAME` | `my.charge.space` login username (email) |
| `CHARGER_PASSWORD` | `my.charge.space` login password |
| `CHARGER_ID_<N>` | Charger ID — referenced by `charge_point_id` |

---

### `deliver_easee.py`

> ⚠ Untested — based on official Easee API documentation at https://developer.easee.com/docs/scheduling. Has not been verified against a real Easee charger.

Delivers via the official Easee REST API (`api.easee.com`). Single charging window → Basic Charge Plan (specific datetime, no repeat). Multiple windows (split plan) → Weekly Charge Plan for the target day (recurring, overwritten by the next daily run).

**Config keys:**

| Key | Default | Description |
|---|---|---|
| `charge_point_id` | — | **Required.** Env var whose value is the Easee charger serial number. Accepts a string or list. |
| `max_charging_rate` | `16` | Maximum current in amps (A), integer |

**Environment variables:**

| Variable | Description |
|---|---|
| `CHARGER_USERNAME` | Easee account username (email or phone number with country code) |
| `CHARGER_PASSWORD` | Easee account password |
| `CHARGER_ID_<N>` | Easee charger serial number — referenced by `charge_point_id` |

---

## Adding a new handler

1. Create `delivery/deliver_<name>.py`
2. Implement the public interface:

```python
def deliver(plan: dict, charge_point_id: str, entry: dict, timezone: str) -> bool:
    """
    plan:             Plan dict from charging_planner.py
    charge_point_id:  Resolved charger ID (env var already read by dispatcher)
    entry:            Delivery config entry from config.yaml
    timezone:         IANA timezone name from the entsoe: block

    Returns True on success, False on failure.
    """
```

3. Add a delivery entry in `config.yaml` with `handler: <name>` and any handler-specific keys
4. Add the relevant env vars to `.github/workflows/schedule.yml`

The dispatcher (`deliver.py`) will find and load the script automatically — no changes to `deliver.py` needed.

---

## Running

```bash
# Dispatch all plans using config.yaml
python delivery/deliver.py plan-*.json --config config.yaml

# Debug logging
python delivery/deliver.py plan-*.json --debug
```
