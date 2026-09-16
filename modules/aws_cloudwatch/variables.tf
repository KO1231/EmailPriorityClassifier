variable "environment" {
  type = string
}

variable "retention_days" {
  description = "Log retention. Logs carry no message content, but they do carry thread IDs."
  type        = number
  default     = 30
}

variable "dlq_name" {
  description = "Dead-letter queue to alarm on. Anything arriving there needs a person."
  type        = string
  default     = ""
}

variable "alarm_topic_arn" {
  description = "Where alarms go. Empty means the alarm exists but notifies nothing."
  type        = string
  default     = ""
}
