"""
Mention notification handler for Ayon Slack addon.

Listens for activity events (comments) and sends Slack DMs
when users are @mentioned.
"""
import logging
import re
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)


def extract_mentions(text: str) -> List[str]:
    """Extract @mentioned usernames from text."""
    if not text:
        return []

    mentions: List[str] = []

    # Ayon markdown format: [name](user:username)
    markdown_pattern = r"\[([^\]]+)\]\(user:([^\)]+)\)"
    markdown_matches = re.findall(markdown_pattern, text)
    for display_name, username in markdown_matches:
        mentions.append(username.strip())
        logger.debug("Found markdown mention: '%s' → '%s'", display_name, username)

    # Traditional @username / @firstname lastname
    pattern = r"@(\w+(?:\s+\w+)*)"
    at_matches = re.findall(pattern, text)
    for match in at_matches:
        username = match.strip()
        if username and username not in mentions:
            mentions.append(username)

    # Remove duplicates while preserving order
    seen: set[str] = set()
    unique_mentions: List[str] = []
    for mention in mentions:
        if mention.lower() not in seen:
            seen.add(mention.lower())
            unique_mentions.append(mention)

    logger.info("Extracted %d mention(s): %s", len(unique_mentions), unique_mentions)
    return unique_mentions


def build_ayon_url(
    ayon_server_url: str,
    project_name: str,
    entity_type: str,
    entity_id: str,
    activity_id: Optional[str] = None,
) -> str:
    """Build a URL to the Ayon web interface for a specific entity.
    
    Uses Ayon's actual URL format: /projects/{project}/overview?project={project}&type={type}&id={id}
    """
    base_url = ayon_server_url.rstrip("/")

    # Ayon uses /projects/{project}/overview with query params for most entities
    if entity_type in ("task", "folder", "version", "product", "workfile", "representation"):
        url = f"{base_url}/projects/{project_name}/overview?project={project_name}&type={entity_type}&id={entity_id}"
    else:
        # Fallback for unknown entity types
        url = f"{base_url}/projects/{project_name}/overview?project={project_name}&type={entity_type}&id={entity_id}"

    # Add activity anchor if provided
    if activity_id:
        url += f"&activity={activity_id}"

    return url


async def process_mention_event(
    event_data: Dict[str, Any],
    settings: Dict[str, Any],
    ayon_server_url: str,
) -> Dict[str, Any]:
    """
    Process an activity/comment event and send notifications for mentions.

    Uses the same Slack API approach as the publish workflow for sending messages:
    - PRIMARY: Uses user mappings to find Slack user IDs (since Ayon names may differ from Slack)
    - FALLBACK: Uses Slack API lookup if user not found in mappings
    - Sends messages via SlackOperations (same as publish)
    """
    from .slack_operations import SlackOperations

    results: Dict[str, Any] = {
        "status": "processed",
        "mentions_found": [],
        "notifications_sent": [],
        "errors": [],
    }

    mention_settings = settings.get("mention_notifications", {})
    plugin_settings = mention_settings.get("MentionNotifications", {})

    logger.warning("🔍 Mention settings check:")
    logger.warning("   - Settings type: %s", type(settings))
    logger.warning("   - Mention settings: %s", mention_settings)
    logger.warning("   - Plugin settings: %s", plugin_settings)
    logger.warning(
        "   - Enabled value: %s", plugin_settings.get("enabled", "NOT FOUND")
    )

    # Get token from mention notifications settings (separate from publish token)
    token = plugin_settings.get("token", "")
    if not token:
        logger.warning("⚠️ No Slack token in mention notification settings")
        results["status"] = "no_token"
        results["errors"].append(
            "Slack token not configured for mention notifications. "
            "Set it in Project Settings → Slack → Mention Notifications → Auth Token"
        )
        return results

    logger.info("✅ Token found: length=%s", len(token))

    # Build user mappings using helper function
    from .settings_utils import build_user_mappings
    
    user_mappings_list = plugin_settings.get("user_mappings", [])
    user_mappings_dict = build_user_mappings(user_mappings_list)
    
    logger.info("✅ User mappings: %s mappings", len(user_mappings_dict))

    author_name = event_data.get("author_name", "Someone")
    body = event_data.get("body", "")
    entity_type = event_data.get("entity_type", "entity")
    entity_id = event_data.get("entity_id", "")
    entity_name = event_data.get("entity_name", "Unknown")
    project_name = event_data.get("project_name", "")
    activity_id = event_data.get("activity_id")

    logger.warning("🔍 Extracting mentions from body: %s", body[:100])
    mentions = extract_mentions(body)
    results["mentions_found"] = mentions
    logger.warning("🔍 Found %d mention(s): %s", len(mentions), mentions)

    if not mentions:
        logger.warning("⚠️ No mentions found in body, returning")
        results["status"] = "no_mentions"
        return results

    ayon_url = build_ayon_url(
        ayon_server_url,
        project_name,
        entity_type,
        entity_id,
        activity_id,
    )

    notify_self_mentions = plugin_settings.get("notify_self_mentions", False)

    slack_ops = SlackOperations(token)
    logger.info("✅ SlackOperations initialized")

    users, groups = slack_ops.get_users_and_groups()
    if users:
        logger.info(
            "✅ Retrieved %d users and %d groups from Slack", len(users), len(groups)
        )

    for mentioned_display_name in mentions:
        logger.warning("🔍 Processing mention: '%s'", mentioned_display_name)

        mentioned_username = mentioned_display_name

        if mentioned_username.lower() == author_name.lower():
            if not notify_self_mentions:
                logger.info(
                    "⏭️ Self-mention detected, skipping "
                    "(notify_self_mentions=False)"
                )
                continue

        slack_user_id: Optional[str] = None

        if user_mappings_dict:
            username_lower = mentioned_username.lower()
            if username_lower in user_mappings_dict:
                slack_user_id = user_mappings_dict[username_lower]
                logger.info(
                    "✅ Found in mappings: %s → %s",
                    mentioned_username,
                    slack_user_id,
                )
            else:
                for ayon_user, slack_id in user_mappings_dict.items():
                    if username_lower in ayon_user or ayon_user in username_lower:
                        slack_user_id = slack_id
                        logger.info(
                            "✅ Found partial match: %s → %s",
                            mentioned_username,
                            slack_id,
                        )
                        break

        if not slack_user_id and users:
            slack_user_id = slack_ops._get_user_id(users, mentioned_username)
            if not slack_user_id:
                slack_user_id = slack_ops._get_user_id(
                    users, mentioned_display_name
                )

        if not slack_user_id:
            logger.warning(
                "⚠️ No Slack user ID found for '%s', skipping",
                mentioned_username,
            )
            results["errors"].append(
                f"Could not find Slack user ID for '{mentioned_username}'"
            )
            continue

        logger.info(
            "✅ Found Slack ID: %s for user '%s'",
            slack_user_id,
            mentioned_username,
        )

        mentioner_slack_id: Optional[str] = None
        if user_mappings_dict:
            author_lower = author_name.lower()
            if author_lower in user_mappings_dict:
                mentioner_slack_id = user_mappings_dict[author_lower]
            else:
                for ayon_user, slack_id in user_mappings_dict.items():
                    if author_lower in ayon_user or ayon_user in author_lower:
                        mentioner_slack_id = slack_id
                        break
        if not mentioner_slack_id and users:
            mentioner_slack_id = slack_ops._get_user_id(users, author_name)
        mentioner_display = (
            f"<@{mentioner_slack_id}>" if mentioner_slack_id else author_name
        )

        clean_body = body
        clean_body = re.sub(
            r"\[([^\]]+)\]\(user:[^\)]+\)", r"@\1", clean_body
        )

        # Message format - styled like the original but without Location
        message_text = (
            f":bell: {mentioner_display} mentioned you in *{project_name}*"
        )

        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f":bell: {mentioner_display} mentioned you in *{project_name}*",
                },
            },
            {
                "type": "divider",
            },
            {
                "type": "section",
                "fields": [
                    {
                        "type": "mrkdwn",
                        "text": f"*Project:*\n{project_name}",
                    },
                ],
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Comment:*\n>{clean_body[:500]}",
                },
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": ":round_pushpin: Open in Ayon",
                        },
                        "url": ayon_url,
                        "style": "primary",
                    }
                ],
            },
        ]

        logger.info(
            "📨 Sending Slack message to %s (user: %s)...",
            slack_user_id,
            mentioned_username,
        )

        try:
            success = slack_ops.send_message(
                slack_user_id, message_text, blocks
            )
        except Exception as e:  # pragma: no cover - defensive logging
            logger.error("❌ Error sending Slack message: %s", e, exc_info=True)
            success = False

        if success:
            logger.info(
                "✅ Slack message sent to %s (%s)",
                mentioned_username,
                slack_user_id,
            )
            results["notifications_sent"].append(
                {
                    "ayon_user": mentioned_username,
                    "mentioned_display_name": mentioned_display_name,
                    "slack_user_id": slack_user_id,
                }
            )
        else:
            error_msg = (
                f"Failed to send message to {mentioned_username} "
                f"({slack_user_id})"
            )
            logger.warning(error_msg)
            results["errors"].append(error_msg)

    if results["notifications_sent"]:
        results["status"] = "success"
    elif results["errors"]:
        results["status"] = "partial_failure"

    return results

