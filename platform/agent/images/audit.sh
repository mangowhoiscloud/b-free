#!/bin/sh
# Audit the agent sandbox images against their baselines.
# Usage: images/audit.sh [--write-baseline] [name ...]   (default: all five images)
# Per image:
#   1. executables in PATH dirs == <name>/expected-binaries.txt  (listed by the image's own /bin/sh)
#   2. runtime uid is 65532
#   3. no forbidden tool (package managers, extra shells, network clients, su/sudo) anywhere in the filesystem
#   4. no setuid/setgid files (checked on `docker export` of the image filesystem)
#   5. smoke: each shipped CLI runs with --network none --read-only --user 65532:65532
# Exit status is non-zero on any mismatch.
set -eu

here=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
ALL="discovery-agent governance-agent app-migration-agent db-agent ci-agent"
FORBIDDEN="apt apt-get dpkg pip pip3 bash perl curl wget nc ssh sudo su"
PATH_DIRS="/bin /sbin /usr/bin /usr/sbin /usr/local/bin /usr/local/sbin"

write_baseline=0
if [ "${1:-}" = "--write-baseline" ]; then write_baseline=1; shift; fi
names=${*:-$ALL}

tmp=$(mktemp -d)
cid=""
cleanup() { [ -n "$cid" ] && docker rm -f "$cid" >/dev/null 2>&1; rm -rf "$tmp"; }
trap cleanup EXIT INT TERM

fail=0
bad() { echo "  FAIL: $*"; fail=1; }
ok() { echo "  ok:   $*"; }

# Uses only shell builtins, so it works in busybox-only images.
LIST_CMD='for d in '"$PATH_DIRS"'; do for f in "$d"/*; do if [ -f "$f" ] && [ -x "$f" ]; then echo "${f##*/}"; fi; done; done'
UID_CMD='while read -r k v rest; do if [ "$k" = "Uid:" ]; then echo "$v"; fi; done < /proc/self/status'

RUN_HARDENED="docker run --rm --network none --read-only --user 65532:65532 \
  --tmpfs /work:uid=65532,gid=65532,mode=0700 --tmpfs /out:uid=65532,gid=65532,mode=0700"

KC_ARGS="-strict -summary -schema-location /opt/bravo/schemas/{{.ResourceKind}}{{.KindSuffix}}.json -schema-location /opt/bravo/schemas/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json"
ZERO_DIGEST="sha256:0000000000000000000000000000000000000000000000000000000000000000"

smoke_cmd() {
  case "$1" in
    discovery-agent)
      echo 'busybox --help | { read -r l; echo "$l"; }; rg --version | { read -r l; echo "$l"; }; jq --version; echo ok | cat; ls / >/dev/null' ;;
    governance-agent)
      echo 'jq --version; echo "kubeconform $(kubeconform -v)"; echo aGk= | base64 -d >/dev/null; diff /src/k8s/configmap.yaml /src/k8s/configmap.yaml;
            kubeconform '"$KC_ARGS"' /src/k8s/ && ! kubeconform '"$KC_ARGS"' /src/k8s-invalid/ >/dev/null' ;;
    app-migration-agent)
      echo 'python3 --version; rg --version | { read -r l; echo "$l"; }; jq --version; diff --version | { read -r l; echo "$l"; };
            python3 -c "import ensurepip" 2>/dev/null && exit 1; true' ;;
    ci-agent)
      echo 'python3 --version; python3 -m pytest --version; echo "helm $(helm version --short)"; hadolint --version;
            echo "kubeconform $(kubeconform -v)"; syft version | { while read -r k v; do [ "$k" = "Version:" ] && echo "syft $v"; done; true; };
            trivy --version | { read -r l; echo "trivy $l"; }; jq --version;
            kubeconform '"$KC_ARGS"' /src/k8s/;
            helm template t /opt/bravo/charts/bravo-app --set-string imageRef=registry.invalid/render-check@'"$ZERO_DIGEST"' > /work/rendered.yaml && echo "helm template ok";
            trivy config --skip-check-update --quiet /opt/bravo/charts/bravo-app > /work/tc.txt && echo "trivy config offline ok";
            trivy fs --scanners misconfig --skip-db-update --skip-check-update --offline-scan --quiet /opt/bravo/charts/bravo-app > /work/tf.txt && echo "trivy fs misconfig offline ok";
            python3 -c "import ensurepip" 2>/dev/null && exit 1; true' ;;
    db-agent)
      echo 'psql --version; pg_dump --version; pg_restore --version; pg_isready --version' ;;
  esac
}

for n in $names; do
  tag="bravo/agent-sandbox-$n:dev"
  echo "== $n ($tag)"
  if ! docker image inspect "$tag" >/dev/null 2>&1; then bad "image not built"; continue; fi

  # 1. PATH executables vs baseline
  docker run --rm --network none --entrypoint /bin/sh "$tag" -c "$LIST_CMD" | LC_ALL=C sort -u > "$tmp/$n.actual"
  if [ "$write_baseline" -eq 1 ]; then
    cp "$tmp/$n.actual" "$here/$n/expected-binaries.txt"
    ok "baseline written ($(wc -l < "$here/$n/expected-binaries.txt" | tr -d ' ') names)"
  fi
  if [ ! -f "$here/$n/expected-binaries.txt" ]; then
    bad "missing $n/expected-binaries.txt"
  elif diff -u "$here/$n/expected-binaries.txt" "$tmp/$n.actual" > "$tmp/$n.diff"; then
    ok "PATH executables match baseline ($(wc -l < "$tmp/$n.actual" | tr -d ' '))"
  else
    bad "PATH executables differ from baseline:"; sed 's/^/        /' "$tmp/$n.diff"
  fi

  # 2. runtime uid (image default user, not overridden)
  uid=$(docker run --rm --network none --entrypoint /bin/sh "$tag" -c "$UID_CMD")
  if [ "$uid" = "65532" ]; then ok "uid 65532"; else bad "uid is '$uid', want 65532"; fi

  # 3 + 4. whole-filesystem checks on the exported image
  cid=$(docker create "$tag")
  docker export "$cid" | tar -tvf - > "$tmp/$n.tar.lst"
  docker rm -f "$cid" >/dev/null; cid=""
  hits=$(awk -v forb=" $FORBIDDEN " '
    { t = substr($1, 1, 1)
      if (t == "l") name = $(NF - 2); else name = $NF
      sub(/.*\//, "", name)
      if ((t == "l" || (t == "-" && $1 ~ /x/)) && index(forb, " " name " ") > 0) print (t == "l" ? $(NF - 2) : $NF) }
  ' "$tmp/$n.tar.lst")
  if [ -z "$hits" ]; then ok "no forbidden tools ($FORBIDDEN)"; else bad "forbidden tools present: $(echo "$hits" | tr '\n' ' ')"; fi
  suid=$(awk '$1 ~ /^-..[sS]/ || $1 ~ /^-.....[sS]/ { print $NF }' "$tmp/$n.tar.lst")
  if [ -z "$suid" ]; then ok "no setuid/setgid files"; else bad "setuid/setgid files: $(echo "$suid" | tr '\n' ' ')"; fi

  # 5. smoke test under the hardened runtime profile
  if [ "$write_baseline" -eq 0 ]; then
    if out=$($RUN_HARDENED -v "$here/testdata:/src:ro" "$tag" /bin/sh -c "set -e; $(smoke_cmd "$n")" 2>&1); then
      ok "smoke:"; echo "$out" | sed 's/^/        /'
    else
      bad "smoke failed:"; echo "$out" | sed 's/^/        /'
    fi
  fi
done

if [ "$fail" -ne 0 ]; then echo "AUDIT FAILED"; exit 1; fi
echo "AUDIT PASSED"
