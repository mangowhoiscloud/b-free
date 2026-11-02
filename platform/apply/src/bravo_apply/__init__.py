"""T31 approved-change apply path (spec r2 §8.2 REHEARSE row, T31; CLOUD-E2E-CONTRACT §8).

Platform-side trusted pipeline: approved diff -> buildx build -> syft SBOM -> trivy scan -> cosign signature
with the platform OpenBao transit key -> destination import -> deploy only if the destination digest equals
the approved digest -> scoped policy apply -> gate re-check. Deterministic code only, no LLM (NFR-5). The code
holds no secrets: the signer token reaches only cosign's environment through ``keys.sh with-signer``.
"""

__version__ = "0.1.0"
