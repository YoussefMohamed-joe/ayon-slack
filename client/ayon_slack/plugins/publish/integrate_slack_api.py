import os
import re
import copy
import time

import pyblish.api

from ayon_core.lib.plugin_tools import prepare_template_data
from ayon_core.pipeline.publish import get_publish_repre_path

from ayon_core.lib import StringTemplate


class SlackOperations:
    def __init__(self, token, log):
        from slack_sdk import WebClient

        self.client = WebClient(token=token)
        self.log = log

    def _get_users_list(self):
        return self.client.users_list()

    def _get_usergroups_list(self):
        return self.client.usergroups_list()

    def get_users_and_groups(self):
        from slack_sdk.errors import SlackApiError
        while True:
            try:
                users = self._get_users()
                groups = self._get_groups()
                break
            except SlackApiError as e:
                retry_after = e.response.headers.get("Retry-After")
                if retry_after:
                    print(
                        "Rate limit hit, sleeping for {}".format(retry_after))
                    time.sleep(int(retry_after))
                else:
                    self.log.warning("Cannot pull user info, "
                                     "mentions won't work", exc_info=True)
                    return [], []
            except Exception:
                self.log.warning("Cannot pull user info, "
                                 "mentions won't work", exc_info=True)
                return [], []

        return users, groups

    def send_message(self, channel, message, publish_files):
        from slack_sdk.errors import SlackApiError
        try:
            attachments = self._upload_attachments(publish_files)

            message = self._add_attachments(attachments, message)

            self.client.chat_postMessage(
                channel=channel,
                text=message
            )
        except SlackApiError as e:
            # # You will get a SlackApiError if "ok" is False
            if e.response.get("error"):
                error_str = self._enrich_error(
                    str(e.response["error"]), channel
                )
            else:
                error_str = self._enrich_error(str(e), channel)
            self.log.warning("Error happened: {}".format(error_str),
                             exc_info=True)
        except Exception as e:
            error_str = self._enrich_error(str(e), channel)
            self.log.warning("Not SlackAPI error", exc_info=True)

    def _upload_attachments(self, publish_files):
        """Returns list of permalinks to uploaded files"""
        file_urls = []
        for published_file in publish_files:
            with open(published_file, "rb") as f:
                uploaded_file = self.client.files_upload_v2(
                    filename=os.path.basename(published_file),
                    file=f
                )
                file_urls.append(uploaded_file.get("file").get("permalink"))

        return file_urls

    def _add_attachments(self, attachments, message):
        """Add permalink urls to message without displaying url."""
        for permalink_url in attachments:
            # format extremely important!
            message += f"<{permalink_url}| >"
        return message

    def _get_users(self):
        """Parse users.list response into list of users (dicts)"""
        first = True
        next_page = None
        users = []
        while first or next_page:
            response = self._get_users_list()
            first = False
            next_page = response.get("response_metadata").get("next_cursor")
            for user in response.get("members"):
                users.append(user)

        return users

    def _get_groups(self):
        """Parses usergroups.list response into list of groups (dicts)"""
        response = self._get_usergroups_list()
        groups = []
        for group in response.get("usergroups"):
            groups.append(group)
        return groups

    def _enrich_error(self, error_str, channel):
        """Enhance known errors with more helpful notations."""
        if "not_in_channel" in error_str:
            # there is no file.write.public scope, app must be explicitly in
            # the channel
            error_str += (
                " - application must added to channel '{}'. Ask Slack admin."
            ).format(channel)
        return error_str


class IntegrateSlackAPI(pyblish.api.InstancePlugin):
    """ Send message notification to a channel.
        Triggers on instances with "slack" family, filled by
        'collect_slack_family'.
        Expects configured profile in
        Project settings > Slack > Publish plugins > Notification to Slack.
        If instance contains 'thumbnail' it uploads it. Bot must be present
        in the target channel.
        If instance contains 'review' it could upload (if configured) or place
        link with {review_filepath} placeholder.
        Message template can contain {} placeholders from anatomyData.
    """
    order = pyblish.api.IntegratorOrder + 0.499
    label = "Integrate Slack Api"
    families = ["slack"]
    settings_category = "slack"

    optional = True

    def process(self, instance):
        if instance.data.get("farm"):
            self.log.debug(
                "Instance is marked to be processed on farm. Skipping")
            return

        thumbnail_path = self._get_thumbnail_path(instance)
        review_path = self._get_review_path(instance)

        publish_files = set()
        token = instance.data["slack_token"]

        additional_message = instance.data.get("slack_additional_message")
        for message_profile in instance.data["slack_channel_message_profiles"]:
            message = message_profile["message"]
            if additional_message:
                message = f"{additional_message} \n {message}"

            message = self._get_filled_content(
                message, instance, review_path)

            if not message:
                return

            if message_profile["upload_thumbnail"] and thumbnail_path:
                publish_files.add(thumbnail_path)

            if message_profile["upload_review"] and review_path:
                message, publish_files = self._handle_review_upload(
                    message, message_profile, publish_files, review_path)

            for channel in message_profile["channels"]:
                channel = self._get_filled_content(
                    channel, instance, review_path)

                client = SlackOperations(token, self.log)

                if "@" in message:
                    cache_key = "__cache_slack_ids"
                    slack_ids = instance.context.data.get(cache_key, None)
                    if slack_ids is None:
                        users, groups = client.get_users_and_groups()
                        instance.context.data[cache_key] = {}
                        instance.context.data[cache_key]["users"] = users
                        instance.context.data[cache_key]["groups"] = groups
                    else:
                        users = slack_ids["users"]
                        groups = slack_ids["groups"]
                    message = self._translate_users(message, users, groups)

                client.send_message(channel, message, publish_files)

    def _handle_review_upload(self, message, message_profile, publish_files,
                              review_path):
        """Check if uploaded file is not too large"""
        review_file_size_MB = os.path.getsize(review_path) / 1024 / 1024
        file_limit = message_profile.get("review_upload_limit", 50)
        if review_file_size_MB > file_limit:
            message += "\nReview upload omitted because of file size."
            if review_path not in message:
                message += "\nFile located at: {}".format(review_path)
        else:
            publish_files.add(review_path)
        return message, publish_files

    def _get_filled_content(self, message, instance, review_path=None):
        """Use message and data from instance to get dynamic message content.

        Reviews might be large, so allow only adding link to message instead of
        uploading only.
        """

        fill_data = copy.deepcopy(instance.data["anatomyData"])
        fill_data["comment"] = instance.data["comment"]
        anatomy = instance.context.data["anatomy"]
        fill_data["root"] = anatomy.roots
        if review_path:
            fill_data["review_filepath"] = review_path

        message = (
            message
            .replace("{task}", "{task[name]}")
            .replace("{Task}", "{Task[name]}")
            .replace("{TASK}", "{TASK[NAME]}")
            .replace("{asset}", "{folder[name]}")
            .replace("{Asset}", "{Folder[name]}")
            .replace("{ASSET}", "{FOLDER[NAME]}")
            .replace("{subset}", "{product[name]}")
            .replace("{Subset}", "{Product[name]}")
            .replace("{SUBSET}", "{PRODUCT[NAME]}")
            .replace("{family}", "{product[type]}")
            .replace("{Family}", "{Product[type]}")
            .replace("{FAMILY}", "{PRODUCT[TYPE]}")
        )

        multiple_case_variants = prepare_template_data(fill_data)
        fill_data.update(multiple_case_variants)
        try:
            message = StringTemplate.format_template(message, fill_data)
        except Exception:
            # shouldn't happen
            self.log.warning(
                "Some keys are missing in {}".format(message),
                exc_info=True)

        return message

    def _get_thumbnail_path(self, instance):
        """Returns abs url for thumbnail if present in instance repres"""
        thumbnail_path = None
        for repre in instance.data.get("representations", []):
            if repre.get("thumbnail") or "thumbnail" in repre.get("tags", []):
                repre_thumbnail_path = get_publish_repre_path(
                    instance, repre, False
                )
                if os.path.exists(repre_thumbnail_path):
                    thumbnail_path = repre_thumbnail_path
                break
        return thumbnail_path

    def _get_review_path(self, instance):
        """Returns abs url for review if present in instance repres"""
        review_path = None
        for repre in instance.data.get("representations", []):
            tags = repre.get("tags", [])
            if (
                repre.get("review")
                or "review" in tags
                or "burnin" in tags
            ):
                repre_review_path = get_publish_repre_path(
                    instance, repre, False
                )
                if repre_review_path and os.path.exists(repre_review_path):
                    review_path = repre_review_path
                if "burnin" in tags:  # burnin has precedence if exists
                    break
        return review_path

    def _get_user_id(self, users, user_name):
        """Returns internal slack id for user name"""
        user_name_lower = user_name.lower()
        for user in users:
            if user.get("deleted"):
                continue
            user_profile = user["profile"]
            if user_name_lower in (
                user["name"].lower(),
                user_profile.get("display_name", "").lower(),
                user_profile.get("real_name", "").lower(),
            ):
                return user["id"]
        return None

    def _get_group_id(self, groups, group_name):
        """Returns internal group id for string name"""
        for group in groups:
            if (
                not group.get("date_delete")
                and group_name.lower() in (
                    group["name"].lower(),
                    group["handle"]
                )
            ):
                return group["id"]
        return None

    def _translate_users(self, message, users, groups):
        """Replace all occurences of @mentions with proper <@name> format."""
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
                mention = "<@{}>".format(slack_id)
            else:
                slack_id = self._get_group_id(groups, user_name)
                if slack_id:
                    mention = "<!subteam^{}>".format(slack_id)
            if mention:
                message = message.replace(orig_user, mention)

        return message

    def _escape_missing_keys(self, message, fill_data):
        """Double escapes placeholder which are missing in 'fill_data'"""
        placeholder_keys = re.findall(r"\{([^}]+)\}", message)

        fill_keys = []
        for key, value in fill_data.items():
            fill_keys.append(key)
            if isinstance(value, dict):
                for child_key in value.keys():
                    fill_keys.append("{}[{}]".format(key, child_key))

        not_matched = set(placeholder_keys) - set(fill_keys)

        for not_matched_item in not_matched:
            message = message.replace(
                f"{not_matched_item}",
                f"{{{not_matched_item}}}"
            )

        return message
