# A scheduled task, not a service: one invocation is one batch, and there is
# nothing to keep running in between.

resource "aws_ecs_task_definition" "classify" {
  family                   = "epc-${var.environment}-classify"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.cpu
  memory                   = var.memory

  runtime_platform {
    operating_system_family = "LINUX"
    # ARM is cheaper on Fargate, and the image built locally on Apple silicon
    # is the same architecture — so what is tested is what runs.
    cpu_architecture = "ARM64"
  }

  execution_role_arn = var.execution_role_arn
  task_role_arn      = var.task_role_arn

  container_definitions = jsonencode([
    {
      name      = "epc"
      image     = var.image
      essential = true

      # The image's ENTRYPOINT is `epc`; this is the subcommand.
      command = ["run"]

      environment = [
        for name, value in var.environment_variables : { name = name, value = value }
      ]

      # Injected by ECS at start. The values never enter the task definition,
      # so they never enter Terraform state either.
      secrets = [
        for name, arn in var.secret_parameters : { name = name, valueFrom = arn }
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = var.log_group_name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "classify"
        }
      }

      readonlyRootFilesystem = true
      # The container writes to /tmp only; log/ and .state/ are not used on AWS,
      # where logs go to CloudWatch and run state to Parameter Store.
      mountPoints = []
    }
  ])
}
