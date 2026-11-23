variable "account_id" {
  type        = string
  description = "Authorized AWS account ID."
  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "Provide an AWS account ID of exactly 12 digits."
  }
}

variable "lab_name" {
  type        = string
  description = "Unique owner-approved resource label."
  validation {
    condition     = can(regex("^cloudops-[a-z0-9][a-z0-9-]{1,32}$", var.lab_name))
    error_message = "Use a unique cloudops-prefixed lowercase lab name."
  }
}

variable "availability_zone" {
  type        = string
  description = "Approved availability zone in us-east-1."
  validation {
    condition     = can(regex("^us-east-1[a-z]$", var.availability_zone))
    error_message = "Select a us-east-1 availability zone."
  }
}

variable "ami_id" {
  type        = string
  description = "Approved Linux x86-64 EBS-backed AMI ID in us-east-1."
  validation {
    condition     = can(regex("^ami-[0-9a-f]{17}$", var.ami_id))
    error_message = "Provide the exact reviewed AMI ID."
  }
}

variable "ami_owner_id" {
  type        = string
  description = "Verified account ID of the approved AMI publisher."
  validation {
    condition     = can(regex("^[0-9]{12}$", var.ami_owner_id))
    error_message = "Provide the approved AMI publisher account ID."
  }
}

variable "instance_type" {
  type        = string
  description = "Approved x86-64 instance type meeting the measured lab requirements."
  validation {
    condition     = can(regex("^[a-z0-9]+[.][a-z0-9]+$", var.instance_type))
    error_message = "Provide an explicit instance type."
  }
}

variable "ssh_key_name" {
  type        = string
  description = "Existing approved EC2 public key name in us-east-1."
  validation {
    condition     = length(trimspace(var.ssh_key_name)) > 0
    error_message = "Provide the name of an existing approved SSH key."
  }
}

variable "operator_ipv4_cidr" {
  type        = string
  description = "One approved operator IPv4 address in /32 CIDR form."
  validation {
    condition     = try(cidrnetmask(var.operator_ipv4_cidr) == "255.255.255.255", false)
    error_message = "Provide one IPv4 host with a /32 mask."
  }
}

variable "root_volume_gib" {
  type        = number
  description = "Approved encrypted gp3 capacity in GiB."
  validation {
    condition     = var.root_volume_gib >= 20 && floor(var.root_volume_gib) == var.root_volume_gib
    error_message = "Use an integer capacity that leaves the required free disk after install."
  }
}
