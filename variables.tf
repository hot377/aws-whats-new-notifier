variable "region" {
  type        = string
  description = "リソースを作成するリージョン"
  default     = "ap-northeast-1"
}

variable "name_prefix" {
  type        = string
  description = "作成するリソース名のプレフィックス（{env}-{product} の形式を想定）"
  default     = "prod-whatsnew"
}

variable "env" {
  type        = string
  description = "全リソースに付ける Env タグの値（prod / dev など）"
  default     = "prod"
}

variable "owner" {
  type        = string
  description = "全リソースに付ける Owner タグの値（管理者やチームの名前）"
}

variable "slack_channel_id" {
  type        = string
  description = "記事の投稿先 Slack チャネルの ID"
}

variable "alarm_slack_channel_id" {
  type        = string
  description = <<-EOT
    DLQ 滞留アラームの通知先 Slack チャネルの ID。
    記事の投稿先（slack_channel_id）とは別のアラート専用チャネルを指定する。
  EOT
}

variable "alarm_slack_bot_token_parameter" {
  type        = string
  description = <<-EOT
    アラート用 Slack Bot の Token（xoxb-）を格納した SSM パラメータ名（SecureString）。
    記事を投稿する Bot（slack_bot_token_parameter）とは別の Bot にする。記事用の Token が
    失効して DLQ に滞留したときも、アラームは通知できるようにするため。
    アラート用 Bot はこのシステム専用にせず、アカウント内のほかのシステムと共有できる。そのため
    パラメータ名は /whatsnew/ ではなく共有のパス（/alerts/）に置く。
    Token 自体は Terraform 管理外で、apply 前に手動で登録する（README.md 参照）。
  EOT
  default     = "/alerts/slack-bot-token"
}

variable "slack_mention_user_id" {
  type        = string
  description = <<-EOT
    アラームの第一報でメンションする Slack ユーザーのメンバー ID（U で始まる）。
    解消報・AI 要約ではメンションしない。空文字ならメンションしない。
  EOT
  default     = ""
}

variable "alarm_thread_ttl_days" {
  type        = number
  description = "アラームの解消報を ALARM と同じ Slack スレッドにまとめる期間（日）。これを過ぎた解消報は新規投稿になる。"
  default     = 30
}

variable "slack_bot_token_parameter" {
  type        = string
  description = <<-EOT
    Slack Bot Token（xoxb-）を格納した SSM パラメータ名（SecureString）。
    Token 自体は Terraform 管理外で、apply 前に手動で登録する（README.md 参照）。
  EOT
  default     = "/whatsnew/slack-bot-token"
}

variable "bedrock_model_id" {
  type        = string
  description = "要約に使う Bedrock 上の Claude モデル ID（推論プロファイル ID）"
  default     = "jp.anthropic.claude-sonnet-4-6"
}

variable "bedrock_region" {
  type        = string
  description = "Bedrock を呼び出すリージョン"
  default     = "ap-northeast-1"
}

variable "feed_url" {
  type        = string
  description = "取得する RSS フィードの URL"
  default     = "https://aws.amazon.com/about-aws/whats-new/recent/feed/"
}

variable "lambda_timeout" {
  type        = number
  description = "Lambda のタイムアウト秒数（Bedrock の応答待ちを含むため長めにする）"
  default     = 180
}

variable "freshness_window_days" {
  type        = number
  description = "新着とみなす期間（日数）"
  default     = 2
}

variable "schedule_state" {
  type        = string
  description = <<-EOT
    crawler を毎時起動するスケジュールの状態（ENABLED / DISABLED）。
    DISABLED にするとクロールが止まり、新着記事の通知も出なくなる。
  EOT
  default     = "ENABLED"

  validation {
    condition     = contains(["ENABLED", "DISABLED"], var.schedule_state)
    error_message = "schedule_state は ENABLED か DISABLED を指定してください。"
  }
}
