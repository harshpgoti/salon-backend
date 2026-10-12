# TASK-054 / FR-081 — staging environment (SKELETON ONLY — never applied).
#
# DQ-003: no cloud spend or provisioning until G-INFRA-APPROVAL passes.
# DQ-006: sandbox/recording adapters only; no real vendors, senders or
# processor accounts exist. Staging NEVER sends production traffic:
# production_sends_off = true is load-bearing here, mirrored by the
# `production-sends-off` flag asserted in DEPLOYMENT_PLAN.md.
#
# COST-PLAN (no spend approved, no ceiling set): staging is the cheapest
# topology that still rehearses migrations/rollback/restore; its quote is a
# line item in the dated calculator quote required before G-INFRA-APPROVAL.

terraform {
  # Reviewed provider versions — pinned. Do not float.
  required_version = ">= 1.9.0, < 2.0.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "5.69.0"
    }
  }
  # Remote state placeholder: encrypted, access-controlled backend resolved
  # at preflight. No local state with secrets is committed.
  # backend "s3" {} # PLACEHOLDER — bucket/key/region set at preflight.
}

provider "aws" {
  region = "ca-central-1" # proposed primary (DQ-003); owner confirms at gate.
}

module "postgres17" {
  source = "../../modules/postgres17"

  environment          = "staging"
  region               = "ca-central-1"
  production_sends_off = true # LOAD-BEARING: staging never sends production traffic.
}

module "oidc_deploy_role" {
  source = "../../modules/oidc-deploy-role"

  environment = "staging"
  # github_org/github_repo placeholders resolved at preflight (no keys).
}

# DB RLS NOTE: tenant isolation lives in DB FORCE RLS policies
# (backend/db/migrations/*), not in network topology. Staging uses synthetic
# fixture data only — never production client records.
