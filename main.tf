# whatsnew: AWS What's New の新着記事を毎時クロールし、
# Bedrock の Claude で日本語要約して Slack の専用チャネルに投稿する。
#
# 構成:
#   EventBridge Scheduler（毎時）
#     → crawler Lambda（RSS 取得 → 鮮度ウィンドウ内の記事を DynamoDB へ put）
#     → DynamoDB（PK: 記事 URL、Streams NEW_IMAGE、TTL 30日）
#     → notifier Lambda（本文取得 → Bedrock 要約 → chat.postMessage で投稿）
#     → リトライ超過した失敗レコードは SQS DLQ へ退避
#     → DLQ 滞留アラーム → EventBridge ルール → alarm-notifier Lambda
#       （アラート用チャネルへ第一報 + AI 要約のスレッド返信）

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition
}
