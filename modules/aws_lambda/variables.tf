variable "environment" { type = string }
variable "image" {
  description = "The same image the classify task runs. One artefact, two entrypoints."
  type        = string
}
variable "role_arn" { type = string }
variable "log_group_name" { type = string }
variable "queue_arn" { type = string }

variable "timeout_seconds" {
  description = "Must stay below the queue's visibility timeout, or a slow apply becomes a duplicate apply."
  type        = number
  default     = 120
}

variable "memory_mb" {
  type    = number
  default = 512
}

variable "batch_size" {
  description = "FIFO event source mappings cap this at 10, which is why the applier batches again inside the invocation."
  type        = number
  default     = 10
}

variable "maximum_concurrency" {
  description = "Caps how fast Gmail is written to, independently of how fast classification produces work."
  type        = number
  default     = 2
}

variable "environment_variables" {
  type    = map(string)
  default = {}
}
