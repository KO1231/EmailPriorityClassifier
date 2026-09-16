locals {
  config      = yamldecode(file("${path.module}/config.yml"))
  environment = "sample" // FIXME: Change this to your environment name
}
