"""T31 policies.apply for rgw_bucket_policy, run for real against the REHEARSAL TEST BUCKET (never the destination):
same code (bravo_apply.targets.apply_rgw), a destination dict that names the test bucket and its owner key path.
Then the policy is deleted again. Prints the stage detail (no key values)."""
import json, sys
from pathlib import Path
REPO = Path("/Users/mango/workspace/softbank2026 hackathon/bravo-platform")
sys.path.insert(0, str(REPO / "platform/apply/src"))
from bravo_apply import targets, policies, digests
from bravo_apply.runtime import Stop

doc = json.loads(Path(sys.argv[1]).read_text())
dest = {"namespace": "chat", "policy_types": ["rgw_bucket_policy"],
        "object_store": {"bucket": "bravo-rehearsal-verify", "secret": "envs/target-onprem/rehearsal-s3",
                         "rgw_service": "rook-ceph/rook-ceph-rgw-bravo"}}
items = [{"source": {"type": "aws_s3_bucket_policy", "id": "x"}, "target": {"type": "rgw_bucket_policy",
          "name": "bravo-rehearsal-verify"}, "document": doc, "control_ids": ["IAM-01"]}]
plan = policies.scoped(items, digests.policy_diff_digest({"translations": items}), dest)
print(json.dumps({"scope": "ok", "rgw_bucket_policy": [n for n, _ in plan["rgw_bucket_policy"]]}))
bao = targets.CustodyBao("kubectl")
step = targets.apply_all(plan["rgw_bucket_policy"], [], dest, "kubectl", "bravo-onprem", bao)
print(json.dumps({"result": step.result.value, "detail": step.detail}, indent=1))
# cleanup: delete the policy (test bucket returns to no policy, as the verification Job expects)
with bao.pf as bport:
    _, body = bao.call(bport, "GET", "secret/data/envs/target-onprem/rehearsal-s3")
rec = body["data"]["data"]
with targets.PortForward("kubectl", "bravo-onprem", "rook-ceph", "svc/rook-ceph-rgw-bravo", 443) as port:
    rgw = targets.Rgw(port, "bravo-rehearsal-verify", rec["ca_pem"], rec["access_key_id"], rec["secret_access_key"])
    print(json.dumps({"cleanup_delete": rgw.policy_call("DELETE")[0], "after_get": rgw.policy_call("GET")[0]}))
    rgw.close()
# refusal path: the same applier refuses an anonymous Allow before anything is written
bad = {"Statement": [{"Effect": "Allow", "Principal": "*", "Action": "s3:GetObject",
                      "Resource": "arn:aws:s3:::bravo-rehearsal-verify/*"}]}
items[0]["document"] = bad
try:
    policies.scoped(items, digests.policy_diff_digest({"translations": items}), dest)
except Stop as e:
    print(json.dumps({"refused": e.reason, "problems": e.detail.get("problems")}))
