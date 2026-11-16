output "alerts_topic_arn" {
  value = aws_sns_topic.alerts.arn
}

output "weekly_metrics_function" {
  description = "Para correrla a mano: aws lambda invoke --function-name <esto> --payload '{\"start\": ..., \"end\": ...}'."
  value       = aws_lambda_function.weekly_metrics.function_name
}

output "dashboard_name" {
  value = aws_cloudwatch_dashboard.this.dashboard_name
}
