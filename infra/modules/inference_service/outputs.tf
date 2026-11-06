output "public_ip" {
  description = "Dirección fija de la API."
  value       = aws_eip.this.public_ip
}

output "instance_id" {
  description = "Para abrir sesión: aws ssm start-session --target <instance_id>."
  value       = aws_instance.this.id
}

output "ecr_repository_url" {
  value = aws_ecr_repository.this.repository_url
}

output "log_group_name" {
  value = aws_cloudwatch_log_group.this.name
}
