resource "aws_dynamodb_table" "risk_agent_checkpoints" {
  name         = "risk_agent_checkpoints"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "thread_id"
  range_key    = "sk"

  attribute {
    name = "thread_id"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }
}
