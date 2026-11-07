"""Config flow to set up the Fridge Vision Inventory integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectOptionDict,
    TextSelector,
)

from .const import (
    CONF_AI_SERVICE_URL,
    CONF_CAMERA_ENTITY,
    CONF_CAPTURE_FPS,
    CONF_CONFIDENCE_THRESHOLD,
    CONF_DOOR_SENSOR_ENTITY,
    CONF_MOTION_VECTOR_INSERT_DIRECTION,
    CONF_NOTIFY_DEVICE,
    DEFAULT_AI_SERVICE_URL,
    DEFAULT_CAPTURE_FPS,
    DEFAULT_CONFIDENCE_THRESHOLD,
    DEFAULT_MOTION_VECTOR_INSERT_DIRECTION,
    DOMAIN,
    NOTIFY_DOMAIN,
    VECTOR_BOTTOM_TO_TOP,
    VECTOR_TOP_TO_BOTTOM,
)
from .coordinator import VisionApiClient, VisionApiError

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_AI_SERVICE_URL, default=DEFAULT_AI_SERVICE_URL): str,
        vol.Required(CONF_CAMERA_ENTITY): EntitySelector(
            EntitySelectorConfig(domain="camera")
        ),
        vol.Required(CONF_DOOR_SENSOR_ENTITY): EntitySelector(
            EntitySelectorConfig(domain="binary_sensor")
        ),
        vol.Required(CONF_CAPTURE_FPS, default=DEFAULT_CAPTURE_FPS): NumberSelector(
            NumberSelectorConfig(min=1, max=10, step=1, mode=NumberSelectorMode.BOX)
        ),
        vol.Required(
            CONF_MOTION_VECTOR_INSERT_DIRECTION,
            default=DEFAULT_MOTION_VECTOR_INSERT_DIRECTION,
        ): SelectSelector(
            SelectSelectorConfig(
                options=[
                    SelectOptionDict(
                        value=VECTOR_TOP_TO_BOTTOM,
                        label="Top → bottom (ręka wchodzi z góry)",
                    ),
                    SelectOptionDict(
                        value=VECTOR_BOTTOM_TO_TOP,
                        label="Bottom → top (ręka wchodzi od dołu)",
                    ),
                ]
            )
        ),
        vol.Required(
            CONF_CONFIDENCE_THRESHOLD, default=DEFAULT_CONFIDENCE_THRESHOLD
        ): NumberSelector(
            NumberSelectorConfig(
                min=0.05, max=0.95, step=0.05, mode=NumberSelectorMode.SLIDER
            )
        ),
        vol.Optional(CONF_NOTIFY_DEVICE, default=""): TextSelector(),
    }
)


class FridgeVisionConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the config flow for Fridge Vision Inventory."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the single-step setup form."""
        errors: dict[str, str] = {}
        if user_input is not None:
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()

            url = str(user_input[CONF_AI_SERVICE_URL]).strip().rstrip("/")
            notify = str(user_input.get(CONF_NOTIFY_DEVICE, "") or "").strip()
            if notify and "." not in notify and "/" not in notify:
                notify = f"{NOTIFY_DOMAIN}.{notify}"

            # Own force_close session (closed right after the probe): the
            # shared HA session can hand back a pooled connection the
            # add-on already closed, which would show a spurious
            # cannot_connect during setup.
            client = VisionApiClient(None, url)
            try:
                await client.async_health()
            except VisionApiError:
                errors["base"] = "cannot_connect"
            finally:
                await client.aclose()

            if not errors:
                return self.async_create_entry(
                    title="Fridge Vision Inventory",
                    data={
                        CONF_AI_SERVICE_URL: url,
                        CONF_CAMERA_ENTITY: user_input[CONF_CAMERA_ENTITY],
                        CONF_DOOR_SENSOR_ENTITY: user_input[
                            CONF_DOOR_SENSOR_ENTITY
                        ],
                        CONF_CAPTURE_FPS: int(user_input[CONF_CAPTURE_FPS]),
                        CONF_MOTION_VECTOR_INSERT_DIRECTION: user_input[
                            CONF_MOTION_VECTOR_INSERT_DIRECTION
                        ],
                        CONF_CONFIDENCE_THRESHOLD: float(
                            user_input[CONF_CONFIDENCE_THRESHOLD]
                        ),
                        CONF_NOTIFY_DEVICE: notify,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
            description_placeholders={"example_url": DEFAULT_AI_SERVICE_URL},
        )
