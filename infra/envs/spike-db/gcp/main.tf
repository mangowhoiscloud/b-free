# DB 이전 매트릭스 스파이크, Google 쪽(docs/architecture/12-db-migration-matrix.md 11절, D-24).
#
# - VPC 하나, 서브넷 하나, PSA(사설 서비스 액세스) 범위 하나.
# - Cloud SQL for PostgreSQL 16, 가장 싼 등급(db-f1-micro, ZONAL, HDD 10 GiB), 사설 IP만, SSL 강제,
#   공유 CA 모드(GOOGLE_MANAGED_CAS_CA). postgres 비밀번호는 Terraform이 만들지 않음:
#   OpenBao에서 파이프로 gcloud --prompt-for-password에 넣음(D-26). 상태와 출력에 비밀 없음.
# - 링크 VM(e2-micro, 외부 IP 없음): 임시 링크(D-16 전)의 VPC 쪽 끝.
#   * 7071: 노트북 → IAP 터널 → VM → Cloud SQL 사설 IP:5432 (정방향 TCP 중계)
#   * 7070: 역방향 터널의 agent 포트(tools/link relay.py). IAP 범위에서만 받음
#   * 5432: 역방향 터널의 client 포트. Cloud SQL(PSA 범위)에서만 받음 → 노트북 PG
# - AlloyDB(선택, var.enable_alloydb): SPK-11.

resource "google_compute_network" "spike" {
  name                    = var.name
  auto_create_subnetworks = false
}

resource "google_compute_subnetwork" "spike" {
  name                     = "${var.name}-${var.region}"
  network                  = google_compute_network.spike.id
  region                   = var.region
  ip_cidr_range            = "10.60.0.0/24"
  private_ip_google_access = true
}

resource "google_compute_global_address" "psa" {
  name          = "${var.name}-psa"
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  address       = "10.61.0.0"
  prefix_length = 20
  network       = google_compute_network.spike.id
}

resource "google_service_networking_connection" "psa" {
  network                 = google_compute_network.spike.id
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.psa.name]
  deletion_policy         = "ABANDON"
}

resource "google_compute_firewall" "iap_to_link" {
  name          = "${var.name}-iap-to-link"
  network       = google_compute_network.spike.id
  direction     = "INGRESS"
  source_ranges = ["35.235.240.0/20"] # IAP TCP forwarding
  target_tags   = ["spike-link"]
  allow {
    protocol = "tcp"
    ports    = ["22", "7070", "7071"]
  }
}

resource "google_compute_firewall" "psa_to_link" {
  name          = "${var.name}-psa-to-link"
  network       = google_compute_network.spike.id
  direction     = "INGRESS"
  source_ranges = ["10.61.0.0/20"] # managed DB subscriber -> relay client port
  target_tags   = ["spike-link"]
  allow {
    protocol = "tcp"
    ports    = ["5432"]
  }
}

resource "google_sql_database_instance" "spike" {
  name                = "${var.name}-pg16"
  database_version    = "POSTGRES_16"
  region              = var.region
  deletion_protection = false

  settings {
    edition           = "ENTERPRISE"
    tier              = var.tier
    availability_type = var.availability_type
    disk_type         = "PD_HDD"
    disk_size         = 10
    disk_autoresize   = false

    ip_configuration {
      ipv4_enabled    = false
      private_network = google_compute_network.spike.id
      ssl_mode        = "ENCRYPTED_ONLY"
      server_ca_mode  = "GOOGLE_MANAGED_CAS_CA"
    }

    backup_configuration {
      enabled = false
    }

    database_flags {
      name  = "cloudsql.logical_decoding"
      value = var.logical_decoding ? "on" : "off"
    }
    # Cloud SQL accepts only 102400..10485760 (MB) for this flag (measured 2026-11-02: 2048 is refused,
    # deploy/pilot/runs/spike-cloudsql-*/spk01-cloudsql-max-slot-wal-keep-size-rejected.txt).
    database_flags {
      name  = "max_slot_wal_keep_size"
      value = "102400"
    }
    # A failed CREATE ROLE ... PASSWORD or CREATE SUBSCRIPTION ... password= is logged with its text at the
    # default (error). RDS spike measured this (spike-rds-*/rds-postgresql.log, redacted copy). Cloud Logging
    # keeps entries after the instance is deleted, so the statement text is never logged here (D-26).
    dynamic "database_flags" {
      for_each = var.quiet_error_statements ? [1] : []
      content {
        name  = "log_min_error_statement"
        value = "panic"
      }
    }

    user_labels = {
      component = "spike"
    }
  }

  depends_on = [google_service_networking_connection.psa]
}

resource "google_sql_database" "chat" {
  name     = "chat"
  instance = google_sql_database_instance.spike.name
}

locals {
  relay_py = file("${path.module}/../../../../tools/link/src/bravo_link/relay.py")
  fwd_py   = <<-PY
    import asyncio, os, sys
    UP_HOST = sys.argv[1]; UP_PORT = int(sys.argv[2]); LISTEN = int(sys.argv[3])
    async def pipe(r, w):
        try:
            while (d := await r.read(65536)):
                w.write(d); await w.drain()
        finally:
            try: w.close()
            except Exception: pass
    async def handle(cr, cw):
        try:
            ur, uw = await asyncio.wait_for(asyncio.open_connection(UP_HOST, UP_PORT), 10)
        except Exception as e:
            print(f"upstream connect failed: {e!r}", flush=True); cw.close(); return
        await asyncio.gather(pipe(cr, uw), pipe(ur, cw), return_exceptions=True)
    async def main():
        srv = await asyncio.start_server(handle, "0.0.0.0", LISTEN)
        print(f"fwd 0.0.0.0:{LISTEN} -> {UP_HOST}:{UP_PORT}", flush=True)
        async with srv: await srv.serve_forever()
    asyncio.run(main())
  PY
}

resource "google_compute_instance" "link" {
  name         = "${var.name}-link"
  zone         = var.zone
  machine_type = "e2-micro"
  tags         = ["spike-link"]

  boot_disk {
    initialize_params {
      image = "debian-cloud/debian-12"
      size  = 10
      type  = "pd-standard"
    }
  }

  network_interface {
    subnetwork = google_compute_subnetwork.spike.id
    # 외부 IP 없음. 노트북은 IAP TCP 전달로만 닿음.
  }

  shielded_instance_config {
    enable_secure_boot = true
  }

  metadata = {
    enable-oslogin = "TRUE"
    relay-py       = local.relay_py
    fwd-py         = local.fwd_py
    upstream-ip    = google_sql_database_instance.spike.private_ip_address
  }

  metadata_startup_script = <<-SH
    #!/bin/bash
    set -eu
    md() { curl -fsS -H 'Metadata-Flavor: Google' "http://metadata.google.internal/computeMetadata/v1/instance/attributes/$1"; }
    install -d -m 755 /opt/bravo-link
    md relay-py > /opt/bravo-link/relay.py
    md fwd-py > /opt/bravo-link/fwd.py
    UP="$(md upstream-ip)"
    id link >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin link
    cat > /etc/systemd/system/bravo-relay.service <<UNIT
    [Unit]
    Description=bravo-link reverse relay (임시 링크 D-16 전)
    [Service]
    User=link
    ExecStart=/usr/bin/python3 /opt/bravo-link/relay.py --client-bind 0.0.0.0 --client-port 5432 --agent-bind 0.0.0.0 --agent-port 7070
    Restart=always
    AmbientCapabilities=CAP_NET_BIND_SERVICE
    [Install]
    WantedBy=multi-user.target
    UNIT
    cat > /etc/systemd/system/bravo-fwd.service <<UNIT
    [Unit]
    Description=bravo-link forward TCP to Cloud SQL
    [Service]
    User=link
    ExecStart=/usr/bin/python3 /opt/bravo-link/fwd.py $UP 5432 7071
    Restart=always
    [Install]
    WantedBy=multi-user.target
    UNIT
    systemctl daemon-reload
    systemctl enable --now bravo-relay bravo-fwd
  SH
}

# ---- AlloyDB (SPK-11), optional -------------------------------------------------------------

resource "google_project_service" "alloydb" {
  count              = var.enable_alloydb ? 1 : 0
  service            = "alloydb.googleapis.com"
  disable_on_destroy = true
}

resource "google_alloydb_cluster" "spike" {
  count      = var.enable_alloydb ? 1 : 0
  cluster_id = "${var.name}-alloydb"
  location   = var.region
  network_config {
    network = google_compute_network.spike.id
  }
  deletion_policy = "FORCE"
  depends_on      = [google_service_networking_connection.psa, google_project_service.alloydb]
}

resource "google_alloydb_instance" "spike" {
  count         = var.enable_alloydb ? 1 : 0
  cluster       = google_alloydb_cluster.spike[0].name
  instance_id   = "${var.name}-alloydb-primary"
  instance_type = "PRIMARY"
  machine_config {
    cpu_count = 2
  }
  client_connection_config {
    ssl_config {
      ssl_mode = "ENCRYPTED_ONLY"
    }
  }
}
