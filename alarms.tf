###############################################################################
# DLQ 滞留アラーム
#
# DLQ にメッセージが1件でも入ったら ALARM になる。通知は alarm_actions ではなく、
# アラーム状態変化イベント（aws.cloudwatch）を EventBridge ルール
# （alarm_notifier.tf の dlq_alarm）が拾い、alarm-notifier Lambda が
# アラート用の Slack チャネルへ投稿する。
###############################################################################

module "dlq_alarm" {
  source = "./modules/alarms/sqs_queue"

  queue_name = aws_sqs_queue.dlq.name
  priority   = "P3"
}
