# RSS 取得済み記事の履歴テーブル。
# crawler が取得した記事 URL を put し、Streams(NEW_IMAGE) で notifier を起動する。

resource "aws_dynamodb_table" "rss_history" {
  name         = "${var.name_prefix}-rss-history"
  billing_mode = "PAY_PER_REQUEST"

  hash_key = "url"

  attribute {
    name = "url"
    type = "S"
  }

  stream_enabled   = true
  stream_view_type = "NEW_IMAGE"

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  deletion_protection_enabled = true
}
