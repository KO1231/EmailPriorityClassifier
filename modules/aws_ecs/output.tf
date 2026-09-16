output "cluster" {
  value = { name = aws_ecs_cluster.epc.name, arn = aws_ecs_cluster.epc.arn }
}

output "task_definition" {
  value = {
    family = aws_ecs_task_definition.classify.family
    arn    = aws_ecs_task_definition.classify.arn
    # Without the revision, so a new revision does not need an IAM change.
    arn_without_revision = replace(aws_ecs_task_definition.classify.arn, "/:[0-9]+$/", "")
  }
}

output "network_configuration" {
  value = {
    subnets          = var.subnet_ids
    security_groups  = var.security_group_ids
    assign_public_ip = var.assign_public_ip
  }
}
