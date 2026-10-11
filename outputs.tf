output "table_name" {
  value       = aws_dynamodb_table.rss_history.name
  description = "記事履歴を保存している DynamoDB テーブル名"
}

output "crawler_function_name" {
  value       = aws_lambda_function.crawler.function_name
  description = "RSS クロール Lambda の関数名（疎通テストの手動実行に使う）"
}

output "notifier_function_name" {
  value       = aws_lambda_function.notifier.function_name
  description = "要約・Slack 投稿 Lambda の関数名"
}

output "alarm_notifier_function_name" {
  value       = aws_lambda_function.alarm_notifier.function_name
  description = "DLQ 滞留アラームを Slack へ通知する Lambda の関数名"
}

output "dlq_url" {
  value       = aws_sqs_queue.dlq.url
  description = "処理に失敗したレコードが退避される DLQ の URL"
}
