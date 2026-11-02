#!/usr/bin/env bash
# NFR-8 / T1: the `component` cost allocation tag (source / platform / target) for Cost Explorer.
# Activation is a billing setting, so a person runs this (lead approval 2026-11-02); the collector never changes it.
# Each Cost Explorer request is billed USD 0.01. Uses the operator's AWS credentials from the default chain.
#
#   cost-tag.sh status     ce:ListCostAllocationTags for `component` (1 request)
#   cost-tag.sh activate   ce:UpdateCostAllocationTagsStatus component=Active (1 request), prints one result line
#
# Until billing has seen resources carrying the tag (up to 24 h after they are created) AWS answers
# "Tag keys not found: component". Record each attempt in deploy/platform/control/config/cost.yaml
# (aws.cost_tag_activation_attempts) so the receipt shows it; GET /v1/cost marks per-component actuals
# "태그 활성화 대기" and uses per-resource estimates until the status is Active.
set -euo pipefail

TAG_KEY="${BRAVO_COST_TAG_KEY:-component}"
CE=(aws ce --region us-east-1 --output json)
now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

case "${1-}" in
  status)
    "${CE[@]}" list-cost-allocation-tags --tag-keys "$TAG_KEY" \
      | python3 -I -c 'import json,sys; t=json.load(sys.stdin)["CostAllocationTags"]; print(json.dumps(t[0] if t else {"TagKey": sys.argv[1], "Status": "NOT_REGISTERED"}))' "$TAG_KEY"
    ;;
  activate)
    if out="$("${CE[@]}" update-cost-allocation-tags-status \
                --cost-allocation-tags-status "TagKey=$TAG_KEY,Status=Active" 2>&1)"; then
      errors="$(python3 -I -c 'import json,sys; print(len(json.load(sys.stdin).get("Errors", [])))' <<<"$out")"
      if [[ $errors == 0 ]]; then
        printf -- '- {at: "%s", result: "Active requested (no errors)"}\n' "$(now)"
      else
        printf -- '- {at: "%s", result: %s}\n' "$(now)" "$(python3 -I -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["Errors"]))' <<<"$out")"
      fi
    else
      msg="$(tr '\n' ' ' <<<"$out" | sed -E 's/^.*\(([A-Za-z]+)\) when calling (the )?[A-Za-z]+ operation: (.*)$/\1: \3/; s/^[[:space:]]+//; s/[[:space:]]+$//')"
      printf -- '- {at: "%s", result: "%s"}\n' "$(now)" "${msg//\"/\'}"
      exit 1
    fi
    ;;
  *) echo "usage: cost-tag.sh status|activate" >&2; exit 2 ;;
esac
