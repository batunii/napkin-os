variable "name" {
  type = string
}

variable "region" {
  type = string
}

variable "vpc_id" {
  type = string
}

variable "subnet_id" {
  description = "A private subnet; every store's volume is created in its AZ."
  type        = string
}

variable "client_security_group_id" {
  description = "Members of this security group may reach the stores' ports."
  type        = string
}

variable "kms_key_arn" {
  description = "The stack's data key: house volume, secrets, root disk."
  type        = string
}

variable "internal_zone_id" {
  type = string
}

variable "internal_domain" {
  type = string
}

# The house corpus (ragAdded): nemotron-3-embed-1b, 2048-d, plus a BM25 sparse
# vector per point: ~8 KB dense each, so the full 11,651-case corpus at ~20k
# chunks is ~160 MB of vectors. An agency store holds only its learnings, far
# less. How many stores fit is memory, and idle Qdrant memory is not measured
# yet: measure before adding agencies past a handful.
variable "instance_type" {
  type    = string
  default = "t4g.medium"
}

# Every store runs this release. Changing it restarts each container on the new
# image; the host and the data stay.
variable "qdrant_version" {
  type    = string
  default = "v1.19.1"
}

variable "house_volume_gb" {
  type    = number
  default = 20
}

variable "house_memory_mb" {
  type    = number
  default = 1536
}

# One entry per agency, keyed by its org slug (org/acme -> "acme").
#
#   slot       1-20, never reused while the store exists: it fixes the port
#              (6340 + slot) and the EBS device, so adding an agency never
#              moves another one.
#   state      "active", or "retired" to stop the container and unmount the
#              disk. Retire first, apply, then delete the entry to delete the
#              volume (see infra/README.md, offboarding).
#   own_kms_key  its own key, so deleting the key makes every copy of its
#              vectors unreadable, snapshots included.
variable "agencies" {
  type = map(object({
    slot        = number
    volume_gb   = optional(number, 5)
    memory_mb   = optional(number, 512)
    own_kms_key = optional(bool, true)
    state       = optional(string, "active")
  }))
  default = {}

  validation {
    condition     = alltrue([for k in keys(var.agencies) : can(regex("^[a-z0-9][a-z0-9-]{1,30}$", k)) && k != "house"])
    error_message = "Agency keys are org slugs: lowercase letters, digits and dashes; \"house\" is reserved."
  }
  validation {
    condition     = alltrue([for a in values(var.agencies) : a.slot >= 1 && a.slot <= 20 && floor(a.slot) == a.slot])
    error_message = "slot must be a whole number from 1 to 20."
  }
  validation {
    condition     = length(distinct([for a in values(var.agencies) : a.slot])) == length(var.agencies)
    error_message = "Two agencies share a slot."
  }
  validation {
    condition     = alltrue([for a in values(var.agencies) : contains(["active", "retired"], a.state)])
    error_message = "state is \"active\" or \"retired\"."
  }
}

variable "snapshot_retention_days" {
  type    = number
  default = 14
}
