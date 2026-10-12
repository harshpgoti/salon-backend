# TASK-054 / FR-081 — GitHub OIDC deploy role (SKELETON ONLY — never applied).
#
# DQ-003: no provisioning until G-INFRA-APPROVAL. No keys, no secrets, no
# static AWS credentials anywhere in this workspace. CI deploys assume this
# role via GitHub OIDC federation; static access keys are never created.
#
# COST-PLAN: IAM/OIDC has no hourly cost; listed here for completeness only.

terraform {
  # Reviewed provider versions — pinned. Do not float.
  required_version = ">= 1.9.0, < 2.0.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "5.69.0"
    }
  }
}

variable "environment" {
  description = "Environment name, e.g. staging or prod."
  type        = string
}

variable "github_org" {
  description = "Placeholder: GitHub org that owns the repo (set at preflight, never hard-code secrets here)."
  type        = string
  default     = "PLACEHOLDER-ORG"
}

variable "github_repo" {
  description = "Placeholder: repo permitted to assume this role."
  type        = string
  default     = "PLACEHOLDER-REPO"
}

# SKELETON: declared for review only. NOT APPLIED. Thumbprints/ARNs are
# placeholders resolved at approved-provisioning time.
resource "aws_iam_role" "github_deploy" {
  name                 = "salon-${var.environment}-github-deploy"
  description          = "OIDC-federated deploy role for salon ${var.environment}. No static keys."
  max_session_duration = 3600

  # Trust policy placeholder: federated to GitHub OIDC, scoped to this
  # org/repo/environment. Final sub-condition reviewed at preflight.
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = "PLACEHOLDER-OIDC-PROVIDER-ARN" }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        }
        StringLike = {
          "token.actions.githubusercontent.com:sub" = "repo:${var.github_org}/${var.github_repo}:environment:${var.environment}"
        }
      }
    }]
  })

  tags = {
    Environment = var.environment
    Product     = "salon-operations-cloud"
    ManagedBy   = "terraform-skeleton-unapplied"
  }
}

output "deploy_role_arn_placeholder" {
  description = "Placeholder — no role exists; ARN recorded here after approved provisioning."
  value       = "UNPROVISIONED-salon-${var.environment}-github-deploy"
}
