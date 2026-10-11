"""notifier.py のユニットテスト。

boto3 / bs4 に依存しない部分（Streams イベントの抽出・プロンプト構築・
Slack メッセージ整形・投稿ペイロード）を検証する。
実行はリポジトリのルートで `python3 -m pytest tests/`。
"""

import io
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda"))

import notifier  # noqa: E402

ARTICLE = {
    "url": "https://aws.amazon.com/about-aws/whats-new/2026/07/s3-new-feature/",
    "title": "Amazon S3 announces a new feature",
    "description": "S3 now supports something new.",
    "published_at": "2026-07-19T12:00:00Z",
}


def _insert_record(article=ARTICLE):
    return {
        "eventName": "INSERT",
        "dynamodb": {
            "NewImage": {
                "url": {"S": article["url"]},
                "title": {"S": article["title"]},
                "description": {"S": article["description"]},
                "published_at": {"S": article["published_at"]},
                "ttl": {"N": "1786190400"},
            }
        },
    }


class TestExtractArticles:
    def test_insert_is_extracted(self):
        event = {"Records": [_insert_record()]}
        articles = notifier.extract_articles(event)
        assert len(articles) == 1
        assert articles[0]["url"] == ARTICLE["url"]
        assert articles[0]["title"] == ARTICLE["title"]
        assert articles[0]["description"] == ARTICLE["description"]
        assert articles[0]["published_at"] == ARTICLE["published_at"]

    def test_modify_and_remove_are_skipped(self):
        # 既存記事の再 put（MODIFY）と TTL 削除（REMOVE）は通知しない
        event = {
            "Records": [
                dict(_insert_record(), eventName="MODIFY"),
                dict(_insert_record(), eventName="REMOVE"),
            ]
        }
        assert notifier.extract_articles(event) == []

    def test_record_without_url_is_skipped(self):
        record = {"eventName": "INSERT", "dynamodb": {"NewImage": {}}}
        assert notifier.extract_articles({"Records": [record]}) == []

    def test_empty_event(self):
        assert notifier.extract_articles({}) == []


class TestBuildPrompt:
    def test_contains_prompt_and_article(self):
        prompt = notifier.build_prompt(ARTICLE, "article body text")
        # prompt.md の内容が先頭に来る
        assert "要約" in prompt
        assert "ユースケース" in prompt
        # 記事情報が後続する
        assert ARTICLE["title"] in prompt
        assert ARTICLE["url"] in prompt
        assert "article body text" in prompt

    def test_long_body_is_truncated(self):
        prompt = notifier.build_prompt(ARTICLE, "x" * (notifier.MAX_BODY_CHARS + 100))
        assert "...(以下省略)" in prompt


class TestFormatMessage:
    def test_with_summary(self):
        text = notifier.format_message(ARTICLE, "*要約*\n新機能の説明。")
        assert f"*<{ARTICLE['url']}|{ARTICLE['title']}>*" in text
        assert "新機能の説明。" in text
        assert ARTICLE["published_at"] in text

    def test_without_summary_falls_back_to_link_only(self):
        # Bedrock 失敗時は要約なしのタイトル + リンクで投稿する
        text = notifier.format_message(ARTICLE, None)
        assert f"*<{ARTICLE['url']}|{ARTICLE['title']}>*" in text
        assert "要約の生成に失敗" in text


class TestPostToSlack:
    @pytest.fixture(autouse=True)
    def env(self, monkeypatch):
        monkeypatch.setenv("SLACK_CHANNEL_ID", "C0123456789")
        monkeypatch.setattr(notifier, "_slack_token_cache", "xoxb-test")

    def _fake_urlopen(self, captured, body=None):
        class FakeResponse(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def fake(request, timeout=None):
            captured["request"] = request
            captured["payload"] = json.loads(request.data)
            return FakeResponse(json.dumps(body or {"ok": True}).encode())

        return fake

    def test_payload(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            notifier.urllib.request, "urlopen", self._fake_urlopen(captured)
        )
        notifier.post_to_slack("hello")
        assert captured["payload"]["channel"] == "C0123456789"
        assert captured["payload"]["text"] == "hello"
        assert captured["request"].headers["Authorization"] == "Bearer xoxb-test"

    def test_slack_error_raises(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            notifier.urllib.request,
            "urlopen",
            self._fake_urlopen(captured, body={"ok": False, "error": "not_in_channel"}),
        )
        with pytest.raises(RuntimeError, match="not_in_channel"):
            notifier.post_to_slack("hello")
