resource "aws_iam_role" "risk_agent_lambda" {
  name = "RiskAgentLambdaRole"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Service = "lambda.amazonaws.com"
      }
      Action = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_basic_execution" {
  role       = aws_iam_role.risk_agent_lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "risk_state_access" {
  name = "RiskStateAccess"
  role = aws_iam_role.risk_agent_lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "dynamodb:GetItem",
        "dynamodb:PutItem"
      ]
      Resource = aws_dynamodb_table.supplier_risk_state.arn
    }]
  })
}

data "aws_caller_identity" "current" {}

resource "aws_iam_role_policy" "bedrock_invoke" {
  name = "BedrockInvoke"
  role = aws_iam_role.risk_agent_lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "bedrock:InvokeModel"
      ]
      Resource = [
        "arn:aws:bedrock:us-west-2:${data.aws_caller_identity.current.account_id}:inference-profile/us.anthropic.claude-haiku-4-5-20251001-v1:0",
        "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0",
        "arn:aws:bedrock:us-east-2::foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0",
        "arn:aws:bedrock:us-west-2::foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0",
        "arn:aws:bedrock:us-west-2::foundation-model/amazon.titan-embed-text-v2:0"
      ]
    }]
  })
}

resource "aws_iam_role_policy" "risk_trace_access" {
  name = "RiskTraceAccess"
  role = aws_iam_role.risk_agent_lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:Scan",
        "dynamodb:Query"
      ]
      Resource = [
        aws_dynamodb_table.risk_traces.arn,
        "${aws_dynamodb_table.risk_traces.arn}/index/alert-id-index"
      ]
    }]
  })
}

resource "aws_iam_role_policy" "risk_checkpoint_access" {
  name = "RiskCheckpointAccess"
  role = aws_iam_role.risk_agent_lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "dynamodb:Query",
        "dynamodb:PutItem"
      ]
      Resource = aws_dynamodb_table.risk_agent_checkpoints.arn
    }]
  })
}
