output "endpoint" {
  description = "RDS 엔드포인트 이름(서버 인증서의 이름). 스파이크 스크립트의 host="
  value       = aws_db_instance.spike.address
}

output "port" {
  value = aws_db_instance.spike.port
}

output "identifier" {
  value = aws_db_instance.spike.identifier
}

output "engine_version_actual" {
  value = aws_db_instance.spike.engine_version_actual
}

output "master_username" {
  value = aws_db_instance.spike.username
}

output "master_user_secret_arn" {
  description = "RDS가 관리하는 마스터 비밀의 ARN. 값이 아니라 참조임"
  value       = aws_db_instance.spike.master_user_secret[0].secret_arn
}

output "parameter_group" {
  value = aws_db_parameter_group.spike.name
}

output "rds_security_group_id" {
  value = aws_security_group.rds.id
}

output "cluster_security_group_id" {
  value = local.cluster_sg_id
}
