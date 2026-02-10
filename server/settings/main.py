from ayon_server.settings import SettingsField, BaseSettingsModel

from .publish_plugins import SlackPublishPlugins

# Mention notifications settings added


class UserMapping(BaseSettingsModel):
    """Mapping between Ayon user and Slack user ID."""
    _layout = "compact"

    ayon_user: str = SettingsField(
        "",
        title="Ayon User"
    )
    slack_user_id: str = SettingsField(
        "",
        title="Slack User ID"
    )


class MentionNotificationsPlugin(BaseSettingsModel):
    """Plugin settings for mention notifications.

    Uses the same Slack API approach as the publish workflow for sending messages:
    - PRIMARY: Uses user mappings to find Slack user IDs (since Ayon names may differ from Slack)
    - FALLBACK: Uses Slack API lookup if user not found in mappings
    - Sends messages via Slack API (same as publish workflow)
    """
    _isGroup = True

    enabled: bool = SettingsField(
        default=True,
        title="Enabled",
        description="Enable Slack notifications when users are @mentioned in comments. Uses the same Slack API approach as publish workflow."
    )
    token: str = SettingsField(
        "",
        title="Auth Token",
        description="Slack Bot OAuth Token for mention notifications (xoxb-...). This is separate from the publish token. Get it from api.slack.com → Your Apps → OAuth & Permissions"
    )
    ayon_server_url: str = SettingsField(
        "",
        title="Ayon Server URL",
        description="URL of your Ayon server (e.g., https://ayon.yourcompany.com) for clickable links"
    )
    user_mappings: list[UserMapping] = SettingsField(
        default_factory=list,
        title="User Mappings",
        description="Map Ayon usernames to Slack user IDs (PRIMARY method). This is the primary way to find Slack users since Ayon usernames may differ from Slack names. The system will use these mappings first, then fall back to Slack API lookup if not found. To auto-populate from CSV: Open csv_mapper_tool.html from the addon folder, upload your Slack CSV, and click 'Apply Mappings to Ayon'. Or add mappings manually. Get Slack IDs from user profile → More → Copy member ID"
    )
    notify_self_mentions: bool = SettingsField(
        default=False,
        title="Allow Self Mentions",
        description="Allow sending notifications when you mention yourself (useful for testing)"
    )
    test_slack_message: bool = SettingsField(
        default=False,
        title="🧪 Test Slack Message (Click to Send)",
        description="Toggle this to send a test message to U0A67LHQXLP. It will automatically reset after sending. This helps debug the Slack integration."
    )


class MentionNotificationsContainer(BaseSettingsModel):
    """Container for mention notification plugins."""
    MentionNotifications: MentionNotificationsPlugin = SettingsField(
        title="Mention Notifications",
        default_factory=MentionNotificationsPlugin,
    )


class SlackSettings(BaseSettingsModel):
    """Slack project settings."""
    enabled: bool = SettingsField(default=True)
    token: str = SettingsField("", title="Auth Token")

    publish: SlackPublishPlugins = SettingsField(
        title="Publish plugins",
        description="Fill combination of families, task names and hosts "
                    "when to send notification",
    )

    mention_notifications: MentionNotificationsContainer = SettingsField(
        title="Mention notifications",
        description="Send Slack notifications when users are mentioned in Ayon. Uses the same Slack API approach as publish workflow.",
        default_factory=MentionNotificationsContainer,
    )


DEFAULT_SLACK_SETTING = {
    "token": "",
    "publish": {
        "CollectSlackFamilies": {
            "enabled": True,
            "optional": True,
            "profiles": [
                {
                    "families": [],
                    "hosts": [],
                    "task_types": [],
                    "task_names": [],
                    "tasks": [],
                    "subsets": [],
                    "product_types": [],
                    "product_names": [],
                    "review_upload_limit": 50.0,
                    "channel_messages": []
                }
            ]
        }
    },
    "mention_notifications": {
        "MentionNotifications": {
            "enabled": True,
            "token": "",
            "ayon_server_url": "",
            "user_mappings": [],
            "notify_self_mentions": False,
            "test_slack_message": False
        }
    }
}
