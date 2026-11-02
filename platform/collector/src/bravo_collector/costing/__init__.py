"""Cost receipt (T1, NFR-8, spec §10 ending scene "이전 중 추가 비용").

Read-only. Every number comes from a real API or a recorded run:
    inventory   AWS Describe/List, GCP Compute/GKE/KMS/Artifact Registry REST, Cloudflare API (read)
    unit price  AWS Pricing API, GCP Cloud Billing Catalog API, a cited plan page, or "not connected"
    actuals     Cost Explorer (``component`` tag when it is Active; account-wide by service otherwise)
    LLM         subscriptions as flat fees (price only when an official page is cited), recorded token
                usage per agent run with the SDK's API-price equivalent (never billed, never in the total)
Nothing is guessed: a value that cannot be read is ``None`` with a reason.
"""

from .receipt import build_receipt, receipt_for_request

__all__ = ["build_receipt", "receipt_for_request"]
