"""Server-side metadata check. No browser, media download or raw exceptions."""

import json
import os
import sys

from yt_dlp import YoutubeDL
from yt_dlp.networking.impersonate import ImpersonateTarget
from yt_dlp.utils import DownloadError
from video_pls_bot.extract import QuietLogger, classify_error
from video_pls_bot.links import parse_link


def probe(platform, cookie_path, url):
    link = parse_link(url)
    if link.platform != platform:
        return {'ok': False, 'code': 'invalid_url'}
    # DOWNLOAD_PROXY / NODE_BINARY come from the bot's EnvironmentFile (see tools/vpb/server.py).
    options = {'proxy': os.environ['DOWNLOAD_PROXY'], 'cookiefile': cookie_path,
               'quiet': True, 'no_warnings': True, 'logger': QuietLogger(),
               'cachedir': False, 'noplaylist': True, 'socket_timeout': 15,
               'retries': 1, 'extractor_retries': 1, 'skip_download': True,
               'js_runtimes': {'node': {'path': os.environ.get('NODE_BINARY', '/usr/bin/node')}}, 'remote_components': set()}
    if platform != 'youtube':
        options['impersonate'] = ImpersonateTarget(client='chrome')
    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(link.url, download=False)
        if (not info or info.get('_type', 'video') != 'video' or info.get('is_live')
                or not any(f.get('url') and f.get('vcodec') != 'none' for f in info.get('formats') or [info])):
            return {'ok': False, 'code': 'source_unavailable'}
        return {'ok': True, 'code': 'access_ok'}


if __name__ == '__main__':
    try:
        result = probe(*sys.argv[1:])
    except DownloadError as exc:
        result = {'ok': False, 'code': classify_error(str(exc))}
    except Exception:
        result = {'ok': False, 'code': 'probe_failed'}
    print(json.dumps(result), flush=True)
