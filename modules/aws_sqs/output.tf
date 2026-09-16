output "mutations" {
  value = {
    name = aws_sqs_queue.mutations.name
    url  = aws_sqs_queue.mutations.url
    arn  = aws_sqs_queue.mutations.arn
  }
}

output "mutations_dlq" {
  value = {
    name = aws_sqs_queue.mutations_dlq.name
    url  = aws_sqs_queue.mutations_dlq.url
    arn  = aws_sqs_queue.mutations_dlq.arn
  }
}
