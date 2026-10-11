"""whatsnew の DLQ 滞留アラームを Slack のアラート用チャネルへ通知する。

CloudWatch アラームの状態変化イベント（aws.cloudwatch）を EventBridge 経由で受け取る。

処理の流れ:
    1. ALARM 遷移は毎回新しいスレッドを始める。解消（ALARM → OK）は DynamoDB の
       スレッド対応表からその ALARM の thread_ts を取得し、同じスレッドへ返信する
    2. イベントを整形して Slack に投稿する。AWS Chatbot の通知に倣い、状態を表す色の帯
       （ALARM は赤、解消は緑）の中に、見出し・項目の2列表示・コンソールへのリンクを並べる。
       帯の上には何も出さない。ただしメンション先が設定されていれば、新しいスレッドを始める
       第一報にだけ、帯の上にメンションだけの行を出す。thread_ts は対応表に保存する
    3. alarm_prompt.md のプロンプト + イベント JSON を Bedrock の Claude（converse API）に
       渡して日本語訳・要約を生成し、同じスレッドに返信する

第一報を要約より先に投稿するため、Bedrock が失敗しても通知そのものは届く。
スレッド対応表の読み書きに失敗しても通知は止めない（新規投稿に倒す）。

この関数自身の失敗はログに出力し、失敗した旨を Slack にも投稿したうえで例外を送出する。
ただし Slack への投稿そのものが失敗する障害（Token 失効・Bot 未招待・Slack 障害）は
Slack では知らせられず、ログにしか残らない。
非同期呼び出しはリトライなしの設定なので、第一報が重複することはない。
boto3 の import は関数内で行い、SDK なしでもユニットテストできるようにしている。
"""

import json
import logging
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

PROMPT_PATH = os.path.join(os.path.dirname(__file__), "alarm_prompt.md")
SLACK_API_URL = "https://slack.com/api/chat.postMessage"

# Slack 投稿に載せる原文の最大文字数（超過分は切り詰める。section ブロックの上限は 3000 文字）
MAX_ORIGINAL_CHARS = 2500
# 帯の色（AWS Chatbot と同じく、ALARM は赤・OK は緑・それ以外は灰色）
STATE_COLORS = {"ALARM": "#D13212", "OK": "#2EB67D"}
DEFAULT_COLOR = "#8D99A5"
# 見出しの先頭に付けるマーク（Slack の絵文字コード。ALARM は回転灯、OK はチェック）
STATE_MARKS = {"ALARM": ":rotating_light:", "OK": ":white_check_mark:"}
JST = timezone(timedelta(hours=9), "JST")
# Bedrock に渡すイベント JSON の最大文字数
MAX_EVENT_JSON_CHARS = 20000
# 失敗通知に載せるエラーメッセージの最大文字数
MAX_ERROR_CHARS = 500
# スレッド対応表の既定の保持日数（環境変数 THREAD_TTL_DAYS で上書き）
DEFAULT_THREAD_TTL_DAYS = 30

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_slack_token_cache = None
_threads_table_cache = None


def handler(event, context):
    # 失敗通知を第一報のスレッドに繋げるため、進行状況（thread_ts）を notify から受け取る
    progress = {}
    try:
        return notify(event, progress)
    except Exception as error:
        logger.exception("アラーム通知の処理に失敗した")
        report_failure(event, error, progress.get("thread_ts"))
        raise


def notify(event, progress):
    """アラームイベント1件を Slack に投稿し、要約をスレッドに返信する。"""
    key = thread_key(event)
    parent_ts = None
    # ALARM 遷移は毎回新しいスレッドにする（時間を置いた別件の障害が
    # 古いスレッドに繋がらないように）。解消（OK）はその ALARM のスレッドへ返信する
    if key and not starts_new_thread(event):
        parent_ts = load_thread_ts(key)

    # 帯の上の本文は、新しいスレッドを始める第一報のメンションだけにする
    # （解消報・要約では鳴らさない。メンション先が未設定なら帯だけを投稿する）
    text = "" if parent_ts else mention_text()
    posted_ts = post_to_slack(
        text, thread_ts=parent_ts, attachments=build_attachments(event)
    )
    thread_ts = parent_ts or posted_ts
    progress["thread_ts"] = thread_ts
    if key and not parent_ts:
        save_thread_ts(key, thread_ts)

    # ここから先で失敗しても第一報は投稿済み
    summary = summarize(event)
    post_to_slack(summary, thread_ts=thread_ts)
    return {"ok": True, "ts": posted_ts, "thread_ts": thread_ts}


def report_failure(event, error, thread_ts=None):
    """この関数自身の失敗を Slack に投稿する。投稿にも失敗した場合はログだけを残す。"""
    try:
        post_to_slack(format_failure(event, error), thread_ts=thread_ts)
    except Exception:
        logger.exception("失敗通知の Slack 投稿にも失敗した（ログのみ残る）")


def format_failure(event, error):
    """失敗通知のテキスト（mrkdwn）を組み立てる。"""
    detail = event.get("detail") or {}
    lines = [
        "*whatsnew アラーム通知の処理に失敗しました*",
        f"アラーム: {detail.get('alarmName', '-')}",
        f"エラー: {type(error).__name__}: {_truncate(str(error), MAX_ERROR_CHARS)}",
    ]
    log_group = os.environ.get("AWS_LAMBDA_LOG_GROUP_NAME")
    if log_group:
        lines.append(f"ログ: {log_group}")
    return "\n".join(lines)


def mention_text():
    """環境変数 SLACK_MENTION_USER_ID が設定されていればユーザーメンション、無ければ空文字を返す。"""
    user_id = os.environ.get("SLACK_MENTION_USER_ID")
    return f"<@{user_id}>" if user_id else ""


def thread_key(event):
    """解消報を ALARM と同じスレッドにまとめるためのキーを返す。取れなければ None（常に新規投稿）。"""
    alarm_name = (event.get("detail") or {}).get("alarmName")
    return f"aws.cloudwatch#{alarm_name}" if alarm_name else None


def starts_new_thread(event):
    """既存スレッドを探さず、必ず新しいスレッドを始めるイベントか。"""
    state = (event.get("detail") or {}).get("state") or {}
    return state.get("value") == "ALARM"


def load_thread_ts(key):
    """スレッド対応表から thread_ts を取得する。無い・失敗時は None（新規投稿に倒す）。"""
    table = _threads_table()
    if table is None:
        return None
    try:
        item = table.get_item(Key={"thread_key": key}).get("Item")
    except Exception:
        logger.exception("スレッド対応表の読み取りに失敗したため新規投稿する: %s", key)
        return None
    if not item:
        return None
    # TTL の削除は遅延するため、期限切れの項目はここで無視する
    if int(item.get("expires_at", 0)) <= int(time.time()):
        return None
    return item.get("thread_ts")


def save_thread_ts(key, thread_ts):
    """スレッド対応表に thread_ts を保存する。失敗しても通知は続ける。"""
    table = _threads_table()
    if table is None:
        return
    ttl_days = int(os.environ.get("THREAD_TTL_DAYS", DEFAULT_THREAD_TTL_DAYS))
    try:
        table.put_item(
            Item={
                "thread_key": key,
                "thread_ts": thread_ts,
                "expires_at": int(time.time()) + ttl_days * 86400,
            }
        )
    except Exception:
        logger.exception("スレッド対応表への保存に失敗した（解消報は新規投稿になる）: %s", key)


def _threads_table():
    """スレッド対応表の DynamoDB Table（未設定なら None。実行環境内でキャッシュ）。"""
    global _threads_table_cache
    table_name = os.environ.get("THREADS_TABLE")
    if not table_name:
        return None
    if _threads_table_cache is None:
        import boto3

        _threads_table_cache = boto3.resource("dynamodb").Table(table_name)
    return _threads_table_cache


def format_headline(event):
    """通知のプレビューや、ブロックを表示できない環境向けの1行の見出し（attachments の fallback）。"""
    detail = event.get("detail") or {}
    state = (detail.get("state") or {}).get("value", "-")
    return f"[{state}] {detail.get('alarmName', '-')}"


def build_attachments(event):
    """アラームイベントを Slack の attachments（色の帯 + Block Kit）に整形する。"""
    detail = event.get("detail") or {}
    state = detail.get("state") or {}
    previous = detail.get("previousState") or {}
    configuration = detail.get("configuration") or {}
    region = event.get("region", "-")
    account = event.get("account", "-")
    alarm_name = detail.get("alarmName", "-")
    metric = _alarm_metric(detail)
    queue_name = (metric.get("dimensions") or {}).get("QueueName")

    mark = STATE_MARKS.get(state.get("value"))
    title = f"*CloudWatch Alarm | {alarm_name}*"
    if mark:
        title = f"{mark} {title}"
    blocks = [_section(f"{title}\n{region} | Account: {account}")]
    # 説明文は「滞留しています」のような ALARM 時の文言なので、解消報では紛らわしいため出さない
    if configuration.get("description") and state.get("value") != "OK":
        blocks.append(_section(_truncate(configuration["description"])))

    fields = [
        ("アラーム名", alarm_name),
        ("状態", f"{previous.get('value', '-')} → {state.get('value', '-')}"),
    ]
    if metric.get("name"):
        fields.append(("メトリクス", f"{metric.get('namespace', '-')} / {metric['name']}"))
    if metric.get("dimensions"):
        dimensions = ", ".join(f"{k} = {v}" for k, v in metric["dimensions"].items())
        fields.append(("ディメンション", dimensions))
    if metric.get("stat"):
        fields.append(("統計", metric["stat"]))
    if metric.get("period"):
        fields.append(("期間", f"{metric['period']} 秒"))
    blocks.append(
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*{label}*\n{value}"}
                for label, value in fields
            ],
        }
    )

    if state.get("reason"):
        blocks.append(_section(f"*理由*\n{_truncate(state['reason'])}"))

    links = [f"<{_alarm_url(region, alarm_name)}|アラームを開く>"]
    if queue_name:
        links.append(f"<{_queue_url(region, account, queue_name)}|キューを開く>")
    blocks.append(
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": " | ".join(links)},
                {"type": "mrkdwn", "text": _format_time(event.get("time"))},
            ],
        }
    )

    color = STATE_COLORS.get(state.get("value"), DEFAULT_COLOR)
    # 帯の上に本文を出さないので、通知のプレビューの文言は fallback で渡す
    return [{"color": color, "fallback": format_headline(event), "blocks": blocks}]


def _section(text):
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _alarm_metric(detail):
    """アラームイベントの metrics 定義から、最初のメトリクスの情報を取り出す。"""
    metrics = (detail.get("configuration") or {}).get("metrics") or []
    for item in metrics:
        metric_stat = item.get("metricStat") or {}
        metric = metric_stat.get("metric") or {}
        if metric.get("name"):
            return {
                "namespace": metric.get("namespace"),
                "name": metric["name"],
                "dimensions": metric.get("dimensions") or {},
                "stat": metric_stat.get("stat"),
                "period": metric_stat.get("period"),
            }
    return {}


def _alarm_url(region, alarm_name):
    quoted = urllib.parse.quote(alarm_name, safe="")
    return (
        f"https://{region}.console.aws.amazon.com/cloudwatch/home"
        f"?region={region}#alarmsV2:alarm/{quoted}"
    )


def _queue_url(region, account, queue_name):
    queue = f"https://sqs.{region}.amazonaws.com/{account}/{queue_name}"
    quoted = urllib.parse.quote(queue, safe="")
    return (
        f"https://{region}.console.aws.amazon.com/sqs/v3/home"
        f"?region={region}#/queues/{quoted}"
    )


def _format_time(value):
    """イベントの時刻（ISO 8601、UTC）を JST の表記にする。解釈できなければそのまま返す。"""
    if not value:
        return "-"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return parsed.astimezone(JST).strftime("%Y-%m-%d %H:%M:%S JST")


def build_prompt(event):
    """alarm_prompt.md のプロンプトにイベント JSON を連結して Bedrock への入力を作る。"""
    with open(PROMPT_PATH, encoding="utf-8") as f:
        prompt = f.read().strip()
    event_json = json.dumps(event, ensure_ascii=False, indent=2)
    return f"{prompt}\n\n---\n\n{_truncate(event_json, MAX_EVENT_JSON_CHARS)}"


def summarize(event):
    """Bedrock の Claude（converse API）でイベントの日本語訳・要約を生成する。"""
    import boto3

    client = boto3.client(
        "bedrock-runtime",
        region_name=os.environ.get("BEDROCK_REGION", "ap-northeast-1"),
    )
    response = client.converse(
        modelId=os.environ.get("BEDROCK_MODEL_ID", "jp.anthropic.claude-sonnet-4-6"),
        messages=[{"role": "user", "content": [{"text": build_prompt(event)}]}],
        inferenceConfig={"maxTokens": 2048},
    )
    content = response["output"]["message"]["content"]
    return "".join(block["text"] for block in content if "text" in block)


def post_to_slack(text, thread_ts=None, attachments=None):
    """chat.postMessage でアラート用チャネルに投稿し、投稿したメッセージの ts を返す。"""
    payload = {
        "channel": os.environ["SLACK_CHANNEL_ID"],
        "unfurl_links": False,
    }
    # 帯だけを投稿するときは本文を付けない（空の本文を送ると帯の上に空行が出るため）
    if text:
        payload["text"] = text
    if thread_ts:
        payload["thread_ts"] = thread_ts
    if attachments:
        payload["attachments"] = attachments

    request = urllib.request.Request(
        SLACK_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {_slack_token()}",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        body = json.loads(response.read())
    if not body.get("ok"):
        raise RuntimeError(f"Slack API error: {body.get('error')}")
    return body["ts"]


def _slack_token():
    """SSM SecureString から Bot Token を取得する（実行環境内でキャッシュ）。"""
    global _slack_token_cache
    if _slack_token_cache is None:
        import boto3

        ssm = boto3.client("ssm")
        _slack_token_cache = ssm.get_parameter(
            Name=os.environ["SLACK_TOKEN_PARAMETER"], WithDecryption=True
        )["Parameter"]["Value"]
    return _slack_token_cache


def _truncate(text, limit=MAX_ORIGINAL_CHARS):
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...(以下省略)"
