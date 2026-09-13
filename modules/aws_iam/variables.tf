variable "environment" {
  type = string
}

variable "ssm_prefix" {
  description = "Parameter path prefix the task may read."
  type        = string
}

variable "gmail_credentials" {
  type = object({ name = string, arn = string })
}

variable "openai_api_key" {
  type = object({ name = string, arn = string })
}

variable "state_parameter" {
  type = object({ name = string, arn = string })
}

variable "mutations_queue" {
  type = object({ name = string, url = string, arn = string })
}

variable "log_group_arns" {
  description = "Log groups these roles may write to. Named rather than wildcarded."
  type        = list(string)
}

variable "bedrock_model_arns" {
  description = "Bedrock models the task may invoke. Empty when the OpenAI backend is used."
  type        = list(string)
  default     = []
}
