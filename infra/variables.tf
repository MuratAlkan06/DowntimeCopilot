variable "aws_profile" {
  description = "AWS CLI profile that assumes the downtime-copilot-admin role (made by infra/bootstrap)."
  type        = string
  default     = "downtime-admin"
}

variable "region" {
  description = "Region for every resource. us-east-1 is the app's default (app/config.py aws_region), so no code change."
  type        = string
  default     = "us-east-1"
}

variable "deploy_user_name" {
  description = "Existing IAM user allowed to assume the runtime role (its ARN is built from the caller's account ID)."
  type        = string
  default     = "mlobs-deploy"
}

variable "budget_limit_usd" {
  description = "Monthly cost budget in USD; alerts at 50, 80 and 100 percent of actual spend."
  type        = string
  default     = "10.0"
}

variable "budget_email" {
  description = "Budget alert recipient. No default: set it in the git-ignored terraform.tfvars (see terraform.tfvars.example)."
  type        = string

  validation {
    condition     = can(regex("^[^@[:space:]]+@[^@[:space:]]+\\.[^@[:space:]]+$", var.budget_email))
    error_message = "budget_email must be an email address."
  }
}
