# delivery/

Delivery handlers for pushing charging plans to EV chargers.

Each handler is a self-contained Python script that receives a plan produced by `charging_planner.py` and delivers it to a specific charger or protocol. `deliver.py` acts as the dispatcher — it reads `config.yaml`, groups deliveries by handler, and calls the correct script automatically.

---

## How it works

```
charging_planner.py  →  plan-{name}.json  →  delivery/deliver.py  →  deliver_chargeamps.py
                                                                  →  deliver_ocpp.py
                                                                  →  deliver_<your_handler>.py
```

`deliver.py` is the only script called directly. It reads the `deliveries:` block nested inside each charging profile in `config.yaml`, resolves charger IDs from environment variables, and dispatches each delivery to the correct handler. The handler's `deliver()` function is called once per charger ID.

---

## Files

| File | Description |
|---|---|
| `deliver.py` | Dispatcher — reads config, resolves IDs, calls handler modules |
| `deliver_chargeamps.py` | Charge Amps handler — delivers via `my.charge.space` API |
| `deliver_ocpp.py` | OCPP handler — delivers via WebSocket `SetChargingProfile.req` |

---

## Configuration

Deliveries are configured inside each charging profile in `config.yaml`. The `handler` field determines which script is used. `charge_point_id` accepts a single env var name or a list — when a list is given, the same plan is delivered to every charger ID independently.

```yaml
entsoe:
  timezone: "Europe/Helsinki"

charging:
  - name: "topup"
    ...
    deliveries:
      - handler: "chargeamps"
        charge_point_id: "CHARGER_ID_1"
        connector_id: 1
        max_charging_rate: 16.0

      - handler: "chargeamps"
        charge_point_id:          # deliver to two chargers
          - "CHARGER_ID_1"
          - "CHARGER_ID_2"
        connector_id: 1
        max_charging_rate: 16.0

      - handler: "ocpp"
        charge_point_id: "CHARGER_ID_2"
        endpoint_url_env: "OCPP_ENDPOINT_URL"
        ocpp_version: "1.6"
        connector_id: 1
        max_charging_rate: 11000.0
        charging_rate_unit: "W"
        profile_id: 1
        stack_level: 0
        timeout: 30
```

`timezone` is set once in the `entsoe:` block and passed to all handlers automatically — it does not appear in charging profiles or delivery entries.

---

## Handler reference

### `deliver_chargeamps.py`

Delivers via the `my.charge.space` internal API. Converts plan windows to weekly `schedulePeriods` anchored to Monday 00:00 local time, which is how the Charge Amps web app represents its schedule. Logs in once per process and caches the token for reuse across multiple charger IDs.

**Mode restore** — before delivering the schedule, the connector's current mode (`On`, `Off`, or `Schedule`) is read from the API. After delivery, if the mode was not already `Schedule`, it is restored to its original value. This prevents the schedule delivery from silently switching the charger into schedule mode when the user had it set to `Always on` or `Off`. If mode restore fails the schedule was still delivered successfully — a warning is logged but the delivery is reported as succeeded.

**Config keys:**

| Key | Default | Description |
|---|---|---|
| `charge_point_id` | — | **Required.** The env var whose value is the Charge Amps charger ID. Accepts a string or list. |
| `connector_id` | `1` | Connector index on the charger |
| `max_charging_rate` | `16.0` | Maximum current in amps (A) |

**Environment variables:**

| Variable | Description |
|---|---|
| `CHARGER_EMAIL` | `my.charge.space` login email |
| `CHARGER_PASSWORD` | `my.charge.space` login password |
| `CHARGER_ID_<N>` | Charger ID — referenced by `charge_point_id` |

---

### `deliver_ocpp.py`

Delivers via OCPP WebSocket (`SetChargingProfile.req`). Connects to `<endpoint_url>/<chargePointId>` — the standard OCPP URL convention. Supports OCPP 1.6, 2.0.1, and 2.1; field names (`chargingProfileId` vs `id`, `connectorId` vs `evseId`) are set correctly per version.

Requires the `websockets` package:

```bash
pip install websockets
```

**Config keys:**

| Key | Default | Description |
|---|---|---|
| `charge_point_id` | — | **Required.** The env var whose value is the OCPP charge point ID. Accepts a string or list. |
| `endpoint_url_env` | `OCPP_ENDPOINT_URL` | Env var name holding the WebSocket base URL |
| `ocpp_version` | `"1.6"` | `"1.6"`, `"2.0.1"`, or `"2.1"` |
| `connector_id` | `1` | OCPP 1.6 `connectorId` / 2.x `evseId` |
| `transaction_id` | `null` | Optional transaction ID; `null` = `TxDefaultProfile` |
| `timeout` | `30` | Seconds to wait for response |
| `charging_rate_unit` | `"W"` | Unit for `max_charging_rate`: `"W"` (watts) or `"A"` (amps per phase) — check your charger's OCPP documentation |
| `max_charging_rate` | `11000.0` | Limit during charging windows in W or A |
| `profile_id` | `1` | `ChargingProfile` identifier — must be unique on the charger |
| `stack_level` | `0` | Higher values take precedence; `0` = lowest priority |

**Environment variables:**

| Variable | Description |
|---|---|
| `CHARGER_ID_<N>` | Charge point ID — referenced by `charge_point_id` |
| `OCPP_ENDPOINT_URL` | WebSocket base URL, e.g. `ws://192.168.1.10:9000/ocpp` |

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

3. Add a delivery entry in `config.yaml` with `handler: "<name>"` and any handler-specific keys
4. Add the relevant env vars to `.github/workflows/schedule.yml`

The dispatcher (`deliver.py`) will find and load the script automatically — no changes to `deliver.py` needed.

---

## Running

```bash
# Dispatch all plans using config.yaml
python delivery/deliver.py plan-*.json --config config.yaml

# Debug logging (shows full API payloads)
python delivery/deliver.py plan-*.json --debug
```
