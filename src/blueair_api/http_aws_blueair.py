import functools
import json
import base64

from logging import getLogger
from typing import Any
from aiohttp import ClientSession, ClientResponse, FormData
from datetime import datetime, timedelta

from .const import AWS_APIKEYS
from .util_http import request_with_logging
from .errors import SessionError, LoginError

_LOGGER = getLogger(__name__)


def request_with_active_session(func):
    @functools.wraps(func)
    async def request_with_active_session_wrapper(*args, **kwargs) -> ClientResponse:
        _LOGGER.debug("session")
        try:
            return await func(*args, **kwargs)
        except SessionError:
            _LOGGER.debug("got invalid session, attempting to repair and resend")
            self = args[0]
            self.session_token = None
            self.session_secret = None
            self.access_token = None
            self.user_id = None
            self.mqtt_auth_name = None
            self.mqtt_auth_signature = None
            self.mqtt_auth_token = None
            self.jwt = None
            response = await func(*args, **kwargs)
            return response

    return request_with_active_session_wrapper


def request_with_errors(func):
    @functools.wraps(func)
    async def request_with_errors_wrapper(*args, **kwargs) -> ClientResponse:
        _LOGGER.debug("checking for errors")
        response: ClientResponse = await func(*args, **kwargs)
        status_code = response.status
        try:
            response_json = await response.json(content_type=None)
            if response_json is not None:
                if "statusCode" in response_json:
                    _LOGGER.debug("response json found, checking status code from response")
                    status_code = response_json["statusCode"]
        except Exception as e:
            _LOGGER.error(f"Error parsing response for errors {e}")
            raise e
        if status_code == 200:
            _LOGGER.debug("response 200")
            return response
        if 400 <= status_code <= 500:
            _LOGGER.debug(f"auth error, {status_code}")
            url = kwargs["url"]
            response_text = await response.text()
            if "accounts.login" in url:
                _LOGGER.debug("login error")
                raise LoginError(response_text)
            else:
                _LOGGER.debug("session error")
                raise SessionError(response_text)
        raise ValueError(f"unknown status code {status_code}")

    return request_with_errors_wrapper


class HttpAwsBlueair:
    def __init__(
        self,
        username: str,
        password: str,
        region: str = "us",
        client_session: ClientSession | None = None,
    ):
        self.username = username
        self.password = password
        self.region = region

        self.session_token = None
        self.session_secret = None

        self.access_token = None
        self.user_id = None

        self.mqtt_auth_name = None
        self.mqtt_auth_signature = None
        self.mqtt_auth_token = None

        self.jwt = None

        if client_session is None:
            self.api_session = ClientSession(raise_for_status=False)
        else:
            self.api_session = client_session

    async def cleanup_client_session(self):
        await self.api_session.close()

    @request_with_errors
    @request_with_logging
    async def _get_request_with_logging_and_errors_raised(
        self, url: str, headers: dict | None = None, params: dict | None = None
    ) -> ClientResponse:
        return await self.api_session.get(url=url, headers=headers, params=params)

    @request_with_errors
    @request_with_logging
    async def _post_request_with_logging_and_errors_raised(
        self,
        url: str,
        json_body: dict | None = None,
        form_data: FormData | None = None,
        headers: dict | None = None,
    ) -> ClientResponse:
        return await self.api_session.post(
            url=url, data=form_data, json=json_body, headers=headers
        )

    async def refresh_session(self) -> None:
        _LOGGER.debug("refresh_session")
        url = f"https://{AWS_APIKEYS[self.region]['gigyaRegion']}/accounts.login"
        form_data = FormData()
        form_data.add_field("apikey", AWS_APIKEYS[self.region]["apiKey"])
        form_data.add_field("loginID", self.username)
        form_data.add_field("password", self.password)
        form_data.add_field("targetEnv", "mobile")
        response: ClientResponse = (
            await self._post_request_with_logging_and_errors_raised(
                url=url, form_data=form_data
            )
        )
        response_json = await response.json(content_type="text/javascript")
        self.session_token = response_json["sessionInfo"]["sessionToken"]
        self.session_secret = response_json["sessionInfo"]["sessionSecret"]

    async def refresh_jwt(self) -> None:
        _LOGGER.debug("refresh_jwt")
        if self.session_token is None or self.session_secret is None:
            await self.refresh_session()
        url = f"https://{AWS_APIKEYS[self.region]['gigyaRegion']}/accounts.getJWT"
        form_data = FormData()
        form_data.add_field("oauth_token", self.session_token)
        form_data.add_field("secret", self.session_secret)
        form_data.add_field("targetEnv", "mobile")
        response: ClientResponse = (
            await self._post_request_with_logging_and_errors_raised(
                url=url, form_data=form_data
            )
        )
        response_json = await response.json(content_type="text/javascript")
        self.jwt = response_json["id_token"]

    async def refresh_access_token(self) -> None:
        _LOGGER.debug("refresh_access_token")
        # Always do a full re-authentication from scratch.
        # The JWT from Gigya has a very short lifetime (~5 minutes) and cannot
        # be reused.  Previously we skipped refresh_jwt() when self.jwt was
        # still set, which caused MQTT credential refreshes to send an expired
        # JWT and get stuck in a 401 loop.
        self.session_token = None
        self.session_secret = None
        self.jwt = None
        await self.refresh_jwt()
        url = f"https://{AWS_APIKEYS[self.region]['restApiId']}.execute-api.{AWS_APIKEYS[self.region]['awsRegion']}/prod/c/login"
        headers = {"idtoken": self.jwt, "authorization": f"Bearer {self.jwt}"}
        response: ClientResponse = (
            await self._post_request_with_logging_and_errors_raised(
                url=url, headers=headers
            )
        )
        response_json = await response.json()
        self.access_token = response_json["access_token"]
        # MQTT auth credentials from the same login response.
        self.mqtt_auth_name = response_json.get("ba_X-Amz-CustomAuthorizer-Name")
        self.mqtt_auth_signature = response_json.get("ba_X-Amz-CustomAuthorizer-Signature")
        self.mqtt_auth_token = response_json.get("ba_X-Amz-CustomAuthorizer-Token")
        if self.mqtt_auth_name and self.mqtt_auth_signature and self.mqtt_auth_token:
            _LOGGER.debug("refresh_access_token: obtained access token and MQTT credentials")
        else:
            _LOGGER.warning(
                "refresh_access_token: MQTT credentials missing from login response; "
                "MQTT real-time updates will not be available"
            )
        # Extract the username claim from the JWT to use as userId in API paths
        try:
            assert self.access_token is not None
            payload = self.access_token.split(".")[1]
            # Add padding if needed
            payload += "=" * (4 - len(payload) % 4)
            claims = json.loads(base64.urlsafe_b64decode(payload))
            self.user_id = claims.get("username", "")
            _LOGGER.debug(f"Extracted user_id from JWT: {self.user_id}")
        except Exception as e:
            _LOGGER.warning(f"Failed to extract user_id from access_token JWT: {e}")
            self.user_id = None

    async def get_user_id(self) -> str:
        if self.user_id is None:
            await self.refresh_access_token()
        assert self.user_id is not None
        return self.user_id

    async def get_access_token(self) -> str:
        _LOGGER.debug("get_access_token")
        if self.access_token is None:
            await self.refresh_access_token()
        assert self.access_token is not None
        return self.access_token

    @request_with_active_session
    async def devices(self) -> dict[str, Any]:
        _LOGGER.debug("devices")
        url = f"https://{AWS_APIKEYS[self.region]['restApiId']}.execute-api.{AWS_APIKEYS[self.region]['awsRegion']}/prod/c/registered-devices"
        headers = {
            "Authorization": f"Bearer {await self.get_access_token()}",
        }
        response: ClientResponse = (
            await self._get_request_with_logging_and_errors_raised(
                url=url, headers=headers
            )
        )
        response_json = await response.json()
        return response_json["devices"]

    @request_with_active_session
    async def device_sensors(self, device_name, device_uuid, duration: timedelta = timedelta(hours=10)):
        user_id = await self.get_user_id()
        url = f"https://{AWS_APIKEYS[self.region]['restApiId']}.execute-api.{AWS_APIKEYS[self.region]['awsRegion']}/prod/c/{user_id}/r/telemetry/5m/historical"
        headers = {
            "Authorization": f"Bearer {await self.get_access_token()}",
        }
        params = {
            "did": device_uuid,
            "from": int((datetime.now()-duration).timestamp()),
            "to": int(datetime.now().timestamp()),
            "s": ["pm1", "pm2_5", "pm10", "tVOC", "hcho", "h", "t", "fsp0"]
        }
        response: ClientResponse = (
            await self._get_request_with_logging_and_errors_raised(
                url=url, headers=headers, params=params
            )
        )
        response_json = await response.json()
        return response_json

    @request_with_active_session
    async def device_info(self, device_name, device_uuid) -> dict[str, Any]:
        _LOGGER.debug("device_info")
        user_id = await self.get_user_id()
        url = f"https://{AWS_APIKEYS[self.region]['restApiId']}.execute-api.{AWS_APIKEYS[self.region]['awsRegion']}/prod/c/{user_id}/r/initial"
        headers = {
            "Authorization": f"Bearer {await self.get_access_token()}",
        }
        json_body = {
            "deviceconfigquery": [
                {
                    "id": device_uuid,
                    "r": {
                        "r": [
                            "sensors",
                        ],
                    },
                },
            ],
            "includestates": True
        }
        response: ClientResponse = (
            await self._post_request_with_logging_and_errors_raised(
                url=url, headers=headers, json_body=json_body
            )
        )
        response_json = await response.json()
        return response_json["deviceInfo"][0]

    @request_with_active_session
    async def set_device_info(
        self, device_uuid, service_name, action_verb, action_value
    ) -> bool:
        _LOGGER.debug("set_device_info")
        url = f"https://{AWS_APIKEYS[self.region]['restApiId']}.execute-api.{AWS_APIKEYS[self.region]['awsRegion']}/prod/c/{device_uuid}/a/{service_name}"
        headers = {
            "Authorization": f"Bearer {await self.get_access_token()}",
        }
        json_body = {"n": service_name, action_verb: action_value}
        response: ClientResponse = (
            await self._post_request_with_logging_and_errors_raised(
                url=url, headers=headers, json_body=json_body
            )
        )
        response_text = await response.text()
        return response_text == "Success"

    # ------------------------------------------------------------------
    # Consumable reset
    # ------------------------------------------------------------------
    # The cloud exposes a dedicated endpoint to reset a consumable's life
    # counter (filter / wick / refresher). The endpoint is *not* a shadow
    # write — it's a cloud REST call whose response carries a status code,
    # and on success the cloud later pushes a shadow update that drops the
    # corresponding `*usage` slug back to 0.
    #
    # Endpoint and payload shape were derived by inspecting the Blueair
    # cloud API responses observed during normal app operation.

    #: Valid `ctype` values accepted by ``reset_consumable``.
    VALID_CONSUMABLE_TYPES: tuple[str, ...] = ("filter", "wick", "refresher")

    #: Cloud response status codes for ``reset_consumable``.
    RESET_STATUS_SUCCESS: int = 0
    RESET_STATUS_DEVICE_OFFLINE: int = 3

    @request_with_active_session
    async def reset_consumable(
        self, device_uuid: str, ctype: str = "filter"
    ) -> dict[str, Any]:
        """Reset a consumable's life counter for a device.

        Args:
            device_uuid: The device's cloud UUID (same value used in MQTT
                topics and ``device_info`` calls).
            ctype: One of ``"filter"``, ``"wick"``, ``"refresher"``.

        Returns:
            The parsed JSON response body. The caller should inspect the
            ``status`` field (see ``RESET_STATUS_*``); non-zero means the
            cloud could not deliver the reset (e.g. device offline).

        Raises:
            ValueError: If ``ctype`` is not in :data:`VALID_CONSUMABLE_TYPES`.
            LoginError / SessionError: Propagated from the shared request
                decorators if authentication fails.
        """
        if ctype not in self.VALID_CONSUMABLE_TYPES:
            raise ValueError(
                f"invalid ctype {ctype!r}; expected one of "
                f"{self.VALID_CONSUMABLE_TYPES}"
            )
        _LOGGER.debug(
            "reset_consumable: device_uuid=%s ctype=%s", device_uuid, ctype
        )
        url = (
            f"https://{AWS_APIKEYS[self.region]['restApiId']}"
            f".execute-api.{AWS_APIKEYS[self.region]['awsRegion']}"
            f"/prod/c/consumable/direct-reset"
        )
        headers = {
            "Authorization": f"Bearer {await self.get_access_token()}",
        }
        json_body = {"deviceid": device_uuid, "ctype": ctype}
        response: ClientResponse = (
            await self._post_request_with_logging_and_errors_raised(
                url=url, headers=headers, json_body=json_body
            )
        )
        try:
            response_json = await response.json(content_type=None)
        except Exception as exc:
            response_text = await response.text()
            _LOGGER.warning(
                "reset_consumable: failed to parse JSON for "
                "device_uuid=%s ctype=%s: %s; raw=%r",
                device_uuid, ctype, exc, response_text,
            )
            return {"status": -1, "raw": response_text}
        if not isinstance(response_json, dict):
            _LOGGER.warning(
                "reset_consumable: unexpected non-dict response for "
                "device_uuid=%s ctype=%s: %r",
                device_uuid, ctype, response_json,
            )
            return {"status": -1, "raw": response_json}
        status = response_json.get("status")
        if status == self.RESET_STATUS_SUCCESS:
            _LOGGER.debug(
                "reset_consumable: success device_uuid=%s ctype=%s",
                device_uuid, ctype,
            )
        elif status == self.RESET_STATUS_DEVICE_OFFLINE:
            _LOGGER.warning(
                "reset_consumable: device offline device_uuid=%s ctype=%s",
                device_uuid, ctype,
            )
        else:
            _LOGGER.warning(
                "reset_consumable: cloud reported failure status=%s "
                "device_uuid=%s ctype=%s body=%s",
                status, device_uuid, ctype, response_json,
            )
        return response_json
