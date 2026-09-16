variable "environment" {
  description = "Environment name, used as a suffix on every resource."
  type        = string
}

variable "untagged_image_days" {
  description = "How long an untagged image survives before the lifecycle policy removes it."
  type        = number
  default     = 7
}

variable "tagged_image_count" {
  description = "How many tagged images to keep. Enough to roll back, not enough to pay for history."
  type        = number
  default     = 10
}
