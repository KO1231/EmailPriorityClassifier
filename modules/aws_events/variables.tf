variable "environment" { type = string }
variable "schedule_expression" {
  description = "When to classify. Hourly is usually enough: a run with no new mail costs one listing, but each new thread costs a model call."
  type        = string
  default     = "rate(1 hour)"
}
variable "schedule_timezone" {
  type    = string
  default = "Asia/Tokyo"
}
variable "enabled" {
  description = "Start disabled. A schedule that begins firing the moment it is created gives nobody a chance to read the first dry run."
  type        = bool
  default     = false
}
variable "cluster_arn" { type = string }
variable "task_definition_arn" { type = string }
variable "role_arn" { type = string }
variable "network_configuration" {
  type = object({
    subnets          = list(string)
    security_groups  = list(string)
    assign_public_ip = bool
  })
}
