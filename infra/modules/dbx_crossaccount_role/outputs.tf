output "role_arn" {
  value = aws_iam_role.this.arn

  # El rol no le sirve a Databricks hasta que tiene su política inline.
  depends_on = [aws_iam_role_policy.this]
}
