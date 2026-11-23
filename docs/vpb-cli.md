# `vpb`: cookie login CLI

Chrome runs only on the admin's Mac. The server uses SSH, systemd credentials and the
existing yt-dlp; it needs no Chrome, Chromium, Selenium or browser containers.

```sh
vpb doctor
vpb status                  # YouTube by default
vpb check youtube
vpb check tiktok
vpb login youtube
vpb login tiktok
vpb check youtube --url 'https://www.youtube.com/watch?v=pySJJbS-1oo'
vpb --json check youtube    # Script-friendly result, exit 0/1
```

The command works from any directory after a one-time installation:

```sh
VPB_SSH_ALIAS=my-server-root VPB_PROXY_PORT=3128 ./deploy/install-vpb.sh
```

`VPB_SSH_ALIAS` is an existing, trusted SSH alias with root access to the server (used
only during installation); `VPB_PROXY_PORT` is the loopback port of the download proxy on
the server (the one in `DOWNLOAD_PROXY`). The same values can be passed as `--server` and
`--proxy-port`.

## Login

1. In a normal Terminal run `vpb login youtube` or `vpb login tiktok`.
2. A separate Chrome window opens through an SSH tunnel to the download proxy. A fresh
   temporary profile is created for this attempt. During the login Chrome runs without
   CDP, remote debugging, headless mode or incognito.
3. Log in and pass any verification the platform shows. Make sure the reference video
   opens, then press Enter in the Terminal.
4. The CLI closes only the Chrome it started, so the browser saves its cookies. A separate
   local Chrome process then reads the cookies of that temporary profile over a private
   pipe, without opening login pages. The temporary profile is deleted afterwards. If you
   closed the window yourself, Enter is still required to transfer the cookies.
5. Cookies are sent over SSH stdin and checked by the server's yt-dlp. The check fetches
   metadata only; it does not download the video or send messages.
6. Only after a successful check is the new credential encrypted, verified by decrypting
   it in memory and installed atomically. The old one stays encrypted for rollback.
7. A graceful restart of the bots starts. Accepted jobs are finished first, so with a long
   queue activation may take a while. `vpb status` shows whether the running process has
   loaded the new credential.

Ctrl+C cancels the local session and closes its Chrome and SSH tunnel. Passwords and 2FA
are entered only on the platform's page; the CLI never reads them. If SSH drops during
installation, check `vpb status` first: the server may have finished the operation. A new
`login` starts a new session.

A platform may ask for verification again; moving cookies does not guarantee that an IP
or account restriction is lifted. If a candidate is rejected, the working credential stays.

## Security

- A dedicated SSH user `vpb-auth` and a dedicated Ed25519 key. The CLI never uses a root
  key, SFTP, a remote shell or arbitrary commands.
- The key allows only a fixed JSON handler and local forwarding to the loopback port of
  the download proxy (`VPB_PROXY_PORT`). PTY, agent/X11/remote forwarding and tunnels are
  disabled.
- Server host key checking is mandatory. The key and configuration on the Mac are 0600
  and live in the ignored directory `secrets/vpb-auth` (0700). The private SSH key never
  leaves the Mac; the installer sends only the public key.
- During the manual login a plain Chrome runs without automation. After the login and a
  clean exit of that process, a separate local reader uses a private pipe
  (`--remote-debugging-pipe`), with no debugging port. It reads only the temporary profile
  created by this CLI and opens no login pages. Your personal Chrome profile and its
  cookies are never read. No export extensions are needed.
- Only `youtube.com` and its subdomains, or only `tiktok.com` and its subdomains, are sent
  to the server; Google and other domains are filtered out on the Mac. The server checks
  domains, fields, expiry, presence of a login and the request size again.
- Values never appear in process arguments, shell history, logs or stdout. Transport is
  SSH; storage is encrypted with systemd-creds using the server key and bound to the
  credential name. Decrypted candidates live in private directories on tmpfs
  `/run/vpb-auth` and are removed after the check.
- The bots receive the credential through systemd. yt-dlp's working cookie jars are
  created only in `/run/video-pls-bot-cookies` (tmpfs) with 0600/0700 permissions. On
  cancellation the parent kills the process group and removes that worker's jars; systemd
  removes the runtime directory when the service stops.
- The previous ciphertext: `/etc/video-pls-bot/PLATFORM-cookies.txt.cred.previous`. The
  protocol has no command to export or read cookies back to the Mac.

Storage protection depends on the integrity of the Mac and the server: root on the server
can access the systemd key and the working credentials. This is not a defense against a
fully compromised host.

## Installation and layout

`deploy/install-vpb.sh` is the local entry point. The installer uses the trusted SSH alias
(`VPB_SSH_ALIAS`) once to create the restricted access. Before changing the downloader
code it checks the hashes of the original files; all other changes of the current release
are kept. A new release is created and the previous one stays in place. The SSH
configuration is validated with `sshd -t` before reload; root login settings are not
changed.

On the Mac: `tools/vpb/cli.py`, `common.py`, `bootstrap.py` and the global wrapper
`~/.local/bin/vpb`. Requires Chrome, the project's `.venv` Python and SSH. No Docker.

On the server: `/opt/video-pls-bot/auth-tools` points to the tools version and
`/usr/local/sbin/vpb-auth` is the fixed root-owned entry point. Checks run as
`video-pls-bot` in a separate restricted systemd unit with the same
`EnvironmentFile=/etc/video-pls-bot/video-pls-bot.env` as the bots. Check and install
requests are serialized with a file lock. The ciphertext is written with 0600
permissions, fsync and an atomic rename.

Protocol version 1: `status`, `check`, `install`; platforms `youtube`, `tiktok`. This is a
local administration tool, not a public network service.

## If Google says "This browser or app may not be secure"

The first version of the CLI attached CDP and created a private context before the
manual login. The CLI now opens a plain Chrome window without automation for the login.
Before retrying, stop the old attempt with Ctrl+C and run `vpb login youtube` again: an
already running process does not pick up the updated file.

Google may also restrict the account or the IP. The launch change does not guarantee a
successful login: a new credential is still installed only after a successful server
check.
