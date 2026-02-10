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

# EventStream is already imported above


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
        
        row = await Postgres.fetch_one(query, entity_id)
        if row:
            entity_info["name"] = row.get("name") or row.get("label") or str(row.get("version", ""))
            
    except Exception as e:
        logger.warning(f"Could not get entity info: {e}")
    
    return entity_info


async def get_user_name(user_id: str) -> str:
    """Get username from user ID."""
    try:
        query = "SELECT name FROM users WHERE id = $1 OR name = $1"
        row = await Postgres.fetch_one(query, user_id)
        if row:
            return row["name"]
    except Exception as e:
        logger.warning(f"Could not get user name: {e}")
    
    return user_id


async def handle_notification_event(
    addon: "SlackAddon",
    event: EventModel,
    mentioned_user: str = None
) -> None:
    """
    Handle notification events from Ayon's feed/notification system.

    This hooks into the same system that sends notifications to the feed,
    so if a mention shows in the feed, we'll catch it here.

    The notification event should contain the exact same data that goes to the feed.
    """
    from .mention_handler import process_mention_event

    try:
        logger.info(f"📬 Notification event received: topic={event.topic}")

        # Get event data - this should be the same data that populates the feed
        payload = event.payload or {}
        summary = getattr(event, 'summary', {}) or {}
        project_name = event.project or payload.get("project_name", "")

        if not project_name:
            logger.warning("⚠️ No project name in notification event")
            return

        # Get settings
        settings = await addon.get_project_settings(project_name)
        if not settings:
            logger.warning(f"Could not get settings for project {project_name}")
            return

        # Get Ayon server URL
        import os
        mention_settings = settings.get("mention_notifications", {})
        plugin_settings = mention_settings.get("MentionNotifications", {})
        ayon_server_url = plugin_settings.get("ayon_server_url", "")

        if not ayon_server_url:
            ayon_server_url = os.environ.get("AYON_SERVER_URL", "http://localhost:5000")

        # Extract notification info - this should be the exact same data that shows in the feed
        # Ayon's notification system might have different field names
        notification_data = payload.get("notification", {}) or payload

        # Try multiple ways to extract the mentioned user
        mentioned_user = (
            mentioned_user or
            notification_data.get("mentioned_user") or
            notification_data.get("target_user") or
            payload.get("mentioned_user") or
            payload.get("target_user") or
            summary.get("mentioned_user")
        )

        # Extract other notification details
        author_name = (
            notification_data.get("author") or
            notification_data.get("from_user") or
            event.user or
            payload.get("author", payload.get("from_user", ""))
        )

        # The notification body/message - this should be what appears in the feed
        body = (
            notification_data.get("body") or
            notification_data.get("text") or
            notification_data.get("message") or
            notification_data.get("description") or
            payload.get("body") or
            payload.get("text") or
            payload.get("message") or
            payload.get("description") or
            f"You were mentioned by {author_name}"
        )

        # Entity info (what was mentioned)
        entity_type = notification_data.get("entity_type") or payload.get("entity_type", "entity")
        entity_id = notification_data.get("entity_id") or payload.get("entity_id", "")
        entity_name = notification_data.get("entity_name") or payload.get("entity_name", "Unknown")

        # Activity info
        activity_id = notification_data.get("activity_id") or payload.get("activity_id")

        logger.info(f"📋 Notification details:")
        logger.info(f"   - Mentioned user: {mentioned_user}")
        logger.info(f"   - Author: {author_name}")
        logger.info(f"   - Body: {body}")
        logger.info(f"   - Entity: {entity_type}/{entity_name}")

        # If we don't have a specific mentioned user, try to extract from the body
        # This handles cases where the notification is generic but contains @mentions
        if not mentioned_user and "@" in body:
            from .mention_handler import extract_mentions
            mentions = extract_mentions(body)
            if mentions:
                mentioned_user = mentions[0]  # Take the first mention
                logger.info(f"🔍 Extracted mentioned user from body: {mentioned_user}")

        if not mentioned_user:
            logger.warning("⚠️ No mentioned user found in notification event")
            return

        # Build event data using the notification content
        event_data = {
            "author_name": author_name,
            "body": body,  # Use the exact notification message that goes to the feed
            "entity_type": entity_type,
            "entity_id": entity_id,
            "entity_name": entity_name,
            "project_name": project_name,
            "activity_id": activity_id,
            "mentioned_user": mentioned_user  # Explicitly pass the mentioned user
        }

        logger.info(f"🚀 Processing notification mention for user: {mentioned_user}")

        # Process the mention using the same system - this will map names and send to Slack
        result = await process_mention_event(
            event_data=event_data,
            settings=settings,
            ayon_server_url=ayon_server_url
        )

        logger.info(f"📊 Notification processing result: {result}")
        logger.info(f"📬 Notification forwarded to Slack for {mentioned_user}")

    except Exception as e:
        logger.error(f"Error handling notification event: {e}", exc_info=True)


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
        logger.warning(f"🔔 Activity event received: topic={event.topic}, project={event.project}, user={event.user}")
        
        # Note: EventStream.dispatch is async and would need to be awaited
        # Logger handles the logging instead
        
        # Get event payload and summary
        payload = event.payload or {}
        summary = getattr(event, 'summary', {}) or {}
        
        # Special handling for inbox.message events - the body text is in the event description
        # CRITICAL: Check topic first to ensure we catch inbox.message events
        if event.topic == "inbox.message":
            logger.warning(f"📬 INBOX.MESSAGE EVENT RECEIVED!")
            logger.warning(f"   Event topic: {event.topic}")
            logger.warning(f"   Event project: {event.project}")
            logger.warning(f"   Event user: {event.user}")
            logger.warning(f"   Payload type: {type(payload)}")
            logger.warning(f"   Payload: {payload}")
            logger.warning(f"   Summary: {summary}")
            
            # CRITICAL: The body text is in the event log description
            # Log shows: [EVENT CREATE] inbox.message ([yousef mohamed](user:yousef.mohamed) test 14)
            # This means the description contains the body text!
            body = None
            
            # Method 1: Direct description attribute (PRIMARY - this is where it should be!)
            try:
                # EventModel has a description attribute according to Ayon docs
                event_description = getattr(event, 'description', None)
                logger.warning(f"   event.description value: {event_description}")
                if event_description:
                    body = str(event_description).strip()
                    logger.warning(f"✅ Found body in event.description: {body[:50]}...")
                else:
                    logger.warning(f"⚠️ event.description is None or empty")
            except Exception as e:
                logger.warning(f"⚠️ Could not get event.description: {e}")
            
            # Also try to inspect the event object more thoroughly
            if not body:
                logger.warning(f"   Inspecting event object attributes...")
                try:
                    for attr in ['description', 'data', 'message', 'text', 'body', 'content']:
                        if hasattr(event, attr):
                            val = getattr(event, attr)
                            logger.warning(f"   event.{attr} = {val}")
                            if val and isinstance(val, str) and len(val) > 5:
                                body = str(val).strip()
                                logger.warning(f"✅ Found body in event.{attr}: {body[:50]}...")
                                break
                except Exception as e:
                    logger.warning(f"⚠️ Error inspecting event: {e}")
            
            # Method 2: Try accessing via dict-like access (some EventModel implementations support this)
            if not body:
                try:
                    if hasattr(event, '__getitem__'):
                        body = str(event['description']).strip()
                        if body:
                            logger.info(f"✅ Found body in event['description']: {body[:50]}...")
                except (KeyError, TypeError, AttributeError):
                    pass
            
            # Method 2: Check if payload is the body string directly
            if not body and isinstance(payload, str) and payload.strip():
                body = payload.strip()
                logger.info(f"✅ Found body in payload (string): {body[:50]}...")
            
            # Method 3: Check payload dict keys
            if not body and isinstance(payload, dict):
                for key in ["message", "text", "body", "description", "content", "data"]:
                    if key in payload:
                        value = payload[key]
                        if value:
                            if isinstance(value, str):
                                body = value.strip()
                            elif isinstance(value, dict) and "body" in value:
                                body = str(value["body"]).strip()
                            else:
                                body = str(value).strip()
                            if body:
                                logger.info(f"✅ Found body in payload['{key}']: {body[:50]}...")
                                break
            
            # Method 4: Check summary dict keys
            if not body and isinstance(summary, dict):
                for key in ["message", "text", "body", "description", "content", "data"]:
                    if key in summary:
                        value = summary[key]
                        if value:
                            if isinstance(value, str):
                                body = value.strip()
                            elif isinstance(value, dict) and "body" in value:
                                body = str(value["body"]).strip()
                            else:
                                body = str(value).strip()
                            if body:
                                logger.info(f"✅ Found body in summary['{key}']: {body[:50]}...")
                                break
            
            # Method 5: Try to get from event's string representation or other attributes
            if not body:
                try:
                    # Check all event attributes for the body text
                    for attr_name in dir(event):
                        if attr_name.startswith('_'):
                            continue
                        try:
                            attr_value = getattr(event, attr_name)
                            if isinstance(attr_value, str) and len(attr_value) > 10 and ("user:" in attr_value or "@" in attr_value):
                                body = attr_value.strip()
                                logger.info(f"✅ Found body in event.{attr_name}: {body[:50]}...")
                                break
                        except:
                            continue
                except Exception as e:
                    logger.debug(f"Error checking event attributes: {e}")
            
            # Convert to string and strip
            if body is not None:
                body = str(body).strip()
            else:
                body = ""
            
            logger.warning(f"   Final extracted body: {body[:100] if body else 'EMPTY'}...")
            
            # IMPORTANT: Return early for inbox.message events (even if body is empty)
            # Don't fall through to regular activity handler
            if not body:
                logger.warning("⚠️ inbox.message event has no body text, skipping")
                logger.warning(f"   Tried all methods but couldn't extract body from event")
                logger.warning(f"   Event type: {type(event)}")
                logger.warning(f"   Event dir: {[x for x in dir(event) if not x.startswith('_')][:10]}")
                return
            
            # If we found body in inbox.message, process it immediately
            if "@" in body or "(user:" in body:
                logger.warning(f"✅ Found @ mention in inbox.message, processing...")
                logger.warning(f"   Body contains mention: {body}")
                # Extract project from payload or use event.project
                project_name = event.project or payload.get("project") or payload.get("project_name", "")
                if not project_name:
                    logger.warning("⚠️ No project in inbox.message event, skipping")
                    return
                
                # Get author
                author_name = event.user or payload.get("user") or payload.get("author", "")
                
                # ENVIRONMENT DETECTION: Check if we're in dev vs prod
                import os
                ayon_env = os.environ.get("AYON_ENVIRONMENT", "unknown")
                ayon_addons_dir = os.environ.get("AYON_ADDONS_DIR", "unknown")
                docker_container = os.environ.get("HOSTNAME", "unknown")
                server_url = os.environ.get("AYON_SERVER_URL", "unknown")
                logger.warning(f"🌍 ENVIRONMENT INFO:")
                logger.warning(f"   - AYON_ENVIRONMENT: {ayon_env}")
                logger.warning(f"   - AYON_ADDONS_DIR: {ayon_addons_dir}")
                logger.warning(f"   - Docker Container: {docker_container}")
                logger.warning(f"   - AYON_SERVER_URL: {server_url}")
                logger.warning(f"   ⚠️ If addon is in DEV but settings are in PROD, they won't be visible!")
                
                # Get addon settings - try project first, then studio
                logger.warning(f"🔍 Getting settings for project: {project_name}")
                project_settings = await addon.get_project_settings(project_name)
                studio_settings = await addon.get_studio_settings()
                
                # CRITICAL: Try to read settings directly from database as fallback
                # This helps if dev addon can't see prod settings
                logger.warning(f"🔍 Attempting DIRECT database read of settings...")
                try:
                    from ayon_server.lib.postgres import Postgres
                    async with Postgres.acquire() as conn:
                        # Try to read project settings directly
                        project_query = """
                            SELECT data FROM project_settings 
                            WHERE project_name = $1 AND addon_name = $2
                        """
                        result = await conn.fetchrow(project_query, project_name, "slack")
                        if result:
                            raw_data = result.get("data", {})
                            logger.warning(f"   ✅ Found project settings in DB: {list(raw_data.keys())}")
                            if isinstance(raw_data, dict):
                                mention_data = raw_data.get("mention_notifications", {})
                                if mention_data:
                                    plugin_data = mention_data.get("MentionNotifications", {})
                                    db_mappings = plugin_data.get("user_mappings", [])
                                    logger.warning(f"   ✅ DB user_mappings count: {len(db_mappings)}")
                                    if db_mappings:
                                        logger.warning(f"   ✅ DB mappings: {db_mappings[:2]}")
                                        # If we found mappings in DB but not in addon settings, use DB!
                                        if not project_mappings and db_mappings:
                                            logger.warning(f"   🎯 FOUND MAPPINGS IN DB! Using them instead of addon settings.")
                                            # Update the settings_dict with DB mappings
                                            if not project_settings:
                                                # Create a mock settings object
                                                project_settings = type('Settings', (), {
                                                    'mention_notifications': type('MN', (), {
                                                        'MentionNotifications': type('PN', (), {
                                                            'user_mappings': db_mappings,
                                                            'enabled': plugin_data.get('enabled', True),
                                                            'ayon_server_url': plugin_data.get('ayon_server_url', '')
                                                        })()
                                                    })()
                                                })()
                        else:
                            logger.warning(f"   ⚠️ No project settings found in DB for {project_name}")
                        
                        # Try studio settings
                        studio_query = """
                            SELECT data FROM studio_settings 
                            WHERE addon_name = $1
                        """
                        result = await conn.fetchrow(studio_query, "slack")
                        if result:
                            raw_data = result.get("data", {})
                            logger.warning(f"   ✅ Found studio settings in DB: {list(raw_data.keys())}")
                            if isinstance(raw_data, dict):
                                mention_data = raw_data.get("mention_notifications", {})
                                if mention_data:
                                    plugin_data = mention_data.get("MentionNotifications", {})
                                    db_mappings = plugin_data.get("user_mappings", [])
                                    logger.warning(f"   ✅ Studio DB user_mappings count: {len(db_mappings)}")
                                    if db_mappings:
                                        logger.warning(f"   ✅ Studio DB mappings: {db_mappings[:2]}")
                                        if not studio_mappings and db_mappings:
                                            logger.warning(f"   🎯 FOUND MAPPINGS IN STUDIO DB! Using them.")
                except Exception as e:
                    logger.warning(f"   ⚠️ Could not read from DB directly: {e}")
                
                # DEEP INSPECTION: Check the raw settings object structure
                logger.warning(f"🔍 DEEP INSPECTION of Project Settings object...")
                if project_settings:
                    logger.warning(f"   Settings type: {type(project_settings)}")
                    logger.warning(f"   Has mention_notifications: {hasattr(project_settings, 'mention_notifications')}")
                    if hasattr(project_settings, 'mention_notifications'):
                        mn_obj = project_settings.mention_notifications
                        logger.warning(f"   mention_notifications type: {type(mn_obj)}")
                        logger.warning(f"   Has MentionNotifications: {hasattr(mn_obj, 'MentionNotifications')}")
                        if hasattr(mn_obj, 'MentionNotifications'):
                            plugin_obj = mn_obj.MentionNotifications
                            logger.warning(f"   MentionNotifications type: {type(plugin_obj)}")
                            logger.warning(f"   Plugin object dir: {[x for x in dir(plugin_obj) if not x.startswith('_')][:10]}")
                            
                            # Try multiple ways to get user_mappings
                            raw_mappings_attr = getattr(plugin_obj, 'user_mappings', None)
                            logger.warning(f"   getattr(user_mappings): {raw_mappings_attr}")
                            logger.warning(f"   getattr(user_mappings) type: {type(raw_mappings_attr)}")
                            logger.warning(f"   getattr(user_mappings) length: {len(raw_mappings_attr) if raw_mappings_attr else 0}")
                            
                            # Try model_dump if available
                            if hasattr(plugin_obj, 'model_dump'):
                                try:
                                    dumped = plugin_obj.model_dump()
                                    logger.warning(f"   model_dump() keys: {list(dumped.keys())}")
                                    logger.warning(f"   model_dump() user_mappings: {dumped.get('user_mappings', 'NOT FOUND')}")
                                except Exception as e:
                                    logger.warning(f"   model_dump() error: {e}")
                            
                            # Try dict() if available
                            if hasattr(plugin_obj, 'dict'):
                                try:
                                    dicted = plugin_obj.dict()
                                    logger.warning(f"   dict() keys: {list(dicted.keys())}")
                                    logger.warning(f"   dict() user_mappings: {dicted.get('user_mappings', 'NOT FOUND')}")
                                except Exception as e:
                                    logger.warning(f"   dict() error: {e}")
                
                # Check user_mappings in BOTH Project and Studio settings
                logger.warning(f"🔍 Checking user_mappings in Project Settings...")
                project_mappings = []
                if project_settings:
                    try:
                        if hasattr(project_settings, 'mention_notifications'):
                            if hasattr(project_settings.mention_notifications, 'MentionNotifications'):
                                project_mappings = getattr(project_settings.mention_notifications.MentionNotifications, 'user_mappings', [])
                                logger.warning(f"   Project user_mappings count: {len(project_mappings)}")
                                if project_mappings:
                                    logger.warning(f"   Project mappings: {[{'ayon': m.get('ayon_user') if isinstance(m, dict) else getattr(m, 'ayon_user', '?'), 'slack': m.get('slack_user_id') if isinstance(m, dict) else getattr(m, 'slack_user_id', '?')} for m in project_mappings[:3]]}")
                    except Exception as e:
                        logger.warning(f"   Error reading project mappings: {e}", exc_info=True)
                
                logger.warning(f"🔍 Checking user_mappings in Studio Settings...")
                studio_mappings = []
                if studio_settings:
                    try:
                        if hasattr(studio_settings, 'mention_notifications'):
                            if hasattr(studio_settings.mention_notifications, 'MentionNotifications'):
                                studio_mappings = getattr(studio_settings.mention_notifications.MentionNotifications, 'user_mappings', [])
                                logger.warning(f"   Studio user_mappings count: {len(studio_mappings)}")
                                if studio_mappings:
                                    logger.warning(f"   Studio mappings: {[{'ayon': m.get('ayon_user') if isinstance(m, dict) else getattr(m, 'ayon_user', '?'), 'slack': m.get('slack_user_id') if isinstance(m, dict) else getattr(m, 'slack_user_id', '?')} for m in studio_mappings[:3]]}")
                    except Exception as e:
                        logger.warning(f"   Error reading studio mappings: {e}", exc_info=True)
                
                # Decide which settings to use: prefer the one with mappings, or enabled=True
                settings = project_settings
                if settings:
                    try:
                        project_enabled = False
                        if hasattr(settings, 'mention_notifications'):
                            if hasattr(settings.mention_notifications, 'MentionNotifications'):
                                project_enabled = getattr(settings.mention_notifications.MentionNotifications, 'enabled', False)
                        
                        # If project has no mappings but studio does, use studio
                        if not project_mappings and studio_mappings:
                            logger.warning(f"✅ Project has no mappings, but Studio has {len(studio_mappings)} mappings! Using Studio Settings.")
                            settings = studio_settings
                        # If project enabled=False, try studio
                        elif not project_enabled:
                            logger.warning(f"⚠️ Project settings enabled=False, trying Studio Settings...")
                            if studio_settings:
                                if hasattr(studio_settings, 'mention_notifications'):
                                    if hasattr(studio_settings.mention_notifications, 'MentionNotifications'):
                                        studio_enabled = getattr(studio_settings.mention_notifications.MentionNotifications, 'enabled', False)
                                        if studio_enabled:
                                            logger.warning(f"✅ Found enabled=True in Studio Settings! Using studio settings.")
                                            settings = studio_settings
                    except Exception as e:
                        logger.warning(f"⚠️ Error checking studio settings: {e}")
                
                if not settings:
                    logger.warning(f"Could not get settings for project {project_name}")
                    return
                
                # CRITICAL: Read enabled value DIRECTLY from object FIRST before conversion
                # This ensures we get the actual value even if conversion fails
                enabled_direct = False
                try:
                    if hasattr(settings, 'mention_notifications'):
                        if hasattr(settings.mention_notifications, 'MentionNotifications'):
                            enabled_direct = getattr(settings.mention_notifications.MentionNotifications, 'enabled', False)
                            logger.warning(f"🔍 DIRECT READ - enabled: {enabled_direct} (type: {type(enabled_direct)})")
                            logger.warning(f"   Settings source: {'Studio' if 'studio' in str(type(settings)).lower() else 'Project'}")
                except Exception as e:
                    logger.warning(f"⚠️ Could not read enabled directly: {e}")
                
                # Convert Pydantic model to dict if needed
                logger.warning(f"🔍 Settings type: {type(settings)}")
                
                # Helper function to convert user_mappings (list of UserMapping Pydantic models) to list of dicts
                def convert_mappings(mappings):
                    """Convert user_mappings from Pydantic models to dicts."""
                    if not mappings:
                        return []
                    result = []
                    for m in mappings:
                        if isinstance(m, dict):
                            result.append(m)
                        elif hasattr(m, 'model_dump'):
                            result.append(m.model_dump())
                        elif hasattr(m, 'dict'):
                            result.append(m.dict())
                        else:
                            # Fallback: access attributes directly
                            result.append({
                                "ayon_user": getattr(m, 'ayon_user', ''),
                                "slack_user_id": getattr(m, 'slack_user_id', '')
                            })
                    return result
                
                if hasattr(settings, 'model_dump'):
                    settings_dict = settings.model_dump()
                    logger.warning(f"✅ Converted using model_dump()")
                    # Ensure user_mappings are properly converted
                    if 'mention_notifications' in settings_dict:
                        if 'MentionNotifications' in settings_dict['mention_notifications']:
                            raw_mappings = settings_dict['mention_notifications']['MentionNotifications'].get('user_mappings', [])
                            logger.warning(f"   Raw mappings from model_dump: {raw_mappings[:2] if raw_mappings else []}...")
                            converted_mappings = convert_mappings(raw_mappings)
                            settings_dict['mention_notifications']['MentionNotifications']['user_mappings'] = converted_mappings
                            logger.warning(f"   Converted mappings count: {len(converted_mappings)}")
                elif hasattr(settings, 'dict'):
                    settings_dict = settings.dict()
                    logger.warning(f"✅ Converted using dict()")
                    # Ensure user_mappings are properly converted
                    if 'mention_notifications' in settings_dict:
                        if 'MentionNotifications' in settings_dict['mention_notifications']:
                            raw_mappings = settings_dict['mention_notifications']['MentionNotifications'].get('user_mappings', [])
                            converted_mappings = convert_mappings(raw_mappings)
                            settings_dict['mention_notifications']['MentionNotifications']['user_mappings'] = converted_mappings
                            logger.warning(f"   Converted mappings count: {len(converted_mappings)}")
                elif isinstance(settings, dict):
                    settings_dict = settings
                    logger.warning(f"✅ Settings already a dict")
                    # Still need to convert mappings if they're Pydantic models
                    if 'mention_notifications' in settings_dict:
                        if 'MentionNotifications' in settings_dict['mention_notifications']:
                            raw_mappings = settings_dict['mention_notifications']['MentionNotifications'].get('user_mappings', [])
                            converted_mappings = convert_mappings(raw_mappings)
                            settings_dict['mention_notifications']['MentionNotifications']['user_mappings'] = converted_mappings
                            logger.warning(f"   Converted mappings count: {len(converted_mappings)}")
                else:
                    # Access as object attributes - use direct read value
                    logger.warning(f"⚠️ Settings is object, building dict from attributes...")
                    try:
                        if hasattr(settings, 'mention_notifications'):
                            mention_notif_obj = settings.mention_notifications
                            if hasattr(mention_notif_obj, 'MentionNotifications'):
                                plugin_obj = mention_notif_obj.MentionNotifications
                                
                                # Use the direct read value if we got it, otherwise get from object
                                if enabled_direct is not False or enabled_direct is True:
                                    final_enabled = enabled_direct
                                else:
                                    final_enabled = getattr(plugin_obj, 'enabled', False)
                                
                                logger.warning(f"   Using enabled value: {final_enabled}")
                                
                                # Get user_mappings - try direct attribute first
                                raw_mappings = getattr(plugin_obj, 'user_mappings', [])
                                logger.warning(f"   Raw user_mappings from object: type={type(raw_mappings)}, length={len(raw_mappings) if raw_mappings else 0}")
                                if raw_mappings:
                                    logger.warning(f"   First mapping: {raw_mappings[0] if raw_mappings else 'N/A'}")
                                
                                # Convert mappings to list of dicts
                                mappings_list = convert_mappings(raw_mappings)
                                logger.warning(f"   Converted mappings count: {len(mappings_list)}")
                                if mappings_list:
                                    logger.warning(f"   First converted mapping: {mappings_list[0]}")
                                
                                settings_dict = {
                                    "token": getattr(settings, 'token', ''),
                                    "mention_notifications": {
                                        "MentionNotifications": {
                                            "enabled": final_enabled,
                                            "ayon_server_url": getattr(plugin_obj, 'ayon_server_url', ''),
                                            "user_mappings": mappings_list,
                                        }
                                    }
                                }
                            else:
                                raise AttributeError("No MentionNotifications attribute")
                        else:
                            raise AttributeError("No mention_notifications attribute")
                    except Exception as e:
                        logger.error(f"   Error accessing as object: {e}", exc_info=True)
                        settings_dict = {
                            "token": getattr(settings, 'token', ''),
                            "mention_notifications": {
                                "MentionNotifications": {
                                    "enabled": enabled_direct if enabled_direct else False,
                                    "ayon_server_url": '',
                                    "user_mappings": [],
                                }
                            }
                        }
                
                # CRITICAL: If we read enabled directly and it's True, FORCE it in the dict
                if enabled_direct and 'mention_notifications' in settings_dict:
                    if 'MentionNotifications' in settings_dict['mention_notifications']:
                        if settings_dict['mention_notifications']['MentionNotifications'].get('enabled') != enabled_direct:
                            logger.warning(f"⚠️ Enabled value mismatch! Direct: {enabled_direct}, Dict: {settings_dict['mention_notifications']['MentionNotifications'].get('enabled')}")
                            logger.warning(f"   FORCING enabled to {enabled_direct} in dict")
                            settings_dict['mention_notifications']['MentionNotifications']['enabled'] = enabled_direct
                
                # Log the final converted settings structure
                final_enabled = settings_dict.get('mention_notifications', {}).get('MentionNotifications', {}).get('enabled', 'NOT FOUND')
                final_mappings = settings_dict.get('mention_notifications', {}).get('MentionNotifications', {}).get('user_mappings', [])
                logger.warning(f"🔍 Final settings_dict:")
                logger.warning(f"   - enabled: {final_enabled}")
                logger.warning(f"   - user_mappings count: {len(final_mappings)}")
                if final_mappings:
                    logger.warning(f"   - user_mappings: {final_mappings[:2]}...")
                else:
                    logger.error(f"   - ⚠️ user_mappings is EMPTY! This might be why messages aren't sending.")
                
                # Get Ayon server URL
                import os
                mention_settings = settings_dict.get("mention_notifications", {})
                plugin_settings = mention_settings.get("MentionNotifications", {})
                ayon_server_url = plugin_settings.get("ayon_server_url", "") or os.environ.get("AYON_SERVER_URL", "http://localhost:5000")
                
                # Build event data for inbox.message
                event_data = {
                    "author_name": author_name,
                    "body": body,
                    "entity_type": payload.get("entity_type", "task"),
                    "entity_id": payload.get("entity_id", ""),
                    "entity_name": payload.get("entity_name", "Unknown"),
                    "project_name": project_name,
                    "activity_id": payload.get("activity_id")
                }
                
                logger.warning(f"📤📤📤 CALLING process_mention_event NOW...")
                logger.warning(f"   Event data: {event_data}")
                logger.warning(f"   Settings dict keys: {list(settings_dict.keys())}")
                logger.warning(f"   Ayon server URL: {ayon_server_url}")
                try:
                    result = await process_mention_event(event_data, settings_dict, ayon_server_url)
                    logger.warning(f"📊📊📊 Inbox message result: {result}")
                except Exception as e:
                    logger.error(f"❌❌❌ ERROR in process_mention_event: {e}", exc_info=True)
                return
        
        # Log full event structure for debugging (first time only, or when issues occur)
        logger.debug(f"📋 Event structure: topic={event.topic}, project={event.project}, user={event.user}")
        logger.debug(f"📋 Payload keys: {list(payload.keys()) if isinstance(payload, dict) else 'not a dict'}")
        logger.debug(f"📋 Summary keys: {list(summary.keys()) if isinstance(summary, dict) else 'not a dict'}")
        if summary:
            logger.debug(f"📋 Summary content: {summary}")
        
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
                    logger.info(f"✅ Extracted body from payload['body']: {body[:50]}...")
        
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
        
        # If body is still empty, log what we have for debugging
        if not body:
            logger.warning(f"⚠️ Body is empty. Payload keys: {list(payload.keys()) if isinstance(payload, dict) else 'not a dict'}")
            logger.warning(f"   Payload content: {payload}")
            logger.warning(f"   Summary content: {summary}")
        
        logger.warning(f"📝 Activity type: {activity_type}, body preview: {body[:50] if body else 'None'}...")
        
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
            logger.warning("⚠️ No body text in activity, skipping")
            logger.warning(f"   Payload keys: {list(payload.keys()) if isinstance(payload, dict) else 'not a dict'}")
            logger.warning(f"   Summary keys: {list(summary.keys()) if isinstance(summary, dict) else 'not a dict'}")
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
            logger.debug("⏭️ No @ or (user:) in body, skipping")
            return
        
        logger.warning(f"✅ Found @ mention in body, processing...")
        
        # Get additional info
        # Try to get entity info if we have entity_type and entity_id
        entity_name = "Unknown"
        if entity_type and entity_id:
            entity_info = await get_entity_info(project_name, entity_type, entity_id)
            entity_name = entity_info.get("name", "Unknown")
        else:
            logger.warning(f"⚠️ Missing entity info: entity_type={entity_type}, entity_id={entity_id}")
            logger.warning(f"   Will still try to process mention, but entity info may be incomplete")
            entity_info = {"name": "Unknown", "type": entity_type or "unknown", "id": entity_id or ""}
        
        author_name = await get_user_name(event.user or payload.get("author", ""))
        
        # Get addon settings - try project first, then studio
        logger.warning(f"🔍 Getting settings for project: {project_name}")
        settings = await addon.get_project_settings(project_name)
        
        # If project settings don't have enabled=True, try studio settings
        if settings:
            try:
                project_enabled = False
                if hasattr(settings, 'mention_notifications'):
                    if hasattr(settings.mention_notifications, 'MentionNotifications'):
                        project_enabled = getattr(settings.mention_notifications.MentionNotifications, 'enabled', False)
                
                if not project_enabled:
                    logger.warning(f"⚠️ Project settings enabled=False, trying Studio Settings...")
                    studio_settings = await addon.get_studio_settings()
                    if studio_settings:
                        if hasattr(studio_settings, 'mention_notifications'):
                            if hasattr(studio_settings.mention_notifications, 'MentionNotifications'):
                                studio_enabled = getattr(studio_settings.mention_notifications.MentionNotifications, 'enabled', False)
                                if studio_enabled:
                                    logger.warning(f"✅ Found enabled=True in Studio Settings! Using studio settings.")
                                    settings = studio_settings
            except Exception as e:
                logger.warning(f"⚠️ Error checking studio settings: {e}")
        
        if not settings:
            logger.warning(f"Could not get settings for project {project_name}")
            return
        
        # CRITICAL: Read enabled value DIRECTLY from object FIRST before conversion
        # This ensures we get the actual value even if conversion fails
        enabled_direct = False
        try:
            if hasattr(settings, 'mention_notifications'):
                if hasattr(settings.mention_notifications, 'MentionNotifications'):
                    enabled_direct = getattr(settings.mention_notifications.MentionNotifications, 'enabled', False)
                    logger.warning(f"🔍 DIRECT READ - enabled: {enabled_direct} (type: {type(enabled_direct)})")
                    logger.warning(f"   Settings source: {'Studio' if 'studio' in str(type(settings)).lower() else 'Project'}")
        except Exception as e:
            logger.warning(f"⚠️ Could not read enabled directly: {e}")
        
        # Convert Pydantic model to dict if needed
        logger.info(f"🔍 Settings type: {type(settings)}")
        
        if hasattr(settings, 'model_dump'):
            settings_dict = settings.model_dump()
            logger.info(f"✅ Converted using model_dump()")
        elif hasattr(settings, 'dict'):
            settings_dict = settings.dict()
            logger.info(f"✅ Converted using dict()")
        elif isinstance(settings, dict):
            settings_dict = settings
            logger.info(f"✅ Settings already a dict")
        else:
            # Access as object attributes - use direct read value
            logger.info(f"⚠️ Settings is object, building dict from attributes...")
            try:
                if hasattr(settings, 'mention_notifications'):
                    mention_notif_obj = settings.mention_notifications
                    if hasattr(mention_notif_obj, 'MentionNotifications'):
                        plugin_obj = mention_notif_obj.MentionNotifications
                        
                        # Use the direct read value if we got it, otherwise get from object
                        if enabled_direct is not False or enabled_direct is True:
                            final_enabled = enabled_direct
                        else:
                            final_enabled = getattr(plugin_obj, 'enabled', False)
                        
                        logger.info(f"   Using enabled value: {final_enabled}")
                        
                        settings_dict = {
                            "token": getattr(settings, 'token', ''),
                            "mention_notifications": {
                                "MentionNotifications": {
                                    "enabled": final_enabled,
                                    "ayon_server_url": getattr(plugin_obj, 'ayon_server_url', ''),
                                    "user_mappings": getattr(plugin_obj, 'user_mappings', []),
                                }
                            }
                        }
                    else:
                        raise AttributeError("No MentionNotifications attribute")
                else:
                    raise AttributeError("No mention_notifications attribute")
            except Exception as e:
                logger.warning(f"   Error accessing as object: {e}, using fallback")
                settings_dict = {
                    "token": getattr(settings, 'token', ''),
                    "mention_notifications": {
                        "MentionNotifications": {
                            "enabled": enabled_direct if enabled_direct else False,
                            "ayon_server_url": '',
                            "user_mappings": [],
                        }
                    }
                }
        
        # CRITICAL: If we read enabled directly and it's True, FORCE it in the dict
        if enabled_direct and 'mention_notifications' in settings_dict:
            if 'MentionNotifications' in settings_dict['mention_notifications']:
                if settings_dict['mention_notifications']['MentionNotifications'].get('enabled') != enabled_direct:
                    logger.warning(f"⚠️ Enabled value mismatch! Direct: {enabled_direct}, Dict: {settings_dict['mention_notifications']['MentionNotifications'].get('enabled')}")
                    logger.warning(f"   FORCING enabled to {enabled_direct} in dict")
                    settings_dict['mention_notifications']['MentionNotifications']['enabled'] = enabled_direct
        
        # Log the final converted settings structure
        logger.info(f"🔍 Final settings_dict - enabled: {settings_dict.get('mention_notifications', {}).get('MentionNotifications', {}).get('enabled', 'NOT FOUND')}")
        
        # Get Ayon server URL from settings or environment
        import os
        mention_settings = settings_dict.get("mention_notifications", {})
        plugin_settings = mention_settings.get("MentionNotifications", {})
        ayon_server_url = plugin_settings.get("ayon_server_url", "")
        
        if not ayon_server_url:
            ayon_server_url = os.environ.get("AYON_SERVER_URL", "http://localhost:5000")
        
        # Get activity_id from summary or payload
        activity_id = summary.get("activity_id") or payload.get("activity_id")
        
        # Process the mention event
        event_data = {
            "author_name": author_name,
            "body": body,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "entity_name": entity_info.get("name", "Unknown"),
            "project_name": project_name,
            "activity_id": activity_id
        }
        
        logger.warning(f"📤 Calling process_mention_event with:")
        logger.warning(f"   - Author: {author_name}")
        logger.warning(f"   - Body preview: {body[:100] if body else 'None'}...")
        logger.warning(f"   - Entity: {entity_type}/{entity_name}")
        logger.warning(f"   - Project: {project_name}")
        
        result = await process_mention_event(
            event_data=event_data,
            settings=settings_dict,
            ayon_server_url=ayon_server_url
        )
        
        logger.warning(f"📊 Mention notification result: {result}")
        
        # Log result status
        status = result.get("status", "unknown")
        if status == "success":
            sent_count = len(result.get('notifications_sent', []))
            logger.warning(f"✅ SUCCESS! Sent {sent_count} Slack notification(s)")
        elif status == "disabled":
            logger.warning("⚠️ Mention notifications are DISABLED in settings")
        elif status == "no_mappings":
            logger.error("❌ No user mappings configured!")
        elif result.get("errors"):
            errors = result.get('errors', [])
            error_msg = ', '.join(errors[:2]) if len(errors) > 0 else "Unknown error"
            logger.error(f"❌ ERROR: {error_msg}")
        else:
            logger.warning(f"⚠️ Status: {status}")
        
    except Exception as e:
        logger.error(f"Error handling activity event: {e}", exc_info=True)

