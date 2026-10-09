resource "aws_lambda_function" "risk_agent" {
  function_name = "ai-supply-chain-risk-agent"
  package_type  = "Image"

  image_uri = "016257616089.dkr.ecr.us-west-2.amazonaws.com/ai-supply-chain-risk-agent@sha256:acb5416dcedfb980c4ce923034d1455481e9e08363e3a65b0e1f1cc1a1ffa8f5"

  role          = "arn:aws:iam::016257616089:role/service-role/ai-supply-chain-risk-agent-role-ed9d5kk0"
  timeout       = 123
  memory_size   = 1028
  architectures = ["x86_64"]

  ephemeral_storage {
    size = 512
  }

  lifecycle {
    ignore_changes = [
      environment
    ]
  }
}