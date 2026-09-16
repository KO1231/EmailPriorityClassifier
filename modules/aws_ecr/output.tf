output "repository" {
  description = "The image repository, as name/url/arn."
  value = {
    name = aws_ecr_repository.epc.name
    url  = aws_ecr_repository.epc.repository_url
    arn  = aws_ecr_repository.epc.arn
  }
}
