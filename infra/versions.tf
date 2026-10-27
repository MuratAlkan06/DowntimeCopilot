# Main stack: the app's AWS resources and its least-privilege runtime role. Remote state in the bucket made by
# infra/bootstrap (backend.tf; S3 native locking needs Terraform >= 1.11). Applied with profile downtime-admin.
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
