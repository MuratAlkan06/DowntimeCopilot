# Bootstrap stack: LOCAL state (terraform.tfstate here, git-ignored), applied once by the account owner
# from a root login session (profile var.aws_profile, default "bootstrap"). It creates what the main stack
# needs before it can run: the S3 state bucket and the admin role the deploy user assumes.
terraform {
  required_version = ">= 1.11"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

provider "aws" {
  profile = var.aws_profile
  region  = var.region

  default_tags {
    tags = {
      project = "downtime-copilot"
    }
  }
}
