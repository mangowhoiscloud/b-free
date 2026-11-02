"""kubectl exec-credential plugin for Amazon EKS without the AWS CLI (in-cluster Executor).

``python -m bravo_executor.ekstoken <cluster-name> <region>`` prints an ExecCredential whose token is
the standard EKS bearer token: ``k8s-aws-v1.`` + base64url of a presigned STS GetCallerIdentity URL
carrying the ``x-k8s-aws-id`` header, signed with the pod's credentials (EKS Pod Identity, no static key).
"""

from __future__ import annotations

import base64
import json
import sys
from datetime import UTC, datetime, timedelta

from boto3.session import Session
from botocore.signers import RequestSigner

TOKEN_TTL_S = 60


def token(cluster: str, region: str) -> str:
    session = Session(region_name=region)
    client = session.client("sts", region_name=region)
    creds = session.get_credentials()
    if creds is None:
        raise RuntimeError("no AWS credentials (EKS Pod Identity or default chain)")
    signer = RequestSigner(client.meta.service_model.service_id, region, "sts", "v4", creds, session.events)
    url = signer.generate_presigned_url(
        {"method": "GET", "url": f"https://sts.{region}.amazonaws.com/?Action=GetCallerIdentity&Version=2011-06-15",
         "body": {}, "headers": {"x-k8s-aws-id": cluster}, "context": {}},
        region_name=region, expires_in=TOKEN_TTL_S, operation_name="",
    )
    if not url:
        raise RuntimeError("STS presign returned no URL")
    return "k8s-aws-v1." + base64.urlsafe_b64encode(str(url).encode()).decode().rstrip("=")


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: python -m bravo_executor.ekstoken <cluster-name> <region>", file=sys.stderr)
        return 2
    expires = (datetime.now(UTC) + timedelta(seconds=TOKEN_TTL_S - 5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(json.dumps({"apiVersion": "client.authentication.k8s.io/v1beta1", "kind": "ExecCredential",
                      "status": {"token": token(sys.argv[1], sys.argv[2]), "expirationTimestamp": expires}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
