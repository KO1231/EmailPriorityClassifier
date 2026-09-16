resource "aws_scheduler_schedule" "classify" {
  name       = "epc-${var.environment}-classify"
  state      = var.enabled ? "ENABLED" : "DISABLED"
  group_name = "default"

  schedule_expression          = var.schedule_expression
  schedule_expression_timezone = var.schedule_timezone

  # A batch job, so a late start is better than two overlapping runs. Overlap
  # is safe — label exclusion and natural idempotency see to that — but it is
  # wasted model spend.
  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = var.cluster_arn
    role_arn = var.role_arn

    ecs_parameters {
      task_definition_arn = var.task_definition_arn
      launch_type         = "FARGATE"
      task_count          = 1

      network_configuration {
        subnets          = var.network_configuration.subnets
        security_groups  = var.network_configuration.security_groups
        assign_public_ip = var.network_configuration.assign_public_ip
      }
    }

    retry_policy {
      # The next scheduled run picks up whatever this one missed, so retrying
      # for hours would only stack duplicate work.
      maximum_retry_attempts       = 2
      maximum_event_age_in_seconds = 900
    }
  }
}
