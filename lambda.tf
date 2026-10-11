###############################################################################
# crawler / notifier Lambda
#
# 依存ライブラリ（feedparser / beautifulsoup4）を含むため、apply 時に
# scripts/build.sh でデプロイパッケージをビルドする。zip は alarm_notifier.tf の
# alarm-notifier を含む3関数で共用し、handler の指定（crawler.handler など）で処理を分ける。
# archive_file データソースはビルド成果物との依存順序を表現できないため、
# terraform_data + local-exec を使う。ソースツリーのハッシュを変更検知に使い、
# 変更があったときだけ再ビルドする。
###############################################################################

locals {
  lambda_src_dir  = "${path.module}/lambda"
  lambda_zip_path = "${path.module}/dist/lambda.zip"

  # lambda/ 配下のソース一式（関数・プロンプト・requirements）のハッシュ。
  # ローカルでのテスト実行で生成される __pycache__ は除外する（偽の再ビルド差分を防ぐ）
  lambda_src_hash = sha256(join("", [
    for f in sort(fileset(local.lambda_src_dir, "**")) : filesha256("${local.lambda_src_dir}/${f}")
    if !strcontains(f, "__pycache__")
  ]))

  crawler_function_name  = "${var.name_prefix}-crawler"
  notifier_function_name = "${var.name_prefix}-notifier"
}

resource "terraform_data" "lambda_build" {
  # ソースが変わったときだけ再ビルドする
  triggers_replace = local.lambda_src_hash
  input            = local.lambda_src_hash

  provisioner "local-exec" {
    command = "${path.module}/scripts/build.sh"
  }
}

###############################################################################
# crawler（毎時 RSS を取得して DynamoDB へ put）
###############################################################################

resource "aws_lambda_function" "crawler" {
  function_name = local.crawler_function_name
  description   = "Crawl AWS What's New RSS feed and put fresh articles into DynamoDB"

  filename = local.lambda_zip_path
  # zip ではなくソースツリーのハッシュを変更検知に使う（ビルド前でも plan できるようにするため）
  source_code_hash = terraform_data.lambda_build.output

  runtime       = "python3.13"
  architectures = ["arm64"] # 依存は純 Python のみなので、ビルド環境のアーキテクチャと揃える必要はない
  handler       = "crawler.handler"
  # RSS 取得と put のみで Bedrock は呼ばないため短めでよい
  timeout     = 60
  memory_size = 256

  role = aws_iam_role.crawler.arn

  environment {
    variables = {
      FEED_URL              = var.feed_url
      FRESHNESS_WINDOW_DAYS = var.freshness_window_days
      TABLE_NAME            = aws_dynamodb_table.rss_history.name
    }
  }

  depends_on = [aws_cloudwatch_log_group.crawler]
}

# Scheduler からの非同期呼び出しはリトライしない。
# put は冪等で、失敗しても次の毎時実行が同じ範囲をクロールし直すため。
resource "aws_lambda_function_event_invoke_config" "crawler" {
  function_name = aws_lambda_function.crawler.function_name

  maximum_retry_attempts = 0
}

resource "aws_cloudwatch_log_group" "crawler" {
  name              = "/aws/lambda/${local.crawler_function_name}"
  retention_in_days = 30
}

###############################################################################
# notifier（Streams の INSERT を受けて要約・Slack 投稿）
###############################################################################

resource "aws_lambda_function" "notifier" {
  function_name = local.notifier_function_name
  description   = "Summarize new What's New articles with Bedrock and post to Slack"

  filename         = local.lambda_zip_path
  source_code_hash = terraform_data.lambda_build.output

  runtime       = "python3.13"
  architectures = ["arm64"]
  handler       = "notifier.handler"
  timeout       = var.lambda_timeout
  memory_size   = 256

  role = aws_iam_role.notifier.arn

  environment {
    variables = {
      SLACK_CHANNEL_ID      = var.slack_channel_id
      SLACK_TOKEN_PARAMETER = var.slack_bot_token_parameter
      BEDROCK_MODEL_ID      = var.bedrock_model_id
      BEDROCK_REGION        = var.bedrock_region
    }
  }

  depends_on = [aws_cloudwatch_log_group.notifier]
}

resource "aws_cloudwatch_log_group" "notifier" {
  name              = "/aws/lambda/${local.notifier_function_name}"
  retention_in_days = 30
}

# DynamoDB Streams → notifier。Streams トリガーは同期呼び出しのため、
# リトライと DLQ 退避は event_invoke_config ではなくここで設定する。
resource "aws_lambda_event_source_mapping" "notifier" {
  event_source_arn  = aws_dynamodb_table.rss_history.stream_arn
  function_name     = aws_lambda_function.notifier.arn
  starting_position = "LATEST"

  # 1 記事ずつ処理する（Bedrock のレート制御と投稿順序の安定）。Streams はシャード単位で直列に
  # 呼び出すので、これで実質 1 並列になる。予約同時実行数（reserved_concurrent_executions）は
  # 使わない。新しいアカウントは Lambda の同時実行クォータが 10 のことがあり、その場合は予約を 1 つも
  # 取れず作成に失敗するため。
  batch_size = 1

  # リトライ最大2回。それでも失敗したレコードは DLQ へ退避する
  maximum_retry_attempts = 2

  destination_config {
    on_failure {
      destination_arn = aws_sqs_queue.dlq.arn
    }
  }
}

# 処理に失敗したレコードの退避先。中身（Streams レコードのメタデータ）を確認して
# 手動で再処理する運用。
resource "aws_sqs_queue" "dlq" {
  name                      = "${var.name_prefix}-dlq"
  message_retention_seconds = 1209600 # 14日（SQS の最大値）
}

###############################################################################
# Lambda 実行ロール（最小権限のため関数ごとに分離する）
###############################################################################

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

# --- crawler: Logs + DynamoDB PutItem のみ ---

resource "aws_iam_role" "crawler" {
  name = "${var.name_prefix}-lambda-crawler"
  # IAM の description は日本語(マルチバイト)不可のため英語で記述する
  description        = "Execution role for the whatsnew crawler Lambda"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "crawler" {
  # CloudWatch Logs への書き込み
  statement {
    sid    = "Logs"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${aws_cloudwatch_log_group.crawler.arn}:*"]
  }

  # 記事レコードの put
  statement {
    sid       = "PutArticles"
    effect    = "Allow"
    actions   = ["dynamodb:PutItem"]
    resources = [aws_dynamodb_table.rss_history.arn]
  }
}

resource "aws_iam_role_policy" "crawler" {
  name   = aws_iam_role.crawler.name
  role   = aws_iam_role.crawler.id
  policy = data.aws_iam_policy_document.crawler.json
}

# --- notifier: Logs + Streams 読み取り + SSM + Bedrock + DLQ ---

resource "aws_iam_role" "notifier" {
  name               = "${var.name_prefix}-lambda-notifier"
  description        = "Execution role for the whatsnew notifier Lambda"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "notifier" {
  # CloudWatch Logs への書き込み
  statement {
    sid    = "Logs"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${aws_cloudwatch_log_group.notifier.arn}:*"]
  }

  # イベントソースマッピングによる Streams の読み取り。
  # ストリームは再作成で ARN 末尾（タイムスタンプ）が変わるため、ワイルドカードで指定する
  statement {
    sid    = "ReadStream"
    effect = "Allow"
    actions = [
      "dynamodb:DescribeStream",
      "dynamodb:GetRecords",
      "dynamodb:GetShardIterator",
      "dynamodb:ListStreams",
    ]
    resources = ["${aws_dynamodb_table.rss_history.arn}/stream/*"]
  }

  # Slack Bot Token（SecureString）の読み取り
  statement {
    sid       = "ReadSlackToken"
    effect    = "Allow"
    actions   = ["ssm:GetParameter"]
    resources = ["arn:${local.partition}:ssm:${var.region}:${local.account_id}:parameter${var.slack_bot_token_parameter}"]
  }

  # Bedrock の Claude 呼び出し。
  # 推論プロファイル（jp. / global. プレフィックス）経由の呼び出しは、
  # プロファイルと転送先の foundation-model の両方への許可が必要。
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

  # リトライ超過レコードの DLQ 退避（イベントソースマッピングの on_failure 先。
  # 送信は関数の実行ロールの権限で行われる）
  statement {
    sid       = "SendToDlq"
    effect    = "Allow"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.dlq.arn]
  }
}

resource "aws_iam_role_policy" "notifier" {
  name   = aws_iam_role.notifier.name
  role   = aws_iam_role.notifier.id
  policy = data.aws_iam_policy_document.notifier.json
}
