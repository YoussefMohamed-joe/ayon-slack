"""Debug endpoints for troubleshooting Slack mention integration."""
import logging
import os
import socket
import sys

from fastapi import APIRouter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/addons/slack", tags=["slack"])


@router.get("/debug-addon-status")
async def debug_addon_status():
    """Check addon status and which server you're connected to."""
    try:
        from ayon_server.addons import get_addon

        server_url = os.environ.get("AYON_SERVER_URL", "unknown")
        hostname = socket.gethostname()
        try:
            host_ip = socket.gethostbyname(hostname)
        except Exception:  # pragma: no cover - defensive logging
            host_ip = "unknown"

        docker_container = os.environ.get("HOSTNAME", "unknown")
        ayon_env = os.environ.get("AYON_ENVIRONMENT", "unknown")
        ayon_addons_dir = os.environ.get("AYON_ADDONS_DIR", "unknown")

        addon = None
        addon_error = None
        try:
            addon = await get_addon("slack")
        except Exception as e:  # pragma: no cover - defensive logging
            addon_error = str(e)

        has_handlers = bool(addon and hasattr(addon, "_on_activity_created"))

        try:
            from slack_sdk import WebClient  # type: ignore[import]

            slack_sdk_available = True
        except ImportError:
            slack_sdk_available = False

        addon_version = "unknown"
        if addon:
            addon_version = getattr(addon, "__version__", "unknown")

        return {
            "status": "ok" if addon else "error",
            "server_info": {
                "hostname": hostname,
                "host_ip": host_ip,
                "docker_container": docker_container,
                "server_url_env": server_url,
                "ayon_environment": ayon_env,
                "ayon_addons_dir": ayon_addons_dir,
                "python_version": sys.version.split()[0],
                "addon_version": addon_version,
            },
            "addon_status": {
                "found": addon is not None,
                "active": addon is not None,
                "error": addon_error,
                "has_handlers": has_handlers,
                "addon_type": type(addon).__name__ if addon else None,
            },
            "dependencies": {
                "slack_sdk": slack_sdk_available,
            },
        }
    except Exception as e:  # pragma: no cover - defensive logging
        logger.error("Error in debug endpoint: %s", e, exc_info=True)
        return {"status": "error", "message": str(e)}


@router.get("/debug-event-handlers")
async def debug_event_handlers():
    """Debug endpoint to check if event handlers are registered."""
    try:
        from ayon_server.addons import get_addon

        addon = await get_addon("slack")
        if not addon:
            return {
                "status": "error",
                "message": "Slack addon not found or not active",
            }

        has_register = hasattr(addon, "register_event_handler")
        has_handlers = hasattr(addon, "_on_activity_created")

        try:
            from slack_sdk import WebClient  # type: ignore[import]

            slack_sdk_available = True
        except ImportError:
            slack_sdk_available = False

        return {
            "status": "ok",
            "addon_found": True,
            "addon_active": True,
            "has_register_event_handler": has_register,
            "has_on_activity_created": has_handlers,
            "addon_type": type(addon).__name__,
            "dependencies": {
                "slack_sdk": slack_sdk_available,
            },
        }
    except Exception as e:  # pragma: no cover - defensive logging
        logger.error("Error in debug endpoint: %s", e, exc_info=True)
        return {"status": "error", "message": str(e)}

