#!/usr/bin/env bash
set -euo pipefail
case "${1:-}" in
  start)
    # Loopback port of the proxy's transparent TCP inbound (EnvironmentFile of the unit).
    port=${TELEGRAM_API_REDIRECT_PORT:?set TELEGRAM_API_REDIRECT_PORT}
    case "$port" in ''|*[!0-9]*) exit 2 ;; esac
    api_uid=$(id -u video-pls-api)
    if nft list table ip video_pls_api >/dev/null 2>&1; then
      nft delete table ip video_pls_api
    fi
    nft -f - <<EOF
table ip video_pls_api {
  chain output {
    type nat hook output priority -110; policy accept;
    meta skuid $api_uid ip daddr != 127.0.0.0/8 meta l4proto tcp counter redirect to :$port
  }
}
EOF
    ;;
  stop)
    if nft list table ip video_pls_api >/dev/null 2>&1; then nft delete table ip video_pls_api; fi
    ;;
  *) exit 2 ;;
esac
