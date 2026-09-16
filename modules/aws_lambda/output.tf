output "apply_function" {
  value = {
    name = aws_lambda_function.apply.function_name
    arn  = aws_lambda_function.apply.arn
  }
}
