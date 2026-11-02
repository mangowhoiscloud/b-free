# 출력 이름은 cluster/gke, cluster/k3s와 같음(infra/README.md 계약).

output "name" {
  description = "클러스터 이름"
  value       = aws_eks_cluster.this.name
}

output "kubernetes_version" {
  description = "K8s minor"
  value       = aws_eks_cluster.this.version
}

output "api_endpoint" {
  description = "API 서버 주소(비공개 엔드포인트)"
  value       = aws_eks_cluster.this.endpoint
}

output "kubeconfig_path" {
  description = "비밀 없는 kubeconfig 경로"
  value       = local_file.kubeconfig.filename
}

output "cni" {
  description = "CNI 요약. VPC CNI가 Pod에 VPC 주소를 바로 주므로 캡슐화가 없음. 정책과 관측은 Cilium chaining"
  value = {
    name           = var.network_policy_engine == "cilium" ? "cilium-aws-cni-chaining" : "aws-vpc-cni"
    network_policy = true
    encapsulation  = "none"
    mtu            = 9001
  }
}

output "node_pools" {
  description = "역할별 노드 풀(AZ, 인스턴스 종류, taint)"
  value = {
    for pool, p in var.node_pools : pool => {
      azs            = p.azs
      instance_types = p.instance_types
      arch           = "arm64"
      desired        = p.desired_per_az * length(p.azs)
      taints         = p.taints
    }
  }
}

output "storage_platform" {
  description = "landing_zone 모듈의 provider_kind 값"
  value       = "eks"
}

output "addon_delivery" {
  description = "landing_zone 모듈의 delivery 값. 클라우드는 Argo CD Application 파일"
  value       = "argocd"
}

output "ebs_kms_key_arn" {
  description = "EBS 고객 관리 키. landing zone eks overlay의 bravo-db, bravo-general이 씀"
  value       = aws_kms_key.ebs.arn
}

output "cluster_security_group_id" {
  description = "EKS가 만든 클러스터 보안 그룹"
  value       = aws_eks_cluster.this.vpc_config[0].cluster_security_group_id
}

output "node_role_arn" {
  description = "노드 역할 ARN"
  value       = aws_iam_role.node.arn
}
