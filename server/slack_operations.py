"""
Server-side Slack operations for mention notifications.

Mirrors the client-side SlackOperations used in the publish workflow,
so mention notifications behave the same way.
"""
import logging
import re
import time
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)


class SlackOperations:
    """Server-side Slack operations using slack_sdk."""

    def __init__(self, token: str):
        self.token = token
        self.client = None
        try:
            from slack_sdk import WebClient  # type: ignore[import]

            self.client = WebClient(token=token)
        except ImportError:  # pragma: no cover - optional dependency
            logger.warning(
                "slack_sdk not available. Will use direct HTTP POST instead."
            )

    def _get_users_list(self):
        """Get users list from Slack API."""
        return self.client.users_list()

    def _get_usergroups_list(self):
        """Get usergroups list from Slack API."""
        return self.client.usergroups_list()

    def get_users_and_groups(self) -> tuple[List[Dict], List[Dict]]:
        """Get all users and groups from Slack workspace."""
        if not self.client:
            logger.warning(
                "slack_sdk not available, cannot get users/groups. "
                "Returning empty lists."
            )
            return [], []

        try:
            from slack_sdk.errors import SlackApiError  # type: ignore[import]

            while True:
                try:
                    users = self._get_users()
                    groups = self._get_groups()
                    logger.info(
                        "✅ Retrieved %d users and %d groups from Slack",
                        len(users),
                        len(groups),
                    )
                    return users, groups
                except SlackApiError as e:  # pragma: no cover - rate limiting
                    retry_after = e.response.headers.get("Retry-After")
                    if retry_after:
                        logger.warning(
                            "Rate limit hit, sleeping for %s seconds", retry_after
                        )
                        time.sleep(int(retry_after))
                    else:
                        logger.warning(
                            "Cannot pull user info, mentions won't work",
                            exc_info=True,
                        )
                        return [], []
                except Exception:  # pragma: no cover - defensive logging
                    logger.warning(
                        "Cannot pull user info, mentions won't work",
                        exc_info=True,
                    )
                    return [], []
        except Exception as e:  # pragma: no cover - defensive logging
            logger.warning("Cannot get users/groups: %s", e, exc_info=True)
            return [], []

    def _get_users(self) -> List[Dict]:
        """Parse users.list response into list of users (dicts)."""
        first = True
        next_page = None
        users: List[Dict] = []
        while first or next_page:
            response = self._get_users_list()
            first = False
            next_page = response.get("response_metadata", {}).get("next_cursor")
            for user in response.get("members", []):
                users.append(user)

        return users

    def _get_groups(self) -> List[Dict]:
        """Parse usergroups.list response into list of groups (dicts)."""
        response = self._get_usergroups_list()
        groups: List[Dict] = []
        for group in response.get("usergroups", []):
            groups.append(group)
        return groups

    def _get_user_id(self, users: List[Dict], user_name: str) -> Optional[str]:
        """Return internal Slack id for a user name."""
        user_name_lower = user_name.lower()
        for user in users:
            if user.get("deleted"):
                continue
            user_profile = user.get("profile", {})
            if user_name_lower in (
                user.get("name", "").lower(),
                user_profile.get("display_name", "").lower(),
                user_profile.get("real_name", "").lower(),
            ):
                return user.get("id")
        return None

    def _get_group_id(self, groups: List[Dict], group_name: str) -> Optional[str]:
        """Return internal group id for string name."""
        for group in groups:
            if not group.get("date_delete") and group_name.lower() in (
                group.get("name", "").lower(),
                group.get("handle", "").lower(),
            ):
                return group.get("id")
        return None

    def translate_users(
        self, message: str, users: List[Dict], groups: List[Dict]
    ) -> str:
        """Replace @mentions with proper <@SLACK_ID> format."""
        matches = re.findall(r"(?<!<)@\S+", message)
        in_quotes = re.findall(r"(?<!<)(['\"])(@[^'\"]+)", message)
        for item in in_quotes:
            matches.append(item[1])
        if not matches:
            return message

        for orig_user in matches:
            user_name = orig_user.replace("@", "")
            slack_id = self._get_user_id(users, user_name)
            mention = None
            if slack_id:
                mention = f"<@{slack_id}>"
            else:
                slack_id = self._get_group_id(groups, user_name)
                if slack_id:
                    mention = f"<!subteam^{slack_id}>"
            if mention:
                message = message.replace(orig_user, mention)

        return message

    def send_message(
        self,
        channel: str,
        message: str,
        blocks: Optional[List[Dict]] = None,
    ) -> bool:
        """
        Send a message to a Slack channel or user.

        Uses slack_sdk.WebClient.chat_postMessage() if available,
        and falls back to direct HTTP POST otherwise.
        """
        if self.client:
            try:
                logger.info(
                    "📤 Sending to Slack API: channel=%s, token=%s...",
                    channel,
                    self.token[:15],
                )
                response = self.client.chat_postMessage(
                    channel=channel,
                    text=message,
                    blocks=blocks or None,
                )
                if response.get("ok"):
                    logger.info("✅ Sent Slack message to %s", channel)
                    return True
                error = response.get("error", "unknown")
                logger.warning(
                    "Error happened: %s", self._enrich_error(str(error), channel)
                )
                return False
            except Exception as e:  # pragma: no cover - defensive logging
                logger.warning(
                    "Error sending Slack message: %s",
                    self._enrich_error(str(e), channel),
                    exc_info=True,
                )
                return False

        # Fallback: direct HTTP POST
        try:
            import asyncio
            import aiohttp  # type: ignore[import]

            async def _send_direct() -> bool:
                headers = {
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                }
                payload: Dict[str, object] = {
                    "channel": channel,
                    "text": message,
                }
                if blocks:
                    payload["blocks"] = blocks

                logger.info(
                    "📤 Sending to Slack API (HTTP fallback): channel=%s", channel
                )

                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        "https://slack.com/api/chat.postMessage",
                        headers=headers,
                        json=payload,
                    ) as resp:
                        result = await resp.json()
                        if result.get("ok"):
                            logger.info(
                                "✅ Sent Slack message to %s (via HTTP fallback)",
                                channel,
                            )
                            return True
                        error = result.get("error", "unknown")
                        logger.error("❌ Slack API error: %s", error)
                        return False

            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():  # pragma: no cover - unlikely in server
                    import concurrent.futures

                    with concurrent.futures.ThreadPoolExecutor() as executor:
                        future = executor.submit(asyncio.run, _send_direct())
                        return future.result(timeout=10)
                return loop.run_until_complete(_send_direct())
            except RuntimeError:
                return asyncio.run(_send_direct())
        except Exception as e:  # pragma: no cover - defensive logging
            logger.error(
                "❌ Error sending Slack message (HTTP fallback): %s",
                e,
                exc_info=True,
            )
            return False

    def _enrich_error(self, error_str: str, channel: str) -> str:
        """Enhance known errors with more helpful notes."""
        if "not_in_channel" in error_str:
            error_str += (
                f" - application must be added to channel '{channel}'. "
                "Ask Slack admin."
            )
        return error_str

