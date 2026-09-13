terraform {
  required_version = "~> 1.14.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  backend "s3" {
    region       = ""                      // FIXME: Change this to your S3 bucket region
    bucket       = ""                      // FIXME: Change this to your S3 bucket name (for terraform state)
    key          = "???/terraform.tfstate" // FIXME: Change this to desired path
    use_lockfile = true
  }
}

provider "aws" {
  region  = local.config["aws"]["region"]
  profile = local.config["aws"]["profile"]

  default_tags {
    tags = {
      Project     = "EmailPriorityClassifier"
      Environment = local.environment
      ManagedBy   = "terraform"
    }
  }
}
