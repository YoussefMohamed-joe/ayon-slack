"""API endpoints for mention notifications."""
import csv
import io
import json
import logging
from typing import List, Dict

from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/addons/slack", tags=["slack"])


class MentionTestRequest(BaseModel):
    """Test mention request."""

    project_name: str
    mentioned_user: str
    mentioner_user: str
    entity_type: str = "task"
    entity_id: str = ""
    entity_name: str = "Test Entity"
    comment_text: str = "Test mention"


@router.post("/test-mention")
async def test_mention(request: MentionTestRequest):
    """
    Test endpoint to manually trigger a mention notification.

    This simulates an Ayon activity event and sends a Slack DM.
    """
    try:
        from ..mention_handler import process_mention_event
        from ayon_server.addons import get_addon

        addon = await get_addon("slack")
        if not addon:
            raise HTTPException(
                status_code=404,
                detail="Slack addon not found. Make sure it's installed and active.",
            )

        settings = await addon.get_project_settings(request.project_name)
        if not settings:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Settings not found for project '{request.project_name}'. "
                    "Check project name."
                ),
            )

        import os

        mention_settings = settings.get("mention_notifications", {})
        plugin_settings = mention_settings.get("MentionNotifications", {})
        ayon_server_url = plugin_settings.get("ayon_server_url", "") or os.environ.get(
            "AYON_SERVER_URL", "http://localhost:5000"
        )

        event_data = {
            "author_name": request.mentioner_user,
            "body": request.comment_text,
            "entity_type": request.entity_type,
            "entity_id": request.entity_id,
            "entity_name": request.entity_name,
            "project_name": request.project_name,
            "activity_id": None,
        }

        result = await process_mention_event(
            event_data=event_data,
            settings=settings,
            ayon_server_url=ayon_server_url,
        )

        return {
            "status": "success",
            "result": result,
        }

    except HTTPException:
        raise
    except Exception as e:  # pragma: no cover - defensive logging
        logger.error("Error testing mention: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error: {str(e)}")


@router.post("/parse-slack-csv")
async def parse_slack_csv(
    file: UploadFile = File(...),
):
    """
    Parse Slack members CSV file and extract user mappings.

    Expected CSV format from Slack:
    username,email,status,billing-active,has-2fa,has-sso,userid,fullname,displayname,expiration-timestamp
    """
    try:
        content = await file.read()
        text = content.decode("utf-8")

        csv_reader = csv.DictReader(io.StringIO(text))

        mappings: List[Dict[str, str]] = []
        skipped: List[Dict[str, str]] = []

        for row in csv_reader:
            username = row.get("username", "").strip()
            userid = row.get("userid", "").strip()
            status = row.get("status", "").strip()

            if status in ("Bot", "Deactivated") or not userid or not username:
                skipped.append(
                    {
                        "username": username,
                        "reason": (
                            "Bot or deactivated"
                            if status in ("Bot", "Deactivated")
                            else "Missing data"
                        ),
                    }
                )
                continue

            if not userid.startswith("U"):
                skipped.append(
                    {
                        "username": username,
                        "reason": "Invalid user ID format",
                    }
                )
                continue

            mappings.append(
                {
                    "ayon_user": username,
                    "slack_user_id": userid,
                    "fullname": row.get("fullname", ""),
                    "email": row.get("email", ""),
                }
            )

        return {
            "status": "success",
            "mappings": mappings,
            "count": len(mappings),
            "skipped": skipped,
            "skipped_count": len(skipped),
        }

    except Exception as e:  # pragma: no cover - defensive logging
        logger.error("Error parsing CSV: %s", e, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Error parsing CSV: {str(e)}",
        )


@router.post("/parse-slack-csv-text")
async def parse_slack_csv_text(
    csv_text: str = Form(...),
):
    """
    Parse Slack members CSV text (pasted content) and extract user mappings.
    """
    try:
        csv_reader = csv.DictReader(io.StringIO(csv_text))

        mappings: List[Dict[str, str]] = []
        skipped: List[Dict[str, str]] = []

        for row in csv_reader:
            username = row.get("username", "").strip()
            userid = row.get("userid", "").strip()
            status = row.get("status", "").strip()

            if status in ("Bot", "Deactivated") or not userid or not username:
                skipped.append(
                    {
                        "username": username,
                        "reason": (
                            "Bot or deactivated"
                            if status in ("Bot", "Deactivated")
                            else "Missing data"
                        ),
                    }
                )
                continue

            if not userid.startswith("U"):
                skipped.append(
                    {
                        "username": username,
                        "reason": "Invalid user ID format",
                    }
                )
                continue

            mappings.append(
                {
                    "ayon_user": username,
                    "slack_user_id": userid,
                    "fullname": row.get("fullname", ""),
                    "email": row.get("email", ""),
                }
            )

        return {
            "status": "success",
            "mappings": mappings,
            "count": len(mappings),
            "skipped": skipped,
            "skipped_count": len(skipped),
        }

    except Exception as e:  # pragma: no cover - defensive logging
        logger.error("Error parsing CSV text: %s", e, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Error parsing CSV: {str(e)}",
        )


@router.post("/update-user-mappings")
async def update_user_mappings(
    project_name: str = Form(...),
    mappings_json: str = Form(...),
):
    """
    Process CSV mappings and return them formatted for Ayon settings.

    Note: Returns mappings structure ready to copy into Ayon settings UI.
    """
    try:
        mappings = json.loads(mappings_json)
        if not isinstance(mappings, list):
            raise HTTPException(
                status_code=400,
                detail="Mappings must be a list",
            )

        user_mappings: List[Dict[str, str]] = []
        for mapping in mappings:
            user_mappings.append(
                {
                    "ayon_user": mapping.get("ayon_user", ""),
                    "slack_user_id": mapping.get("slack_user_id", ""),
                }
            )

        return {
            "status": "success",
            "message": f"Generated {len(user_mappings)} user mappings",
            "mappings_count": len(user_mappings),
            "mappings": user_mappings,
        }

    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON format")
    except Exception as e:  # pragma: no cover - defensive logging
        logger.error("Error processing mappings: %s", e, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Error: {str(e)}",
        )

