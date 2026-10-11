###############################################################################
# EventBridge Scheduler（毎時 crawler を起動）
#
# EventBridge Rule と違い、Scheduler は Lambda のリソースベースポリシー
# （aws_lambda_permission）ではなく、Scheduler 自身が assume する実行ロールの
# 権限でターゲットを呼び出す。
###############################################################################

resource "aws_scheduler_schedule" "crawler" {
  name = "${var.name_prefix}-crawl-schedule-lambda"

  # notifier 側（Streams のマッピング）ができてから起動する。マッピングは LATEST から読むため、
  # 先に crawler が走ると、その回に put した記事は通知されないままテーブルに残る。
  depends_on = [aws_lambda_event_source_mapping.notifier]

  flexible_time_window {
    mode = "OFF"
  }

  schedule_expression = "rate(1 hour)"

  # 毎時のクロールを止めたいときは tfvars で DISABLED にする
  state = var.schedule_state

  target {
    arn      = aws_lambda_function.crawler.arn
    role_arn = aws_iam_role.scheduler.arn

    # crawler はリトライしない方針（put は冪等で、次の毎時実行が同じ範囲を拾い直す）。
    # 既定は最大185回リトライのため明示的に 0 にする
    retry_policy {
      maximum_retry_attempts = 0
    }
  }
}

data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name = "${var.name_prefix}-scheduler-crawler"
  # IAM の description は日本語(マルチバイト)不可のため英語で記述する
  description        = "Role assumed by EventBridge Scheduler to invoke the whatsnew crawler Lambda"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

data "aws_iam_policy_document" "scheduler" {
  statement {
    sid       = "InvokeCrawler"
    effect    = "Allow"
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.crawler.arn]
  }
}

resource "aws_iam_role_policy" "scheduler" {
  name   = aws_iam_role.scheduler.name
  role   = aws_iam_role.scheduler.id
  policy = data.aws_iam_policy_document.scheduler.json
}
