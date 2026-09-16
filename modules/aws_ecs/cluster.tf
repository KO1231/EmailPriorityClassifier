resource "aws_ecs_cluster" "epc" {
  name = "epc-${var.environment}"

  setting {
    name  = "containerInsights"
    value = "disabled"
  }
}
