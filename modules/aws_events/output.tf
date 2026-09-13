output "schedule" {
  value = { name = aws_scheduler_schedule.classify.name, arn = aws_scheduler_schedule.classify.arn }
}
