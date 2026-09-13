variable "environment" {
  type = string
}

variable "max_receive_count" {
  description = "Attempts before a message is moved to the dead-letter queue."
  type        = number
  default     = 5
}

variable "visibility_timeout_seconds" {
  description = "How long a received message is hidden. Must exceed the consumer's own timeout, or a slow apply becomes a duplicate apply."
  type        = number
  default     = 180
}

variable "retention_seconds" {
  description = "How long an unhandled message survives. Long enough to notice and fix the cause."
  type        = number
  default     = 345600 # 4 days
}
