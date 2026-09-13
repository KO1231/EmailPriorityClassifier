# Named to match Delibird's convention: /aws/<service>/<project>-<env>-<name>.
resource "aws_cloudwatch_log_group" "classify" {
  name              = "/aws/ecs/epc-${var.environment}-classify"
  retention_in_days = var.retention_days
}

resource "aws_cloudwatch_log_group" "apply" {
  name              = "/aws/lambda/epc-${var.environment}-apply"
  retention_in_days = var.retention_days
}
