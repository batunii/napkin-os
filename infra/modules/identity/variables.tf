variable "name" {
  description = "Prefix for every resource, e.g. napkin-staging"
  type        = string
}

variable "kms_key_arn" {
  description = "The customer key that encrypts the session secret"
  type        = string
}

variable "deletion_protection" {
  description = "Refuse to delete the user pool (on for anything with real accounts)"
  type        = bool
  default     = true
}
