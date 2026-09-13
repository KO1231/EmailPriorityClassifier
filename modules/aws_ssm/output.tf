output "prefix" {
  description = "Parameter path prefix, for scoping IAM."
  value       = local.prefix
}

output "gmail_credentials" {
  value = { name = aws_ssm_parameter.gmail_credentials.name, arn = aws_ssm_parameter.gmail_credentials.arn }
}

output "openai_api_key" {
  value = { name = aws_ssm_parameter.openai_api_key.name, arn = aws_ssm_parameter.openai_api_key.arn }
}

output "state" {
  value = { name = aws_ssm_parameter.state.name, arn = aws_ssm_parameter.state.arn }
}
