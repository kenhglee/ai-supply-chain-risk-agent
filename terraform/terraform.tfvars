risk_agent_image_digest = "sha256:a0500bbef2e4f5d1939b2d7815b37d2faa2f0f9ed065cbb2e9d11c8cb0a7b224"
# Rollout hold: keep the Lambda stopped through the IAM and image applies.
# Remove (or set -1) only after image readiness is verified
# (docs/OWNERSHIP_DEPLOYMENT_REVIEW.md §5–§6).
risk_agent_reserved_concurrency = 0
