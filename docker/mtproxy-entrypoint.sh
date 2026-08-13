#!/bin/sh
set -eu

if [ "${#SECRET}" -ne 32 ]; then
    echo "SECRET must contain exactly 32 hexadecimal characters" >&2
    exit 1
fi
case "$SECRET" in
    *[!0-9a-fA-F]*)
        echo "SECRET must contain exactly 32 hexadecimal characters" >&2
        exit 1
        ;;
esac

WORKERS="${WORKERS:-2}"
case "$WORKERS" in
    ''|*[!0-9]*)
        echo "WORKERS must be a positive integer" >&2
        exit 1
        ;;
esac
if [ "$WORKERS" -lt 1 ]; then
    echo "WORKERS must be a positive integer" >&2
    exit 1
fi

mkdir -p /data

fetch_file() {
    url="$1"
    destination="$2"
    temporary="${destination}.tmp"
    if curl --fail --silent --show-error --location \
        --connect-timeout 15 --retry 3 "$url" --output "$temporary"; then
        chmod 0644 "$temporary"
        mv -f "$temporary" "$destination"
        return 0
    fi
    rm -f "$temporary"
    return 1
}

refresh_failed=0
fetch_file "https://core.telegram.org/getProxySecret" "/data/proxy-secret" || refresh_failed=1
fetch_file "https://core.telegram.org/getProxyConfig" "/data/proxy-multi.conf" || refresh_failed=1

if [ "$refresh_failed" -ne 0 ]; then
    if [ ! -s /data/proxy-secret ] || [ ! -s /data/proxy-multi.conf ]; then
        echo "Could not download the initial Telegram proxy configuration" >&2
        exit 1
    fi
    echo "Warning: using the last saved Telegram proxy configuration" >&2
fi

exec /usr/local/bin/mtproto-proxy \
    -u nobody \
    -p 8888 \
    -H 443 \
    -S "$SECRET" \
    --aes-pwd /data/proxy-secret /data/proxy-multi.conf \
    -M "$WORKERS"
