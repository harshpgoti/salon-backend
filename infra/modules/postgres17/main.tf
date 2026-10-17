# TASK-054 / FR-081 — PostgreSQL 17 module (SKELETON ONLY — never applied).
#
# D-016: owner selected ca-central-1; no cloud spend or provisioning until G-INFRA-APPROVAL passes.
# This module declares intent only; no `terraform apply` has run and no
# resource exists. See .loop-engineer/DEPLOYMENT_PLAN.md "Resource ledger".
#
# DB RLS NOTE: tenant isolation is enforced in the DATABASE, not in this
# module. Every tenant-owned table carries FORCE ROW LEVEL SECURITY with an
# `app.tenant_id` policy (see backend/db/migrations/*, e.g.
# 028_consents_suppressions.sql, 015_privacy_requests.sql). The RDS instance
# below provides encryption/TLS/backups; the application role `salon_app`
# receives only per-table GRANTs declared in each migration. Do NOT rely on
# security groups or subnet placement as a tenancy boundary.
#
# COST-PLAN COMMENT BLOCK (owner accepts open-ended spend in principle; no apply is approved yet):
#   - Engine: PostgreSQL 17 on Amazon RDS, Multi-AZ (commercial topology).
#   - Persistent cost drivers to quote before G-INFRA-APPROVAL: Multi-AZ DB
#     instance hours + storage, PITR backup retention (target 35 days),
#     ALB + NAT/egress, ECS Fargate tasks (web/API/worker x2 AZ), SQS/DLQ,
#     S3 + KMS, CloudWatch logs/traces, Secrets Manager, SES/SMS/WhatsApp
#     provider volume, support tier. Quote at 5/50/250/1000 salons.
#   - Alerts at 70/90/100% of the reviewed monthly estimate. No fixed cap is
#     set; the alerts inform and do not block or authorize changes.
#   - Per-tenant message budgets reserve cost before send (backend budgets
#     module); over-ceiling sends fail visibly instead of spending.

terraform {
  # Reviewed provider versions — pinned. Do not float.
  required_version = ">= 1.9.0, < 2.0.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "6.68.0"
    }
  }
}

# Placeholder only — replaced by the env caller (staging/prod). No default
# credentials exist anywhere in this skeleton.
variable "environment" {
  description = "Environment name, e.g. staging or prod. No other values permitted."
  type        = string
}

variable "region" {
  description = "Owner-selected AWS region; currently ca-central-1 (D-016)."
  type        = string
  default     = "ca-central-1"
}

variable "instance_class" {
  description = "RDS instance class placeholder — sized at preflight, not here."
  type        = string
  default     = "db.t4g.medium"
}

variable "allocated_storage_gb" {
  description = "Allocated storage placeholder (GB)."
  type        = number
  default     = 100
}

variable "backup_retention_days" {
  description = "PITR backup target (proposed 35 days; measured in TASK-053)."
  type        = number
  default     = 35
}

variable "production_sends_off" {
  description = "When true, production provider sends are disabled (staging default true; prod gated by release)."
  type        = bool
  default     = true
}

# SKELETON: resource declared for plan-shape review only. NOT APPLIED.
resource "aws_db_instance" "salon_postgres17" {
  identifier              = "salon-${var.environment}-postgres17"
  engine                  = "postgres"
  engine_version          = "17"
  instance_class          = var.instance_class
  allocated_storage       = var.allocated_storage_gb
  storage_encrypted       = true
  multi_az                = true
  publicly_accessible     = false
  backup_retention_period = var.backup_retention_days
  deletion_protection     = true
  # Credentials are NEVER in files: injected via Secrets Manager at deploy.
  # username = <from secrets> / password = <from secrets> — placeholders only.
  skip_final_snapshot = false

  tags = {
    Environment        = var.environment
    Product            = "salon-operations-cloud"
    ProductionSendsOff = tostring(var.production_sends_off)
    ManagedBy          = "terraform-skeleton-unapplied"
  }
}

output "db_endpoint_placeholder" {
  description = "Placeholder — no instance exists; endpoint recorded here after approved provisioning."
  value       = "UNPROVISIONED-salon-${var.environment}-postgres17"
}
