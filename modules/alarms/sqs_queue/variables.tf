variable "queue_name" {
  type        = string
  description = "監視対象の SQS キュー名（アラーム名のベースにもなる）"
}

variable "priority" {
  type        = string
  description = "アラームの優先度（P1〜P5。P1 が最高優先度。アラーム名の末尾に小文字で付与する）"

  validation {
    condition     = contains(["p1", "p2", "p3", "p4", "p5"], lower(var.priority))
    error_message = "priority は P1〜P5 のいずれかを指定してください。"
  }
}
