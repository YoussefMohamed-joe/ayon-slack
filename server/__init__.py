from __future__ import annotations

from typing import Any

from ayon_server.addons import BaseServerAddon
from ayon_server.events import EventModel, EventStream

from .settings import (
    SlackSettings,
    DEFAULT_SLACK_SETTING,
    convert_settings_overrides,
)


class Slack(BaseServerAddon):

    settings_model = SlackSettings

    async def get_default_settings(self):
        settings_model_cls = self.get_settings_model()
        return settings_model_cls(**DEFAULT_SLACK_SETTING)

    async def convert_settings_overrides(
        self,
        source_version: str,
        overrides: dict[str, Any],
    ) -> dict[str, Any]:
        await convert_settings_overrides(source_version, overrides)
        return await super().convert_settings_overrides(
            source_version, overrides
        )

    def initialize(self):
        """Initialize the addon and register event handlers."""
        import logging
        import os
        import socket
        logger = logging.getLogger(__name__)

        # Get server identification info
        server_url = os.environ.get("AYON_SERVER_URL", "unknown")
        hostname = socket.gethostname()
        try:
            host_ip = socket.gethostbyname(hostname)
        except Exception:
            host_ip = "unknown"

        # Get environment info (dev vs prod)
        docker_container = os.environ.get("HOSTNAME", "unknown")  # Docker sets this
        ayon_env = os.environ.get("AYON_ENVIRONMENT", "unknown")
        ayon_addons_dir = os.environ.get("AYON_ADDONS_DIR", "unknown")

        logger.warning("=" * 60)
        logger.warning("🚀 SLACK ADDON INITIALIZING")
        logger.warning("=" * 60)
        logger.warning("📍 SERVER INFO:")
        logger.warning(f"   - Hostname: {hostname}")
        logger.warning(f"   - IP: {host_ip}")
        logger.warning(f"   - Docker Container: {docker_container}")
        logger.warning(f"   - AYON_SERVER_URL: {server_url}")
        logger.warning(f"   - AYON_ENVIRONMENT: {ayon_env}")
        logger.warning(f"   - AYON_ADDONS_DIR: {ayon_addons_dir}")
        logger.warning("=" * 60)

        # Subscribe to activity events using EventStream
        # Based on official Ayon documentation pattern
        try:
            # Subscribe to activity.created event
            # EventStream.subscribe() accepts async handlers directly
            EventStream.subscribe(
                "activity.created",
                self._on_activity_created,
                all_nodes=False,  # Only process on this node
            )

            # Also subscribe to entity.activity.created
            EventStream.subscribe(
                "entity.activity.created",
                self._on_activity_created,
                all_nodes=False,
            )

            # Subscribe to inbox.message events (where comment text actually appears)
            EventStream.subscribe(
                "inbox.message",
                self._on_activity_created,
                all_nodes=False,
            )

            logger.warning("✅ Event handlers registered via EventStream")
            logger.warning("   - Subscribed to: activity.created")
            logger.warning("   - Subscribed to: entity.activity.created")
            logger.warning("   - Subscribed to: inbox.message (comment text)")
        except Exception as e:  # pragma: no cover - defensive logging
            error_msg = f"Could not register event handlers: {e}"
            logger.error(f"❌ {error_msg}", exc_info=True)

        # Note: Routers are registered via get_routers() method (official Ayon pattern)
        # Do NOT import routers here in initialize() - it causes import errors
        logger.warning(
            "ℹ️ Routers will be registered via get_routers() method "
            "when Ayon requests them"
        )

        logger.warning("=" * 60)

    def get_routers(self):
        """Return list of routers to register.

        This method is called by Ayon to get routers for the addon.
        """
        import logging
        logger = logging.getLogger(__name__)

        logger.warning("🔌 get_routers() called - Ayon is requesting API routers")

        try:
            from .routers import mentions, debug
            routers = [mentions.router, debug.router]
            logger.warning(f"✅ Returning {len(routers)} routers:")
            logger.warning(f"   - Router 1: {mentions.router.prefix} (mentions)")
            logger.warning(f"   - Router 2: {debug.router.prefix} (debug)")

            return routers
        except Exception as e:  # pragma: no cover - defensive logging
            error_msg = f"Error getting routers: {e}"
            logger.error(f"❌ {error_msg}", exc_info=True)

            try:
                from .routers import mentions
                logger.warning(
                    "⚠️ Returning mentions router only "
                    "(debug import failed)"
                )
                return [mentions.router]
            except Exception as e2:  # pragma: no cover - defensive logging
                error_msg2 = f"Could not import any routers: {e2}"
                logger.error(f"❌ {error_msg2}", exc_info=True)
                return []

    async def _on_notification_created(self, event) -> None:
        """Handle notification events from Ayon's feed/notification system."""
        import logging
        logger = logging.getLogger(__name__)

        # Handle both EventModel and dict formats
        if isinstance(event, EventModel):
            topic = event.topic
            project = event.project
            user = event.user
            payload = event.payload
            summary = getattr(event, "summary", {})
        else:
            topic = event.get("topic", "")
            project = event.get("project", "")
            user = event.get("user", "")
            payload = event.get("payload") or event.get("data", {})
            summary = event.get("summary") or {}

            # Create wrapper
            class EventWrapper:
                def __init__(self, topic, project, user, payload, summary=None):
                    self.topic = topic
                    self.project = project
                    self.user = user
                    self.payload = payload
                    self.summary = summary or {}

            event = EventWrapper(topic, project, user, payload, summary)

        logger.warning(
            f"🔔 Notification event received: topic={topic}, project={project}"
        )

        # Check if this is a mention notification - look in payload and summary
        payload = event.payload or {}
        summary = getattr(event, "summary", {}) or {}

        # Try to extract mention info from notification
        mentioned_user = (
            payload.get("mentioned_user")
            or payload.get("target_user")
            or summary.get("mentioned_user")
            or payload.get("notification", {}).get("mentioned_user")
            or payload.get("notification", {}).get("target_user")
        )

        notification_type = (
            payload.get("type")
            or summary.get("type")
            or payload.get("notification", {}).get("type")
            or ""
        )

        # Check notification body for @mentions if no explicit mentioned_user
        notification_body = (
            payload.get("body")
            or payload.get("text")
            or payload.get("message")
            or payload.get("description")
            or payload.get("notification", {}).get("body")
            or payload.get("notification", {}).get("text")
            or ""
        )

        # If this notification contains @mentions or is explicitly a mention type, process it
        is_mention_notification = (
            "mention" in notification_type.lower()
            or mentioned_user
            or ("@" in notification_body and "mentioned" in notification_body.lower())
        )

        if is_mention_notification:
            logger.warning(
                f"✅ Found mention notification for user: {mentioned_user}"
            )
            try:
                from .events import handle_notification_event
                await handle_notification_event(self, event, mentioned_user)
            except Exception as e:  # pragma: no cover - defensive logging
                logger.error(
                    f"Error processing notification event: {e}", exc_info=True
                )
                # Fallback: try activity handler
                try:
                    logger.warning("🔄 Falling back to activity handler")
                    from .events import handle_activity_event
                    await handle_activity_event(self, event)
                except Exception as e2:  # pragma: no cover
                    logger.error(f"Fallback also failed: {e2}", exc_info=True)

    async def _on_activity_created(
        self, event: EventModel, *args, **kwargs
    ) -> None:
        """Handle activity created events for mention notifications.

        Based on official Ayon pattern:
        - event: EventModel instance
        - event.summary: brief info structure
        - event.payload: full details
        - event.project: project name
        - event.user: user who created the activity
        """
        import logging
        logger = logging.getLogger(__name__)

        logger.warning("=" * 60)
        logger.warning("🎯 ACTIVITY.CREATED EVENT RECEIVED!")
        logger.warning("=" * 60)
        logger.warning(f"   Topic: {event.topic}")
        logger.warning(f"   Project: {event.project}")
        logger.warning(f"   User: {event.user}")

        # Access summary (brief info)
        summary = event.summary or {}
        logger.warning(
            "   Summary keys: "
            f"{list(summary.keys()) if isinstance(summary, dict) else 'not a dict'}"
        )

        # Access payload (full details)
        payload = event.payload or {}
        logger.warning(
            "   Payload keys: "
            f"{list(payload.keys()) if isinstance(payload, dict) else 'not a dict'}"
        )

        logger.warning("🔍 Processing activity event...")

        try:
            from .events import handle_activity_event
            await handle_activity_event(self, event)
        except Exception as e:  # pragma: no cover - defensive logging
            error_msg = f"Error in activity event handler: {e}"
            logger.error(f"❌ {error_msg}", exc_info=True)
