variable "environment" {
  type = string
}

variable "path_prefix" {
  description = "Parameter path prefix. Scoping IAM to this path is what keeps the task role narrow."
  type        = string
  default     = "/epc"
}
