# SQS キューの滞留を検知する CloudWatch アラーム。
#
# アラーム名は「<キュー名>-<状態>-<優先度>」の規約で付ける
# （例: prod-whatsnew-dlq-messages-visible-p2）。
# 通知は alarm_actions ではなく、アラーム状態変化イベント
# （source aws.cloudwatch）を、このモジュールを呼ぶ側の EventBridge ルールで拾う。
# OK→ALARM の遷移時に1回だけイベントが発行されるため滞留が続いても通知は
# 連発しない（逆に ALARM 継続中は追加滞留があっても再通知されない）。

resource "aws_cloudwatch_metric_alarm" "messages_visible" {
  alarm_name        = "${var.queue_name}-messages-visible-${lower(var.priority)}"
  alarm_description = "${var.queue_name} にメッセージが滞留しています"

  namespace   = "AWS/SQS"
  metric_name = "ApproximateNumberOfMessagesVisible"
  dimensions = {
    QueueName = var.queue_name
  }

  statistic           = "Maximum"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = 1
  period              = 300
  evaluation_periods  = 1

  # メトリクスが欠落してもキューが空なだけなので ALARM にしない
  treat_missing_data = "notBreaching"

  # 通知は状態変化イベント経由のため alarm_actions は設定しない
}
