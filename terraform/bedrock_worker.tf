# Phase 3E: Bedrock authority enforced by IAM.
#
# RiskAgentBedrockWorkerRole is a dedicated runtime identity whose only
# permission is Claude inference. RiskAgentBedrockClaudeDeny is the
# operator-controlled switch: an explicit Deny on the same action/resources.
# Terraform defines the Deny policy but deliberately does NOT attach it; the
# operator attaches (revoke) and detaches (restore) it with
# iam:AttachRolePolicy / iam:DetachRolePolicy, so an apply can never undo a
# revocation. The worker role has no IAM permissions and cannot change its
# own authority.

locals {
  bedrock_worker_role_name  = "RiskAgentBedrockWorkerRole"
  bedrock_claude_deny_name  = "RiskAgentBedrockClaudeDeny"
  bedrock_claude_model_id   = "anthropic.claude-haiku-4-5-20251001-v1:0"
  bedrock_claude_profile_id = "us.${local.bedrock_claude_model_id}"

  # The inference profile and every Region it routes to.
  bedrock_claude_resources = [
    "arn:aws:bedrock:us-west-2:${data.aws_caller_identity.current.account_id}:inference-profile/${local.bedrock_claude_profile_id}",
    "arn:aws:bedrock:us-east-1::foundation-model/${local.bedrock_claude_model_id}",
    "arn:aws:bedrock:us-east-2::foundation-model/${local.bedrock_claude_model_id}",
    "arn:aws:bedrock:us-west-2::foundation-model/${local.bedrock_claude_model_id}",
  ]
}

resource "aws_iam_role" "risk_agent_bedrock_worker" {
  name                 = local.bedrock_worker_role_name
  max_session_duration = 3600

  # Assumable only by this account's IAM Identity Center AdministratorAccess
  # role (the operator), which hands the session credentials to the probe.
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"
      }
      Action = "sts:AssumeRole"
      Condition = {
        ArnLike = {
          "aws:PrincipalArn" = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/aws-reserved/sso.amazonaws.com/*/AWSReservedSSO_AdministratorAccess_*"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "risk_agent_bedrock_worker_claude" {
  name = "BedrockClaudeInvoke"
  role = aws_iam_role.risk_agent_bedrock_worker.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["bedrock:InvokeModel"]
      Resource = local.bedrock_claude_resources
    }]
  })
}

resource "aws_iam_policy" "risk_agent_bedrock_claude_deny" {
  name        = local.bedrock_claude_deny_name
  description = "Operator kill switch: attach to RiskAgentBedrockWorkerRole to revoke Claude inference; detach to restore."

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Deny"
      Action = [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream"
      ]
      Resource = local.bedrock_claude_resources
    }]
  })
}
