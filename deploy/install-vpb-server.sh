#!/bin/bash
set -euo pipefail
umask 077
test "$(id -u)" = 0
# Loopback port of the bot's download proxy; the only port vpb-auth may forward to.
proxy_port=${VPB_PROXY_PORT:?set VPB_PROXY_PORT}
case "$proxy_port" in ''|*[!0-9]*) echo 'Invalid VPB_PROXY_PORT' >&2; exit 1 ;; esac
bundle=$(cd -- "$(dirname -- "$0")" && pwd)
base=/opt/video-pls-bot
current=$(readlink -f "$base/current")
for file in extract.py process.py; do
  expected=$(cat "$bundle/base-$file.sha256")
  actual=$(sha256sum "$current/video_pls_bot/$file" | cut -d' ' -f1)
  replacement=$(sha256sum "$bundle/$file" | cut -d' ' -f1)
  if [ "$actual" != "$expected" ] && [ "$actual" != "$replacement" ]; then
    echo 'Current download code changed; refusing to overwrite it.' >&2
    exit 1
  fi
done
test "$(findmnt -n -o FSTYPE --target /run)" = tmpfs
/usr/bin/ssh-keygen -l -f "$bundle/id_ed25519.pub" >/dev/null
stamp=$(date -u +%Y%m%dT%H%M%SZ)
release="$base/auth-releases/$stamp"
install -d -m 0755 "$base/auth-releases" "$release"
for file in common.py probe.py server.py; do
  install -m 0644 "$bundle/$file" "$release/$file"
done
if ! id vpb-auth >/dev/null 2>&1; then
  useradd --system --create-home --home-dir /var/lib/vpb-auth --shell /bin/sh vpb-auth
fi
test "$(getent passwd vpb-auth | cut -d: -f6)" = /var/lib/vpb-auth
install -d -m 0755 -o root -g root /var/lib/vpb-auth /var/lib/vpb-auth/.ssh
read -r key_type key_body key_comment < "$bundle/id_ed25519.pub"
test "$key_type" = ssh-ed25519
printf 'restrict,port-forwarding,permitopen="127.0.0.1:%s",command="sudo -n /usr/local/sbin/vpb-auth" %s %s vpb-auth\n' "$proxy_port" "$key_type" "$key_body" > "$bundle/authorized_keys"
install -m 0644 -o root -g root "$bundle/authorized_keys" /var/lib/vpb-auth/.ssh/authorized_keys
cat > "$bundle/launcher" <<'EOF'
#!/bin/sh
export PYTHONDONTWRITEBYTECODE=1
exec /opt/video-pls-bot/venv/bin/python /opt/video-pls-bot/auth-tools/server.py
EOF
install -m 0755 -o root -g root "$bundle/launcher" /usr/local/sbin/vpb-auth
printf 'vpb-auth ALL=(root) NOPASSWD: /usr/local/sbin/vpb-auth ""\n' > "$bundle/sudoers"
visudo -cf "$bundle/sudoers" >/dev/null
install -m 0440 -o root -g root "$bundle/sudoers" /etc/sudoers.d/vpb-auth
cat > "$bundle/ssh.conf" <<EOF
Match User vpb-auth
    AllowUsers vpb-auth
    AuthenticationMethods publickey
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    AllowTcpForwarding local
    AllowStreamLocalForwarding no
    PermitOpen 127.0.0.1:$proxy_port
    PermitListen none
    AllowAgentForwarding no
    X11Forwarding no
    PermitTTY no
    PermitUserRC no
    PermitTunnel no
    MaxSessions 2
Match all
EOF
ssh_dropin=/etc/ssh/sshd_config.d/60-vpb-auth.conf
had_dropin=0
if [ -f "$ssh_dropin" ]; then
  cp "$ssh_dropin" "$bundle/ssh.conf.previous"
  had_dropin=1
fi
install -m 0644 -o root -g root "$bundle/ssh.conf" "$ssh_dropin"
if ! /usr/sbin/sshd -t; then
  if [ "$had_dropin" = 1 ]; then
    cp "$bundle/ssh.conf.previous" "$ssh_dropin"
  else
    rm -f "$ssh_dropin"
  fi
  exit 1
fi
ln -sfn "$release" "$base/auth-tools"
systemctl reload ssh.service
# Preserve all current application changes; replace only the reviewed extractor.
bot_release="$base/releases/$stamp-vpb-auth"
cp -a "$current" "$bot_release"
for file in extract.py process.py; do
  install -m 0644 "$bundle/$file" "$bot_release/video_pls_bot/$file"
done
install -d -m 0755 /etc/systemd/system/video-pls-bot.service.d
cat > /etc/systemd/system/video-pls-bot.service.d/cookie-runtime.conf <<'EOF'
[Service]
RuntimeDirectory=video-pls-bot-cookies
RuntimeDirectoryMode=0700
Environment=COOKIE_TEMP_DIR=/run/video-pls-bot-cookies
EOF
ln -sfn "$bot_release" "$base/current"
systemctl daemon-reload
systemctl --no-block restart video-pls-bot.service
printf 'VPB server installed; browser remains local.\n'
