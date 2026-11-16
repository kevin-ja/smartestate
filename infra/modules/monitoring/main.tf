# Monitoreo de la inferencia (monitor.md): alarmas del sistema y la API, latido, Lambda semanal del modelo y dashboard.
#
# Lee lo que ya produce el servicio: el log JSON por request en el log group de la API, las métricas nativas de la EC2
# y las del CloudWatch Agent (memoria y disco, lo instala Ansible). No cambia nada del servicio de inferencia.

locals {
  api_namespace   = "SmartEstate/API"
  model_namespace = "SmartEstate/Model"
  agent_namespace = "CWAgent"
  instance_dims   = { InstanceId = var.instance_id }
}

# ─── Avisos ──────────────────────────────────────────────────────────────────

resource "aws_sns_topic" "alerts" {
  name = "${var.name}-alerts"
  tags = var.tags
}

# AWS manda un correo de confirmación: hasta aceptarlo, no llega ningún aviso.
resource "aws_sns_topic_subscription" "email" {
  count     = var.alert_email == "" ? 0 : 1
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# ─── Métricas de la API, desde el log por request ───────────────────────────

resource "aws_cloudwatch_log_metric_filter" "requests" {
  name           = "${var.name}-requests"
  log_group_name = var.log_group_name
  pattern        = "{ $.event = \"request\" }"

  metric_transformation {
    name          = "requests"
    namespace     = local.api_namespace
    value         = "1"
    default_value = "0"
  }
}

resource "aws_cloudwatch_log_metric_filter" "errors_5xx" {
  name           = "${var.name}-5xx"
  log_group_name = var.log_group_name
  pattern        = "{ $.event = \"request\" && $.status >= 500 }"

  metric_transformation {
    name          = "errors_5xx"
    namespace     = local.api_namespace
    value         = "1"
    default_value = "0"
  }
}

# Solo clics: /health casi no trabaja y bajaría el p95.
resource "aws_cloudwatch_log_metric_filter" "latency" {
  name           = "${var.name}-latency"
  log_group_name = var.log_group_name
  pattern        = "{ $.event = \"request\" && $.path = \"/recommendations/*\" }"

  metric_transformation {
    name      = "latency_ms"
    namespace = local.api_namespace
    value     = "$.latency_ms"
    unit      = "Milliseconds"
  }
}

# ─── Latido: Route 53 llama a /health desde afuera ──────────────────────────

resource "aws_route53_health_check" "api" {
  ip_address        = var.public_ip
  port              = var.api_port
  type              = "HTTP"
  resource_path     = "/health"
  request_interval  = 30
  failure_threshold = 3
  regions           = var.health_check_regions
  tags              = merge(var.tags, { Name = "${var.name}-health" })
}

# ─── Alarmas (umbrales de monitor.md) ───────────────────────────────────────

resource "aws_cloudwatch_metric_alarm" "heartbeat" {
  alarm_name          = "${var.name}-heartbeat"
  alarm_description   = "/health no responde desde afuera"
  namespace           = "AWS/Route53"
  metric_name         = "HealthCheckStatus"
  dimensions          = { HealthCheckId = aws_route53_health_check.api.id }
  statistic           = "Minimum"
  period              = 60
  evaluation_periods  = 2
  comparison_operator = "LessThanThreshold"
  threshold           = 1
  treat_missing_data  = "breaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
  tags                = var.tags
}

resource "aws_cloudwatch_metric_alarm" "status_check" {
  alarm_name          = "${var.name}-status-check"
  alarm_description   = "La EC2 o su host fallan el chequeo de estado"
  namespace           = "AWS/EC2"
  metric_name         = "StatusCheckFailed"
  dimensions          = local.instance_dims
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 2
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = 1
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
  tags                = var.tags
}

resource "aws_cloudwatch_metric_alarm" "cpu" {
  alarm_name          = "${var.name}-cpu"
  alarm_description   = "CPU > 80 % durante 10 min"
  namespace           = "AWS/EC2"
  metric_name         = "CPUUtilization"
  dimensions          = local.instance_dims
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 2
  comparison_operator = "GreaterThanThreshold"
  threshold           = 80
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
  tags                = var.tags
}

resource "aws_cloudwatch_metric_alarm" "memory" {
  alarm_name          = "${var.name}-memory"
  alarm_description   = "Memoria > 85 % durante 10 min (CloudWatch Agent)"
  namespace           = local.agent_namespace
  metric_name         = "mem_used_percent"
  dimensions          = local.instance_dims
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 2
  comparison_operator = "GreaterThanThreshold"
  threshold           = 85
  treat_missing_data  = "breaching" # sin datos: el agente no corre
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
  tags                = var.tags
}

resource "aws_cloudwatch_metric_alarm" "disk" {
  alarm_name          = "${var.name}-disk"
  alarm_description   = "Disco raíz > 80 % (CloudWatch Agent)"
  namespace           = local.agent_namespace
  metric_name         = "disk_used_percent"
  dimensions          = local.instance_dims
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = 80
  treat_missing_data  = "breaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
  tags                = var.tags
}

resource "aws_cloudwatch_metric_alarm" "errors_5xx" {
  alarm_name          = "${var.name}-5xx"
  alarm_description   = "Más del 1 % de los requests con 5xx durante 5 min"
  comparison_operator = "GreaterThanThreshold"
  threshold           = 1
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching" # sin tráfico no hay errores
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
  tags                = var.tags

  metric_query {
    id          = "rate"
    expression  = "IF(requests > 0, 100 * errors / requests, 0)"
    label       = "5xx (%)"
    return_data = true
  }

  metric_query {
    id = "errors"
    metric {
      namespace   = local.api_namespace
      metric_name = "errors_5xx"
      period      = 300
      stat        = "Sum"
    }
  }

  metric_query {
    id = "requests"
    metric {
      namespace   = local.api_namespace
      metric_name = "requests"
      period      = 300
      stat        = "Sum"
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "latency_p95" {
  alarm_name          = "${var.name}-latency-p95"
  alarm_description   = "p95 del clic dentro del servicio > 1 s durante 10 min (SLO)"
  namespace           = local.api_namespace
  metric_name         = "latency_ms"
  extended_statistic  = "p95"
  period              = 300
  evaluation_periods  = 2
  comparison_operator = "GreaterThanThreshold"
  threshold           = 1000
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
  tags                = var.tags
}

# Las tablas online cambian de nombre con cada corrida: Metrics Insights suma todas las de la cuenta.
resource "aws_cloudwatch_metric_alarm" "dynamodb_throttles" {
  alarm_name          = "${var.name}-dynamodb-throttles"
  alarm_description   = "DynamoDB rechaza lecturas por capacidad"
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
  tags                = var.tags

  metric_query {
    id          = "throttles"
    expression  = "SELECT SUM(ThrottledRequests) FROM SCHEMA(\"AWS/DynamoDB\", Operation, TableName)"
    period      = 300
    return_data = true
  }
}

# Una vez por semana: el día que la Lambda publica cuenta; los demás no hay dato y no alarma.
resource "aws_cloudwatch_metric_alarm" "ndcg" {
  alarm_name          = "${var.name}-ndcg"
  alarm_description   = "NDCG@10 semanal de producción < 0.9 × el del entrenamiento (benchmark_scores.csv)"
  namespace           = local.model_namespace
  metric_name         = "production_ndcg_ratio"
  statistic           = "Minimum"
  period              = 86400
  evaluation_periods  = 1
  comparison_operator = "LessThanThreshold"
  threshold           = 0.9
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  tags                = var.tags
}

# ─── Lambda semanal del modelo ──────────────────────────────────────────────

# Solo los dos módulos que usa, con la misma ruta src/... que en el repo. boto3 ya viene en el runtime.
data "archive_file" "weekly_metrics" {
  type        = "zip"
  output_path = "${path.module}/.build/weekly_metrics.zip"

  dynamic "source" {
    for_each = toset([
      "src/__init__.py",
      "src/common/__init__.py",
      "src/common/ranking_metrics.py",
      "src/monitoring/__init__.py",
      "src/monitoring/weekly_metrics.py",
    ])
    content {
      filename = source.value
      content  = file("${var.repo_root}/${source.value}")
    }
  }
}

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "lambda" {
  statement {
    sid       = "ConsultarLogsDeLaApi"
    actions   = ["logs:StartQuery"]
    resources = ["${var.log_group_arn}:*", var.log_group_arn]
  }

  # GetQueryResults no admite recurso.
  statement {
    sid       = "LeerResultados"
    actions   = ["logs:GetQueryResults"]
    resources = ["*"]
  }

  statement {
    sid       = "EscribirSuPropioLog"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.lambda.arn}:*"]
  }

  statement {
    sid       = "LeerBenchmarkYCorridas"
    actions   = ["s3:GetObject"]
    resources = ["${var.lake_bucket_arn}/ref/benchmark/*", "${var.lake_bucket_arn}/runs/*"]
  }

  # Sin ListBucket, S3 responde AccessDenied y no NoSuchKey cuando falta un archivo opcional.
  statement {
    sid       = "DistinguirArchivoFaltante"
    actions   = ["s3:ListBucket"]
    resources = [var.lake_bucket_arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["ref/benchmark/*", "runs/*"]
    }
  }

  statement {
    sid       = "EscribirReporteSemanal"
    actions   = ["s3:PutObject"]
    resources = ["${var.lake_bucket_arn}/runs/*/metrics/*"]
  }

  statement {
    sid       = "PublicarMetricas"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = [local.model_namespace]
    }
  }
}

resource "aws_iam_role" "lambda" {
  name               = "${var.name}-weekly-metrics"
  description        = "Lambda semanal: lee logs y el lake, escribe el reporte y publica métricas del modelo."
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
  tags               = var.tags
}

resource "aws_iam_role_policy" "lambda" {
  name   = "metricas-semanales"
  role   = aws_iam_role.lambda.id
  policy = data.aws_iam_policy_document.lambda.json
}

resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${var.name}-weekly-metrics"
  retention_in_days = var.log_retention_days
  tags              = var.tags
}

resource "aws_lambda_function" "weekly_metrics" {
  function_name    = "${var.name}-weekly-metrics"
  description      = "Recall@10 y NDCG@10 de producción contra el benchmark (monitor.md)"
  role             = aws_iam_role.lambda.arn
  runtime          = "python3.12"
  handler          = "src.monitoring.weekly_metrics.handler"
  filename         = data.archive_file.weekly_metrics.output_path
  source_code_hash = data.archive_file.weekly_metrics.output_base64sha256
  timeout          = 300
  memory_size      = 256
  tags             = var.tags

  environment {
    variables = {
      LOG_GROUP        = var.log_group_name
      LAKE_BUCKET      = var.lake_bucket_name
      METRIC_NAMESPACE = local.model_namespace
    }
  }

  depends_on = [aws_cloudwatch_log_group.lambda, aws_iam_role_policy.lambda]
}

resource "aws_cloudwatch_event_rule" "weekly" {
  name                = "${var.name}-weekly-metrics"
  description         = "Domingo 06:00 UTC: mide la semana anterior"
  schedule_expression = "cron(0 6 ? * SUN *)"
  tags                = var.tags
}

resource "aws_cloudwatch_event_target" "weekly" {
  rule = aws_cloudwatch_event_rule.weekly.name
  arn  = aws_lambda_function.weekly_metrics.arn
}

resource "aws_lambda_permission" "events" {
  statement_id  = "EventBridgeSemanal"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.weekly_metrics.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.weekly.arn
}

# ─── Dashboard: sistema, API, modelo ─────────────────────────────────────────

resource "aws_cloudwatch_dashboard" "this" {
  dashboard_name = var.name
  dashboard_body = jsonencode({
    widgets = [
      { type = "text", x = 0, y = 0, width = 24, height = 1, properties = { markdown = "## Sistema" } },
      {
        type = "metric", x = 0, y = 1, width = 8, height = 6
        properties = {
          title   = "CPU (%)", region = var.region, stat = "Average", period = 300
          metrics = [["AWS/EC2", "CPUUtilization", "InstanceId", var.instance_id]]
        }
      },
      {
        type = "metric", x = 8, y = 1, width = 8, height = 6
        properties = {
          title = "Memoria y disco (%)", region = var.region, stat = "Average", period = 300
          metrics = [
            [local.agent_namespace, "mem_used_percent", "InstanceId", var.instance_id],
            [local.agent_namespace, "disk_used_percent", "InstanceId", var.instance_id],
          ]
        }
      },
      {
        type = "metric", x = 16, y = 1, width = 8, height = 6
        properties = {
          title = "Latido y DynamoDB", region = var.region, period = 300
          metrics = [
            ["AWS/Route53", "HealthCheckStatus", "HealthCheckId", aws_route53_health_check.api.id, { stat = "Minimum", region = "us-east-1" }],
            [{ expression = "SELECT SUM(ThrottledRequests) FROM SCHEMA(\"AWS/DynamoDB\", Operation, TableName)", label = "DynamoDB throttles" }],
          ]
        }
      },
      { type = "text", x = 0, y = 7, width = 24, height = 1, properties = { markdown = "## API" } },
      {
        type = "metric", x = 0, y = 8, width = 8, height = 6
        properties = {
          title   = "Requests y 5xx", region = var.region, stat = "Sum", period = 300
          metrics = [[local.api_namespace, "requests"], [local.api_namespace, "errors_5xx"]]
        }
      },
      {
        type = "metric", x = 8, y = 8, width = 16, height = 6
        properties = {
          title = "Latencia del clic dentro del servicio (ms)", region = var.region, period = 300
          metrics = [
            [local.api_namespace, "latency_ms", { stat = "p50" }],
            [local.api_namespace, "latency_ms", { stat = "p95" }],
            [local.api_namespace, "latency_ms", { stat = "p99" }],
          ]
          annotations = { horizontal = [{ label = "SLO p95", value = 1000 }] }
        }
      },
      { type = "text", x = 0, y = 14, width = 24, height = 1, properties = { markdown = "## Modelo (semanal, por run_id)" } },
      {
        type = "metric", x = 0, y = 15, width = 12, height = 6
        properties = {
          title = "NDCG@10: producción y entrenamiento", region = var.region, period = 604800, stat = "Average"
          metrics = [
            [{ expression = "SEARCH('{${local.model_namespace},run_id} MetricName=\"ndcg_at_10\"', 'Average', 604800)", id = "prod" }],
            [{ expression = "SEARCH('{${local.model_namespace},run_id} MetricName=\"benchmark_ndcg_at_10\"', 'Average', 604800)", id = "train" }],
          ]
        }
      },
      {
        type = "metric", x = 12, y = 15, width = 12, height = 6
        properties = {
          title = "Recall@10 y cobertura", region = var.region, period = 604800, stat = "Average"
          metrics = [
            [{ expression = "SEARCH('{${local.model_namespace},run_id} MetricName=\"recall_at_10\"', 'Average', 604800)", id = "recall" }],
            [{ expression = "SEARCH('{${local.model_namespace},run_id} MetricName=\"coverage\"', 'Average', 604800)", id = "coverage" }],
          ]
        }
      },
    ]
  })
}
