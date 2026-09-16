# The apply half. It reads the queue and writes Gmail labels; it classifies
# nothing, so it needs neither a model credential nor an LLM SDK — the import
# boundary asserted in tests/unit/test_layering.py is what makes that true.

resource "aws_lambda_function" "apply" {
  function_name = "epc-${var.environment}-apply"
  role          = var.role_arn

  package_type = "Image"
  image_uri    = var.image

  image_config {
    # The image's ENTRYPOINT is the CLI; Lambda needs a handler instead.
    entry_point = ["/opt/venv/bin/python", "-m", "awslambdaric"]
    command     = ["epc.aws.apply_handler.handler"]
  }

  architectures = ["arm64"]
  timeout       = var.timeout_seconds
  memory_size   = var.memory_mb

  environment {
    variables = var.environment_variables
  }

  logging_config {
    log_format = "JSON"
    log_group  = var.log_group_name
  }
}

resource "aws_lambda_event_source_mapping" "mutations" {
  event_source_arn = var.queue_arn
  function_name    = aws_lambda_function.apply.arn

  batch_size = var.batch_size

  scaling_config {
    maximum_concurrency = var.maximum_concurrency
  }

  # A failed batch reports which messages failed rather than replaying all ten,
  # so one bad mutation does not drag nine good ones back through the queue.
  function_response_types = ["ReportBatchItemFailures"]
}
