"""crawler.py のユニットテスト。

boto3 に依存しない部分（フィードのパース・鮮度判定・put する item の構築）を検証する。
実行はリポジトリのルートで `python3 -m pytest tests/`。
"""

import calendar
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda"))

import crawler  # noqa: E402

# 2026-07-20 00:00:00 UTC を「現在」としてテストする
NOW = calendar.timegm((2026, 7, 20, 0, 0, 0, 0, 0, 0))

FEED_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Recent Announcements</title>
    <item>
      <title>Amazon S3 announces a new feature</title>
      <link>https://aws.amazon.com/about-aws/whats-new/2026/07/s3-new-feature/</link>
      <description>S3 now supports something new.</description>
      <pubDate>Sun, 19 Jul 2026 12:00:00 +0000</pubDate>
    </item>
    <item>
      <title>Old announcement</title>
      <link>https://aws.amazon.com/about-aws/whats-new/2026/07/old/</link>
      <description>This one is stale.</description>
      <pubDate>Fri, 10 Jul 2026 12:00:00 +0000</pubDate>
    </item>
    <item>
      <title>No publish date</title>
      <link>https://aws.amazon.com/about-aws/whats-new/2026/07/no-date/</link>
      <description>Entry without pubDate.</description>
    </item>
  </channel>
</rss>
"""


class TestParseEntries:
    def test_returns_entries_with_link_and_date(self):
        entries = crawler.parse_entries(FEED_XML.encode())
        # pubDate の無い3件目は除外される
        assert len(entries) == 2
        assert (
            entries[0]["link"]
            == "https://aws.amazon.com/about-aws/whats-new/2026/07/s3-new-feature/"
        )
        assert entries[0]["title"] == "Amazon S3 announces a new feature"

    def test_broken_xml_returns_empty(self):
        assert crawler.parse_entries(b"not xml at all") == []


class TestIsFresh:
    def _entry(self, pubdate_utc):
        return {"published_parsed": time.gmtime(calendar.timegm(pubdate_utc))}

    def test_within_window(self):
        entry = self._entry((2026, 7, 19, 12, 0, 0, 0, 0, 0))  # 12時間前
        assert crawler.is_fresh(entry, NOW, window_days=2)

    def test_outside_window(self):
        entry = self._entry((2026, 7, 10, 12, 0, 0, 0, 0, 0))  # 9.5日前
        assert not crawler.is_fresh(entry, NOW, window_days=2)

    def test_boundary_is_inclusive(self):
        entry = self._entry((2026, 7, 18, 0, 0, 0, 0, 0, 0))  # ちょうど2日前
        assert crawler.is_fresh(entry, NOW, window_days=2)


class TestBuildItem:
    def test_item_attributes(self):
        entries = crawler.parse_entries(FEED_XML.encode())
        item = crawler.build_item(entries[0], NOW)
        assert item["url"] == (
            "https://aws.amazon.com/about-aws/whats-new/2026/07/s3-new-feature/"
        )
        assert item["title"] == "Amazon S3 announces a new feature"
        assert item["description"] == "S3 now supports something new."
        assert item["published_at"] == "2026-07-19T12:00:00Z"
        # ttl 属性（テーブル定義の attribute_name と同名）は put 時刻 + 30日
        assert item["ttl"] == int(NOW) + crawler.TTL_DAYS * 86400
