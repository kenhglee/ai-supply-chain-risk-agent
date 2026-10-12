# Phase 3F: policy-as-code enforcement infrastructure.
#
# RiskAgentGovernedWorkerRole   F2 runtime principal: Claude ceiling + exactly
#                               the DynamoDB calls the act path makes.
# RiskAgentPolicyClaudeDeny     policy-layer Deny, attached by the materializer
#                               when policy denies claude_inference. Distinct
#                               from the operator's emergency
#                               RiskAgentBedrockClaudeDeny. Terraform owns the
#                               document, never the attachment.
# RiskAgentPolicyMaterializerRole  writes only the policy#/policymat# namespace
#                               and attaches/detaches only
#                               RiskAgentPolicyClaudeDeny on only the governed
#                               worker (iam:PolicyARN condition).
#
# DynamoDB actions are derived from the code
# (tests/test_governance_iam_requirements.py), not guessed.

locals {
  governed_worker_role_name = "RiskAgentGovernedWorkerRole"
  policy_materializer_name  = "RiskAgentPolicyMaterializerRole"
  policy_claude_deny_name   = "RiskAgentPolicyClaudeDeny"
  governance_agent_id       = "supplier-risk-agent"

  sso_admin_principal_arn = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/aws-reserved/sso.amazonaws.com/*/AWSReservedSSO_AdministratorAccess_*"

  # Assumable only by this account's Identity Center AdministratorAccess role.
  sso_admin_trust_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root" }
      Action    = "sts:AssumeRole"
      Condition = { ArnLike = { "aws:PrincipalArn" = local.sso_admin_principal_arn } }
    }]
  })
}

# ---- Governed worker (F2 runtime principal) ----

resource "aws_iam_role" "risk_agent_governed_worker" {
  name                 = local.governed_worker_role_name
  max_session_duration = 3600
  assume_role_policy   = local.sso_admin_trust_policy
}

resource "aws_iam_role_policy" "risk_agent_governed_worker" {
  name = "GovernedWorkerAccess"
  role = aws_iam_role.risk_agent_governed_worker.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ClaudeInferenceCeiling"
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel"]
        Resource = local.bedrock_claude_resources
      },
      {
        # Emergency item + materialized policy item for the one F2 capability:
        # read before admission, ConditionCheck inside the admission transaction.
        Sid      = "ReadAuthorityAndPolicyForAdmission"
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:ConditionCheckItem"]
        Resource = aws_dynamodb_table.risk_agent_authority.arn
        Condition = {
          "ForAllValues:StringEquals" = {
            "dynamodb:LeadingKeys" = [
              "agent#${local.governance_agent_id}",
              "policy#${local.governance_agent_id}#open_review_ticket",
            ]
          }
          Null = { "dynamodb:LeadingKeys" = "false" }
        }
      },
      {
        # Probe actions only: claim/deny/complete updates and the replay read.
        Sid      = "ProbeActionLedger"
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:UpdateItem"]
        Resource = aws_dynamodb_table.risk_agent_actions.arn
        Condition = {
          "ForAllValues:StringLike" = { "dynamodb:LeadingKeys" = ["action#probe-*"] }
          Null                      = { "dynamodb:LeadingKeys" = "false" }
        }
      },
    ]
  })
}

# ---- Policy-layer Deny (switch position owned by the materializer) ----

resource "aws_iam_policy" "risk_agent_policy_claude_deny" {
  name        = local.policy_claude_deny_name
  description = "Policy-as-code layer: attached to RiskAgentGovernedWorkerRole by the policy materializer when policy denies claude_inference. Not the emergency switch."

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

# ---- Policy materializer ----

resource "aws_iam_role" "risk_agent_policy_materializer" {
  name                 = local.policy_materializer_name
  max_session_duration = 3600
  assume_role_policy   = local.sso_admin_trust_policy
}

resource "aws_iam_role_policy" "risk_agent_policy_materializer" {
  name = "PolicyMaterializerAccess"
  role = aws_iam_role.risk_agent_policy_materializer.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Only the policy/materialization namespace: never agent#... (emergency).
        # Deliberately no dynamodb:Scan: Scan does not support
        # dynamodb:LeadingKeys, so it could not be confined to this namespace.
        Sid      = "PolicyNamespaceReadWrite"
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem"]
        Resource = aws_dynamodb_table.risk_agent_authority.arn
        Condition = {
          "ForAllValues:StringLike" = {
            "dynamodb:LeadingKeys" = [
              "policy#${local.governance_agent_id}#*",
              "policymat#${local.governance_agent_id}#*",
            ]
          }
          Null = { "dynamodb:LeadingKeys" = "false" }
        }
      },
      {
        Sid      = "InspectGovernedWorkerAttachments"
        Effect   = "Allow"
        Action   = ["iam:ListAttachedRolePolicies"]
        Resource = aws_iam_role.risk_agent_governed_worker.arn
      },
      {
        # iam:PolicyARN is a supported condition key for both actions
        # (AWS Service Reference). Any other policy, including the emergency
        # RiskAgentBedrockClaudeDeny, fails the condition (implicit deny).
        Sid      = "SwitchPolicyLayerDenyOnly"
        Effect   = "Allow"
        Action   = ["iam:AttachRolePolicy", "iam:DetachRolePolicy"]
        Resource = aws_iam_role.risk_agent_governed_worker.arn
        Condition = {
          ArnEquals = { "iam:PolicyARN" = aws_iam_policy.risk_agent_policy_claude_deny.arn }
        }
      },
    ]
  })
}
