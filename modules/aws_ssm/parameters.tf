# Terraform owns these parameters. It does not own their contents.
#
# `terraform.tfstate` stores values in plaintext, so a secret passed through a
# Terraform variable ends up in the state file, in every plan output, and in
# whatever CI logs them. These are created empty and filled out of band:
#
#   uv run epc login                      # writes the Gmail credential
#   aws ssm put-parameter --overwrite \
#     --name /epc/<env>/openai-api-key --type SecureString --value sk-...
#
# `ignore_changes` on the value is what stops the next apply reverting them.

locals {
  prefix = "${var.path_prefix}/${var.environment}"
}

resource "aws_ssm_parameter" "gmail_credentials" {
  name        = "${local.prefix}/gmail-credentials"
  description = "Durable half of the Gmail OAuth credentials. Written by `epc login`."
  type        = "SecureString"
  value       = "placeholder"

  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_ssm_parameter" "openai_api_key" {
  name        = "${local.prefix}/openai-api-key"
  description = "Scoped to api.responses.write and api.responses.read; model.request is not needed."
  type        = "SecureString"
  value       = "placeholder"

  lifecycle {
    ignore_changes = [value]
  }
}

# Thread IDs of mail that keeps failing, and counters. Not secret, and holding
# no content; encrypting it would add a kms:Decrypt to the task role for
# nothing. Written by the task after every run, so its value is ignored here too.
resource "aws_ssm_parameter" "state" {
  name        = "${local.prefix}/state"
  description = "Threads that keep failing, so runs stop retrying them."
  type        = "String"
  value       = jsonencode({})

  lifecycle {
    ignore_changes = [value]
  }
}
