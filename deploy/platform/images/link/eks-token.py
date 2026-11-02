#!/app/.venv/bin/python
"""kubectl exec credential plugin: EKS bearer token from the default AWS chain (EKS Pod Identity).

    eks-token <cluster-name> <region>

Prints a client.authentication.k8s.io/v1beta1 ExecCredential on stdout. The token is
``k8s-aws-v1.`` + base64url(presigned STS GetCallerIdentity URL), the same value
``aws eks get-token`` prints. No static key: botocore reads the Pod Identity agent endpoint
(AWS_CONTAINER_CREDENTIALS_FULL_URI + AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE). Nothing is logged.
"""

import base64
import datetime as dt
import json
import sys

import botocore.session
from botocore.signers import RequestSigner

EXPIRES_S = 60


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: eks-token <cluster-name> <region>", file=sys.stderr)
        return 2
    cluster, region = sys.argv[1], sys.argv[2]
    session = botocore.session.get_session()
    creds = session.get_credentials()
    if creds is None:
        print("eks-token: no AWS credentials in the default chain (Pod Identity association missing?)", file=sys.stderr)
        return 1
    sts = session.create_client("sts", region_name=region)
    signer = RequestSigner(sts.meta.service_model.service_id, region, "sts", "v4", creds, session.get_component("event_emitter"))
    url = signer.generate_presigned_url(
        {
            "method": "GET",
            "url": f"https://sts.{region}.amazonaws.com/?Action=GetCallerIdentity&Version=2011-06-15",
            "body": {},
            "headers": {"x-k8s-aws-id": cluster},
            "context": {},
        },
        region_name=region,
        expires_in=EXPIRES_S,
        operation_name="",
    )
    token = "k8s-aws-v1." + base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
    # Tell kubectl to refresh well before the presigned URL (and the 15 min EKS token window) expires.
    exp = (dt.datetime.now(dt.UTC) + dt.timedelta(seconds=EXPIRES_S - 10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    json.dump({"kind": "ExecCredential", "apiVersion": "client.authentication.k8s.io/v1beta1",
               "spec": {}, "status": {"expirationTimestamp": exp, "token": token}}, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
