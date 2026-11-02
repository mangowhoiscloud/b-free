# Platform OpenBao (D-26): the only key store, now on the platform EKS. Same settings as the pilot
# (deploy/pilot/bao/bao.hcl): production mode, raft, TLS listener, file audit in the data volume.
# The data came over from the pilot by raft snapshot (deploy/pilot/scripts/keys.sh migrate-to-platform),
# so the Shamir shares in operator custody (~/.bravo/custody/openbao-init.json) unseal this node.
ui            = false
api_addr      = "https://openbao-0.openbao-internal.openbao.svc.cluster.local:8200"
cluster_addr  = "https://openbao-0.openbao-internal.openbao.svc.cluster.local:8201"
disable_mlock = true

storage "raft" {
  path    = "/openbao/file"
  node_id = "platform-eks-bao-0"
}

listener "tcp" {
  address         = "0.0.0.0:8200"
  cluster_address = "0.0.0.0:8201"
  tls_cert_file   = "/openbao/tls/tls.crt"
  tls_key_file    = "/openbao/tls/tls.key"
  tls_min_version = "tls12"
}

# File audit inside the data volume. Values are HMAC'd (log_raw stays off).
audit "file" "file" {
  options {
    file_path = "/openbao/file/audit.log"
  }
}
