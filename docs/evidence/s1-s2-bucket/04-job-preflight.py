"""Pre-flight of the behavioural Job on bravo-onprem: one good and one open policy, no agent.
Reads secret/envs/target-onprem/rehearsal-s3 with the custody admin token (db_roles.Bao)."""
import json, sys, uuid
from pathlib import Path
REPO = Path("/Users/mango/workspace/softbank2026 hackathon/bravo-platform")
sys.path.insert(0, str(REPO / "deploy/cloud"))
from db_roles import Bao
from bravo_rehearsal import behavioral
from bravo_rehearsal.cluster import Names

rec = Bao().read("envs/target-onprem/rehearsal-s3")
good = json.loads((REPO / "platform/rehearsal/runs/t-e08677b4d9e24a1f82dbc1b5b812edeb/bravo-trial.json").read_text())
doc = next(t["document"] for t in good["artifact"]["translations"] if t["target"]["type"] == "rgw_bucket_policy")
open_doc = {"Version": "2012-10-17", "Statement": [{"Sid": "Open", "Effect": "Allow", "Principal": "*",
            "Action": "s3:GetObject", "Resource": "arn:aws:s3:::chat-attachments/*"}]}
plan = str(uuid.uuid4()); tid = "t-" + uuid.uuid4().hex
names = Names.of(plan, 1, tid)
img = "python:3.12-slim-bookworm@sha256:6dd8b6dab6b3adc7812bab69eca5dbdc1a5cc690ad28306d234ffb4ef8ac9753"
out = behavioral.JobRun(Path.home() / ".kube/config", "bravo-onprem").run(
    names=names, trial_id=tid, image=img, record=rec, docs=[doc, open_doc], deadline_s=120)
print(json.dumps(out, indent=1, ensure_ascii=False))
