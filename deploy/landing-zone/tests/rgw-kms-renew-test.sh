#!/usr/bin/env bash
# Offline test of the RGW SSE-KMS token renewer (ConfigMap rook-ceph/rgw-kms-renew in overlays/rke2/rook/rgw-kms-link.yaml).
# The script runs as written in the manifest with a stub curl first on PATH. Checks:
#   1. renew-self 200: the lease comes from auth.lease_duration (not the top-level 0), the token never reaches stdout,
#      and the token reaches curl only on stdin as the X-Vault-Token header.
#   2. lease under 1h (close to the 168h limit): the re-issue hint is printed.
#   3. 403 (revoked or expired token): exit 1.  4. curl cannot connect (link down): exit 1 with the link message.
#   bash deploy/landing-zone/tests/rgw-kms-renew-test.sh
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
awk '/^  renew.sh: \|/ {on=1; next} on && /^---/ {exit} on {sub(/^    /, ""); print}' \
  "$here/overlays/rke2/rook/rgw-kms-link.yaml" >"$work/renew.sh"
grep -q 'renew-self' "$work/renew.sh" || { echo "FAIL: renew.sh not found in the manifest"; exit 1; }
printf 's.TESTTOKEN0123456789abcd' >"$work/token"; : >"$work/ca"
mkdir "$work/bin"
cat >"$work/bin/curl" <<'STUB'
#!/bin/sh
# Stub: records the header from stdin, answers like OpenBao renew-self (STUB_MODE: ok | short | denied | down).
cat >"$STUB_DIR/header"
case $STUB_MODE in
  down) echo "curl: (7) Failed to connect to openbao port 8200" >&2; exit 7 ;;
  denied) printf '{"errors":["permission denied"]}\n\n403' ;;
  *) l=3600; [ "$STUB_MODE" = short ] && l=1200
     printf '{"request_id":"x","lease_id":"","renewable":false,"lease_duration":0,"data":null,"wrap_info":null,"warnings":null,"auth":{"client_token":"s.TESTTOKEN0123456789abcd","accessor":"a","policies":["platform-rgw-kms-target-onprem"],"lease_duration":%s,"renewable":true}}\n\n200' "$l" ;;
esac
STUB
chmod +x "$work/bin/curl"
run() { STUB_MODE=$1 STUB_DIR=$work PATH="$work/bin:$PATH" RGW_KMS_TOKEN_FILE="$work/token" RGW_KMS_CA_FILE="$work/ca" \
  RGW_KMS_ADDR=https://openbao:8200 sh "$work/renew.sh"; }
fail() { echo "FAIL: $*"; exit 1; }

out="$(run ok)" || fail "ok: exit $?"
[[ $out == *"renew-self HTTP 200, lease 3600s"* ]] || fail "ok: $out"
[[ $out != *TESTTOKEN* ]] || fail "ok: token on stdout"
[[ $out != *"near the 168h limit"* ]] || fail "ok: hint printed at 3600s"
[[ $(cat "$work/header") == "X-Vault-Token: s.TESTTOKEN0123456789abcd" ]] || fail "ok: header on stdin"
out="$(run short)" || fail "short: exit $?"
[[ $out == *"lease 1200s"* && $out == *"near the 168h limit"* ]] || fail "short: $out"
if out="$(run denied)"; then fail "denied: exit 0"; fi
[[ $out == *"HTTP 403"* && $out != *TESTTOKEN* ]] || fail "denied: $out"
if out="$(run down 2>/dev/null)"; then fail "down: exit 0"; fi
[[ $out == *"not reachable through the link"* ]] || fail "down: $out"
echo "PASS rgw-kms-renew: lease from auth, token kept off stdout, hint under 1h, 403 and link-down exit 1"
