"""Root-owned, fixed SSH RPC. Never expose a shell or decrypted credentials."""

import fcntl
import json
import os
import pwd
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

if __package__:
    from .common import AuthError, MAX_PAYLOAD, netscape_bytes, platform_spec, validate_url
else:
    from common import AuthError, MAX_PAYLOAD, netscape_bytes, platform_spec, validate_url

CONFIG = Path('/etc/video-pls-bot')
# The same environment file the bot units load (DOWNLOAD_PROXY, NODE_BINARY, ...).
ENV_FILE = CONFIG / 'video-pls-bot.env'
RUNTIME = Path('/run/vpb-auth')
TOOLS = Path('/opt/video-pls-bot/auth-tools')
CURRENT = Path('/opt/video-pls-bot/current')
PYTHON = '/opt/video-pls-bot/venv/bin/python'
SERVICE = 'video-pls-bot.service'


def run(*args, input=None, timeout=20):
    try:
        result = subprocess.run(args, input=input, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=timeout, check=False, env={**os.environ, 'LC_ALL': 'C',
                                                                  'PYTHONDONTWRITEBYTECODE': '1'})
    except (OSError, subprocess.TimeoutExpired):
        raise AuthError('server_command_failed') from None
    if result.returncode or len(result.stdout) > MAX_PAYLOAD:
        raise AuthError('server_command_failed')
    return result.stdout


def credential(platform):
    platform_spec(platform)
    return CONFIG / f'{platform}-cookies.txt.cred'


def service_status(platform):
    fields = dict(line.split('=', 1) for line in run('systemctl', 'show', SERVICE, '-p', 'ActiveState',
                                                    '-p', 'SubState', '-p', 'MainPID').decode().splitlines())
    path = credential(platform)
    loaded = False
    pid = int(fields.get('MainPID', '0'))
    if pid and path.is_file():
        try:
            ticks = float(Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19])
            uptime = float(Path('/proc/uptime').read_text().split()[0])
            started = time.time() - uptime + ticks / os.sysconf('SC_CLK_TCK')
            loaded = started >= path.stat().st_mtime
        except (OSError, ValueError, IndexError):
            pass
    return {'ok': True, 'code': 'status', 'state': fields.get('ActiveState', 'unknown'),
            'credential_exists': path.is_file(), 'credential_loaded': loaded}


def ensure_memory_runtime():
    if run('findmnt', '-n', '-o', 'FSTYPE', '--target', '/run').strip() not in (b'tmpfs', b'ramfs'):
        raise AuthError('runtime_not_memory')
    RUNTIME.mkdir(mode=0o711, parents=True, exist_ok=True)
    if RUNTIME.is_symlink() or RUNTIME.stat().st_uid != 0:
        raise AuthError('unsafe_runtime')
    RUNTIME.chmod(0o711)


def check_access(platform, plain, url):
    user = pwd.getpwnam('video-pls-bot')
    with tempfile.TemporaryDirectory(prefix='probe-', dir=RUNTIME) as root:
        directory = Path(root)
        directory.chmod(0o700)
        cookie = directory / 'cookies.txt'
        with cookie.open('xb') as output:
            output.write(plain)
        cookie.chmod(0o600)
        os.chown(cookie, user.pw_uid, user.pw_gid)
        os.chown(directory, user.pw_uid, user.pw_gid)
        unit = 'vpb-auth-probe-' + uuid4().hex
        output = run('systemd-run', '--quiet', '--wait', '--pipe', '--collect', f'--unit={unit}',
                     '--property=User=video-pls-bot', '--property=Group=video-pls-bot',
                     f'--property=WorkingDirectory={CURRENT}', f'--property=Environment=PYTHONPATH={CURRENT}',
                     f'--property=EnvironmentFile={ENV_FILE}',
                     '--property=RuntimeMaxSec=120', '--property=MemoryMax=600M', '--property=CPUQuota=80%',
                     '--property=TasksMax=64', '--property=NoNewPrivileges=yes',
                     '--property=ProtectSystem=strict', '--property=ProtectHome=yes',
                     f'--property=ReadWritePaths={directory}',
                     '--property=IPAddressDeny=any', '--property=IPAddressAllow=localhost',
                     '--property=LimitCORE=0', '--property=UMask=0077',
                     PYTHON, str(TOOLS / 'probe.py'), platform, str(cookie), url, timeout=140)
        try:
            result = json.loads(output)
        except (ValueError, UnicodeError):
            raise AuthError('probe_failed') from None
        allowed = {'access_ok', 'source_login_required', 'source_unavailable', 'invalid_url', 'probe_failed', 'download_failed'}
        if not isinstance(result, dict) or result.get('code') not in allowed or type(result.get('ok')) is not bool:
            raise AuthError('probe_failed')
        return {'ok': result['ok'], 'code': result['code']}


def encrypt_and_install(platform, plain):
    target = credential(platform)
    name = f'{platform}-cookies.txt'
    CONFIG.mkdir(mode=0o700, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f'.{platform}-candidate-', suffix='.cred', dir=CONFIG)
    os.close(fd)
    temporary = Path(temporary)
    # systemd-creds refuses overwriting an existing file; the parent is root-only.
    temporary.unlink()
    try:
        run('systemd-creds', 'encrypt', '--with-key=host', f'--name={name}', '-', str(temporary), input=plain)
        temporary.chmod(0o600)
        verified = run('systemd-creds', 'decrypt', f'--name={name}', str(temporary), '-')
        if verified != plain:
            raise AuthError('encryption_failed')
        if target.exists():
            if target.is_symlink() or not stat.S_ISREG(target.stat().st_mode):
                raise AuthError('unsafe_credential')
            backup = target.with_suffix('.cred.previous')
            backup_temp = CONFIG / ('.backup-' + uuid4().hex)
            try:
                with backup_temp.open('xb') as output:
                    output.write(target.read_bytes())
                    output.flush()
                    os.fsync(output.fileno())
                backup_temp.chmod(0o600)
                os.replace(backup_temp, backup)
            finally:
                backup_temp.unlink(missing_ok=True)
        with temporary.open('rb') as source:
            os.fsync(source.fileno())
        os.replace(temporary, target)
        directory_fd = os.open(CONFIG, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def activate(platform):
    try:
        # SIGTERM drains the accepted queue before systemd starts the new process.
        run('systemctl', '--no-block', 'restart', SERVICE)
    except AuthError:
        return {'ok': True, 'code': 'saved_restart_needed', 'credential_loaded': False}
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        status = service_status(platform)
        if status['state'] == 'active' and status['credential_loaded']:
            return {'ok': True, 'code': 'installed', 'credential_loaded': True}
        time.sleep(0.5)
    return {'ok': True, 'code': 'saved_activation_pending', 'credential_loaded': False}


def handle(request):
    if not isinstance(request, dict) or type(request.get('version')) is not int or request.get('version') != 1:
        raise AuthError('invalid_request')
    action, platform = request.get('action'), request.get('platform')
    platform_spec(platform)
    if action not in ('status', 'check', 'install') or not request.keys() <= {'version', 'action', 'platform', 'cookies', 'url'}:
        raise AuthError('invalid_request')
    if action != 'install' and 'cookies' in request:
        raise AuthError('invalid_request')
    if action == 'status':
        return service_status(platform)
    url = validate_url(platform, request.get('url'))
    ensure_memory_runtime()
    with (RUNTIME / 'update.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AuthError('busy') from None
        if action == 'check':
            path = credential(platform)
            if not path.is_file():
                return {'ok': False, 'code': 'credential_missing'}
            plain = run('systemd-creds', 'decrypt', f'--name={platform}-cookies.txt', str(path), '-')
            return check_access(platform, plain, url)
        plain = netscape_bytes(request.get('cookies'), platform)
        result = check_access(platform, plain, url)
        if not result['ok']:
            return result
        encrypt_and_install(platform, plain)
        return activate(platform)


def alarm(_signum, _frame):
    raise AuthError('request_timeout')


def main():
    os.umask(0o077)
    try:
        if len(sys.argv) != 1 or os.geteuid() != 0:
            raise AuthError('invalid_request')
        signal.signal(signal.SIGALRM, alarm)
        signal.alarm(10)
        line = sys.stdin.buffer.readline(MAX_PAYLOAD + 1)
        signal.alarm(0)
        if len(line) > MAX_PAYLOAD or not line.endswith(b'\n'):
            raise AuthError('invalid_request')
        request = json.loads(line)
        result = handle(request)
    except AuthError as exc:
        result = {'ok': False, 'code': str(exc)}
    except Exception:
        result = {'ok': False, 'code': 'server_failed'}
    print(json.dumps(result), flush=True)


if __name__ == '__main__':
    main()
