# FIFO, and the ordering is the point rather than the deduplication.
#
# Once a rule can *remove* a label, an add and a remove racing on the same
# thread is a real bug. `MessageGroupId` is the thread ID, so ordering holds
# within a thread while different threads still process in parallel.
#
# The five-minute dedup window suppresses duplicates from a retry storm. It is
# not a durable "never twice" guarantee and nothing depends on one: applying a
# label twice is a no-op, and already-labelled threads are excluded from the
# next run anyway.

resource "aws_sqs_queue" "mutations_dlq" {
  name       = "epc-${var.environment}-mutations-dlq.fifo"
  fifo_queue = true

  # Longer than the main queue: a message reaching here is one somebody has to
  # look at, and it should still be there when they do.
  message_retention_seconds = 1209600 # 14 days
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue" "mutations" {
  name       = "epc-${var.environment}-mutations.fifo"
  fifo_queue = true

  # Deduplication ids are supplied by the producer (thread plus the exact
  # change requested), not derived from the body — two different changes to one
  # thread must not look like a duplicate.
  content_based_deduplication = false

  visibility_timeout_seconds = var.visibility_timeout_seconds
  message_retention_seconds  = var.retention_seconds
  sqs_managed_sse_enabled    = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.mutations_dlq.arn
    maxReceiveCount     = var.max_receive_count
  })
}

# Only this queue may write to the dead-letter queue.
resource "aws_sqs_queue_redrive_allow_policy" "dlq" {
  queue_url = aws_sqs_queue.mutations_dlq.id

  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.mutations.arn]
  })
}
