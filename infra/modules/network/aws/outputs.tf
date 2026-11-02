output "vpc_id" {
  description = "VPC ID"
  value       = aws_vpc.this.id
}

output "cidr" {
  description = "VPC 대역"
  value       = aws_vpc.this.cidr_block
}

output "azs" {
  description = "사설 서브넷이 있는 가용 영역(입력 순서)"
  value       = var.azs
}

output "private_subnet_ids" {
  description = "사설 서브넷 ID(azs 순서)"
  value       = [for az in var.azs : aws_subnet.private[az].id]
}

output "private_subnet_ids_by_az" {
  description = "가용 영역별 사설 서브넷 ID"
  value       = { for az, s in aws_subnet.private : az => s.id }
}

output "nat_gateway_id" {
  description = "Regional NAT Gateway ID"
  value       = aws_nat_gateway.regional.id
}
