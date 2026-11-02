# 비밀이 없는 kubeconfig. DNS 엔드포인트로 닿고, 토큰은 gke-gcloud-auth-plugin이 받음.

locals {
  dns_endpoint = google_container_cluster.this.control_plane_endpoints_config[0].dns_endpoint_config[0].endpoint
}

resource "local_file" "kubeconfig" {
  filename        = var.kubeconfig_path
  file_permission = "0600"

  content = yamlencode({
    apiVersion      = "v1"
    kind            = "Config"
    current-context = var.name
    clusters = [{
      name    = var.name
      cluster = { server = "https://${local.dns_endpoint}" }
    }]
    contexts = [{
      name    = var.name
      context = { cluster = var.name, user = var.name }
    }]
    users = [{
      name = var.name
      user = {
        exec = {
          apiVersion         = "client.authentication.k8s.io/v1beta1"
          command            = "gke-gcloud-auth-plugin"
          provideClusterInfo = true
          installHint        = "gcloud components install gke-gcloud-auth-plugin"
        }
      }
    }]
  })
}
