"""One-time local/global installer. Root SSH is used only for provisioning."""

import argparse
import io
import json
import os
import re
import shlex
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / 'secrets/vpb-auth'
# The hash identifies the unmodified extractor the tmpfs patch is based on.
BASE_EXTRACT = '0d2fcadd6dc389882f5fbac144193960b38e2bab7806703f46f6aed14e5bf725'
BASE_PROCESS = '0055b48f0e7ae3582900fb84ae1fc400dc473d62a43f27929390056294028282'


def run(*args, input=None, timeout=120):
    result = subprocess.run(args, input=input, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if result.returncode:
        # Do not dump SSH/cookie-bearing diagnostic output.
        raise RuntimeError('Installation command failed; secret-bearing output is suppressed.')
    return result.stdout


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description='Install global vpb and its restricted server helper')
    parser.add_argument('--server', default=os.environ.get('VPB_SSH_ALIAS'), required='VPB_SSH_ALIAS' not in os.environ,
                        help='Existing trusted root SSH alias, used for provisioning only (or VPB_SSH_ALIAS)')
    parser.add_argument('--proxy-port', type=int, default=int(os.environ.get('VPB_PROXY_PORT') or 0) or None,
                        required='VPB_PROXY_PORT' not in os.environ,
                        help="Loopback port of the bot's download proxy on the server (or VPB_PROXY_PORT)")
    args = parser.parse_args()
    if not 0 < args.proxy_port < 65536:
        raise RuntimeError('Invalid proxy port')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', args.server):
        raise RuntimeError('Invalid SSH alias')
    binary = Path.home() / '.local/bin'
    wrapper = ('#!/bin/sh\n' + f'export PYTHONPATH={shlex.quote(str(ROOT))}\n' +
               f'exec {shlex.quote(str(ROOT / ".venv/bin/python"))} -m tools.vpb.cli "$@"\n')
    for name in ('vpb',):
        target = binary / name
        if target.is_symlink() or (target.exists() and target.read_text() != wrapper):
            raise RuntimeError('Global command already exists; refusing to overwrite a different command')
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    if STATE.is_symlink() or STATE.stat().st_uid != os.getuid():
        raise RuntimeError('Unsafe state directory')
    STATE.chmod(0o700)
    key = STATE / 'id_ed25519'
    if any((STATE / name).is_symlink() for name in ('id_ed25519', 'id_ed25519.pub', 'ssh_config', 'config.json')):
        raise RuntimeError('Refusing symlinks in the private state directory')
    if not key.exists():
        run('ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', 'vpb-auth', '-f', str(key))
    key.chmod(0o600)
    parsed = {}
    for line in run('ssh', '-G', args.server).decode().splitlines():
        name, _, value = line.partition(' ')
        parsed.setdefault(name, value)
    if parsed.get('proxyjump', 'none') != 'none' or parsed.get('proxycommand', 'none') != 'none':
        raise RuntimeError('This installer requires a direct SSH host alias')
    hostname = parsed['hostname']
    if not re.fullmatch(r'[A-Za-z0-9_.:-]+', hostname):
        raise RuntimeError('Invalid host configuration')
    port = int(parsed['port'])
    if not 1 <= port <= 65535:
        raise RuntimeError('Invalid SSH port')
    ssh_config = STATE / 'ssh_config'
    ssh_config.write_text('\n'.join((
        'Host vpb-server', f'    HostName {hostname}', f'    Port {port}', '    User vpb-auth',
        f'    IdentityFile "{key}"', '    IdentitiesOnly yes', '    BatchMode yes',
        '    StrictHostKeyChecking yes', f'    UserKnownHostsFile "{Path.home() / ".ssh/known_hosts"}"',
        '    ForwardAgent no', '    ForwardX11 no', '    RequestTTY no',
        '    ServerAliveInterval 15', '    ServerAliveCountMax 2', '')))
    ssh_config.chmod(0o600)
    configuration = STATE / 'config.json'
    configuration.write_text(json.dumps({'version': 1, 'ssh_config': str(ssh_config), 'identity_file': str(key),
                                         'proxy_port': args.proxy_port}) + '\n')
    configuration.chmod(0o600)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
        for name in ('common.py', 'probe.py', 'server.py'):
            archive.add(ROOT / 'tools/vpb' / name, arcname=name)
        archive.add(ROOT / 'deploy/install-vpb-server.sh', arcname='install.sh')
        for name in ('extract.py', 'process.py'):
            archive.add(ROOT / 'video_pls_bot' / name, arcname=name)
        archive.add(key.with_suffix('.pub'), arcname='id_ed25519.pub')
        for filename, digest in (('extract.py', BASE_EXTRACT), ('process.py', BASE_PROCESS)):
            info = tarfile.TarInfo(f'base-{filename}.sha256')
            value = (digest + '\n').encode()
            info.size, info.mode = len(value), 0o600
            archive.addfile(info, io.BytesIO(value))
    command = ('set -eu; umask 077; staging=$(mktemp -d /opt/video-pls-bot/.vpb-install.XXXXXX); '
               'trap \'rm -rf "$staging"\' EXIT; tar -xzf - -C "$staging"; '
               f'VPB_PROXY_PORT={args.proxy_port} bash "$staging/install.sh"')
    print('Installing restricted SSH access and the server handler…', flush=True)
    run('ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=10',
        '-o', 'ForwardAgent=no', args.server, command, input=buffer.getvalue())
    status_request = b'{"version":1,"action":"status","platform":"youtube"}\n'
    result = json.loads(run('ssh', '-F', str(ssh_config), '-T', 'vpb-server', input=status_request))
    if not result.get('ok'):
        raise RuntimeError('Restricted helper did not pass its status check')
    binary.mkdir(parents=True, exist_ok=True)
    for name in ('vpb',):
        target = binary / name
        if target.is_symlink() or (target.exists() and target.read_text() != wrapper):
            raise RuntimeError('Global command already exists; refusing to overwrite a different command')
        target.write_text(wrapper)
        target.chmod(0o755)
    print('vpb installed. Check: vpb doctor. Login: vpb login youtube.')


if __name__ == '__main__':
    main()
