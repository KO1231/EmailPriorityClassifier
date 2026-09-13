output "ecs_execution_role" {
  value = { name = aws_iam_role.ecs_execution.name, arn = aws_iam_role.ecs_execution.arn }
}

output "ecs_task_role" {
  value = { name = aws_iam_role.ecs_task.name, arn = aws_iam_role.ecs_task.arn }
}

output "lambda_apply_role" {
  value = { name = aws_iam_role.lambda_apply.name, arn = aws_iam_role.lambda_apply.arn }
}

output "scheduler_role" {
  value = { name = aws_iam_role.scheduler.name, arn = aws_iam_role.scheduler.arn }
}
