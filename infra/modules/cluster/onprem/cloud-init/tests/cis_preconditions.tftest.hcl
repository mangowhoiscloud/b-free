# cis_preconditions: true (default) writes the CIS host conditions; false (spec S1 4a demo only) leaves them out.
# The bootstrap starts RKE2 with enable and start apart (a refused start fails at once) and skips the
# packaged Traefik CRD chart on servers.
#   cd infra/modules/cluster/onprem/cloud-init && terraform init && terraform test
variables {
  role = "server"
  rke2 = { version = "v1.36.5+rke2r1", sha256 = "244454bed11d9525452e10a2e5055da5d5fc6b61928ca3fae778bc4254ce467f" }
}

run "default_writes_cis_preconditions" {
  command = plan

  assert {
    condition     = can(yamldecode(output.user_data))
    error_message = "user_data must be valid YAML."
  }
  assert {
    condition = alltrue([for s in ["vm.panic_on_oom=0", "vm.overcommit_memory=1", "kernel.panic=10", "kernel.panic_on_oops=1",
    "groupadd --system etcd", "--comment \"etcd user\" etcd", "profile: cis"] : strcontains(output.user_data, s)])
    error_message = "The default render must write the CIS sysctls, create the etcd user and group, and set profile: cis."
  }
  assert {
    condition     = !strcontains(output.user_data, "S1 4a")
    error_message = "The default render must not carry the S1 4a demo marker."
  }
  assert {
    condition = (strcontains(output.user_data, "start_unit rke2-server") &&
    !strcontains(output.user_data, "systemctl enable --now rke2-server.service"))
    error_message = "The server must be started with enable and start apart, so a refused start fails the bootstrap."
  }
  assert {
    condition     = strcontains(output.user_data, "touch /var/lib/rancher/rke2/server/manifests/rke2-traefik-crd.yaml.skip")
    error_message = "The server must skip the packaged rke2-traefik-crd manifest."
  }
}

run "agent_fails_fast" {
  command = plan

  variables {
    role = "agent"
  }

  assert {
    condition     = can(yamldecode(output.user_data)) && strcontains(output.user_data, "then start_unit rke2-agent")
    error_message = "The agent must be started through start_unit once it has its join file."
  }
}

run "demo_leaves_out_cis_preconditions" {
  command = plan

  variables {
    cis_preconditions = false
  }

  assert {
    condition     = can(yamldecode(output.user_data))
    error_message = "user_data must be valid YAML."
  }
  assert {
    condition = !anytrue([for s in ["vm.panic_on_oom", "vm.overcommit_memory", "kernel.panic", "groupadd", "useradd"] :
    strcontains(output.user_data, s)])
    error_message = "cis_preconditions = false must leave out the CIS sysctls and the etcd user and group."
  }
  assert {
    condition     = strcontains(output.user_data, "profile: cis") && strcontains(output.user_data, "fs.inotify.max_user_watches=524288")
    error_message = "Only the CIS conditions go: profile: cis and the other sysctls stay."
  }
}
