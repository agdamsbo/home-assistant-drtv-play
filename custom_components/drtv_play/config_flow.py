"""Config flow for DRTV Play.

Lets the user either add an anonymous DRTV source, or log in with their
dr.dk account so the media source can also show "My List" and "Continue
watching" (this is the Home Assistant equivalent of the username/password
fields in the Kodi add-on's settings.xml).
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_USERNAME, CONF_PASSWORD
from homeassistant.core import callback

from . import DOMAIN
from .video_url_fetch.tvapi import full_login

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_USERNAME, default=""): str,
        vol.Optional(CONF_PASSWORD, default=""): str,
    }
)


class DrtvPlayConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a DRTV Play config flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ):
        errors: dict[str, str] = {}

        if user_input is not None:
            username = user_input.get(CONF_USERNAME, "").strip()
            password = user_input.get(CONF_PASSWORD, "")

            if username and not password:
                errors["base"] = "password_required"
            elif not username and password:
                errors["base"] = "username_required"
            else:
                if username:
                    result = await self.hass.async_add_executor_job(
                        full_login, username, password
                    )
                    if "error" in result:
                        _LOGGER.debug("DRTV login failed: %s", result["error"])
                        errors["base"] = "invalid_auth"

                if not errors:
                    unique_id = username or "anonymous"
                    await self.async_set_unique_id(unique_id)
                    self._abort_if_unique_id_configured()

                    title = username if username else "DRTV (anonymous)"
                    return self.async_create_entry(
                        title=title,
                        data={CONF_USERNAME: username, CONF_PASSWORD: password},
                    )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
            description_placeholders={"docs_url": "https://www.dr.dk/drtv/"},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return DrtvPlayOptionsFlow()

    async def async_step_reauth(self, entry_data: dict[str, Any]):
        """Start a reauth flow when a saved login stops working."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ):
        errors: dict[str, str] = {}
        reauth_entry = self._get_reauth_entry()

        if user_input is not None:
            username = user_input.get(CONF_USERNAME, "").strip()
            password = user_input.get(CONF_PASSWORD, "")

            result = await self.hass.async_add_executor_job(full_login, username, password)
            if "error" in result:
                _LOGGER.debug("DRTV reauth failed: %s", result["error"])
                errors["base"] = "invalid_auth"
            else:
                await self.async_set_unique_id(username)
                self._abort_if_unique_id_mismatch(reason="wrong_account")
                return self.async_update_reload_and_abort(
                    reauth_entry,
                    data_updates={CONF_USERNAME: username, CONF_PASSWORD: password},
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME, default=reauth_entry.data.get(CONF_USERNAME, "")): str,
                vol.Required(CONF_PASSWORD): str,
            }
        )
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=schema, errors=errors
        )


class DrtvPlayOptionsFlow(config_entries.OptionsFlow):
    """Allow updating/removing stored DRTV credentials after setup.

    `config_entry` is provided automatically as a read-only property by
    the base class - it must not be assigned in __init__ (that pattern
    was deprecated in HA 2024.12 and removed in 2025.12, and raises
    AttributeError, seen in the UI as a 500 error opening this flow).
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ):
        errors: dict[str, str] = {}
        current = self.config_entry.data

        if user_input is not None:
            username = user_input.get(CONF_USERNAME, "").strip()
            password = user_input.get(CONF_PASSWORD, "")

            if username and not password:
                errors["base"] = "password_required"
            elif not username and password:
                errors["base"] = "username_required"
            else:
                if username:
                    result = await self.hass.async_add_executor_job(
                        full_login, username, password
                    )
                    if "error" in result:
                        errors["base"] = "invalid_auth"

                if not errors:
                    self.hass.config_entries.async_update_entry(
                        self.config_entry,
                        data={CONF_USERNAME: username, CONF_PASSWORD: password},
                    )
                    return self.async_create_entry(title="", data={})

        schema = vol.Schema(
            {
                vol.Optional(CONF_USERNAME, default=current.get(CONF_USERNAME, "")): str,
                vol.Optional(CONF_PASSWORD, default=current.get(CONF_PASSWORD, "")): str,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
