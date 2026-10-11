# Phase 3D runtime authority. Authority items are operator-managed data
# (app.governance.authority_cli), deliberately not seeded here: a later
# apply must never undo an operator's revocation.
resource "aws_dynamodb_table" "risk_agent_authority" {
  name         = "risk_agent_authority"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "scope_id"

  attribute {
    name = "scope_id"
    type = "S"
  }
}

# Governed side-effect lifecycle and idempotency (app.governance.action_ledger).
resource "aws_dynamodb_table" "risk_agent_actions" {
  name         = "risk_agent_actions"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "action_id"

  attribute {
    name = "action_id"
    type = "S"
  }
}
