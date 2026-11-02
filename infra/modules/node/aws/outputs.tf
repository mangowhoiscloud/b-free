output "instance_id" {
  description = "EC2 인스턴스 ID(SSM 세션 대상)"
  value       = aws_instance.node.id
}

output "private_ip" {
  description = "사설 IP"
  value       = aws_instance.node.private_ip
}

output "security_group_id" {
  description = "노드 보안 그룹(인바운드 없음)"
  value       = aws_security_group.node.id
}

output "node_role_name" {
  description = "노드 역할(SSM, ECR 읽기만)"
  value       = aws_iam_role.node.name
}

output "ami_id" {
  description = "실제로 쓴 AMI"
  value       = aws_instance.node.ami
}

output "kms_key_arn" {
  description = "노드 루트 디스크 KMS 키. EBS CSI의 bravo-db · bravo-general 볼륨도 이 키로 암호화"
  value       = aws_kms_key.root.arn
}
