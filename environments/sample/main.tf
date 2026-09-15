# Modules are wired together here, passing {name, arn} objects between them —
# the same shape as the rest of the estate, so both repositories read alike.

module "aws_ecr" {
  source = "../../modules/aws_ecr"

  environment = local.environment
}

module "aws_ssm" {
  source = "../../modules/aws_ssm"

  environment = local.environment
}

module "aws_sqs" {
  source = "../../modules/aws_sqs"

  environment = local.environment

  # Comfortably longer than the apply Lambda's timeout: if a message became
  # visible again while it was still being applied, a slow apply would become a
  # duplicate apply.
  visibility_timeout_seconds = 180
}

module "aws_cloudwatch" {
  source = "../../modules/aws_cloudwatch"

  environment     = local.environment
  dlq_name        = module.aws_sqs.mutations_dlq.name
  alarm_topic_arn = local.config["alarm_topic_arn"]
}

module "aws_iam" {
  source = "../../modules/aws_iam"

  environment = local.environment

  ssm_prefix        = module.aws_ssm.prefix
  gmail_credentials = module.aws_ssm.gmail_credentials
  state_parameter   = module.aws_ssm.state
  injected_parameters = [
    module.aws_ssm.openai_api_key,
    module.aws_ssm.config,
    module.aws_ssm.policy,
  ]

  mutations_queue = module.aws_sqs.mutations
  log_group_arns  = module.aws_cloudwatch.log_group_arns

  bedrock_model_arns = local.config["bedrock_model_arns"]

  # Without the revision: a new task definition revision should not require an
  # IAM change.
  task_definition_arn = "arn:aws:ecs:${local.config["aws"]["region"]}:${data.aws_caller_identity.current.account_id}:task-definition/epc-${local.environment}-classify"
}

module "aws_ecs" {
  source = "../../modules/aws_ecs"

  environment = local.environment
  image       = "${module.aws_ecr.repository.url}:${local.config["image_tag"]}"
  aws_region  = local.config["aws"]["region"]

  execution_role_arn = module.aws_iam.ecs_execution_role.arn
  task_role_arn      = module.aws_iam.ecs_task_role.arn
  log_group_name     = module.aws_cloudwatch.classify_log_group.name

  subnet_ids         = local.config["aws"]["network"]["subnet_ids"]
  security_group_ids = local.config["aws"]["network"]["security_group_ids"]
  assign_public_ip   = local.config["aws"]["network"]["assign_public_ip"]

  # Non-secret settings only. Secrets go through `secret_parameters` below, so
  # they stay out of the task definition and out of Terraform state.
  environment_variables = {
    EPC__AWS_REGION                  = local.config["aws"]["region"]
    EPC__LLM__BACKEND                = local.config["app"]["llm_backend"]
    EPC__LLM__MODEL                  = local.config["app"]["llm_model"]
    EPC__GMAIL__MAX_THREADS          = local.config["app"]["max_threads"]
    EPC__GMAIL__EXTRA_QUERY          = local.config["app"]["extra_query"]
    EPC__DRY_RUN                     = local.config["app"]["dry_run"]
    EPC__CREDENTIALS__BACKEND        = "ssm"
    EPC__CREDENTIALS__PARAMETER_NAME = module.aws_ssm.gmail_credentials.name
    EPC__STATE__BACKEND              = "ssm"
    EPC__STATE__PARAMETER_NAME       = module.aws_ssm.state.name
    EPC__DISPATCH__SINK              = "sqs"
    EPC__DISPATCH__QUEUE_URL         = module.aws_sqs.mutations.url
    EPC__OBSERVABILITY__LOG_JSON     = "true"
    # A dry run's plan: written where the filesystem allows, and logged, since
    # the file is gone when the task stops.
    EPC__DISPATCH__JSONL_PATH  = "/tmp/mutations.jsonl"
    EPC__DISPATCH__LOG_PLANNED = "true"
  }

  # Injected by ECS with the execution role. config.yml and policy.yml arrive
  # this way too: the image carries neither.
  secret_parameters = {
    OPENAI_API_KEY  = module.aws_ssm.openai_api_key.arn
    EPC_CONFIG_YAML = module.aws_ssm.config.arn
    EPC_POLICY_YAML = module.aws_ssm.policy.arn
  }
}

module "aws_lambda" {
  source = "../../modules/aws_lambda"

  environment = local.environment
  # The same image the classify task runs. One artefact, two entrypoints.
  image = "${module.aws_ecr.repository.url}:${local.config["image_tag"]}"

  role_arn       = module.aws_iam.lambda_apply_role.arn
  log_group_name = module.aws_cloudwatch.apply_log_group.name
  queue_arn      = module.aws_sqs.mutations.arn

  environment_variables = {
    EPC__AWS_REGION                  = local.config["aws"]["region"]
    EPC__CREDENTIALS__PARAMETER_NAME = module.aws_ssm.gmail_credentials.name
  }
}

module "aws_events" {
  source = "../../modules/aws_events"

  environment = local.environment

  schedule_expression = local.config["schedule"]["expression"]
  schedule_timezone   = local.config["schedule"]["timezone"]
  enabled             = local.config["schedule"]["enabled"]

  cluster_arn           = module.aws_ecs.cluster.arn
  task_definition_arn   = module.aws_ecs.task_definition.arn
  role_arn              = module.aws_iam.scheduler_role.arn
  network_configuration = module.aws_ecs.network_configuration
}

data "aws_caller_identity" "current" {}

output "next_steps" {
  description = "What has to happen out of band before this runs."
  value       = <<-EOT
    1. Build and push the image:
         aws ecr get-login-password | docker login --username AWS --password-stdin ${module.aws_ecr.repository.url}
         docker build --platform linux/arm64 -t ${module.aws_ecr.repository.url}:${local.config["image_tag"]} .
         docker push ${module.aws_ecr.repository.url}:${local.config["image_tag"]}

    2. Fill the parameters. Terraform owns them but never their contents —
       a value passed through Terraform ends up in the state file.
         uv run epc login   # with credentials.backend: ssm in your config.yml
         aws ssm put-parameter --overwrite --type SecureString \
           --name ${module.aws_ssm.openai_api_key.name} --value sk-...
         # --tier: without it a put falls back to Standard (4 KB) and a full config is refused.
         aws ssm put-parameter --overwrite --type SecureString --tier Intelligent-Tiering \
           --name ${module.aws_ssm.config.name} --value file://config.yml
         aws ssm put-parameter --overwrite --type SecureString --tier Intelligent-Tiering \
           --name ${module.aws_ssm.policy.name} --value file://policy.yml   # optional

    3. Read one run before enabling the schedule:
         aws ecs run-task --cluster ${module.aws_ecs.cluster.name} ...
       then set schedule.enabled and app.dry_run in config.yml accordingly.
  EOT
}
