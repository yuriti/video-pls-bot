import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit


@dataclass(frozen=True)
class VideoLink:
    url: str
    platform: str


def parse_link(text: str) -> VideoLink:
    links = re.findall(r"https?://[^\s<>]+", text or "")
    if len(links) != 1:
        raise ValueError("one_link_required")
    url = links[0].rstrip(".,!?)\"'»")
    if len(url) > 2048:
        raise ValueError("unsupported_link")
    parsed = urlsplit(url)
    if parsed.username or parsed.password or parsed.port not in (None, 80, 443):
        raise ValueError("unsupported_link")
    host = (parsed.hostname or "").lower()
    path = parsed.path
    query = ""
    if host in ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"):
        video_id = parse_qs(parsed.query).get("v", [""])[0]
        if path == "/watch" and re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            query = urlencode({"v": video_id})
        elif not re.fullmatch(r"/(?:shorts|embed)/[A-Za-z0-9_-]{11}/?", path):
            raise ValueError("unsupported_link")
        platform = "youtube"
    elif host == "youtu.be" and re.fullmatch(r"/[A-Za-z0-9_-]{11}/?", path):
        platform = "youtube"
    elif host in ("tiktok.com", "www.tiktok.com", "m.tiktok.com") and re.fullmatch(
        r"/(?:@[^/]+/video/\d+|t/[A-Za-z0-9]+)/?", path
    ):
        platform = "tiktok"
    elif host in ("vm.tiktok.com", "vt.tiktok.com") and re.fullmatch(r"/[A-Za-z0-9]+/?", path):
        platform = "tiktok"
    elif host in ("instagram.com", "www.instagram.com", "m.instagram.com") and re.fullmatch(
        r"/(?:reel|reels|p)/[A-Za-z0-9_-]+/?", path
    ):
        platform = "instagram"
    else:
        raise ValueError("unsupported_link")
    if platform == "tiktok" and host in ("tiktok.com", "m.tiktok.com"):
        host = "www.tiktok.com"
    elif platform == "instagram":
        host = "www.instagram.com"
    return VideoLink(urlunsplit(("https", host, path, query, "")), platform)
