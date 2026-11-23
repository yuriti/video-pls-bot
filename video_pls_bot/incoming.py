"""Small, local-only filter for Telegram text and media captions."""

import re

from .links import VideoLink, parse_link


def message_link(message: dict) -> VideoLink:
    """Return one distinct supported video; never inspect replies or previews.

    Entity offsets are UTF-16 code units, rather than Python character offsets.
    Mask entity labels before scanning remaining text so a hidden link's visible
    label cannot accidentally be treated as another destination.
    """
    links = set()
    for field, entity_field in (("text", "entities"), ("caption", "caption_entities")):
        text = message.get(field) or ""
        encoded = bytearray(text.encode("utf-16-le"))
        candidates = []
        for entity in message.get(entity_field) or []:
            kind = entity.get("type")
            if kind not in ("url", "text_link"):
                continue
            start = entity.get("offset", -1) * 2
            end = start + entity.get("length", 0) * 2
            if not 0 <= start < end <= len(encoded):
                continue
            label = encoded[start:end].decode("utf-16-le", errors="replace")
            url = entity.get("url", "") if kind == "text_link" else label
            if kind == "url" and "://" not in url:
                url = "https://" + url
            candidates.append(url)
            encoded[start:end] = b" \x00" * ((end - start) // 2)
        candidates.extend(re.findall(r"https?://[^\s<>]+", encoded.decode("utf-16-le")))
        for candidate in candidates:
            try:
                links.add(parse_link(candidate))
            except ValueError:
                continue
    if len(links) != 1:
        raise ValueError("one_video_link_required")
    return links.pop()


def sender_id(message: dict) -> int | None:
    # sender_chat takes precedence over Telegram's compatibility fake user.
    if message.get("sender_chat") or message.get("chat", {}).get("type") == "channel":
        return None
    return message.get("from", {}).get("id")


def reply_fields(message: dict) -> dict:
    fields = {}
    if message.get("message_thread_id"):
        fields["message_thread_id"] = message["message_thread_id"]
    if message.get("chat", {}).get("type") != "private" and message.get("message_id"):
        fields["reply_parameters"] = {"message_id": message["message_id"],
                                      "allow_sending_without_reply": True}
    return fields
