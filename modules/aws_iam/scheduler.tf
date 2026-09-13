# EventBridge Scheduler starts the task. It may start that one task definition
# and pass those two roles, and it may do nothing else.

data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "epc-${var.environment}-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

variable "task_definition_arn" {
  description = "The task definition the scheduler may run. Without the revision, so a new revision does not need a policy change."
  type        = string
}

data "aws_iam_policy_document" "scheduler" {
  statement {
    sid       = "RunTheTask"
    actions   = ["ecs:RunTask"]
    resources = ["${var.task_definition_arn}:*"]
  }

  statement {
    sid     = "PassTheTaskRoles"
    actions = ["iam:PassRole"]
    resources = [
      aws_iam_role.ecs_execution.arn,
      aws_iam_role.ecs_task.arn,
    ]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "scheduler" {
  name   = "epc-${var.environment}-scheduler"
  role   = aws_iam_role.scheduler.id
  policy = data.aws_iam_policy_document.scheduler.json
}
