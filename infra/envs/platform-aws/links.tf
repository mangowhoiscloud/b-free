# 플랫폼 → 출발지 EKS(스펙 §7.5 "플랫폼 → 출발지 · 목적지", 수집 역할과 제어 역할 분리).
# access entry는 출발지 클러스터의 자원이지만 플랫폼이 만들고 지움. K8s 권한은 그룹 이름에만 묶고,
# 그룹의 Role · ClusterRole은 deploy/platform/link/source-rbac/ 매니페스트가 출발지에 둠.
#
# 경로: 플랫폼 Pod → 플랫폼 Regional NAT → 출발지 EKS 공개 엔드포인트(허용 목록에 플랫폼 NAT IP만 추가).
# 이것은 임시 링크(D-16 전)임. D-16 목표는 출발지 관리 Tunnel + Access 서비스 토큰(infra/envs/edge, Zero Trust 켠 뒤).

locals {
  source_access = var.source_cluster_name == null ? {} : {
    collector = "bravo-collector" # 읽기 전용 ClusterRole(Secret 없음)
    executor  = "bravo-executor"  # ns chat: pods get/list, pods/exec, pods/portforward
    src-link  = "bravo-link"      # ns chat: pods/services get/list, pods/portforward
  }
}

resource "aws_eks_access_entry" "source" {
  for_each = local.source_access

  cluster_name      = var.source_cluster_name
  principal_arn     = aws_iam_role.pod[each.key].arn
  type              = "STANDARD"
  kubernetes_groups = [each.value]
}
