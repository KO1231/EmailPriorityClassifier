# Two roles, and the split matters: the *execution* role is what ECS itself
# uses to start the task — pull the image, create log streams. The *task* role
# is what the program running inside uses. Merging them would hand the program
# permission to pull images and read any secret the executor can.

data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "ecs_execution" {
  name               = "epc-${var.environment}-ecs-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy_attachment" "ecs_execution" {
  role       = aws_iam_role.ecs_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role" "ecs_task" {
  name               = "epc-${var.environment}-ecs-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

data "aws_iam_policy_document" "ecs_task" {
  # Read its own credentials, and nothing else's. Scoped to exact parameter
  # ARNs rather than the path, so a new parameter under /epc is not readable
  # by accident.
  statement {
    sid     = "ReadOwnParameters"
    actions = ["ssm:GetParameter", "ssm:GetParameters"]
    resources = [
      var.gmail_credentials.arn,
      var.openai_api_key.arn,
      var.state_parameter.arn,
    ]
  }

  # SecureString parameters are KMS-encrypted. Required outright for a
  # customer-managed key, and harmless to state for the AWS-managed one.
  statement {
    sid       = "DecryptParameters"
    actions   = ["kms:Decrypt"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["ssm.${data.aws_region.current.region}.amazonaws.com"]
    }
  }

  # Run state — the threads that keep failing — is written back after each run.
  statement {
    sid       = "WriteRunState"
    actions   = ["ssm:PutParameter"]
    resources = [var.state_parameter.arn]
  }

  statement {
    sid       = "EnqueueMutations"
    actions   = ["sqs:SendMessage", "sqs:GetQueueUrl", "sqs:GetQueueAttributes"]
    resources = [var.mutations_queue.arn]
  }

  statement {
    sid       = "WriteOwnLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = [for arn in var.log_group_arns : "${arn}:*"]
  }

  dynamic "statement" {
    for_each = length(var.bedrock_model_arns) > 0 ? [1] : []
    content {
      sid       = "InvokeNamedModels"
      actions   = ["bedrock:InvokeModel", "bedrock:Converse"]
      resources = var.bedrock_model_arns
    }
  }
}

resource "aws_iam_role_policy" "ecs_task" {
  name   = "epc-${var.environment}-ecs-task"
  role   = aws_iam_role.ecs_task.id
  policy = data.aws_iam_policy_document.ecs_task.json
}

data "aws_region" "current" {}
