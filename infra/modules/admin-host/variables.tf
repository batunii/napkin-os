variable "name" {
  type = string
}

variable "vpc_id" {
  type = string
}

variable "vpc_cidr" {
  type = string
}

variable "subnet_id" {
  type = string
}

variable "extra_security_group_ids" {
  description = "Groups that grant access to the data stores (the data-clients group)."
  type        = list(string)
  default     = []
}

variable "secret_arns" {
  type = list(string)
}

variable "parameter_arns" {
  type    = list(string)
  default = []
}

variable "kms_key_arn" {
  type = string
}

variable "corpus_bucket_arn" {
  type = string
}

variable "instance_type" {
  type    = string
  default = "t4g.small"
}

variable "policy_arns" {
  description = "Managed policies attached to the host's role, by a fixed name (the ARNs are only known after apply), e.g. model invocation for the seeding job."
  type        = map(string)
  default     = {}
}
