output "role_arn" {
  value = aws_iam_role.this.arn
}

output "instance_profile_arn" {
  value = aws_iam_instance_profile.this.arn
}
