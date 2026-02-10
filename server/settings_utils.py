"""Utility functions for handling Slack addon settings."""
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


def get_mention_plugin_settings(settings: Any) -> Optional[Dict[str, Any]]:
    """
    Extract mention notification settings from addon settings object.
    
    Handles both Pydantic models and dict formats.
    
    Args:
        settings: Addon settings (Pydantic model or dict)
        
    Returns:
        Plugin settings dict or None if not found
    """
    # Early return if no settings
    if not settings:
        return None
    
    # Try to get enabled value directly first
    enabled = _get_enabled_value(settings)
    
    # Convert to dict
    settings_dict = _convert_to_dict(settings, enabled)
    
    # Extract plugin settings
    mention_settings = settings_dict.get("mention_notifications", {})
    return mention_settings.get("MentionNotifications", {})


def _get_enabled_value(settings: Any) -> bool:
    """Extract enabled value directly from settings object."""
    try:
        if not hasattr(settings, 'mention_notifications'):
            return False
        
        mention_notif = settings.mention_notifications
        if not hasattr(mention_notif, 'MentionNotifications'):
            return False
        
        plugin = mention_notif.MentionNotifications
        return getattr(plugin, 'enabled', False)
    except Exception:
        return False


def _convert_to_dict(settings: Any, enabled: bool) -> Dict[str, Any]:
    """Convert settings to dict format."""
    # Already a dict
    if isinstance(settings, dict):
        return settings
    
    # Try standard Pydantic methods
    if hasattr(settings, 'model_dump'):
        return settings.model_dump()
    
    if hasattr(settings, 'dict'):
        return settings.dict()
    
    # Fallback: manual extraction
    return _extract_settings_manually(settings, enabled)


def _extract_settings_manually(settings: Any, enabled: bool) -> Dict[str, Any]:
    """Manually extract settings from object attributes."""
    try:
        mention_notif = settings.mention_notifications
        plugin = mention_notif.MentionNotifications
        
        return {
            "token": getattr(settings, 'token', ''),
            "mention_notifications": {
                "MentionNotifications": {
                    "enabled": enabled,
                    "token": getattr(plugin, 'token', ''),
                    "ayon_server_url": getattr(plugin, 'ayon_server_url', ''),
                    "user_mappings": getattr(plugin, 'user_mappings', []),
                    "notify_self_mentions": getattr(plugin, 'notify_self_mentions', False),
                }
            }
        }
    except Exception:
        # Ultimate fallback
        return {
            "token": getattr(settings, 'token', ''),
            "mention_notifications": {
                "MentionNotifications": {
                    "enabled": enabled,
                    "token": '',
                    "ayon_server_url": '',
                    "user_mappings": [],
                    "notify_self_mentions": False,
                }
            }
        }


async def get_enabled_settings(addon: Any, project_name: str) -> Optional[Any]:
    """
    Get settings with enabled mention notifications.
    
    Tries project settings first, falls back to studio settings ONLY if 
    project settings don't exist (not if they're explicitly disabled).
    
    Args:
        addon: Slack addon instance
        project_name: Project name
        
    Returns:
        Settings object with enabled=True, or None
    """
    # Try project settings
    try:
        settings = await addon.get_project_settings(project_name)
        if settings:
            # Project settings exist - check if mention notifications are configured
            if _has_mention_config(settings):
                # Project has mention config - respect its enabled value (True or False)
                # Do NOT fall back to studio if user explicitly disabled it here
                if _is_enabled(settings):
                    return settings
                else:
                    # Explicitly disabled in project - respect that, don't fall back
                    return None
            # Project settings exist but no mention config - fall through to studio
    except Exception as e:
        logger.debug(f"Error getting project settings: {e}")
    
    # Fallback to studio settings (only if project has no mention config)
    try:
        settings = await addon.get_studio_settings()
        if settings and _is_enabled(settings):
            return settings
    except Exception as e:
        logger.debug(f"Error getting studio settings: {e}")
    
    return None


def _has_mention_config(settings: Any) -> bool:
    """Check if settings have mention_notifications configured.

    Supports both Pydantic models and plain dict structures.
    """
    try:
        # Dict-style settings
        if isinstance(settings, dict):
            mention_notif = settings.get("mention_notifications") or {}
            return "MentionNotifications" in mention_notif

        # Object / Pydantic-style settings
        if not hasattr(settings, "mention_notifications"):
            return False
        mention_notif = settings.mention_notifications
        if not hasattr(mention_notif, "MentionNotifications"):
            return False
        return True
    except Exception:
        return False


def _is_enabled(settings: Any) -> bool:
    """Check if mention notifications are enabled in settings.

    Supports both Pydantic models and plain dict structures.
    """
    try:
        # Dict-style settings
        if isinstance(settings, dict):
            mention_notif = settings.get("mention_notifications") or {}
            plugin = mention_notif.get("MentionNotifications") or {}
            return bool(plugin.get("enabled", False))

        # Object / Pydantic-style settings
        mention_notif = settings.mention_notifications
        plugin = mention_notif.MentionNotifications
        return getattr(plugin, "enabled", False)
    except Exception:
        return False


def build_user_mappings(user_mappings_list: list) -> Dict[str, str]:
    """
    Build user mappings dict from settings list.
    
    Args:
        user_mappings_list: List of user mapping dicts
        
    Returns:
        Dict mapping lowercase ayon_user to slack_user_id
    """
    mappings = {}
    
    for mapping in user_mappings_list:
        if not isinstance(mapping, dict):
            continue
        
        ayon_user = mapping.get("ayon_user", "").strip()
        slack_id = mapping.get("slack_user_id", "").strip()
        
        if ayon_user and slack_id:
            mappings[ayon_user.lower()] = slack_id
    
    return mappings
