# Resetting a Consumable's Life Counter

Blueair air purifiers and humidifiers track the remaining life of their
consumables (the particulate filter, the humidifier wick, the water
refresher cartridge) as a percentage in the device shadow. When you
replace the physical part, the on-device counter does not reset itself;
the device waits for the cloud to push it a reset command.

`blueair_api` exposes that flow as a one-shot async call:
`DeviceAws.reset_consumable(ctype)` (with `reset_filter` / `reset_wick` /
`reset_refresher` as convenience wrappers).

## What happens on the wire

1. The library issues `POST` to
   `https://<rest-api-id>.execute-api.<aws-region>.amazonaws.com/prod/c/consumable/direct-reset`
   with an `Authorization: Bearer <access_token>` header.
2. The request body is JSON: `{"deviceid": "<uuid>", "ctype": "<filter|wick|refresher>"}`.
3. The cloud responds with JSON; the meaningful field is `status`.
4. On `status: 0` (success) the cloud subsequently pushes a shadow update
   that sets the corresponding `*usage` slug back to `0`. That update
   arrives via the normal MQTT path and updates
   `DeviceAws.filter_usage_percentage` /
   `DeviceAws.wick_usage_percentage` /
   `DeviceAws.water_refresher_usage_percentage`. The library does *not*
   optimistically zero those locally; letting the shadow drive the value
   keeps the displayed life accurate when the reset is rejected.

## Cloud status codes

| `status` | Meaning |
| -------- | ------- |
| `0` | Success — the reset was accepted and forwarded to the device. |
| `3` | Device offline — the cloud could not deliver the reset; try again when the device is back online. |
| anything else | Generic failure — typically an unknown `ctype` for that SKU or a transient backend error. |

The convenience helpers (`reset_filter`, `reset_wick`, `reset_refresher`)
and `reset_consumable` itself collapse this to a `bool`: `True` only when
`status == 0`.

## API reference

### `DeviceAws.reset_consumable(ctype: str) -> bool`

Reset the named consumable on this device.

- **`ctype`** — one of `"filter"`, `"wick"`, `"refresher"`. Unknown
  values raise `ValueError` from the HTTP layer.
- **Returns** — `True` if the cloud accepted the reset (`status == 0`),
  `False` otherwise.
- **Raises**
  - `ValueError` — `ctype` is not a known consumable type.
  - `RuntimeError` — called on a `DeviceAws` whose `uuid` is `None`
    (defensive; should never happen for a properly-constructed device).
  - `LoginError` / `SessionError` / `aiohttp.ClientError` — propagated
    from the HTTP layer so callers can surface auth or transport
    failures to the user.

### `DeviceAws.reset_filter() -> bool`

### `DeviceAws.reset_wick() -> bool`

### `DeviceAws.reset_refresher() -> bool`

Thin wrappers around `reset_consumable(ctype)` for each supported
consumable type. They exist to give downstream UIs (e.g. one Home
Assistant button per consumable) a method per entity rather than
forcing the entity to thread `ctype` through.

### `HttpAwsBlueair.reset_consumable(device_uuid: str, ctype: str = "filter") -> dict`

Low-level call if you need the raw cloud response (e.g. to inspect the
exact `status` value, surface a localized error message, or build your
own retry policy on top of it). Returns the parsed JSON body; falls back
to `{"status": -1, "raw": <response>}` when the cloud returns an
unexpected shape so callers can always read `.get("status")` safely.

The class also exposes constants you can compare against instead of
hard-coded integers:

- `HttpAwsBlueair.VALID_CONSUMABLE_TYPES` — `("filter", "wick", "refresher")`
- `HttpAwsBlueair.RESET_STATUS_SUCCESS` — `0`
- `HttpAwsBlueair.RESET_STATUS_DEVICE_OFFLINE` — `3`

## Which consumables does a given device have?

Not every Blueair device has all three consumables. The presence of the
corresponding `*usage` field on the device shadow is the source of truth:

| Consumable | `ctype` | Shadow field | `DeviceAws` attribute |
| ---------- | ------- | ------------ | --------------------- |
| Particulate filter | `filter` | `filterusage` | `filter_usage_percentage` |
| Humidifier wick | `wick` | `wickusage` | `wick_usage_percentage` |
| Water refresher | `refresher` | `ywrmusage` | `water_refresher_usage_percentage` |

If the attribute is `NotImplemented` after `device.refresh()`, the device
does not expose that consumable and you should not surface a reset button
for it.

## Example: reset the filter when life drops below 5%

```python
import asyncio
import logging

from blueair_api import get_aws_devices

logging.basicConfig(level=logging.INFO)
_LOGGER = logging.getLogger(__name__)


async def main() -> None:
    # get_aws_devices returns (api, [DeviceAws, ...]) after authenticating
    # and populating the device list from the cloud.
    api, devices = await get_aws_devices(
        username="you@example.com",
        password="...",
        region="us",
    )

    try:
        for device in devices:
            await device.refresh()

            life_remaining = device.filter_usage_percentage
            if life_remaining is None or life_remaining is NotImplemented:
                continue  # this device has no particulate filter

            # `filter_usage_percentage` is "% used", so "% remaining" is
            # `100 - filter_usage_percentage`.
            remaining = 100 - life_remaining
            _LOGGER.info(
                "Device %s (%s): filter life remaining = %s%%",
                device.name_api, device.uuid, remaining,
            )

            if remaining >= 5:
                continue

            _LOGGER.info(
                "Resetting filter on %s after physical replacement",
                device.name_api,
            )
            ok = await device.reset_filter()
            if ok:
                _LOGGER.info("Reset accepted by the cloud")
                # filter_usage_percentage will reset to 0 on the next
                # shadow update — typically within a few seconds.
            else:
                _LOGGER.warning(
                    "Reset rejected. Device may be offline; "
                    "try again after the next refresh."
                )
    finally:
        await api.cleanup_client_session()


if __name__ == "__main__":
    asyncio.run(main())
```

## Example: low-level call with explicit status handling

When you need the raw status code (for example, to distinguish "device
offline, retry later" from "permanent failure"), bypass the helper and
talk to the HTTP layer directly:

```python
from blueair_api import HttpAwsBlueair

api = HttpAwsBlueair(username="...", password="...", region="us")
await api.refresh_access_token()

response = await api.reset_consumable(device_uuid="abcd-1234", ctype="wick")
status = response.get("status")

if status == HttpAwsBlueair.RESET_STATUS_SUCCESS:
    print("Wick reset accepted")
elif status == HttpAwsBlueair.RESET_STATUS_DEVICE_OFFLINE:
    print("Device offline — retry after it comes back online")
else:
    # `status` may be `-1` if the cloud returned a non-JSON or non-dict
    # body; `response["raw"]` carries the original payload in that case.
    print(f"Reset failed: status={status} body={response}")
```

## Failure paths and logging

The library logs at the following levels (visible by configuring the
`blueair_api.http_aws_blueair` logger):

| Outcome | Level | Where |
| ------- | ----- | ----- |
| Entering the call | `DEBUG` | `http_aws_blueair`, `device_aws` |
| `status == 0` (success) | `DEBUG` | `http_aws_blueair` |
| `status == 3` (device offline) | `WARNING` | `http_aws_blueair` |
| Other non-zero `status` | `WARNING` | `http_aws_blueair` |
| Non-JSON / non-dict response body | `WARNING` | `http_aws_blueair` |
| Auth / transport failure | (raised) | propagated via `LoginError` / `SessionError` / `aiohttp.ClientError` |
| Unknown `ctype` | (raised) | `ValueError` from `HttpAwsBlueair.reset_consumable` |
| `uuid is None` on `DeviceAws` | (raised) | `RuntimeError` from `DeviceAws.reset_consumable` |

To watch the full request/response flow during debugging:

```python
import logging
logging.getLogger("blueair_api.http_aws_blueair").setLevel(logging.DEBUG)
logging.getLogger("blueair_api.device_aws").setLevel(logging.DEBUG)
```

## See also

If you're using this library through the
[ha_blueair Home Assistant integration](https://github.com/dahlb/ha_blueair),
you don't need to call any of the methods above directly — the
integration exposes one HA `button` entity per detected consumable:

- **Reset Filter Life** — appears for any device that reports
  `filter_usage_percentage`
- **Reset Wick Life** — appears for humidifiers that report
  `wick_usage_percentage`
- **Reset Refresher Life** — appears for devices that report
  `water_refresher_usage_percentage`

The buttons live under **Configuration** on the device card (they use
`EntityCategory.CONFIG`) so they don't clutter the main controls. Press
a button after physically replacing the part; the
`*_usage_percentage` sensor will update to 0 within a few seconds via
the next MQTT shadow update. Failed presses (device offline, cloud
rejection, auth failure) surface as Home Assistant notifications with
the cloud's failure mode in the log.
