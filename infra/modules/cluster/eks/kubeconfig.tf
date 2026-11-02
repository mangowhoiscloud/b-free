# 비밀이 없는 kubeconfig. 토큰은 실행할 때 aws eks get-token이 받음.
# API는 비공개 엔드포인트라서 VPC 안(또는 링크 `*-k8s`)에서만 닿음.

resource "local_file" "kubeconfig" {
  filename        = var.kubeconfig_path
  file_permission = "0600"

  content = yamlencode({
    apiVersion      = "v1"
    kind            = "Config"
    current-context = var.name
    clusters = [{
      name = var.name
      cluster = {
        server                     = aws_eks_cluster.this.endpoint
        certificate-authority-data = aws_eks_cluster.this.certificate_authority[0].data
      }
    }]
    contexts = [{
      name    = var.name
      context = { cluster = var.name, user = var.name }
    }]
    users = [{
      name = var.name
      user = {
        exec = {
          apiVersion = "client.authentication.k8s.io/v1beta1"
          command    = "aws"
          args       = ["eks", "get-token", "--cluster-name", var.name, "--region", local.region, "--output", "json"]
        }
      }
    }]
  })
}
