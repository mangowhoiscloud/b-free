output "cloudsql_instance" {
  value = google_sql_database_instance.spike.name
}

output "cloudsql_private_ip" {
  value = google_sql_database_instance.spike.private_ip_address
}

output "cloudsql_dns_name" {
  description = "공유 CA 모드의 인스턴스 DNS 이름(서버 인증서 SAN 후보)"
  value       = google_sql_database_instance.spike.dns_name
}

output "cloudsql_server_ca_mode" {
  value = google_sql_database_instance.spike.settings[0].ip_configuration[0].server_ca_mode
}

output "link_vm" {
  value = google_compute_instance.link.name
}

output "link_vm_ip" {
  value = google_compute_instance.link.network_interface[0].network_ip
}

output "alloydb_ip" {
  value = var.enable_alloydb ? google_alloydb_instance.spike[0].ip_address : null
}
