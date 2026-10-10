resource "aws_dynamodb_table" "risk_traces" {
  name         = "risk_traces"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "trace_id"

  attribute {
    name = "trace_id"
    type = "S"
  }

  attribute {
    name = "alert_id"
    type = "S"
  }

  global_secondary_index {
    name            = "alert-id-index"
    hash_key        = "alert_id"
    projection_type = "KEYS_ONLY"
  }
}