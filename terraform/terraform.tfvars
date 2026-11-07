# Ownership image: source a117c8cf2d300296e33de04dfd5c707ef26df78d, ECR tag a117c8c.
# Previous (pre-ownership, rollback target): sha256:a0500bbef2e4f5d1939b2d7815b37d2faa2f0f9ed065cbb2e9d11c8cb0a7b224
risk_agent_image_digest = "sha256:7701dd3365cae6e753d2864d051d105bcee31fe3bfa41bb16fe5ea3a250662bd"
# Rollout hold released after image readiness was verified on 2026-11-01
# (docs/OWNERSHIP_DEPLOYMENT_REVIEW.md §5–§6). -1 = unreserved.
risk_agent_reserved_concurrency = -1
