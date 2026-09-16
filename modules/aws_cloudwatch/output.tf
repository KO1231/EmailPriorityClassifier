output "classify_log_group" {
  value = { name = aws_cloudwatch_log_group.classify.name, arn = aws_cloudwatch_log_group.classify.arn }
}

output "apply_log_group" {
  value = { name = aws_cloudwatch_log_group.apply.name, arn = aws_cloudwatch_log_group.apply.arn }
}

output "log_group_arns" {
  value = [aws_cloudwatch_log_group.classify.arn, aws_cloudwatch_log_group.apply.arn]
}
