# One alarm, on the one condition that always means a person should look.
#
# A message in the dead-letter queue has failed every retry. Everything else
# this tool can get wrong is either visible in the run summary or self-correcting
# on the next run, so alarming on it would train people to ignore alarms.

locals {
  alarm_actions = var.alarm_topic_arn == "" ? [] : [var.alarm_topic_arn]
}

resource "aws_cloudwatch_metric_alarm" "dead_letter" {
  count = var.dlq_name == "" ? 0 : 1

  alarm_name        = "epc-${var.environment}-dead-letter"
  alarm_description = "Mutations failed every retry and are waiting in the dead-letter queue."

  namespace   = "AWS/SQS"
  metric_name = "ApproximateNumberOfMessagesVisible"
  dimensions  = { QueueName = var.dlq_name }

  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"

  # An empty queue reports no datapoints rather than zero, and treating that as
  # a breach would alarm constantly when everything is fine.
  treat_missing_data = "notBreaching"

  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions
}
