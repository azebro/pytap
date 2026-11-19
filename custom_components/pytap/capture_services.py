"""Explicit user actions for private, opt-in packet recording."""

import asyncio
import logging
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.util.hass_dict import HassKey
import voluptuous as vol

from .const import DOMAIN
from .pytap.core.capture import CaptureError, CaptureRuntimeError, remove_capture_root

_LOGGER = logging.getLogger(__name__)

_ENTRY = {vol.Required("config_entry_id"): str}
_CAPTURE = {**_ENTRY, vol.Optional("capture_id"): str}
_STORAGE = vol.All(vol.Coerce(int), vol.Range(min=16, max=4096))

# Export/delete jobs by capture root. Domain-wide, not per coordinator: a job
# keeps running in the executor across unload/reload of its config entry.
_CAPTURE_JOBS: HassKey[dict[str, asyncio.Future[Any]]] = HassKey(f"{DOMAIN}_capture_jobs")
CAPTURE_JOB_WAIT_TIMEOUT = 30.0


def capture_root(hass: HomeAssistant, entry_id: str) -> Path:
    """Private per-instance capture directory (included in Home Assistant backups)."""
    return Path(hass.config.path("pytap_captures", entry_id))


def _capture_job(hass: HomeAssistant, root: Path) -> asyncio.Future[Any] | None:
    job = hass.data.get(_CAPTURE_JOBS, {}).get(str(root))
    return job if job is not None and not job.done() else None


def capture_job_running(hass: HomeAssistant, root: Path) -> bool:
    """Whether any coordinator (including an unloaded one) has a job on this root."""
    return _capture_job(hass, root) is not None


def track_capture_job(hass: HomeAssistant, root: Path, job: asyncio.Future[Any]) -> None:
    """Register a running export/delete; call without awaiting after capture_job_running."""
    jobs = hass.data.setdefault(_CAPTURE_JOBS, {})
    key = str(root)
    jobs[key] = job

    def _done(_job: asyncio.Future[Any]) -> None:
        if jobs.get(key) is job:
            del jobs[key]

    job.add_done_callback(_done)


async def async_wait_for_capture_jobs(hass: HomeAssistant, entry_id: str, timeout: float) -> bool:
    """Wait for this entry's export/delete job; False if it is still running after timeout."""
    job = _capture_job(hass, capture_root(hass, entry_id))
    if job is None:
        return True
    done, _ = await asyncio.wait({job}, timeout=timeout)
    return bool(done)


async def async_remove_captures(hass: HomeAssistant, entry_id: str) -> None:
    """Delete all captures/exports of a removed config entry; never raises.

    Never deletes under a running export: removal is deferred until it ends.
    """
    if not await async_wait_for_capture_jobs(hass, entry_id, CAPTURE_JOB_WAIT_TIMEOUT):
        job = _capture_job(hass, capture_root(hass, entry_id))
        if job is not None:
            _LOGGER.info("PyTap packet capture export still running; captures are removed when it ends")
            job.add_done_callback(
                lambda _job: hass.async_create_task(
                    async_remove_captures(hass, entry_id), "pytap_remove_captures"
                )
            )
            return
    try:
        await hass.async_add_executor_job(remove_capture_root, capture_root(hass, entry_id))
    except (OSError, ValueError) as err:
        # Exception text may include filesystem paths.
        _LOGGER.warning("Could not remove PyTap packet captures (%s)", type(err).__name__)


def register_capture_services(hass: HomeAssistant) -> None:
    async def handle(call: ServiceCall) -> ServiceResponse:
        entry = hass.config_entries.async_get_entry(call.data["config_entry_id"])
        if entry is None or entry.domain != DOMAIN or entry.state is not ConfigEntryState.LOADED:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="capture_entry_not_loaded"
            )
        coordinator = entry.runtime_data
        try:
            if call.service == "start_capture":
                return await coordinator.async_start_capture(
                    call.data["duration_minutes"], call.data["max_storage_mib"]
                )
            if call.service == "stop_capture":
                return await coordinator.async_stop_capture()
            if call.service == "capture_status":
                return await coordinator.async_capture_info(call.data.get("capture_id"))
            if call.service == "list_captures":
                return {"captures": await coordinator.async_list_captures()}
            if call.service == "delete_capture":
                return await coordinator.async_delete_capture(call.data["capture_id"])
            return await coordinator.async_export_capture(
                call.data.get("capture_id"), call.data.get("max_storage_mib")
            )
        except CaptureRuntimeError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key=err.translation_key,
                translation_placeholders=err.translation_placeholders,
            ) from None
        except CaptureError as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key=err.translation_key,
                translation_placeholders=err.translation_placeholders,
            ) from None
        except (OSError, KeyError) as err:
            # Exception text may include filesystem or raw-data details.
            _LOGGER.debug("PyTap %s failed (%s)", call.service, type(err).__name__)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="capture_failed",
                translation_placeholders={"error": type(err).__name__},
            ) from None
        except ValueError as err:
            # Selected evidence failed validation (corrupt, incomplete, unsupported).
            _LOGGER.debug("PyTap %s rejected the capture (%s)", call.service, type(err).__name__)
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="capture_invalid"
            ) from None

    for name, schema in {
        "start_capture": {
            **_ENTRY,
            vol.Optional("duration_minutes", default=1440): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=10080)
            ),
            vol.Optional("max_storage_mib", default=1024): _STORAGE,
        },
        "stop_capture": _ENTRY,
        "capture_status": _CAPTURE,
        "list_captures": _ENTRY,
        "export_capture": {**_CAPTURE, vol.Optional("max_storage_mib"): _STORAGE},
        "delete_capture": {**_ENTRY, vol.Required("capture_id"): str},
    }.items():
        async_register_admin_service(
            hass, DOMAIN, name, handle, schema=vol.Schema(schema), supports_response=SupportsResponse.OPTIONAL
        )
