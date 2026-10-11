output "alarm_name" {
  description = "作成した CloudWatch アラームの名前"
  value       = aws_cloudwatch_metric_alarm.messages_visible.alarm_name
}

output "alarm_arn" {
  description = "作成した CloudWatch アラームの ARN"
  value       = aws_cloudwatch_metric_alarm.messages_visible.arn
}
