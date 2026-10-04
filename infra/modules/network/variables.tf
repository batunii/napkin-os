variable "name" {
  type = string
}

variable "region" {
  type = string
}

variable "cidr" {
  type    = string
  default = "10.40.0.0/16"
}

# Aurora needs subnets in at least two AZs, even with a single instance.
variable "az_count" {
  type    = number
  default = 2
  validation {
    condition     = var.az_count >= 2 && var.az_count <= 3
    error_message = "az_count must be 2 or 3."
  }
}

variable "internal_domain" {
  type    = string
  default = "napkin.internal"
}
