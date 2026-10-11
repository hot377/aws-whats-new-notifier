"""AWS What's New の RSS を取得し、鮮度ウィンドウ内の記事を DynamoDB に put する。

処理の流れ:
    1. RSS フィードを取得して feedparser でパースする
    2. 発行日時が鮮度ウィンドウ（既定2日）内の記事だけを対象にする
    3. 記事ごとに DynamoDB へ put する（URL が PK。既存記事の再 put は
       MODIFY イベントになり、notifier は INSERT のみ処理するため再通知されない）

put は冪等なため、失敗しても次の毎時実行が同じ範囲をクロールし直す（リトライ不要）。
boto3 / feedparser の import は関数内で行い、SDK なしでもユニットテストできるようにしている。
"""
import calendar
import os
import time
import urllib.request

# put する記事レコードの TTL（日数）。鮮度ウィンドウより十分長くし、
# TTL 削除後に同じ記事が再 put されても鮮度ウィンドウ外なので再通知は起きない
TTL_DAYS = 30

_table = None


def handler(event, context):
    entries = parse_entries(fetch_feed(os.environ["FEED_URL"]))
    now = time.time()
    window_days = int(os.environ.get("FRESHNESS_WINDOW_DAYS", "2"))

    fresh = [e for e in entries if is_fresh(e, now, window_days)]
    for entry in fresh:
        put_article(build_item(entry, now))

    print(f"feed entries: {len(entries)}, put: {len(fresh)}")
    return {"total": len(entries), "put": len(fresh)}


def fetch_feed(url):
    """RSS フィードを取得して生の XML（bytes）を返す。"""
    request = urllib.request.Request(url, headers={"User-Agent": "whatsnew-crawler"})
    with urllib.request.urlopen(request, timeout=15) as response:
        return response.read()


def parse_entries(feed_xml):
    """フィード XML をパースし、必須項目（URL・発行日時）が揃った記事だけ返す。"""
    import feedparser

    feed = feedparser.parse(feed_xml)
    return [e for e in feed.entries if e.get("link") and e.get("published_parsed")]


def is_fresh(entry, now, window_days):
    """発行日時が鮮度ウィンドウ内かどうか。初回実行時に過去記事を大量 put しないためのガード。"""
    published = calendar.timegm(entry["published_parsed"])  # struct_time は UTC
    return now - published <= window_days * 86400


def build_item(entry, now):
    """DynamoDB へ put する属性を組み立てる。ttl はテーブル定義の attribute_name と一致させる。"""
    return {
        "url": entry["link"],
        "title": entry.get("title", ""),
        "description": entry.get("summary", ""),
        "published_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", entry["published_parsed"]),
        "ttl": int(now) + TTL_DAYS * 86400,
    }


def put_article(item):
    """記事1件を DynamoDB へ put する（実行環境内でテーブルをキャッシュ）。"""
    global _table
    if _table is None:
        import boto3

        _table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
    _table.put_item(Item=item)