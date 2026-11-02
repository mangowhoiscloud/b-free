# Platform OpenBao (D-26): the only key store. Production mode, never -dev.
# Used by compose.platform.yaml service platform-bao. Operator steps: scripts/keys.sh, README.md.
ui            = false
api_addr      = "https://platform-bao:8200"
cluster_addr  = "https://platform-bao:8201"

storage "raft" {
  path    = "/openbao/file"
  node_id = "platform-bao-1"
}

listener "tcp" {
  address         = "0.0.0.0:8200"
  cluster_address = "0.0.0.0:8201"
  tls_cert_file   = "/openbao/tls/server.crt"
  tls_key_file    = "/openbao/tls/server.key"
  tls_min_version = "tls12"
}

# File audit inside the data volume. Values are HMAC'd (log_raw stays off).
audit "file" "file" {
  options {
    file_path = "/openbao/file/audit.log"
  }
}
