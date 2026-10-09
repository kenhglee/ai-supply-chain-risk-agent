resource "aws_ecr_repository" "risk_agent" {
  name                 = "ai-supply-chain-risk-agent"
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
}