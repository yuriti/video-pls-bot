#!/usr/bin/env bash
set -euo pipefail
# Run as root from an unpacked release. Credentials are provisioned separately.
test "$(id -u)" = 0
root=/opt/video-pls-bot
release="${1:?Release directory required}"
test -f "$release/video_pls_bot/app.py"
for name in video-pls-bot video-pls-api; do
  id "$name" >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin "$name"
done
install -d -m 0755 "$root"
install -d -m 0700 /etc/video-pls-bot
install -d -m 0700 -o video-pls-bot -g video-pls-bot /var/lib/video-pls-bot /var/cache/video-pls-bot
chgrp telegram-bot-api /var/cache/video-pls-bot
chmod 0711 /var/cache/video-pls-bot
chmod 0755 "$release/deploy/video-pls-api-route.sh"
if ! python3 -m venv "$root/venv"; then
  apt-get update -qq
  apt-get install -y -qq python3-venv
  python3 -m venv "$root/venv"
fi
"$root/venv/bin/pip" install --disable-pip-version-check -q -r "$release/requirements.lock"
chown -R root:root "$release" "$root/venv"
chmod -R go-w "$release" "$root/venv"
ln -sfn "$release" "$root/current"
install -m 0644 "$release/deploy/video-pls-bot.service" /etc/systemd/system/video-pls-bot.service
install -m 0644 "$release/deploy/video-pls-health.service" /etc/systemd/system/video-pls-health.service
install -m 0644 "$release/deploy/video-pls-health.timer" /etc/systemd/system/video-pls-health.timer
install -m 0644 "$release/deploy/video-pls-daily.service" /etc/systemd/system/video-pls-daily.service
install -m 0644 "$release/deploy/video-pls-daily.timer" /etc/systemd/system/video-pls-daily.timer
install -m 0644 "$release/deploy/video-pls-api.service" /etc/systemd/system/video-pls-api.service
install -m 0644 "$release/deploy/video-pls-api-route.service" /etc/systemd/system/video-pls-api-route.service
install -m 0755 "$release/deploy/video-pls-stats" /usr/local/bin/video-pls-stats
# Optional: a non-root operator account allowed to read the statistics.
if [ -n "${STATS_SUDO_USER:-}" ] && id "$STATS_SUDO_USER" >/dev/null 2>&1; then
  printf '%s ALL=(video-pls-bot) NOPASSWD: /usr/local/bin/video-pls-stats\n' "$STATS_SUDO_USER" > /etc/sudoers.d/video-pls-stats
  chmod 0440 /etc/sudoers.d/video-pls-stats
  visudo -cf /etc/sudoers.d/video-pls-stats >/dev/null
fi
# Optional site-specific additions kept outside this repository (proxy unit, drop-ins, ...):
# an executable script that receives the release directory.
if [ -n "${VIDEO_PLS_SITE_HOOK:-}" ]; then
  "$VIDEO_PLS_SITE_HOOK" "$release"
fi
systemctl daemon-reload
test -f /etc/video-pls-bot/video-pls-bot.env || echo 'Missing /etc/video-pls-bot/video-pls-bot.env (see .env.example).' >&2
echo 'Release installed. Provision credentials before starting the services.'
