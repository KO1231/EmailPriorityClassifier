variable "environment" { type = string }
variable "image" {
  description = "Fully qualified image reference. Pin by digest in prod: a tag that moves under a scheduled task is a deploy nobody performed."
  type        = string
}
variable "execution_role_arn" { type = string }
variable "task_role_arn" { type = string }
variable "log_group_name" { type = string }
variable "aws_region" { type = string }

variable "cpu" {
  description = "Fargate CPU units. The work is HTTP wait, so this buys concurrency headroom rather than throughput."
  type        = string
  default     = "512"
}
variable "memory" {
  type    = string
  default = "1024"
}

variable "subnet_ids" {
  description = "Subnets with a route to the internet — Gmail and the model provider are both external."
  type        = list(string)
}
variable "security_group_ids" { type = list(string) }
variable "assign_public_ip" {
  description = "True for public subnets; false when a NAT gateway provides egress."
  type        = bool
  default     = true
}

variable "environment_variables" {
  description = "Non-secret settings, as EPC__-prefixed overrides."
  type        = map(string)
  default     = {}
}

variable "secret_parameters" {
  description = "Secrets injected by ECS from SSM, so they never appear in the task definition."
  type        = map(string)
  default     = {}
}
