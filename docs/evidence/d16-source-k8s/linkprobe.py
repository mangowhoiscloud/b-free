# Runs inside the platform collector pod with its own web identity (D-16 pre-check). Prints no token.
import ssl, socket, time, urllib.parse
from datetime import datetime, timezone
import boto3
from kubernetes import client
from bravo_collector.clusters import _eks_endpoint, eks_token
LINK = "https://src-k8s.bravo-link.svc.cluster.local:443"
s = boto3.Session()
print("caller_arn", s.client("sts", region_name="ap-northeast-2").get_caller_identity()["Arn"])
ep = _eks_endpoint(s, "bravo-source", "ap-northeast-2")
host = urllib.parse.urlparse(ep.host).hostname
print("eks_endpoint_host", host)
# 1) TLS handshake through the link, verified with the EKS CA and the real EKS host name
ctx = ssl.create_default_context(cafile=ep.ca_file)
with socket.create_connection(("src-k8s.bravo-link.svc.cluster.local", 443), timeout=10) as raw:
    with ctx.wrap_socket(raw, server_hostname=host) as tls:
        cert = tls.getpeercert()
        print("tls_verified", tls.version(), "subject", dict(x[0] for x in cert["subject"]))
        print("san_has_eks_host", any(v == host for k, v in cert.get("subjectAltName", ())))
# 2) wrong name must fail (proves verification is on)
try:
    with socket.create_connection(("src-k8s.bravo-link.svc.cluster.local", 443), timeout=10) as raw:
        with ctx.wrap_socket(raw, server_hostname="wrong.example.com"):
            print("wrong_name_accepted (BAD)")
except ssl.SSLCertVerificationError as e:
    print("wrong_name_rejected", e.verify_message)
# 3) API calls with the IAM token
cfg = client.Configuration()
cfg.host = LINK
cfg.ssl_ca_cert = ep.ca_file
cfg.verify_ssl = True
cfg.tls_server_name = host
cfg.api_key = {"authorization": eks_token(s, "bravo-source", "ap-northeast-2")}
cfg.api_key_prefix = {"authorization": "Bearer"}
api = client.ApiClient(cfg)
t0 = time.monotonic()
v = client.VersionApi(api).get_code(_request_timeout=10).git_version
print("server_version", v, "ms", int((time.monotonic() - t0) * 1000))
nodes = client.CoreV1Api(api).list_node(_request_timeout=10).items
print("nodes", [n.metadata.name for n in nodes])
ns = client.CoreV1Api(api).list_namespace(_request_timeout=10).items
print("namespaces", len(ns))
print("at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
