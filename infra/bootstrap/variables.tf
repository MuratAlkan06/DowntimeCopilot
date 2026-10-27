variable "aws_profile" {
  description = "AWS CLI profile for the one-time bootstrap apply (a root login session; never stored long-lived keys)."
  type        = string
  default     = "bootstrap"
}

variable "region" {
  description = "Region for the state bucket. us-east-1 matches the app's default (app/config.py aws_region)."
  type        = string
  default     = "us-east-1"
}

variable "deploy_user_name" {
  description = "Existing IAM user allowed to assume the admin role (its ARN is built from the caller's account ID)."
  type        = string
  default     = "mlobs-deploy"
}

variable "require_admin_mfa" {
  description = "Require MFA on the session assuming the admin role. Disable only if the deploy user has no MFA device, and record why in docs/decisions.md."
  type        = bool
  default     = true
}
