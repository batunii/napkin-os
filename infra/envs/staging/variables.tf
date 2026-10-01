variable "env" {
  type    = string
  default = "staging"
}

# Ireland: close to the team, EU data for EU agencies. No residency constraint
# is known yet; changing region later means recreating the stack.
variable "region" {
  type    = string
  default = "eu-west-1"
}

variable "vpc_cidr" {
  type    = string
  default = "10.40.0.0/16"
}

variable "aurora_engine_version" {
  type    = string
  default = "16.15"
}

variable "aurora_min_acu" {
  type    = number
  default = 0
}

variable "aurora_max_acu" {
  type    = number
  default = 4
}

variable "aurora_seconds_until_auto_pause" {
  type    = number
  default = 1800
}

variable "deletion_protection" {
  type    = bool
  default = true
}

variable "rag_instance_type" {
  type    = string
  default = "t4g.medium"
}

variable "house_volume_gb" {
  type    = number
  default = 20
}

# The agencies this stack serves, keyed by org slug. Each gets a RAG learnings
# store (its own Qdrant, disk, key and port) and napkin_agency_<slug>.
# See modules/rag-host/variables.tf for the fields and the offboarding order.
variable "agencies" {
  type = map(object({
    slot        = number
    volume_gb   = optional(number, 5)
    memory_mb   = optional(number, 512)
    own_kms_key = optional(bool, true)
    state       = optional(string, "active")
  }))
  default = {}
}

variable "qdrant_version" {
  type    = string
  default = "v1.19.1"
}

variable "budget_emails" {
  description = "Where budget alerts go; empty = no budgets."
  type        = list(string)
  default     = []
}

# Two budgets, because promotional credits pay most of the bill:
#   burn  everything the stack uses, credits or not: are we spending the credit
#         faster than planned?
#   card  only what the credits do not cover: is real money leaving the card?
variable "monthly_budget_usd" {
  description = "The burn budget: gross monthly cost, credits ignored."
  type        = number
  default     = 150
}

variable "monthly_card_budget_usd" {
  description = "The card budget: monthly cost after credits."
  type        = number
  default     = 5
}
