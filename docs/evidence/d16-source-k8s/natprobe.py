# Negative control from the platform collector pod: the old path (platform NAT -> EKS public endpoint) must fail now.
import socket, ssl, time
from datetime import datetime, timezone
host = "4085fe75e8bd75e727bd03fbb9729334.gr7.ap-northeast-2.eks.amazonaws.com"
print("at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
print("dns", sorted({a[4][0] for a in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)}))
t0 = time.monotonic()
try:
    with socket.create_connection((host, 443), timeout=8) as s:
        print("public_endpoint_tcp CONNECTED (old path still open)", s.getpeername())
except OSError as e:
    print("public_endpoint_tcp FAILED", type(e).__name__, str(e)[:80], "after_s", round(time.monotonic() - t0, 1))
