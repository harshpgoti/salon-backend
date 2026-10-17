# TASK-054 / FR-081 — prod environment (SKELETON ONLY — never applied).
#
# D-016: ca-central-1 is owner-selected; no cloud spend or provisioning until G-INFRA-APPROVAL passes, and
# production actions additionally gate on G-RELEASE. This file declares shape
# only; distinct prod account/credentials/data/send configuration required.
#
# DQ-006: production integrations stay gated; sandbox/recording adapters are
# the only approved delivery paths until owners name processors (TASK-029,
# TASK-031, TASK-055, TASK-060) and contracts are reviewed.
#
# COST-PLAN (open-ended funding intent, no apply approved): prod quote covers Multi-AZ
# DB + 35-day PITR, ALB, NAT/egress, 2-AZ Fargate tasks, SQS/DLQ, S3/KMS,
# logs/traces, Secrets Manager, provider volume and support tier at
# 5/50/250/1000 salons. Owner sets monthly ceiling + 70/90/100% alerts.

terraform {
  # Reviewed provider versions — pinned. Do not float.
  required_version = ">= 1.9.0, < 2.0.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "6.68.0"
    }
  }
  # Remote state placeholder: encrypted, access-controlled backend resolved
  # at preflight. Prod state is separate from staging state. Never shared.
  # backend "s3" {} # PLACEHOLDER — bucket/key/region set at preflight.
}

provider "aws" {
  region = "ca-central-1" # owner-selected primary (D-016); apply remains gated.
}

module "postgres17" {
  source = "../../modules/postgres17"

  environment = "prod"
  region      = "ca-central-1"
  # Gated flag: remains true until the owner-approved production action in
  # DEPLOYMENT_PLAN.md step 5 flips it with a reviewed plan + resource ledger.
  production_sends_off = true
}

module "oidc_deploy_role" {
  source = "../../modules/oidc-deploy-role"

  environment = "prod"
  # github_org/github_repo placeholders resolved at preflight (no keys).
  # Prod deploys additionally require the named release gates + owner-approved
  # production action (DEPLOYMENT_PLAN.md CI/CD section). OIDC only — no keys.
}

# DB RLS NOTE: tenant isolation lives in DB FORCE RLS policies
# (backend/db/migrations/*), not in network topology. No cross-env data copy:
# staging fixture data never flows into prod; prod restores replay deletion
# tombstones (backend/src/modules/crm/privacy.py) so deleted subjects stay
# deleted.
