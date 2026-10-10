resource "aws_iam_role" "terraform_deployment" {
  name = "TerraformDeploymentRole"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"
      }
      Action = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "terraform_deployment" {
  name = "TerraformDeploymentPolicy"
  role = aws_iam_role.terraform_deployment.id

  policy = jsonencode({
    Version = "2012-10-17"

    Statement = [
      {
        Sid    = "DynamoDB"
        Effect = "Allow"
        Action = [
          "dynamodb:CreateTable",
          "dynamodb:DeleteTable",
          "dynamodb:DescribeTable",
          "dynamodb:DescribeContinuousBackups",
          "dynamodb:DescribeTimeToLive",
          "dynamodb:ListTagsOfResource",
          "dynamodb:TagResource",
          "dynamodb:UntagResource",
          "dynamodb:UpdateTable",
          "dynamodb:UpdateTimeToLive"
        ]
        Resource = "arn:aws:dynamodb:us-west-2:${data.aws_caller_identity.current.account_id}:table/*"
      },
      {
        Sid    = "ECR"
        Effect = "Allow"
        Action = [
          "ecr:CreateRepository",
          "ecr:DeleteRepository",
          "ecr:DescribeRepositories",
          "ecr:GetRepositoryPolicy",
          "ecr:ListTagsForResource",
          "ecr:TagResource",
          "ecr:UntagResource",
          "ecr:PutImageScanningConfiguration",
          "ecr:PutImageTagMutability"
        ]
        Resource = "arn:aws:ecr:us-west-2:${data.aws_caller_identity.current.account_id}:repository/*"
      },
      {
        Sid    = "Lambda"
        Effect = "Allow"
        Action = [
          "lambda:CreateFunction",
          "lambda:DeleteFunction",
          "lambda:GetFunction",
          "lambda:GetFunctionCodeSigningConfig",
          "lambda:GetFunctionConfiguration",
          "lambda:ListTags",
          "lambda:TagResource",
          "lambda:UntagResource",
          "lambda:UpdateFunctionCode",
          "lambda:UpdateFunctionConfiguration",
          "lambda:ListVersionsByFunction"
        ]
        Resource = "arn:aws:lambda:us-west-2:${data.aws_caller_identity.current.account_id}:function:*"
      },
      {
        Sid    = "ManageRiskAgentRole"
        Effect = "Allow"
        Action = [
          "iam:GetRole",
          "iam:CreateRole",
          "iam:DeleteRole",
          "iam:UpdateAssumeRolePolicy",
          "iam:TagRole",
          "iam:UntagRole",
          "iam:ListRolePolicies",
          "iam:GetRolePolicy",
          "iam:PutRolePolicy",
          "iam:DeleteRolePolicy",
          "iam:ListAttachedRolePolicies",
          "iam:AttachRolePolicy",
          "iam:DetachRolePolicy"
        ]
        Resource = aws_iam_role.risk_agent_lambda.arn
      },
      {
        Sid      = "PassRiskAgentRoleToLambda"
        Effect   = "Allow"
        Action   = "iam:PassRole"
        Resource = aws_iam_role.risk_agent_lambda.arn

        Condition = {
          StringEquals = {
            "iam:PassedToService" = "lambda.amazonaws.com"
          }
        }
      },
      {
        Sid    = "InspectTerraformDeploymentRole"
        Effect = "Allow"
        Action = [
          "iam:GetRole",
          "iam:ListRolePolicies",
          "iam:GetRolePolicy",
          "iam:ListAttachedRolePolicies"
        ]
        Resource = aws_iam_role.terraform_deployment.arn
      },
      {
        Sid    = "TerraformStateBucket"
        Effect = "Allow"
        Action = [
          "s3:ListBucket"
        ]
        Resource = "arn:aws:s3:::hgkenlee-ai-dev-tfstate-714781437949"
      },
      {
        Sid    = "TerraformStateObjects"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject"
        ]
        Resource = "arn:aws:s3:::hgkenlee-ai-dev-tfstate-714781437949/supplychain-ai-agent/*"
      },
    ]
  })
}