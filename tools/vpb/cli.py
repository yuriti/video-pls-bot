"""Local Chrome login. Cookies travel only in an authenticated SSH stdin pipe."""

import argparse
import asyncio
import contextlib
import fcntl
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
from pathlib import Path


from .common import AuthError, MAX_PAYLOAD, PLATFORMS, scoped_domain, validate_cookies, validate_url

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / 'secrets/vpb-auth/config.json'
CHROME = Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
TEXT = {
    'access_ok': 'Video access works.',
    'source_login_required': 'The platform requires a login or a "not a bot" check.',
    'source_unavailable': 'The video is unavailable or the platform restricted access.',
    'credential_missing': 'The server has no cookies for this platform yet.',
    'login_required': 'No completed login was found in this session. The working cookies are unchanged.',
    'installed': 'Cookies verified, encrypted and installed. The new bot process has loaded them.',
    'saved_activation_pending': 'Cookies verified and stored encrypted. The bots finish their queue before restarting. Status: vpb status.',
    'saved_restart_needed': 'Cookies stored encrypted, but the restart did not start. The service needs an administrator check.',
    'busy': 'Another check or installation is in progress. Repeat the command later.',
    'ssh_failed': 'SSH connection failed. Check server access and the server host key; security settings are not relaxed.',
    'ssh_timeout': 'The SSH check did not finish in time. Check vpb status before installing again.',
    'chrome_missing': 'Google Chrome was not found in /Applications.',
    'chrome_failed': 'Could not open a separate Chrome session.',
    'browser_quit_failed': 'Could not close the separate Chrome cleanly. Close it and log in again; production cookies are unchanged.',
    'browser_closed': 'The session window was closed. Production cookies are unchanged.',
    'invalid_config': 'The CLI is not configured. Run the installer deploy/install-vpb.sh.',
    'invalid_url': 'An HTTPS link to a video of the selected platform is required.',
    'session_busy': 'Another vpb login session is already open. Finish it first.',
    'unsafe_permissions': 'The key or configuration has permissions that are too broad. 0600 is required.',
}


def config(path):
    try:
        if path.is_symlink() or path.stat().st_mode & 0o077 or path.stat().st_uid != os.getuid():
            raise AuthError('unsafe_permissions')
        value = json.loads(path.read_text())
        ssh_file = Path(value['ssh_config'])
        key = Path(value['identity_file'])
        if any(p.is_symlink() or p.stat().st_mode & 0o077 or p.stat().st_uid != os.getuid()
               for p in (ssh_file, key)):
            raise AuthError('unsafe_permissions')
        if (value.get('version') != 1 or not ssh_file.is_absolute() or not key.is_absolute() or type(value.get('proxy_port')) is not int
                or not 0 < value['proxy_port'] < 65536):
            raise AuthError('invalid_config')
        return value
    except AuthError:
        raise
    except (OSError, ValueError, KeyError, TypeError):
        raise AuthError('invalid_config') from None


def ssh_args(settings):
    return ['ssh', '-F', settings['ssh_config'], '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
            '-o', 'ConnectTimeout=10', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=2']


async def rpc(settings, action, platform, *, cookies=None, url=None):
    request = {'version': 1, 'action': action, 'platform': platform}
    if url:
        request['url'] = url
    if cookies is not None:
        request['cookies'] = validate_cookies(cookies, platform)
    payload = (json.dumps(request, ensure_ascii=True) + '\n').encode()
    if len(payload) > MAX_PAYLOAD:
        raise AuthError('invalid_cookies')
    child = await asyncio.create_subprocess_exec(*ssh_args(settings), '-T', 'vpb-server',
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True, limit=MAX_PAYLOAD)
    try:
        try:
            async with asyncio.timeout(180):
                output, _ = await child.communicate(payload)
        except TimeoutError:
            raise AuthError('ssh_timeout') from None
        if child.returncode or len(output) > MAX_PAYLOAD:
            raise AuthError('ssh_failed')
        try:
            result = json.loads(output)
        except (ValueError, UnicodeError):
            raise AuthError('ssh_failed') from None
        if not isinstance(result, dict) or type(result.get('ok')) is not bool or not isinstance(result.get('code'), str):
            raise AuthError('ssh_failed')
        return result
    finally:
        await stop(child)


async def stop(child):
    if child.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(child.wait(), timeout=5)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGKILL)
            await child.wait()


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


async def tunnel(settings):
    port = free_port()
    child = await asyncio.create_subprocess_exec(*ssh_args(settings), '-o', 'ExitOnForwardFailure=yes',
        '-N', '-T', '-L', f'127.0.0.1:{port}:127.0.0.1:{settings["proxy_port"]}', 'vpb-server',
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
    try:
        for _ in range(100):
            if child.returncode is not None:
                raise AuthError('ssh_failed')
            try:
                reader, writer = await asyncio.open_connection('127.0.0.1', port)
                writer.close()
                await writer.wait_closed()
                return child, port
            except OSError:
                await asyncio.sleep(0.1)
        raise AuthError('ssh_failed')
    except BaseException:
        await stop(child)
        raise


class CDP:
    """Private process pipe. No Chrome debugging socket is exposed on localhost."""
    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer
        self.sequence = 0

    async def call(self, method, params=None, *, session=None):
        self.sequence += 1
        sequence = self.sequence
        command = {'id': sequence, 'method': method, 'params': params or {}}
        if session:
            command['sessionId'] = session
        try:
            self.writer.write(json.dumps(command).encode() + b'\0')
            await self.writer.drain()
            async with asyncio.timeout(15):
                while True:
                    message = json.loads((await self.reader.readuntil(b'\0'))[:-1])
                    if message.get('id') != sequence:
                        continue
                    if 'error' in message:
                        raise AuthError('chrome_failed')
                    return message.get('result', {})
        except (TimeoutError, OSError, ValueError, TypeError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            raise AuthError('browser_closed') from None

    async def close(self):
        self.writer.close()
        with contextlib.suppress(OSError):
            await self.writer.wait_closed()


async def launch_browser(profile, proxy_port, *, headless=False):
    parent, child_socket = socket.socketpair()
    parent.setblocking(False)
    browser = cdp = None
    try:
        # The shell only duplicates inherited file descriptors. All paths and
        # Chrome arguments are positional parameters; no evaluated input/code.
        browser = await asyncio.create_subprocess_exec('/bin/sh', '-c',
            'exec 3<&"$1" 4>&"$1"; shift; exec "$@"', 'vpb-chrome', str(child_socket.fileno()),
            str(CHROME), f'--user-data-dir={profile}', '--remote-debugging-pipe',
            f'--proxy-server=http://127.0.0.1:{proxy_port}', '--disable-quic', '--no-first-run',
            '--no-default-browser-check', '--disable-background-networking',
            *(['--headless=new', '--no-startup-window', '--disable-sync'] if headless else []), 'about:blank',
            pass_fds=(child_socket.fileno(),), stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
        child_socket.close()
        reader, writer = await asyncio.open_connection(sock=parent, limit=MAX_PAYLOAD * 4)
        cdp = CDP(reader, writer)
        await cdp.call('Browser.getVersion')
        return browser, cdp
    except BaseException:
        child_socket.close()
        if cdp:
            await cdp.close()
        else:
            parent.close()
        if browser:
            await stop(browser)
        raise


async def launch_login_browser(profile, proxy_port, url):
    """Human login in a plain Chrome window without automation.

    No debugging, automation or private-context switches during authentication.
    This process owns a new profile created by vpb, never a personal profile.
    """
    return await asyncio.create_subprocess_exec(str(CHROME), f'--user-data-dir={profile}',
        '--no-first-run', '--no-default-browser-check', '--new-window', '--start-maximized',
        f'--proxy-server=http://127.0.0.1:{proxy_port}', '--disable-quic', url,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, start_new_session=True)


async def close_login_browser(browser):
    """Let the ordinary browser flush its own profile before reading cookies."""
    if browser.returncode is not None:
        if browser.returncode != 0:
            raise AuthError('chrome_failed')
        return
    try:
        if sys.platform == 'darwin':
            # Native Quit flushes Chrome's profile; SIGTERM on macOS can lose
            # recently set cookies. Address only the exact child we launched.
            script = ('ObjC.import("AppKit"); var app = '
                      '$.NSRunningApplication.runningApplicationWithProcessIdentifier(%d); '
                      'if (!app.terminate) throw new Error("quit_failed");') % browser.pid
            quitter = await asyncio.create_subprocess_exec('/usr/bin/osascript', '-l', 'JavaScript', '-e', script,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
            try:
                if await asyncio.wait_for(quitter.wait(), timeout=8) != 0:
                    raise AuthError('browser_quit_failed')
            finally:
                await stop(quitter)
        else:
            browser.terminate()
        await asyncio.wait_for(browser.wait(), timeout=20)
    except ProcessLookupError:
        await browser.wait()
    except TimeoutError:
        await stop(browser)
        raise AuthError('browser_quit_failed') from None
    if browser.returncode not in (0, -signal.SIGTERM):
        raise AuthError('chrome_failed')


async def wait_for_enter(browser, proxy, *, allow_browser_exit=False):
    """Keep Ctrl+C and child-exit handling live while the human signs in."""
    loop = asyncio.get_running_loop()
    ready = loop.create_future()
    descriptor = sys.stdin.fileno()
    def read_line():
        if ready.done():
            return
        line = sys.stdin.readline()
        if line:
            ready.set_result(None)
        else:
            ready.set_exception(EOFError())
    loop.add_reader(descriptor, read_line)
    browser_exit = asyncio.create_task(browser.wait())
    proxy_exit = asyncio.create_task(proxy.wait())
    print('When you are done, press Enter here. Ctrl+C cancels: ', end='', flush=True)
    try:
        done, _ = await asyncio.wait((ready, browser_exit, proxy_exit), return_when=asyncio.FIRST_COMPLETED)
        if proxy_exit in done:
            raise AuthError('ssh_failed')
        if browser_exit in done:
            if not allow_browser_exit or browser_exit.result() != 0:
                raise AuthError('browser_closed')
            # Closing a window is not consent to install credentials. Still
            # require the terminal's Enter; Ctrl+C remains a safe cancellation.
            if not ready.done():
                print('\nChrome is closed. Enter verifies and transfers the cookies; Ctrl+C cancels: ', end='', flush=True)
                done, _ = await asyncio.wait((ready, proxy_exit), return_when=asyncio.FIRST_COMPLETED)
                if proxy_exit in done:
                    raise AuthError('ssh_failed')
        await ready
    finally:
        loop.remove_reader(descriptor)
        for task in (ready, browser_exit, proxy_exit):
            if not task.done():
                task.cancel()
        await asyncio.gather(ready, browser_exit, proxy_exit, return_exceptions=True)


async def finish_browser(cdp, platform):
    # The human's ordinary Chrome has exited. This reader starts with the same
    # vpb-owned profile on about:blank and never opens an account/login page.
    all_cookies = (await cdp.call('Storage.getCookies'))['cookies']
    selected = [c for c in all_cookies if scoped_domain(c.get('domain'), platform)]
    cookies = validate_cookies(selected, platform)
    # Close the owned browser even if Chrome closes the socket before replying.
    with contextlib.suppress(AuthError):
        await cdp.call('Browser.close')
    return cookies

async def login(settings, platform, url):
    if not CHROME.is_file():
        raise AuthError('chrome_missing')
    if not sys.stdin.isatty():
        raise AuthError('terminal_required')
    state = await rpc(settings, 'status', platform)
    if not state['ok']:
        return state
    lock_dir = DEFAULT_CONFIG.parent
    lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (lock_dir / 'session.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AuthError('session_busy') from None
        proxy = browser = cdp = None
        with tempfile.TemporaryDirectory(prefix='vpb-chrome-') as root:
            profile = Path(root)
            profile.chmod(0o700)
            try:
                proxy, port = await tunnel(settings)
                browser = await launch_login_browser(profile, port, url)
                print(f'Opened a plain Chrome with a separate {platform} profile through the bot proxy.', flush=True)
                print('Log in, pass any check and make sure the video opens.', flush=True)
                print('After Enter this separate Chrome session will be closed.', flush=True)
                await wait_for_enter(browser, proxy, allow_browser_exit=True)
                await close_login_browser(browser)
                browser = None
                print('Login session closed. Reading cookies of the profile created by vpb…', flush=True)
                browser, cdp = await launch_browser(profile, port, headless=True)
                cookies = await finish_browser(cdp, platform)
                await cdp.close()
                cdp = None
                await stop(browser)
                browser = None
                print('Checking the cookies on the server. On success they are encrypted and installed…', flush=True)
                return await rpc(settings, 'install', platform, cookies=cookies, url=url)
            finally:
                if cdp:
                    await cdp.close()
                if browser:
                    await stop(browser)
                if proxy:
                    await stop(proxy)


def show(result, json_output=False):
    # The wire response contains no cookies; still allow only fixed fields in output.
    clean = {key: result[key] for key in ('ok', 'code', 'state', 'credential_exists', 'credential_loaded') if key in result}
    codes = set(TEXT) | {'status', 'invalid_platform', 'invalid_cookie_domain', 'invalid_cookies',
                        'invalid_request', 'runtime_not_memory', 'unsafe_runtime', 'unsafe_credential',
                        'server_command_failed', 'encryption_failed', 'server_failed', 'probe_failed',
                        'download_failed', 'request_timeout', 'terminal_required'}
    if clean.get('code') not in codes:
        clean['code'] = 'unknown'
    if 'state' in clean and clean['state'] not in {'active', 'inactive', 'failed', 'activating', 'deactivating', 'reloading'}:
        clean['state'] = 'unknown'
    for key in ('credential_exists', 'credential_loaded'):
        if key in clean:
            clean[key] = clean[key] is True
    if json_output:
        print(json.dumps(clean))
    elif clean.get('code') == 'status':
        print('Service:', clean.get('state', 'unknown'))
        print('Encrypted cookies:', 'present' if clean.get('credential_exists') else 'absent')
        print('Loaded by the current process:', 'yes' if clean.get('credential_loaded') else 'no / restart pending')
    else:
        print(TEXT.get(clean.get('code'), 'The operation did not complete. Code: ' + clean.get('code', 'unknown')))
    return 0 if clean.get('ok') else 1


async def execute(args):
    settings = config(Path(args.config))
    if args.action == 'doctor':
        if not CHROME.is_file():
            raise AuthError('chrome_missing')
        if not shutil.which('ssh'):
            raise AuthError('ssh_failed')
        print('Chrome found. Configuration and SSH key permissions verified.')
        return show(await rpc(settings, 'status', 'youtube'), args.json)
    platform = args.platform or 'youtube'
    url = validate_url(platform, args.url) if args.action in ('check', 'login') else None
    if args.action == 'login':
        return show(await login(settings, platform, url), args.json)
    if args.action == 'check':
        print('Checking access from the server…', file=sys.stderr, flush=True)
    return show(await rpc(settings, args.action, platform, url=url), args.json)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(prog='vpb', description='Video Pls authorization: Chrome on the Mac, cookies over SSH to the server.')
    parser.add_argument('--config', default=os.environ.get('VPB_CONFIG', str(DEFAULT_CONFIG)))
    parser.add_argument('--json', action='store_true', help='Safe JSON result without cookies')
    commands = parser.add_subparsers(dest='action', required=True)
    for action in ('check', 'login', 'status'):
        child = commands.add_parser(action, help={'check': 'Check access from the server', 'login': 'Open a separate Chrome session',
                                                  'status': 'Check whether the service loaded the credential'}[action])
        child.add_argument('platform', choices=PLATFORMS, nargs='?' if action == 'status' else None, default='youtube')
        if action in ('check', 'login'):
            child.add_argument('--url', help='Check this video instead of the reference one')
    commands.add_parser('doctor', help='Check the local setup and the SSH connection')
    args = parser.parse_args()
    args.platform = getattr(args, 'platform', None)
    args.url = getattr(args, 'url', None)
    try:
        code = asyncio.run(execute(args))
    except AuthError as exc:
        code = show({'ok': False, 'code': str(exc)}, args.json)
    except (KeyboardInterrupt, EOFError):
        print('\nCancelled. Local processes and the temporary profile are closed.')
        code = 130
    except Exception:
        print('The operation did not complete. Cookie values and internal errors are hidden.', file=sys.stderr)
        code = 1
    raise SystemExit(code)


if __name__ == '__main__':
    main()
