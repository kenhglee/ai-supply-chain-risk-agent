terraform {
  required_version = ">= 1.16.0"

  backend "s3" {
    bucket       = "hgkenlee-ai-dev-tfstate-714781437949"
    key          = "supplychain-ai-agent/terraform.tfstate"
    region       = "us-west-2"
    encrypt      = true
    use_lockfile = true
  }

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}