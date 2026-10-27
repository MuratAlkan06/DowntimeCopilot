# Deliberately empty: bucket, key, region and locking come from a local, git-ignored backend.hcl at init.
#   cp backend.hcl.example backend.hcl   # then set the bucket from `terraform -chdir=bootstrap output state_bucket`
#   terraform init -backend-config=backend.hcl
# CI and `make infra-validate` run `terraform init -backend=false` and never read it.
terraform {
  backend "s3" {}
}
