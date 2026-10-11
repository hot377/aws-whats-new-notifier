"""DynamoDB Streams の INSERT イベントを受けて記事を要約し、Slack に投稿する。

処理の流れ:
    1. Streams イベントから INSERT レコードだけを取り出す（MODIFY / REMOVE はスキップ。
       既存記事の再 put は MODIFY になるため、これだけで重複通知が防がれる）
    2. 記事ページの本文を取得する（<main> タグを抽出。失敗時は RSS の description に
       フォールバック）
    3. prompt.md のプロンプト + 記事本文を Bedrock の Claude（converse API）に渡して
       日本語要約を生成する（失敗時は要約なしのタイトル + リンクにフォールバック）
    4. chat.postMessage で 1 記事 1 メッセージを Slack に投稿する

Slack 投稿の失敗などで例外が送出されると、イベントソースマッピングが最大2回
リトライし、それでも失敗したレコードは DLQ へ退避される。
Slack 投稿成功後に失敗した場合はリトライで重複投稿の可能性があるが、
取りこぼし防止を優先する。
boto3 / bs4 の import は関数内で行い、SDK なしでもユニットテストできるようにしている。
"""

import json
import os
import urllib.request

PROMPT_PATH = os.path.join(os.path.dirname(__file__), "prompt.md")
SLACK_API_URL = "https://slack.com/api/chat.postMessage"

# Bedrock に渡す記事本文の最大文字数（超過分は切り詰める）
MAX_BODY_CHARS = 20000

_slack_token_cache = None


def handler(event, context):
    articles = extract_articles(event)
    for article in articles:
        notify(article)
    print(f"records: {len(event.get('Records', []))}, notified: {len(articles)}")
    return {"notified": len(articles)}


def extract_articles(event):
    """Streams イベントから INSERT レコードの記事情報だけを取り出す。"""
    articles = []
    for record in event.get("Records", []):
        if record.get("eventName") != "INSERT":
            continue
        image = record.get("dynamodb", {}).get("NewImage", {})
        article = {
            "url": _attr(image, "url"),
            "title": _attr(image, "title"),
            "description": _attr(image, "description"),
            "published_at": _attr(image, "published_at"),
        }
        if article["url"]:
            articles.append(article)
    return articles


def notify(article):
    """記事1件を要約して Slack に投稿する。"""
    body = fetch_article_body(article["url"]) or article["description"]
    try:
        summary = summarize(article, body)
    except Exception as error:  # noqa: BLE001
        # 要約は付加価値。新着があった事実の通知を優先し、要約なしで投稿する
        print(f"summarize failed for {article['url']}: {error}")
        summary = None
    post_to_slack(format_message(article, summary))


def fetch_article_body(url):
    """記事ページの <main> 部分のテキストを返す。取得・抽出に失敗したら None。"""
    from bs4 import BeautifulSoup

    try:
        request = urllib.request.Request(
            url, headers={"User-Agent": "whatsnew-notifier"}
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            html = response.read()
        main = BeautifulSoup(html, "html.parser").find("main")
        if main is None:
            return None
        text = main.get_text(separator="\n", strip=True)
        return text or None
    except Exception as error:  # noqa: BLE001
        print(f"fetch article body failed for {url}: {error}")
        return None


def build_prompt(article, body):
    """prompt.md のプロンプトに記事情報を連結して Bedrock への入力を作る。"""
    with open(PROMPT_PATH, encoding="utf-8") as f:
        prompt = f.read().strip()
    return (
        f"{prompt}\n\n---\n\n"
        f"タイトル: {article['title']}\n"
        f"URL: {article['url']}\n"
        f"発行日時: {article['published_at']}\n\n"
        f"本文:\n{_truncate(body)}"
    )


def summarize(article, body):
    """Bedrock の Claude（converse API）で記事の日本語要約を生成する。"""
    import boto3

    client = boto3.client(
        "bedrock-runtime",
        region_name=os.environ.get("BEDROCK_REGION", "ap-northeast-1"),
    )
    response = client.converse(
        modelId=os.environ.get("BEDROCK_MODEL_ID", "jp.anthropic.claude-sonnet-4-6"),
        messages=[{"role": "user", "content": [{"text": build_prompt(article, body)}]}],
        inferenceConfig={"maxTokens": 2048},
    )
    content = response["output"]["message"]["content"]
    return "".join(block["text"] for block in content if "text" in block)


def format_message(article, summary):
    """Slack へ投稿する 1 記事分のメッセージ（mrkdwn）を組み立てる。"""
    lines = [f"*<{article['url']}|{article['title']}>*"]
    if summary:
        lines.append(summary)
    else:
        lines.append("(要約の生成に失敗したため原文リンクのみ)")
    lines.append(f"発行: {article['published_at']} | <{article['url']}|原文>")
    return "\n\n".join(lines)


def post_to_slack(text):
    """chat.postMessage で投稿する。"""
    payload = {
        "channel": os.environ["SLACK_CHANNEL_ID"],
        "text": text,
        "unfurl_links": False,
    }
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


def _attr(image, name):
    """Streams の NewImage（型付き属性）から文字列値を取り出す。"""
    return image.get(name, {}).get("S", "")


def _truncate(text, limit=MAX_BODY_CHARS):
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...(以下省略)"
