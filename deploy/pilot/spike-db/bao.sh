#!/usr/bin/env bash
# Spike secrets in the platform OpenBao (D-26). Never prints a value: `get` writes to stdout only
# for $(...) capture or a pipe; `gen` creates random passwords inside OpenBao's write body.
#
#   bao.sh gen  PATH FIELD...   generate 32-byte random values for FIELDs (skips if PATH exists)
#   bao.sh get  PATH FIELD      print one field (capture it, never echo it)
#   bao.sh put  PATH FIELD      read one value from stdin and patch it into PATH
#   bao.sh del  PATH            delete all versions of PATH (spike teardown)
#   bao.sh has  PATH            exit 0 if PATH exists (prints field names and "set", no values)
# PATH is under secret/ (KV v2), e.g. envs/spike-db/rds.
set -euo pipefail
umask 077
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PILOT_DIR="$(cd "$HERE/.." && pwd)"
CUSTODY_DIR="${BRAVO_KEY_CUSTODY_DIR:-$HOME/.bravo/custody}"
CUSTODY="$CUSTODY_DIR/openbao-init.json"
# Same target rule as deploy/pilot/scripts/keys.sh: custody .bao_target says where the one platform store is
# (pilot | platform-eks | platform-rke2). Platform targets are reached through a kubectl port-forward to
# openbao/openbao-0: BRAVO_BAO_LOCAL_PORT if a supervised forward already runs, else a private one per call.
BAO_TARGET="$(jq -r '.bao_target // "pilot"' "$CUSTODY")"
PF_PID=""
if [[ $BAO_TARGET == pilot ]]; then
  TLS_DIR="$PILOT_DIR/bao/tls"
  BAO_ADDR="${BAO_ADDR:-https://127.0.0.1:8200}"
else
  TLS_DIR="$CUSTODY_DIR/$BAO_TARGET"
  case $BAO_TARGET in platform-eks) CTX=bravo-platform ;; platform-rke2) CTX=bravo-platform-rke2 ;; *) echo "bao.sh: unknown target $BAO_TARGET" >&2; exit 1 ;; esac
  if [[ -n ${BRAVO_BAO_LOCAL_PORT:-} ]] && curl -s -o /dev/null --max-time 2 --cacert "$TLS_DIR/ca.crt" "https://127.0.0.1:$BRAVO_BAO_LOCAL_PORT/v1/sys/seal-status"; then
    BAO_ADDR="https://127.0.0.1:$BRAVO_BAO_LOCAL_PORT"
  else
    port="$(python3 -I -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1])')"
    kubectl --context "$CTX" -n openbao port-forward pod/openbao-0 "$port:8200" >/dev/null 2>&1 &
    PF_PID=$!
    trap 'kill "$PF_PID" 2>/dev/null || true' EXIT
    BAO_ADDR="https://127.0.0.1:$port"
    for _ in $(seq 1 40); do
      curl -s -o /dev/null --max-time 2 --cacert "$TLS_DIR/ca.crt" "$BAO_ADDR/v1/sys/seal-status" && break; sleep 0.25
    done
  fi
fi

die() { printf 'bao.sh: %s\n' "$*" >&2; exit 1; }
tok_hdr() { printf 'X-Vault-Token: %s\n' "$(jq -er .admin_token "$CUSTODY")"; }
api() { # METHOD PATH [content-type]; body on stdin when content-type given
  local method=$1 path=$2 ctype=${3-} out code
  if [[ -n $ctype ]]; then
    out="$(curl -sS --cacert "$TLS_DIR/ca.crt" --max-time 30 -X "$method" -H @<(tok_hdr) \
      -H "Content-Type: $ctype" --data-binary @- -w '\n%{http_code}' "$BAO_ADDR/v1/$path")" || die "cannot reach OpenBao"
  else
    out="$(curl -sS --cacert "$TLS_DIR/ca.crt" --max-time 30 -X "$method" -H @<(tok_hdr) \
      -w '\n%{http_code}' "$BAO_ADDR/v1/$path" </dev/null)" || die "cannot reach OpenBao"
  fi
  code="${out##*$'\n'}"; out="${out%$'\n'*}"
  [[ $code == 2* ]] || { printf 'bao.sh: %s %s -> HTTP %s\n' "$method" "$path" "$code" >&2; return 1; }
  printf '%s' "$out"
}

cmd=${1:?usage}; shift
case $cmd in
  gen)
    path=$1; shift
    if api GET "secret/data/$path" >/dev/null 2>&1; then echo "bao.sh: secret/$path exists, kept"; exit 0; fi
    for f in "$@"; do printf '%s\t%s\n' "$f" "$(openssl rand -base64 32 | tr -d '/+=\n' | cut -c1-32)"; done \
      | jq -Rn '{data: ([inputs | split("\t") | {(.[0]): .[1]}] | add)}' \
      | api POST "secret/data/$path" application/json | jq -r '"bao.sh: wrote secret/'"$path"' version \(.data.version)"'
    ;;
  get) api GET "secret/data/$1" | jq -er --arg f "$2" '.data.data[$f]' ;;
  put) jq -Rs --arg f "$2" '{data: {($f): .}}' | api PATCH "secret/data/$1" application/merge-patch+json >/dev/null \
         && echo "bao.sh: patched secret/$1 field $2" ;;
  has) api GET "secret/data/$1" | jq -c '.data.data | to_entries | map("\(.key)=set")' ;;
  del) api DELETE "secret/metadata/$1" >/dev/null && echo "bao.sh: deleted secret/$1 (all versions)" ;;
  *) die "unknown command $cmd" ;;
esac
