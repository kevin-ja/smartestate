output "deploy_role_arn" {
  value = aws_iam_role.deploy.arn
}

output "terraform_role_arn" {
  value = aws_iam_role.terraform.arn
}

output "ansible_bucket_name" {
  value = aws_s3_bucket.this["ansible"].id
}

output "ansible_bucket_arn" {
  value = aws_s3_bucket.this["ansible"].arn
}

output "tfplans_bucket_name" {
  value = aws_s3_bucket.this["tfplans"].id
}
