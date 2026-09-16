resource "aws_ecr_repository" "epc" {
  name = "epc-${var.environment}"

  # Tags are mutable by default, which makes "what is actually deployed"
  # unanswerable. Immutable tags mean a digest and a tag agree forever.
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}

resource "aws_ecr_lifecycle_policy" "epc" {
  repository = aws_ecr_repository.epc.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Expire untagged images"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = var.untagged_image_days
        }
        action = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Keep a rollback window of tagged images"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = var.tagged_image_count
        }
        action = { type = "expire" }
      },
    ]
  })
}
