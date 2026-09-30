variable "name" {
  type = string
}

variable "agencies" {
  description = "Agency org slugs; each gets napkin_agency_<slug>."
  type        = list(string)
}

variable "host" {
  type = string
}

variable "port" {
  type = number
}

variable "master_secret_arn" {
  type = string
}

variable "kms_key_arn" {
  type = string
}
