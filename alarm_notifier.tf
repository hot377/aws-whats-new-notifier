###############################################################################
# DLQ 滞留アラームの通知（alarm-notifier）
#
# alarms.tf のアラームの状態変化イベント（aws.cloudwatch）を EventBridge ルールで拾い、
# alarm-notifier Lambda が Slack のアラート用チャネルへ投稿する。
# Slack の Bot（Token）は記事用とは分け、記事用の Token が失効しても通知できるようにする。
# アラート用 Bot はアカウント内のほかのシステムと共有できるよう、Token は共有のパス（/alerts/）から読む。
#
# 記事を投稿する notifier とは関数を分ける。notifier は batch size 1 でストリームを
# 直列処理しており、詰まって DLQ に入ったときのアラーム通知を巻き込まないため。
# zip は共用し、handler の指定（alarm_notifier.handler）で処理を分ける。
###############################################################################

locals {
  alarm_notifier_function_name = "${var.name_prefix}-alarm-notifier"
}

resource "aws_lambda_function" "alarm_notifier" {
  function_name = local.alarm_notifier_function_name
  description   = "Post whatsnew DLQ alarm state changes to Slack with AI summary in thread"

  filename         = local.lambda_zip_path
  source_code_hash = terraform_data.lambda_build.output

  runtime       = "python3.13"
  architectures = ["arm64"]
  handler       = "alarm_notifier.handler"
  timeout       = var.lambda_timeout
  memory_size   = 256

  role = aws_iam_role.alarm_notifier.arn

  environment {
    variables = {
      SLACK_CHANNEL_ID      = var.alarm_slack_channel_id
      SLACK_TOKEN_PARAMETER = var.alarm_slack_bot_token_parameter
      SLACK_MENTION_USER_ID = var.slack_mention_user_id
      BEDROCK_MODEL_ID      = var.bedrock_model_id
      BEDROCK_REGION        = var.bedrock_region
      THREADS_TABLE         = aws_dynamodb_table.alarm_threads.name
      THREAD_TTL_DAYS       = tostring(var.alarm_thread_ttl_days)
    }
  }

  depends_on = [aws_cloudwatch_log_group.alarm_notifier]
}

# 非同期呼び出し（EventBridge 経由）の設定。
# リトライすると第一報が重複投稿されるためリトライは行わない。
# 失敗は関数自身がログに出力し Slack へ通知する。DLQ には退避しない
# （記事用の DLQ に入れても、ALARM 継続中は再通知されないため）。
resource "aws_lambda_function_event_invoke_config" "alarm_notifier" {
  function_name = aws_lambda_function.alarm_notifier.function_name

  maximum_retry_attempts = 0
}

resource "aws_cloudwatch_log_group" "alarm_notifier" {
  name              = "/aws/lambda/${local.alarm_notifier_function_name}"
  retention_in_days = 30
}

###############################################################################
# 通知スレッドの対応表
#
# アラームの解消（ALARM → OK）を ALARM と同じ Slack スレッドに返信するため、
# スレッドキー（アラーム名）→ 第一報の thread_ts を保存する。
# 中身は TTL で自動削除される一時データで、消えても解消報が新規投稿になるだけのため
# 削除保護は付けない。
###############################################################################

resource "aws_dynamodb_table" "alarm_threads" {
  name         = "${var.name_prefix}-alarm-threads"
  billing_mode = "PAY_PER_REQUEST"

  hash_key = "thread_key"

  attribute {
    name = "thread_key"
    type = "S"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }
}

###############################################################################
# EventBridge ルール
#
# DLQ 滞留アラームの ALARM 遷移と、ALARM からの解消（OK 遷移）だけを拾う。
# ALARM への遷移時に1回だけ発火するため滞留が続いても連発しない（逆に ALARM 継続中は
# 追加滞留があっても再通知されない）。
# アラーム作成直後の INSUFFICIENT_DATA → OK は解消ではないので拾わない。
###############################################################################

resource "aws_cloudwatch_event_rule" "dlq_alarm" {
  name        = "${var.name_prefix}-dlq-alarm-cloudwatch-lambda"
  description = "whatsnew の DLQ 滞留アラームの ALARM 遷移と解消（ALARM から OK）を通知する"

  event_pattern = jsonencode({
    source        = ["aws.cloudwatch"]
    "detail-type" = ["CloudWatch Alarm State Change"]
    detail = {
      alarmName = [module.dlq_alarm.alarm_name]
      "$or" = [
        { state = { value = ["ALARM"] } },
        {
          state         = { value = ["OK"] }
          previousState = { value = ["ALARM"] }
        },
      ]
    }
  })
}

resource "aws_cloudwatch_event_target" "dlq_alarm" {
  rule = aws_cloudwatch_event_rule.dlq_alarm.name
  arn  = aws_lambda_function.alarm_notifier.arn
}

resource "aws_lambda_permission" "dlq_alarm" {
  statement_id  = "AllowEventBridge-dlq-alarm"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.alarm_notifier.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.dlq_alarm.arn
}

###############################################################################
# Lambda 実行ロール（Logs + SSM + Bedrock + スレッド対応表）
###############################################################################

resource "aws_iam_role" "alarm_notifier" {
  name = "${var.name_prefix}-lambda-alarm-notifier"
  # IAM の description は日本語(マルチバイト)不可のため英語で記述する
  description        = "Execution role for the whatsnew alarm notifier Lambda"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "alarm_notifier" {
  # CloudWatch Logs への書き込み
  statement {
    sid    = "Logs"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${aws_cloudwatch_log_group.alarm_notifier.arn}:*"]
  }

  # アラート用 Bot の Token（SecureString）の読み取り。記事用とは別の Bot の Token
  statement {
    sid       = "ReadSlackToken"
    effect    = "Allow"
    actions   = ["ssm:GetParameter"]
    resources = ["arn:${local.partition}:ssm:${var.region}:${local.account_id}:parameter${var.alarm_slack_bot_token_parameter}"]
  }

  # Bedrock の Claude 呼び出し（notifier と同じく、プロファイルと転送先の両方を許可する）
  statement {
    sid    = "InvokeBedrock"
    effect = "Allow"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
    ]
    resources = [
      "arn:${local.partition}:bedrock:*::foundation-model/anthropic.*",
      "arn:${local.partition}:bedrock:*:${local.account_id}:inference-profile/*",
    ]
  }

  # スレッド対応表の読み書き
  statement {
    sid    = "ThreadsTable"
    effect = "Allow"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
    ]
    resources = [aws_dynamodb_table.alarm_threads.arn]
  }
}

resource "aws_iam_role_policy" "alarm_notifier" {
  name   = aws_iam_role.alarm_notifier.name
  role   = aws_iam_role.alarm_notifier.id
  policy = data.aws_iam_policy_document.alarm_notifier.json
}
