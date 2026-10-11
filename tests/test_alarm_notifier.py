"""alarm_notifier.py のユニットテスト。

boto3 に依存しない部分（イベントの整形（attachments）・プロンプト構築・投稿ペイロード・
スレッド化・メンション・失敗通知）を検証する。
実行はリポジトリのルートで `python3 -m pytest tests/`。
"""

import io
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda"))

import alarm_notifier  # noqa: E402

ALARM_NAME = "prod-whatsnew-dlq-messages-visible-p3"
THREAD_KEY = f"aws.cloudwatch#{ALARM_NAME}"

ALARM_EVENT = {
    "source": "aws.cloudwatch",
    "detail-type": "CloudWatch Alarm State Change",
    "account": "123456789012",
    "region": "ap-northeast-1",
    "time": "2026-10-10T12:00:00Z",
    "detail": {
        "alarmName": ALARM_NAME,
        "state": {
            "value": "ALARM",
            "reason": "Threshold Crossed: 1 datapoint [1.0] was greater than or equal to the threshold (1.0).",
        },
        "previousState": {"value": "OK"},
        "configuration": {
            "description": "prod-whatsnew-dlq にメッセージが滞留しています",
            "metrics": [
                {
                    "id": "m1",
                    "metricStat": {
                        "metric": {
                            "namespace": "AWS/SQS",
                            "name": "ApproximateNumberOfMessagesVisible",
                            "dimensions": {"QueueName": "prod-whatsnew-dlq"},
                        },
                        "period": 300,
                        "stat": "Maximum",
                    },
                    "returnData": True,
                }
            ]
        },
    },
}


def _with_detail(event, **detail):
    """サンプルイベントの detail を一部上書きしたコピーを返す。"""
    copied = json.loads(json.dumps(event))
    copied["detail"].update(detail)
    return copied


OK_EVENT = _with_detail(
    ALARM_EVENT, state={"value": "OK"}, previousState={"value": "ALARM"}
)


def _blocks_text(attachments):
    """attachments の全ブロックのテキストを連結する（表示内容の検証用）。"""
    texts = []
    for block in attachments[0]["blocks"]:
        if "text" in block:
            texts.append(block["text"]["text"])
        for item in block.get("fields", []) + block.get("elements", []):
            texts.append(item["text"])
    return "\n".join(texts)


class TestFormatHeadline:
    def test_alarm(self):
        assert alarm_notifier.format_headline(ALARM_EVENT) == f"[ALARM] {ALARM_NAME}"

    def test_resolved(self):
        assert alarm_notifier.format_headline(OK_EVENT) == f"[OK] {ALARM_NAME}"


class TestBuildAttachments:
    def test_alarm_is_red(self):
        attachments = alarm_notifier.build_attachments(ALARM_EVENT)
        assert len(attachments) == 1
        assert attachments[0]["color"] == "#D13212"

    def test_resolved_is_green(self):
        attachments = alarm_notifier.build_attachments(OK_EVENT)
        assert attachments[0]["color"] == "#2EB67D"
        assert "ALARM → OK" in _blocks_text(attachments)

    def test_other_state_is_gray(self):
        event = _with_detail(ALARM_EVENT, state={"value": "INSUFFICIENT_DATA"})
        assert alarm_notifier.build_attachments(event)[0]["color"] == "#8D99A5"

    def test_title_shows_alarm_region_and_account(self):
        # どのアカウント・リージョンの発信かを判別できること
        title = alarm_notifier.build_attachments(ALARM_EVENT)[0]["blocks"][0]
        assert title["text"]["text"] == (
            f":rotating_light: *CloudWatch Alarm | {ALARM_NAME}*\n"
            "ap-northeast-1 | Account: 123456789012"
        )

    def test_resolved_title_has_check_mark(self):
        title = alarm_notifier.build_attachments(OK_EVENT)[0]["blocks"][0]
        assert title["text"]["text"].startswith(
            f":white_check_mark: *CloudWatch Alarm | {ALARM_NAME}*"
        )

    def test_other_state_title_has_no_mark(self):
        event = _with_detail(ALARM_EVENT, state={"value": "INSUFFICIENT_DATA"})
        title = alarm_notifier.build_attachments(event)[0]["blocks"][0]
        assert title["text"]["text"].startswith(f"*CloudWatch Alarm | {ALARM_NAME}*")

    def test_fields(self):
        text = _blocks_text(alarm_notifier.build_attachments(ALARM_EVENT))
        assert f"*アラーム名*\n{ALARM_NAME}" in text
        assert "*状態*\nOK → ALARM" in text
        assert "*メトリクス*\nAWS/SQS / ApproximateNumberOfMessagesVisible" in text
        assert "*ディメンション*\nQueueName = prod-whatsnew-dlq" in text
        assert "*統計*\nMaximum" in text
        assert "*期間*\n300 秒" in text

    def test_description_and_reason(self):
        text = _blocks_text(alarm_notifier.build_attachments(ALARM_EVENT))
        assert "prod-whatsnew-dlq にメッセージが滞留しています" in text
        assert "*理由*\nThreshold Crossed" in text

    def test_resolved_omits_description(self):
        # 説明文は ALARM 時の文言なので、解消報では出さない
        text = _blocks_text(alarm_notifier.build_attachments(OK_EVENT))
        assert "滞留しています" not in text
        assert "*状態*\nALARM → OK" in text

    def test_links_and_time(self):
        text = _blocks_text(alarm_notifier.build_attachments(ALARM_EVENT))
        assert (
            "<https://ap-northeast-1.console.aws.amazon.com/cloudwatch/home"
            f"?region=ap-northeast-1#alarmsV2:alarm/{ALARM_NAME}|アラームを開く>"
        ) in text
        assert (
            "<https://ap-northeast-1.console.aws.amazon.com/sqs/v3/home"
            "?region=ap-northeast-1#/queues/https%3A%2F%2Fsqs.ap-northeast-1.amazonaws.com"
            "%2F123456789012%2Fprod-whatsnew-dlq|キューを開く>"
        ) in text
        # イベントの時刻（UTC）は JST で表示する
        assert "2026-10-10 21:00:00 JST" in text

    def test_without_metric(self):
        # メトリクスの定義が取れなくても、その項目とキューのリンクが抜けるだけで整形できること
        event = _with_detail(ALARM_EVENT, configuration={})
        text = _blocks_text(alarm_notifier.build_attachments(event))
        assert ALARM_NAME in text
        assert "メトリクス" not in text
        assert "キューを開く" not in text

    def test_without_queue_dimension(self):
        event = json.loads(json.dumps(ALARM_EVENT))
        event["detail"]["configuration"]["metrics"][0]["metricStat"]["metric"][
            "dimensions"
        ] = {"FunctionName": "notifier"}
        text = _blocks_text(alarm_notifier.build_attachments(event))
        assert "*ディメンション*\nFunctionName = notifier" in text
        assert "キューを開く" not in text

    def test_fields_fit_in_one_section(self):
        # section ブロックの fields は最大 10 個
        blocks = alarm_notifier.build_attachments(ALARM_EVENT)[0]["blocks"]
        fields = next(block["fields"] for block in blocks if "fields" in block)
        assert len(fields) <= 10

    def test_long_reason_is_truncated(self):
        event = _with_detail(ALARM_EVENT, state={"value": "ALARM", "reason": "x" * 5000})
        text = _blocks_text(alarm_notifier.build_attachments(event))
        assert "...(以下省略)" in text
        # section ブロックのテキストは 3000 文字まで
        for block in alarm_notifier.build_attachments(event)[0]["blocks"]:
            if "text" in block:
                assert len(block["text"]["text"]) <= 3000

    def test_unparsable_time_is_shown_as_is(self):
        event = dict(ALARM_EVENT, time="not-a-time")
        assert "not-a-time" in _blocks_text(alarm_notifier.build_attachments(event))


class TestBuildPrompt:
    def test_contains_prompt_and_event_json(self):
        prompt = alarm_notifier.build_prompt(ALARM_EVENT)
        assert "推奨対応" in prompt
        assert ALARM_NAME in prompt

    def test_large_event_is_truncated(self):
        event = _with_detail(ALARM_EVENT, padding="x" * 30000)
        prompt = alarm_notifier.build_prompt(event)
        assert "...(以下省略)" in prompt


class TestPostToSlack:
    @pytest.fixture(autouse=True)
    def env(self, monkeypatch):
        monkeypatch.setenv("SLACK_CHANNEL_ID", "C0123456789")
        monkeypatch.setattr(alarm_notifier, "_slack_token_cache", "xoxb-test")

    def _fake_urlopen(self, captured):
        class FakeResponse(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def fake(request, timeout=None):
            captured["request"] = request
            captured["payload"] = json.loads(request.data)
            return FakeResponse(json.dumps({"ok": True, "ts": "123.456"}).encode())

        return fake

    def test_first_post(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            alarm_notifier.urllib.request, "urlopen", self._fake_urlopen(captured)
        )
        ts = alarm_notifier.post_to_slack("hello")
        assert ts == "123.456"
        assert captured["payload"]["channel"] == "C0123456789"
        assert captured["payload"]["text"] == "hello"
        assert "thread_ts" not in captured["payload"]
        assert captured["request"].headers["Authorization"] == "Bearer xoxb-test"

    def test_thread_reply(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            alarm_notifier.urllib.request, "urlopen", self._fake_urlopen(captured)
        )
        alarm_notifier.post_to_slack("summary", thread_ts="123.456")
        assert captured["payload"]["thread_ts"] == "123.456"
        assert "attachments" not in captured["payload"]

    def test_attachments_are_sent(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            alarm_notifier.urllib.request, "urlopen", self._fake_urlopen(captured)
        )
        attachments = alarm_notifier.build_attachments(ALARM_EVENT)
        alarm_notifier.post_to_slack("<@U0123456789>", attachments=attachments)
        assert captured["payload"]["text"] == "<@U0123456789>"
        assert captured["payload"]["attachments"] == attachments

    def test_empty_text_is_omitted(self, monkeypatch):
        # 帯だけを投稿するときは text を送らない（帯の上に空行を出さない）
        captured = {}
        monkeypatch.setattr(
            alarm_notifier.urllib.request, "urlopen", self._fake_urlopen(captured)
        )
        attachments = alarm_notifier.build_attachments(ALARM_EVENT)
        alarm_notifier.post_to_slack("", attachments=attachments)
        assert "text" not in captured["payload"]
        assert captured["payload"]["attachments"] == attachments

    def test_thread_reply_is_not_broadcast(self, monkeypatch):
        # 解消報・要約はスレッド内だけに返信し、チャネルには表示しない
        captured = {}
        monkeypatch.setattr(
            alarm_notifier.urllib.request, "urlopen", self._fake_urlopen(captured)
        )
        alarm_notifier.post_to_slack("resolved", thread_ts="123.456")
        assert "reply_broadcast" not in captured["payload"]

    def test_slack_error_raises(self, monkeypatch):
        class FakeResponse(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        monkeypatch.setattr(
            alarm_notifier.urllib.request,
            "urlopen",
            lambda request, timeout=None: FakeResponse(
                json.dumps({"ok": False, "error": "channel_not_found"}).encode()
            ),
        )
        with pytest.raises(RuntimeError, match="channel_not_found"):
            alarm_notifier.post_to_slack("hello")


class TestThreadRules:
    def test_key_uses_alarm_name(self):
        assert alarm_notifier.thread_key(ALARM_EVENT) == THREAD_KEY

    def test_missing_alarm_name_returns_none(self):
        assert alarm_notifier.thread_key({"detail": {}}) is None

    def test_alarm_starts_new_thread(self):
        assert alarm_notifier.starts_new_thread(ALARM_EVENT) is True

    def test_ok_does_not_start_new_thread(self):
        assert alarm_notifier.starts_new_thread(OK_EVENT) is False


class FakeTable:
    """DynamoDB Table の get_item / put_item だけを模倣する。"""

    def __init__(self, fail=False):
        self.items = {}
        self.fail = fail

    def get_item(self, Key):
        if self.fail:
            raise RuntimeError("dynamodb unavailable")
        item = self.items.get(Key["thread_key"])
        return {"Item": item} if item else {}

    def put_item(self, Item):
        if self.fail:
            raise RuntimeError("dynamodb unavailable")
        self.items[Item["thread_key"]] = Item


class TestHandlerThreading:
    @pytest.fixture
    def env(self, monkeypatch):
        monkeypatch.setenv("THREADS_TABLE", "prod-whatsnew-alarm-threads")
        monkeypatch.setenv("THREAD_TTL_DAYS", "30")
        table = FakeTable()
        monkeypatch.setattr(alarm_notifier, "_threads_table_cache", table)
        monkeypatch.setattr(alarm_notifier, "summarize", lambda event: "要約")

        posts = []

        def fake_post(text, thread_ts=None, attachments=None):
            posts.append(
                {"text": text, "thread_ts": thread_ts, "attachments": attachments}
            )
            return f"{len(posts)}.000"

        monkeypatch.setattr(alarm_notifier, "post_to_slack", fake_post)
        return table, posts

    def test_alarm_posts_first_report_and_summary(self, env):
        table, posts = env
        result = alarm_notifier.handler(ALARM_EVENT, None)

        # 第一報は新規投稿、要約はそのスレッドへ返信する
        assert posts[0]["thread_ts"] is None
        assert posts[0]["attachments"][0]["color"] == "#D13212"
        # 要約は色の帯を付けないテキストの返信
        assert posts[1] == {"text": "要約", "thread_ts": result["ts"], "attachments": None}
        assert table.items[THREAD_KEY]["thread_ts"] == result["ts"]

    def test_alarm_then_ok_then_new_alarm(self, env):
        _, posts = env

        alarm1 = alarm_notifier.handler(ALARM_EVENT, None)
        resolved = alarm_notifier.handler(OK_EVENT, None)
        alarm2 = alarm_notifier.handler(ALARM_EVENT, None)

        # OK は ALARM のスレッドへ返信する
        assert resolved["thread_ts"] == alarm1["ts"]
        assert posts[2]["thread_ts"] == alarm1["ts"]
        # 次の ALARM は古いスレッドに繋がらず新しいスレッドになる
        assert posts[4]["thread_ts"] is None
        assert alarm2["thread_ts"] == alarm2["ts"] != alarm1["ts"]

    def test_ttl_is_saved(self, env):
        table, _ = env
        before = int(alarm_notifier.time.time())
        alarm_notifier.handler(ALARM_EVENT, None)
        expires_at = table.items[THREAD_KEY]["expires_at"]
        assert before + 30 * 86400 <= expires_at <= before + 30 * 86400 + 5

    def test_expired_item_is_ignored(self, env):
        table, posts = env
        table.items[THREAD_KEY] = {
            "thread_key": THREAD_KEY,
            "thread_ts": "old.000",
            "expires_at": int(alarm_notifier.time.time()) - 1,
        }
        alarm_notifier.handler(OK_EVENT, None)
        assert posts[0]["thread_ts"] is None

    def test_table_failure_falls_back_to_new_post(self, env, monkeypatch):
        _, posts = env
        monkeypatch.setattr(alarm_notifier, "_threads_table_cache", FakeTable(fail=True))
        result = alarm_notifier.handler(OK_EVENT, None)
        # 読み書きに失敗しても第一報と要約は投稿される
        assert posts[0]["thread_ts"] is None
        assert posts[1]["thread_ts"] == result["ts"]

    def test_no_table_configured(self, env, monkeypatch):
        _, posts = env
        monkeypatch.delenv("THREADS_TABLE")
        alarm_notifier.handler(ALARM_EVENT, None)
        alarm_notifier.handler(OK_EVENT, None)
        assert posts[2]["thread_ts"] is None


class TestMention:
    def test_with_mention_user(self, monkeypatch):
        monkeypatch.setenv("SLACK_MENTION_USER_ID", "U0123456789")
        assert alarm_notifier.mention_text() == "<@U0123456789>"

    def test_without_mention_user(self, monkeypatch):
        monkeypatch.delenv("SLACK_MENTION_USER_ID", raising=False)
        assert alarm_notifier.mention_text() == ""

    def test_empty_mention_user(self, monkeypatch):
        # 変数の既定値（空文字）ではメンションしない
        monkeypatch.setenv("SLACK_MENTION_USER_ID", "")
        assert alarm_notifier.mention_text() == ""


class TestHandlerWithoutMention(TestHandlerThreading):
    """メンション先が未設定なら、帯の上に本文を出さず帯だけを投稿すること。"""

    @pytest.fixture(autouse=True)
    def no_mention_env(self, monkeypatch):
        monkeypatch.delenv("SLACK_MENTION_USER_ID", raising=False)

    def test_first_report_and_resolution_have_no_text(self, env):
        _, posts = env
        alarm_notifier.handler(ALARM_EVENT, None)
        alarm_notifier.handler(OK_EVENT, None)

        # 第一報と解消報は帯だけ（見出しは帯の中と fallback にある）
        assert posts[0]["text"] == ""
        assert posts[0]["attachments"][0]["fallback"] == f"[ALARM] {ALARM_NAME}"
        assert posts[2]["text"] == ""
        assert posts[2]["attachments"][0]["fallback"] == f"[OK] {ALARM_NAME}"


class TestHandlerMention(TestHandlerThreading):
    """第一報だけにメンションが付き、解消報・要約には付かないこと。"""

    @pytest.fixture(autouse=True)
    def mention_env(self, monkeypatch):
        # 親クラスの env（スレッド対応表・投稿の差し替え）に加えてメンションを設定する。
        # 親クラスのスレッド化テストもメンションありの状態で再実行される
        monkeypatch.setenv("SLACK_MENTION_USER_ID", "U0123456789")

    def test_each_alarm_mentions(self, env):
        # ALARM 遷移は毎回新しいスレッドなので毎回メンションし、OK（解消）ではしない
        _, posts = env
        alarm_notifier.handler(ALARM_EVENT, None)
        alarm_notifier.handler(OK_EVENT, None)
        alarm_notifier.handler(ALARM_EVENT, None)

        mention = "<@U0123456789>"
        # 第一報の帯の上は、メンションだけの行にする
        assert posts[0]["text"] == mention
        # 要約・解消報・解消報の要約には付けない
        assert all(mention not in p["text"] for p in posts[1:4])
        # 解消報は帯だけ
        assert posts[2]["text"] == ""
        assert posts[4]["text"] == mention


class TestFailureReport:
    """関数自身の失敗をログに残し、Slack にも通知すること。"""

    @pytest.fixture
    def env(self, monkeypatch):
        monkeypatch.delenv("THREADS_TABLE", raising=False)
        monkeypatch.delenv("SLACK_MENTION_USER_ID", raising=False)
        monkeypatch.setenv(
            "AWS_LAMBDA_LOG_GROUP_NAME", "/aws/lambda/prod-whatsnew-alarm-notifier"
        )
        posts = []

        def fake_post(text, thread_ts=None, attachments=None):
            posts.append(
                {"text": text, "thread_ts": thread_ts, "attachments": attachments}
            )
            return f"{len(posts)}.000"

        monkeypatch.setattr(alarm_notifier, "post_to_slack", fake_post)
        return posts

    def test_summarize_failure_is_reported_in_thread(self, env, monkeypatch, caplog):
        posts = env

        def fail(event):
            raise RuntimeError("ThrottlingException: Too many requests")

        monkeypatch.setattr(alarm_notifier, "summarize", fail)

        with pytest.raises(RuntimeError, match="ThrottlingException"):
            alarm_notifier.handler(ALARM_EVENT, None)

        # 第一報は投稿済みで、失敗通知はそのスレッドへ返信する
        assert len(posts) == 2
        assert posts[0]["thread_ts"] is None
        assert posts[1]["thread_ts"] == "1.000"
        assert "処理に失敗しました" in posts[1]["text"]
        assert ALARM_NAME in posts[1]["text"]
        assert "RuntimeError: ThrottlingException" in posts[1]["text"]
        assert "/aws/lambda/prod-whatsnew-alarm-notifier" in posts[1]["text"]
        # ログにも残る
        assert "アラーム通知の処理に失敗した" in caplog.text

    def test_slack_failure_is_only_logged(self, monkeypatch, caplog):
        # Slack への投稿そのものが失敗する障害は Slack では知らせられず、ログだけが残る
        monkeypatch.delenv("THREADS_TABLE", raising=False)
        attempts = []

        def fail(text, thread_ts=None, attachments=None):
            attempts.append(text)
            raise RuntimeError("Slack API error: invalid_auth")

        monkeypatch.setattr(alarm_notifier, "post_to_slack", fail)

        with pytest.raises(RuntimeError, match="invalid_auth"):
            alarm_notifier.handler(ALARM_EVENT, None)

        # 第一報と失敗通知の2回を試み、どちらも失敗する
        assert len(attempts) == 2
        assert "アラーム通知の処理に失敗した" in caplog.text
        assert "失敗通知の Slack 投稿にも失敗した" in caplog.text

    def test_long_error_is_truncated(self):
        text = alarm_notifier.format_failure(ALARM_EVENT, RuntimeError("x" * 2000))
        assert "...(以下省略)" in text
        assert len(text) < 1000
