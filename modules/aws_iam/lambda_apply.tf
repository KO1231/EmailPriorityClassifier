# The apply worker reads the queue and writes Gmail labels. It classifies
# nothing, so it has no model permissions and no need for the API key — which
# is the whole reason `actions/` and `dispatch/` may not import `classify/`.

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda_apply" {
  name               = "epc-${var.environment}-lambda-apply"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "lambda_apply" {
  statement {
    sid = "ConsumeMutations"
    actions = [
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:GetQueueAttributes",
      "sqs:ChangeMessageVisibility",
    ]
    resources = [var.mutations_queue.arn]
  }

  # Gmail credentials only. Not the model key: this half never calls a model.
  statement {
    sid       = "ReadGmailCredentials"
    actions   = ["ssm:GetParameter"]
    resources = [var.gmail_credentials.arn]
  }

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

  statement {
    sid       = "WriteOwnLogs"
    actions   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
    resources = [for arn in var.log_group_arns : "${arn}:*"]
  }
}

resource "aws_iam_role_policy" "lambda_apply" {
  name   = "epc-${var.environment}-lambda-apply"
  role   = aws_iam_role.lambda_apply.id
  policy = data.aws_iam_policy_document.lambda_apply.json
}
