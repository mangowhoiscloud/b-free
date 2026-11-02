variable "name" {
  description = "노드 이름(Name 태그, 역할 접두어). 예: bravo-platform"
  type        = string
}

variable "vpc_id" {
  description = "노드를 둘 VPC(network/aws의 vpc_id)"
  type        = string
}

variable "subnet_id" {
  description = "사설 서브넷 하나. 공인 IP는 붙이지 않음(D-16, 인바운드 0)"
  type        = string
}

variable "instance_type" {
  description = "EC2 종류. t4g(arm64)면 ami_ssm_parameter도 arm64"
  type        = string
}

variable "ami_ssm_parameter" {
  description = "AMI ID를 담은 공개 SSM 파라미터(Canonical Ubuntu). 스펙 §7.2 · T2: Ubuntu + RKE2(CIS)"
  type        = string
  default     = "/aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id"
}

variable "root_volume_gib" {
  description = "루트 EBS 크기(GiB). RKE2 이미지, etcd, local-path 볼륨(bravo-db, bravo-general)이 여기에 놓임"
  type        = number
  default     = 60
}

variable "user_data" {
  description = "cloud-init 문서(공통 RKE2 cloud-init, infra/modules/cluster/onprem/cloud-init). 비밀 없음"
  type        = string
}

variable "tags" {
  description = "추가 태그. component 태그는 provider default_tags가 붙임"
  type        = map(string)
  default     = {}
}
