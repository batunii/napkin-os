variable "name" {
  type = string
}

variable "vpc_id" {
  type = string
}

variable "subnet_ids" {
  type = list(string)
}

variable "client_security_group_id" {
  description = "Members of this security group may connect on 5432."
  type        = string
}

variable "kms_key_arn" {
  type = string
}

# 0 ACU (pause) needs Aurora PostgreSQL >= 16.3 / 15.7 / 14.12 / 13.15.
# List what the region offers with:
#   aws rds describe-db-engine-versions --engine aurora-postgresql \
#     --query 'DBEngineVersions[].EngineVersion'
variable "engine_version" {
  type    = string
  default = "16.8"
}

variable "database_name" {
  type    = string
  default = "napkin"
}

variable "min_acu" {
  description = "0 lets the cluster pause when idle; 0.5 is the smallest always-on size."
  type        = number
  default     = 0
}

variable "max_acu" {
  description = "The cost ceiling: at most this many ACUs are ever billed per hour."
  type        = number
  default     = 4
}

variable "seconds_until_auto_pause" {
  type    = number
  default = 1800
  validation {
    condition     = var.seconds_until_auto_pause >= 300 && var.seconds_until_auto_pause <= 86400
    error_message = "Aurora accepts 300 to 86400 seconds."
  }
}

variable "backup_retention_days" {
  type    = number
  default = 7
}

variable "deletion_protection" {
  type    = bool
  default = true
}
