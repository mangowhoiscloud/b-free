#!/usr/bin/env bash
# Init entry for the PostgreSQL 16 Docker Official Image (/docker-entrypoint-initdb.d). Runs once, on an
# empty data directory, against the temporary server (unix socket only).
#
# Required environment:
#   BRAVO_SIDE              src | dst
#   BRAVO_SCHEMA_DIR        directory holding 01_schema.sql, 02_roles.sql, 03_<side>.sql
#   APP_WRITER_PASSWORD, REPLICATOR_PASSWORD, EXECUTOR_CTL_PASSWORD, EXECUTOR_OBSERVE_PASSWORD
# Optional:
#   BRAVO_ATTACHMENT_STORE_ID  active object store id for attachments (default: BRAVO_SIDE)
#   BRAVO_PLANT_REGRESSION     1 = plant the demo regression on dst (심은 시나리오, see 03_dst.sql)
#
# Secrets (D-26): no password is put in a psql argument or in SQL text. Each password is turned
# into a SCRAM-SHA-256 verifier here, on the client side; psql reads the verifier with \getenv and
# the server only ever receives the verifier.
# The file is executable, so the image entrypoint runs it in its own shell (not sourced).
set -Eeuo pipefail

bravo_schema_dir="${BRAVO_SCHEMA_DIR:?bravo-init: BRAVO_SCHEMA_DIR is required}"

bravo_psql() {
  psql -v ON_ERROR_STOP=1 --no-psqlrc --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" "$@"
}

# Prints the SCRAM-SHA-256 verifier (RFC 5802/7677, PostgreSQL format, 4096 iterations, 16-byte
# random salt) of the password held in the environment variable named $1. The password is read
# from the environment, never from argv. Printable ASCII only, where SASLprep is the identity.
scram_verifier() {
  BRAVO_PW_VAR="$1" perl -MDigest::SHA=hmac_sha256,sha256 -MMIME::Base64=encode_base64 -e '
    my $pw = $ENV{$ENV{BRAVO_PW_VAR}};
    die "bravo-init: $ENV{BRAVO_PW_VAR} is empty\n" unless defined $pw && length $pw;
    die "bravo-init: $ENV{BRAVO_PW_VAR} must be printable ASCII\n" unless $pw =~ /\A[\x21-\x7e]+\z/;
    open(my $rnd, "<:raw", "/dev/urandom") or die "bravo-init: /dev/urandom: $!\n";
    read($rnd, my $salt, 16) == 16 or die "bravo-init: short read from /dev/urandom\n";
    close $rnd;
    my $iter = 4096;
    my $u = hmac_sha256($salt . pack("N", 1), $pw);
    my $salted = $u;
    for (2 .. $iter) { $u = hmac_sha256($u, $pw); $salted ^= $u; }
    my $stored = sha256(hmac_sha256("Client Key", $salted));
    my $server = hmac_sha256("Server Key", $salted);
    printf "SCRAM-SHA-256\$%d:%s\$%s:%s", $iter,
      encode_base64($salt, ""), encode_base64($stored, ""), encode_base64($server, "");
  '
}

export_verifier() {  # export_verifier <password var> <verifier var>
  local v
  v="$(scram_verifier "$1")"
  [[ "$v" == SCRAM-SHA-256\$4096:* ]] || { echo "bravo-init: could not hash $1" >&2; return 1; }
  export "$2=$v"
}

bravo_init() {
  case "${BRAVO_SIDE:-}" in
    src|dst) ;;
    *) echo "bravo-init: BRAVO_SIDE must be src or dst" >&2; return 1 ;;
  esac
  : "${APP_WRITER_PASSWORD:?bravo-init: APP_WRITER_PASSWORD is required}"
  : "${REPLICATOR_PASSWORD:?bravo-init: REPLICATOR_PASSWORD is required}"
  : "${EXECUTOR_CTL_PASSWORD:?bravo-init: EXECUTOR_CTL_PASSWORD is required}"
  : "${EXECUTOR_OBSERVE_PASSWORD:?bravo-init: EXECUTOR_OBSERVE_PASSWORD is required}"

  local plant=false
  case "${BRAVO_PLANT_REGRESSION:-0}" in
    1) plant=true ;;
    0) ;;
    *) echo "bravo-init: BRAVO_PLANT_REGRESSION must be 0 or 1" >&2; return 1 ;;
  esac

  echo "bravo-init: side=${BRAVO_SIDE} plant_regression=${plant}"
  bravo_psql -f "${bravo_schema_dir}/01_schema.sql"

  export_verifier APP_WRITER_PASSWORD BRAVO_APP_WRITER_VERIFIER
  export_verifier REPLICATOR_PASSWORD BRAVO_REPLICATOR_VERIFIER
  export_verifier EXECUTOR_CTL_PASSWORD BRAVO_EXECUTOR_CTL_VERIFIER
  export_verifier EXECUTOR_OBSERVE_PASSWORD BRAVO_EXECUTOR_OBSERVE_VERIFIER
  bravo_psql -f "${bravo_schema_dir}/02_roles.sql"
  unset BRAVO_APP_WRITER_VERIFIER BRAVO_REPLICATOR_VERIFIER BRAVO_EXECUTOR_CTL_VERIFIER BRAVO_EXECUTOR_OBSERVE_VERIFIER

  bravo_psql \
    -v attachment_store_id="${BRAVO_ATTACHMENT_STORE_ID:-$BRAVO_SIDE}" \
    -v plant_regression="$plant" \
    -f "${bravo_schema_dir}/03_${BRAVO_SIDE}.sql"
  echo "bravo-init: done"
}

bravo_init
