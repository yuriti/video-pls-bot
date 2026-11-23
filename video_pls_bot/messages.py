"""Shared user-facing text for every bot."""

import re

from .texts import t


def build_video_caption(bot_username: str) -> str:
    """Shared caption for sendVideo and sendAudio with parse_mode='HTML'.

    Pass the sending bot's username from getMe, rather than a shared username
    or a name taken from the user's message.
    """
    username = bot_username.removeprefix("@")
    if not re.fullmatch(r"[A-Za-z0-9_]+", username):
        raise ValueError("A bot username is required for the video caption")
    return t("caption", username=username)
