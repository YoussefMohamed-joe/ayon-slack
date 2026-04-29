"""
Event handlers for Ayon Slack addon.

Listens to Ayon server events (activities/comments) and triggers
mention notifications.
"""
import logging
from typing import Any, Dict

from ayon_server.events import EventModel, EventStream
from ayon_server.lib.postgres import Postgres

logger = logging.getLogger(__name__)

# Event batching to prevent too many concurrent async tasks
_event_semaphore = None

def _get_event_semaphore():
    """Get or create semaphore to limit concurrent event processing."""
    global _event_semaphore
    if _event_semaphore is None:
        import asyncio
        # Limit to 10 concurrent background tasks max
        _event_semaphore = asyncio.Semaphore(10)
    return _event_semaphore


def _log_mention_result(result: Dict[str, Any]) -> None:
    """Log mention notification result - simplified logging."""
    status = result.get("status", "unknown")

    if status == "success":
        sent_count = len(result.get('notifications_sent', []))
        logger.info(f"Sent {sent_count} Slack notification(s).")
    elif status == "disabled":
        logger.info("Mention notifications are disabled in project settings.")
    elif status == "no_mappings":
        logger.error("No user mappings configured in settings.")
    elif result.get("errors"):
        errors = result.get('errors', [])
        error_msg = ', '.join(errors[:2])
        logger.error(f"Error processing mention: {error_msg}")
    else:
        logger.info(f"Mention processing status: {status}")


async def get_entity_info(
    project_name: str,
    entity_type: str,
    entity_id: str
) -> Dict[str, Any]:
    """
    Get entity information from the database.
    
    Returns dict with entity details.
    """
    entity_info = {
        "name": "Unknown",
        "type": entity_type,
        "id": entity_id
    }
    
    try:
        if entity_type == "task":
            query = """
                SELECT name, label FROM project_{project_name}.tasks 
                WHERE id = $1
            """.format(project_name=project_name)
        elif entity_type == "folder":
            query = """
                SELECT name, label FROM project_{project_name}.folders 
                WHERE id = $1
            """.format(project_name=project_name)
        elif entity_type == "version":
            query = """
                SELECT version, id FROM project_{project_name}.versions 
                WHERE id = $1
            """.format(project_name=project_name)
        else:
            return entity_info
        
        async with Postgres.acquire() as conn:
            row = await conn.fetchrow(query, entity_id)
            if row:
                entity_info["name"] = row.get("name") or row.get("label") or str(row.get("version", ""))
            
    except Exception as e:
        logger.warning(f"Could not get entity info: {e}")
    
    return entity_info


async def get_user_name(user_id: str) -> str:
    """Get username from user ID."""
    try:
        # Ayon users table uses 'name' as primary key, not 'id'
        query = "SELECT name FROM public.users WHERE name = $1"
        async with Postgres.acquire() as conn:
            row = await conn.fetchrow(query, user_id)
            if row:
                return row["name"]
    except Exception as e:
        logger.debug(f"Could not get user name: {e}")
    
    # Fallback: just return the user_id as name
    return user_id


def _extract_inbox_body(event, payload, summary) -> str:
    """Extract body text from inbox.message event (pure CPU, no I/O)."""
    body = None
    
    # Try event.description first
    event_description = getattr(event, 'description', None)
    if event_description:
        body = str(event_description).strip()
    
    # Try common event attributes
    if not body:
        for attr in ['data', 'message', 'text', 'body', 'content']:
            val = getattr(event, attr, None)
            if val and isinstance(val, str) and len(val) > 5:
                body = str(val).strip()
                break
    
    # Try dict-like access
    if not body:
        try:
            if hasattr(event, '__getitem__'):
                body = str(event['description']).strip()
        except (KeyError, TypeError, AttributeError):
            pass
    
    # Try payload as string
    if not body and isinstance(payload, str) and payload.strip():
        body = payload.strip()
    
    # Try payload dict keys
    if not body and isinstance(payload, dict):
        for key in ["message", "text", "body", "description", "content", "data"]:
            value = payload.get(key)
            if value:
                if isinstance(value, str):
                    body = value.strip()
                elif isinstance(value, dict) and "body" in value:
                    body = str(value["body"]).strip()
                else:
                    body = str(value).strip()
                if body:
                    break
    
    # Try summary dict keys
    if not body and isinstance(summary, dict):
        for key in ["message", "text", "body", "description", "content", "data"]:
            value = summary.get(key)
            if value:
                if isinstance(value, str):
                    body = value.strip()
                elif isinstance(value, dict) and "body" in value:
                    body = str(value["body"]).strip()
                else:
                    body = str(value).strip()
                if body:
                    break
    
    return str(body).strip() if body else ""


async def handle_activity_event(
    addon: "SlackAddon",
    event: EventModel
) -> None:
    """
    Handle activity created events.
    
    This is called when a new activity (comment) is created in Ayon.
    We check for @mentions and send Slack notifications.
    """
    from .mention_handler import process_mention_event
    
    try:
        
        # Note: EventStream.dispatch is async and would need to be awaited
        # Logger handles the logging instead
        
        # Get event payload and summary
        payload = event.payload or {}
        summary = getattr(event, 'summary', {}) or {}
        
        # Special handling for inbox.message events
        if event.topic == "inbox.message":
            logger.info(f"📬 inbox.message event received for project: {event.project}")
            
            # Extract body from event (pure CPU, no I/O)
            body = _extract_inbox_body(event, payload, summary)
            
            if not body:
                return
            
            # Check for mentions
            if "@" not in body and "(user:" not in body:
                return
            
            project_name = event.project or payload.get("project") or payload.get("project_name", "")
            if not project_name:
                logger.warning("⚠️ No project in inbox.message event, skipping")
                return
            
            # Capture minimal data (instant, no I/O)
            import asyncio
            import os
            
            raw_event_data = {
                "project_name": project_name,
                "author_user_id": event.user or payload.get("user") or payload.get("author", ""),
                "body": body,
                "entity_type": payload.get("entity_type", "task"),
                "entity_id": payload.get("entity_id", ""),
                "activity_id": payload.get("activity_id"),
            }
            
            logger.info(f"🚀 Queuing inbox mention (non-blocking) for project: {project_name}")
            
            # Fire-and-forget: ALL slow work in background
            async def _process_in_background():
                sem = _get_event_semaphore()
                async with sem:
                    try:
                        from .settings_utils import get_enabled_settings, get_mention_plugin_settings
                        
                        # CHECK ENABLED FIRST - if disabled, do absolutely nothing
                        settings = await get_enabled_settings(addon, raw_event_data["project_name"])
                        if not settings:
                            return  # Disabled or no settings - zero work done
                        
                        plugin_settings = get_mention_plugin_settings(settings)
                        if not plugin_settings:
                            return  # No plugin settings - zero work done
                        
                        # Only now do we do any real work (settings are enabled)
                        settings_dict = {
                            "mention_notifications": {
                                "MentionNotifications": plugin_settings
                            }
                        }
                        
                        ayon_server_url = plugin_settings.get("ayon_server_url", "") or os.environ.get("AYON_SERVER_URL", "http://localhost:5000")
                        
                        # Get user name (slow - DB query, only if enabled)
                        author_name = await get_user_name(raw_event_data["author_user_id"])
                        
                        event_data = {
                            "author_name": author_name,
                            "body": raw_event_data["body"],
                            "entity_type": raw_event_data["entity_type"],
                            "entity_id": raw_event_data["entity_id"],
                            "entity_name": "Unknown",
                            "project_name": raw_event_data["project_name"],
                            "activity_id": raw_event_data["activity_id"],
                        }
                        
                        result = await process_mention_event(event_data, settings_dict, ayon_server_url)
                        _log_mention_result(result)
                    except Exception as e:
                        logger.error(f"Error in inbox.message background processing: {e}", exc_info=True)
            
            # Returns IMMEDIATELY - zero blocking
            asyncio.create_task(_process_in_background())
            return
        

        
        # Activity events have this structure
        project_name = event.project or payload.get("project_name", "")
        if not project_name:
            logger.warning("⚠️ No project name in event, skipping")
            logger.warning(f"   Event details: topic={event.topic}, payload keys={list(payload.keys()) if isinstance(payload, dict) else type(payload)}")
            logger.warning(f"   Full payload: {payload}")
            return
        
        # Get activity details - check summary first (where Ayon puts it), then payload
        activity_type = summary.get("activity_type") or payload.get("activity_type") or payload.get("type") or payload.get("activityType", "")
        
        # Try multiple ways to get the comment body text
        # CRITICAL: Logs show body is in payload['body'] - extract it directly!
        body = ""
        
        # First priority: payload.body (logs confirm this is where it is!)
        if isinstance(payload, dict) and "body" in payload:
            body_value = payload.get("body")
            if body_value is not None:
                body = str(body_value).strip()
                if body:
                    logger.debug(f"Extracted body from payload['body']: {body[:50]}...")
        
        # Second priority: summary.body
        if not body and isinstance(summary, dict) and "body" in summary:
            body_value = summary.get("body")
            if body_value is not None:
                body = str(body_value).strip()
        
        # Third priority: other payload keys
        if not body and isinstance(payload, dict):
            for key in ["text", "comment", "message", "description"]:
                body_value = payload.get(key)
                if body_value:
                    body = str(body_value).strip()
                    break
        
        # Fourth priority: other summary keys
        if not body and isinstance(summary, dict):
            for key in ["text", "comment", "message"]:
                body_value = summary.get(key)
                if body_value:
                    body = str(body_value).strip()
                    break
        
        # Fifth priority: nested data structures
        if not body and isinstance(payload, dict):
            for key in ["data", "activity", "comment_data"]:
                if key in payload and isinstance(payload[key], dict):
                    body_value = payload[key].get("body") or payload[key].get("text")
                    if body_value:
                        body = str(body_value).strip()
                        break
        
        # If body is still empty, skip
        if not body:
            pass
        
        # Get entity info early to check if it's publish-related
        # Check summary.references first (Ayon structure), then payload
        entity_type = ""
        entity_id = ""
        
        # Try to get from summary.references (Ayon's structure)
        references = summary.get("references", [])
        if references:
            # Get the first reference with entity_type and entity_id
            for ref in references:
                if ref.get("entity_type") and ref.get("entity_id"):
                    entity_type = ref.get("entity_type", "")
                    entity_id = ref.get("entity_id", "")
                    break
        
        # Fallback to payload if not found in summary
        if not entity_type:
            entity_type = payload.get("entity_type", payload.get("reference_type", ""))
        if not entity_id:
            entity_id = payload.get("entity_id", payload.get("reference_id", ""))
        
        # Skip publish-related activities to avoid duplicate messages
        # BUT: If there's a @ mention in the body, process it anyway (user wants to notify)
        skip_entity_types = ["version", "representation", "workfile", "product"]
        skip_activity_types = ["publish", "version", "representation", "workfile", "product"]
        
        # Only skip if NO @ mention in body (if there's a mention, process it regardless)
        # Check for both @ and Ayon markdown format (user:username)
        has_mention = body and ("@" in body or "(user:" in body)
        
        if activity_type.lower() in [t.lower() for t in skip_activity_types] and not has_mention:
            logger.debug(f"⏭️ Activity type '{activity_type}' is publish-related and no @ mention, skipping")
            return
        
        if entity_type and entity_type.lower() in [t.lower() for t in skip_entity_types] and not has_mention:
            logger.debug(f"⏭️ Entity type '{entity_type}' is publish-related and no @ mention, skipping")
            return
        
        # If it's a publish entity BUT has @ mention, log that we're processing it anyway
        if entity_type and entity_type.lower() in [t.lower() for t in skip_entity_types] and has_mention:
            logger.info(f"ℹ️ Entity type '{entity_type}' is publish-related, but @ mention found - processing anyway")
        
        # Process activities that can contain mentions
        # Include: comment, note, review, watch, and empty (which might be comments)
        # Also allow None or any type if body contains @ or (user:
        allowed_activity_types = ["comment", "note", "review", "watch", "", None]
        body_has_mention = "@" in (body or "") or "(user:" in (body or "")
        if activity_type not in allowed_activity_types and not body_has_mention:
            # Only skip if activity_type is not allowed AND there's no @ or (user: in body
            # This way, if someone mentions with @, we'll process it even if activity_type is unexpected
            logger.debug(f"⏭️ Activity type '{activity_type}' not in allowed list, but checking if @ mention exists...")
            # Don't return yet - let it check for @ mentions below
        
        # Skip if no body text
        if not body:
            logger.debug("No body text in activity, skipping")
            # Try one more time - maybe the body is in a different format
            # Check if payload itself is a string (some Ayon versions might do this)
            if isinstance(payload, str):
                body = payload
            elif isinstance(payload, dict) and len(payload) == 1:
                # Sometimes payload is wrapped in a single key
                body = list(payload.values())[0] if payload else ""
            if not body:
                return
        
        # Check for @ mentions in body (both @ and Ayon markdown format)
        if "@" not in body and "(user:" not in body:
            return  # Exit immediately
        
        # Fire-and-forget: Create background task with ZERO blocking
        import asyncio
        import os
        
        # Capture minimal data (instant operation)
        raw_event_data = {
            "project_name": project_name,
            "author_user_id": event.user or payload.get("author", ""),
            "body": body,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "activity_type": activity_type,
            "activity_id": summary.get("activity_id") or payload.get("activity_id"),
        }
        
        async def _process_in_background():
            """Process mention in background with concurrency control."""
            # Use semaphore to limit concurrent tasks (prevents overload)
            sem = _get_event_semaphore()
            async with sem:
                try:
                    from .settings_utils import get_enabled_settings, get_mention_plugin_settings
                    
                    # CHECK ENABLED FIRST - if disabled, do absolutely nothing
                    settings = await get_enabled_settings(addon, raw_event_data["project_name"])
                    if not settings:
                        return  # Disabled or no settings - zero work done
                    
                    plugin_settings = get_mention_plugin_settings(settings)
                    if not plugin_settings:
                        return  # No plugin settings - zero work done
                    
                    # Only now do we do any real work (settings are enabled)
                    settings_dict = {
                        "mention_notifications": {
                            "MentionNotifications": plugin_settings
                        }
                    }
                    
                    ayon_server_url = plugin_settings.get("ayon_server_url", "") or os.environ.get("AYON_SERVER_URL", "http://localhost:5000")
                    
                    # Get user name (slow - DB query, only if enabled)
                    author_name = await get_user_name(raw_event_data["author_user_id"])
                    
                    # Get entity info (slow - DB query, only if enabled)
                    entity_name = "Unknown"
                    if raw_event_data["entity_type"] and raw_event_data["entity_id"]:
                        entity_info = await get_entity_info(
                            raw_event_data["project_name"],
                            raw_event_data["entity_type"],
                            raw_event_data["entity_id"]
                        )
                        entity_name = entity_info.get("name", "Unknown")
                    
                    # Build event data
                    event_data = {
                        "author_name": author_name,
                        "body": raw_event_data["body"],
                        "entity_type": raw_event_data["entity_type"],
                        "entity_id": raw_event_data["entity_id"],
                        "entity_name": entity_name,
                        "project_name": raw_event_data["project_name"],
                        "activity_id": raw_event_data["activity_id"]
                    }
                    
                    # Process mention (slow - Slack API)
                    result = await process_mention_event(
                        event_data=event_data,
                        settings=settings_dict,
                        ayon_server_url=ayon_server_url
                    )
                    
                    # Log result
                    _log_mention_result(result)
                        
                except Exception as e:
                    logger.error(f"Error in background mention processing: {e}", exc_info=True)
        
        # Create background task - returns IMMEDIATELY
        asyncio.create_task(_process_in_background())
        
    except Exception as e:
        logger.error(f"Error handling activity event: {e}", exc_info=True)
