resource "aws_dynamodb_table" "supplier_risk_state" {
  name         = "supplier_risk_state"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"

  attribute {
    name = "pk"
    type = "S"
  }
}