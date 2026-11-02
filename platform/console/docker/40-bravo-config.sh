#!/bin/sh
# Writes the console runtime config and the read-only service proxy from the environment.
# No default API address: the container stops at start when BRAVO_API_BASE is missing or malformed.
# Proxy upstreams default to in-cluster service names; observability links default to empty (not shown).
set -eu

check_url() {
    # $1 name, $2 value
    case "$2" in
        http://*|https://*) ;;
        *) echo "40-bravo-config: $1 must start with http:// or https://" >&2; exit 1 ;;
    esac
    case "$2" in
        *'"'*|*'\'*|*' '*|*'<'*|*'>'*|*';'*|*'$'*|*'{'*|*'}'*) echo "40-bravo-config: $1 has characters not allowed in a URL" >&2; exit 1 ;;
    esac
}

if [ -z "${BRAVO_API_BASE:-}" ]; then
    echo "40-bravo-config: BRAVO_API_BASE is not set (API origin the browser calls)" >&2
    exit 1
fi
check_url BRAVO_API_BASE "$BRAVO_API_BASE"

# Links the browser opens directly (empty = not shown as a link).
GRAFANA_URL="${BRAVO_GRAFANA_URL-}"
PROMETHEUS_URL="${BRAVO_PROMETHEUS_URL-}"
TEMPO_URL="${BRAVO_TEMPO_URL-}"
# Customer demo app's public name (CLOUD-E2E-CONTRACT §6). b-free itself is the platform.
PUBLIC_FRONT_HOST="${BRAVO_PUBLIC_FRONT_HOST-b-free-chat.mangowhois.ai}"
# Where the demo app runs: <collector cluster id or kubectl context>=<public host>, comma separated.
APP_ENVS="${BRAVO_APP_ENVS-source-aws=b-free-chat-src.mangowhois.ai,target-onprem=b-free-chat-dst.mangowhois.ai}"
# Public DNS-over-HTTPS resolver (JSON API) used to show whether the edge names resolve. Empty = not shown.
DOH_URL="${BRAVO_DOH_URL-https://cloudflare-dns.com/dns-query}"
for pair in "BRAVO_GRAFANA_URL=$GRAFANA_URL" "BRAVO_PROMETHEUS_URL=$PROMETHEUS_URL" "BRAVO_TEMPO_URL=$TEMPO_URL" "BRAVO_DOH_URL=$DOH_URL"; do
    name="${pair%%=*}"; value="${pair#*=}"
    [ -n "$value" ] && check_url "$name" "$value"
done
case "$APP_ENVS" in
    *[!a-zA-Z0-9.,=_-]*) echo "40-bravo-config: BRAVO_APP_ENVS must be id=host pairs separated by commas" >&2; exit 1 ;;
esac
case "$PUBLIC_FRONT_HOST" in
    *[!a-zA-Z0-9.-]*) echo "40-bravo-config: BRAVO_PUBLIC_FRONT_HOST must be a host name" >&2; exit 1 ;;
esac

# Upstreams the console proxies under /svc/<name>/ (GET and HEAD only, see below).
COLLECTOR_UP="${BRAVO_COLLECTOR_UPSTREAM:-http://collector:8083}"
GATE_UP="${BRAVO_GATE_UPSTREAM:-http://gate:8084}"
PROMETHEUS_UP="${BRAVO_PROMETHEUS_UPSTREAM:-http://prometheus:9090}"
RESOLVER="${BRAVO_DNS_RESOLVER:-127.0.0.11}"
for pair in "BRAVO_COLLECTOR_UPSTREAM=$COLLECTOR_UP" "BRAVO_GATE_UPSTREAM=$GATE_UP" "BRAVO_PROMETHEUS_UPSTREAM=$PROMETHEUS_UP"; do
    check_url "${pair%%=*}" "${pair#*=}"
done
case "$RESOLVER" in
    *[!0-9a-fA-F.:\[\]]*) echo "40-bravo-config: BRAVO_DNS_RESOLVER must be an IP address" >&2; exit 1 ;;
esac

mkdir -p /tmp/bravo
printf '{"apiBase":"%s","grafanaUrl":"%s","prometheusUrl":"%s","tempoUrl":"%s","publicFrontHost":"%s","dohUrl":"%s","appEnvs":"%s"}\n' \
    "$BRAVO_API_BASE" "$GRAFANA_URL" "$PROMETHEUS_URL" "$TEMPO_URL" "$PUBLIC_FRONT_HOST" "$DOH_URL" "$APP_ENVS" > /tmp/bravo/config.json

# Read-only proxy. limit_except denies every method except GET/HEAD, and no client
# credentials or cookies are forwarded, so these routes cannot change anything upstream.
{
    printf 'resolver %s valid=10s ipv6=off;\n' "$RESOLVER"
    for pair in "collector=$COLLECTOR_UP" "gate=$GATE_UP" "prometheus=$PROMETHEUS_UP"; do
        name="${pair%%=*}"; up="${pair#*=}"; up="${up%/}"
        # The gate measures live when its report is read (about 16 s per environment pair, up to ~60 s).
        read_timeout=20s; [ "$name" = gate ] && read_timeout=90s
        cat <<CONF
location /svc/$name/ {
    limit_except GET { deny all; }
    set \$bravo_up "$up";
    rewrite ^/svc/$name/(.*)\$ /\$1 break;
    proxy_pass \$bravo_up;
    proxy_set_header Authorization "";
    proxy_set_header Cookie "";
    proxy_set_header Cf-Access-Jwt-Assertion "";
    proxy_hide_header Set-Cookie;
    proxy_connect_timeout 2s;
    proxy_read_timeout $read_timeout;
    include /etc/nginx/bravo-headers.conf;
    add_header Cache-Control "no-store" always;
}
CONF
    done
} > /tmp/bravo/proxy.conf
echo "40-bravo-config: apiBase=$BRAVO_API_BASE proxy=collector,gate,prometheus"
