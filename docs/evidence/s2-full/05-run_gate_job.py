"""Debug run of the gate-probe verification Job with the product code (behavioral.JobRun) on bravo-onprem."""
import json, subprocess, sys
from pathlib import Path
REPO = Path("/Users/mango/workspace/softbank2026 hackathon/bravo-platform")
sys.path[:0] = [str(REPO / "platform/rehearsal/src"), str(REPO / "deploy/cloud")]
from db_roles import Bao
from bravo_rehearsal import behavioral
from bravo_rehearsal.cluster import Names

record = Bao().read("envs/target-onprem/rehearsal-s3")
app = record["target_app_arn"]
doc = {"Version": "2012-10-17", "Statement": [
    {"Sid": "DenyInsecureTransport", "Effect": "Deny", "Principal": "*", "Action": "s3:*",
     "Resource": ["arn:aws:s3:::bravo-demo-attachments", "arn:aws:s3:::bravo-demo-attachments/*"],
     "Condition": {"BoolIfExists": {"aws:SecureTransport": "false"}}},
    {"Sid": "AppReadWrite", "Effect": "Allow", "Principal": {"AWS": [app]},
     "Action": ["s3:GetObject", "s3:PutObject", "s3:ListBucket"],
     "Resource": ["arn:aws:s3:::bravo-demo-attachments", "arn:aws:s3:::bravo-demo-attachments/*"]}]}
run = behavioral.JobRun(Path.home() / ".kube/config", "bravo-onprem")
orig_delete = run._delete_namespace
def dump_then_delete(ns, timeout_s=120):
    out = subprocess.run(["kubectl", "--context", "bravo-onprem", "-n", ns, "get", "events", "--sort-by=.lastTimestamp",
                          "-o", "custom-columns=T:.lastTimestamp,R:.reason,O:.involvedObject.name,M:.message"],
                         capture_output=True, text=True).stdout
    print("EVENTS\n" + out[-3000:], file=sys.stderr)
    out = subprocess.run(["kubectl", "--context", "bravo-onprem", "-n", ns, "get", "pods,jobs", "-o", "wide"], capture_output=True, text=True).stdout
    print("PODS\n" + out, file=sys.stderr)
    return orig_delete(ns, timeout_s)
run._delete_namespace = dump_then_delete
names = Names.of(sys.argv[1], 1, "t-" + sys.argv[1], "governance")
res = run.run(names=names, trial_id="t-" + sys.argv[1], image=sys.argv[2], record=record, docs=[doc], deadline_s=int(sys.argv[3]))
print(json.dumps(res, indent=1, default=str))
