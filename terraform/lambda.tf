resource "aws_lambda_function" "risk_agent" {
  function_name = "ai-supply-chain-risk-agent"
  package_type  = "Image"

  image_uri = "${aws_ecr_repository.risk_agent.repository_url}@sha256:6edb8b9b12f6dc608bbc4967e47cce8cc44c482fd0bd71367c72f96f3bd7870c"

  role = aws_iam_role.risk_agent_lambda.arn

  architectures = ["x86_64"]
  memory_size   = 1028
  timeout       = 123

  ephemeral_storage {
    size = 512
  }

  environment {
    variables = {
      LOG_LEVEL          = "INFO"
      RISK_STATE_BACKEND = "dynamodb"
      RISK_STATE_TABLE   = aws_dynamodb_table.supplier_risk_state.name

      TRACE_STORE_BACKEND = "dynamodb"
      RISK_TRACES_TABLE   = aws_dynamodb_table.risk_traces.name

      LLM_PROVIDER     = "bedrock"
      BEDROCK_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
      RUNTIME_DIR      = "/tmp"
    }
  }
}