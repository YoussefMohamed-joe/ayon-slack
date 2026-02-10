"""
Server-side Slack operations for mention notifications.

Mirrors the client-side SlackOperations used in the publish workflow,
so mention notifications behave the same way.
"""
import logging
import re
import time
from typing import List, Dict, Optional
import threading

logger = logging.getLogger(__name__)

# Global batch system - collects messages and sends every 10 seconds
_message_batch = []
_batch_lock = threading.Lock()
_batch_worker_started = False

# Production safety limits
MAX_BATCH_SIZE = 500  # Prevent memory overflow
MAX_RETRIES = 3  # Retry failed sends
CIRCUIT_BREAKER_THRESHOLD = 10  # After 10 failures, pause
CIRCUIT_BREAKER_TIMEOUT = 60  # Resume after 60 seconds

# Circuit breaker state
_consecutive_failures = 0
_circuit_open_until = 0


def _start_batch_worker():
    """Start the background worker that sends batched messages every 10 seconds."""
    global _batch_worker_started
    
    # Check without lock first (fast path)
    if _batch_worker_started:
        return
    
    # Only acquire lock if needed
    with _batch_lock:
        if _batch_worker_started:
            return
        _batch_worker_started = True
    
    def _process_batches():
        """Send batched messages every 10 seconds with production safeguards."""
        import requests
        global _consecutive_failures, _circuit_open_until
        
        while True:
            try:
                # Wait 10 seconds
                time.sleep(10)
                
                # Check circuit breaker
                current_time = time.time()
                if _circuit_open_until > current_time:
                    wait_time = int(_circuit_open_until - current_time)
                    logger.warning(f"⚠️ Circuit breaker open, resuming in {wait_time}s")
                    continue
                
                # Get batch and clear it
                with _batch_lock:
                    if not _message_batch:
                        continue
                    
                    # Warn if batch exceeded limit
                    if len(_message_batch) > MAX_BATCH_SIZE:
                        overflow = len(_message_batch) - MAX_BATCH_SIZE
                        logger.warning(f"⚠️ Batch overflow! Dropping {overflow} oldest messages")
                    
                    # Take up to MAX_BATCH_SIZE messages, clear the rest
                    batch = _message_batch[:MAX_BATCH_SIZE]
                    _message_batch.clear()
                
                logger.info(f"📤 Sending batch of {len(batch)} messages...")
                
                # Send with retry logic
                success_count = 0
                failure_count = 0
                
                for msg_data in batch:
                    sent = False
                    
                    # Retry up to MAX_RETRIES times
                    for attempt in range(MAX_RETRIES):
                        try:
                            response = requests.post(
                                "https://slack.com/api/chat.postMessage",
                                headers=msg_data["headers"],
                                json=msg_data["payload"],
                                timeout=3
                            )
                            
                            if response.status_code == 200:
                                result = response.json()
                                if result.get("ok"):
                                    sent = True
                                    break
                                else:
                                    error = result.get("error", "unknown")
                                    if error == "rate_limited":
                                        # Wait and retry
                                        retry_after = int(response.headers.get("Retry-After", 1))
                                        time.sleep(retry_after)
                                        continue
                                    else:
                                        logger.warning(f"⚠️ Slack error: {error}")
                                        break
                        except requests.exceptions.Timeout:
                            if attempt < MAX_RETRIES - 1:
                                time.sleep(1)  # Wait before retry
                                continue
                        except Exception as e:
                            logger.debug(f"Send error (attempt {attempt+1}): {e}")
                            if attempt < MAX_RETRIES - 1:
                                time.sleep(1)
                                continue
                            break
                    
                    if sent:
                        success_count += 1
                        _consecutive_failures = 0  # Reset on success
                    else:
                        failure_count += 1
                        _consecutive_failures += 1
                
                # Log results
                logger.info(f"✅ Batch complete: {success_count} sent, {failure_count} failed")
                
                # Circuit breaker: if too many failures, pause
                if _consecutive_failures >= CIRCUIT_BREAKER_THRESHOLD:
                    _circuit_open_until = time.time() + CIRCUIT_BREAKER_TIMEOUT
                    logger.error(
                        f"🚨 Circuit breaker OPEN! "
                        f"{_consecutive_failures} consecutive failures. "
                        f"Pausing for {CIRCUIT_BREAKER_TIMEOUT}s"
                    )
                
            except Exception as e:
                logger.error(f"❌ Batch worker error: {e}", exc_info=True)
    
    # Start worker thread
    worker = threading.Thread(target=_process_batches, daemon=True, name="slack-batch-worker")
    worker.start()
    logger.info("✅ Slack batch worker started (sends every 10 seconds)")


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
        
        ABSOLUTE NON-BLOCKING: Offloads even the batch append to a detached thread.
        Zero impact on calling thread - guaranteed instant return.
        """
        # Ensure batch worker is running (only starts once, non-blocking)
        _start_batch_worker()
        
        # Prepare message data (fast operation, no I/O)
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        payload = {
            "channel": channel,
            "text": message,
        }
        if blocks:
            payload["blocks"] = blocks
        
        msg_data = {
            "headers": headers,
            "payload": payload,
            "channel": channel,
            "timestamp": time.time(),
        }
        
        # Offload batch append to separate thread (absolute zero blocking)
        def _append_to_batch():
            try:
                with _batch_lock:
                    # Production safety: prevent memory overflow
                    if len(_message_batch) >= MAX_BATCH_SIZE:
                        logger.warning(f"⚠️ Batch full, dropping oldest")
                        _message_batch.pop(0)
                    
                    _message_batch.append(msg_data)
                    logger.debug(f"📥 Message queued for {channel} (batch size: {len(_message_batch)})")
            except Exception as e:
                logger.error(f"❌ Batch add error: {e}")
        
        # Start detached thread - returns IMMEDIATELY
        threading.Thread(target=_append_to_batch, daemon=True, name="batch-append").start()
        
        return True  # Instant return, zero blocking!

    def _enrich_error(self, error_str: str, channel: str) -> str:
        """Enhance known errors with more helpful notes."""
        if "not_in_channel" in error_str:
            error_str += (
                f" - application must be added to channel '{channel}'. "
                "Ask Slack admin."
            )
        return error_str

