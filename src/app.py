import json
import os

from linebot.v3 import (
    WebhookHandler
)
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage
)
from linebot.v3.webhooks import (
    MessageEvent,
    TextMessageContent
)

from openai import OpenAI


CHANNEL_SECRET = os.environ.get("CHANNEL_SECRET", "")
CHANNEL_ACCESS_TOKEN = os.environ.get("CHANNEL_ACCESS_TOKEN", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

CONFIGURATION = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
WEBHOOK_HANDLER = WebhookHandler(CHANNEL_SECRET)

OPENAI_CLIENT = OpenAI(
    api_key=OPENAI_API_KEY,
    timeout=8,
    max_retries=0
)


INSTRUCTIONS = """
你是部署在 LINE Bot 背後的助理。請遵守以下規則作答：
- 一律使用繁體中文，不使用 Markdown 語法。
- 不要捏造權限或背景作業能力；不知道就明確說不知道並給下一步建議。
- 答案盡量在 50 字內完成。

安全與合規：
- 若請求涉及非法、危險或個資，禮貌拒絕並提供合規替代方案。
- 若請求需要權限或外部存取（例如：讀取使用者位置），請明確說明能力限制與正規取得方式。
"""

@WEBHOOK_HANDLER.add(MessageEvent, message=TextMessageContent)
def handle_message(event: MessageEvent):
    user_text = event.message.text
    try:
        resp = OPENAI_CLIENT.responses.create(
            model="gpt-4o-mini",
            instructions=INSTRUCTIONS,
            input=user_text,
            max_output_tokens=512,
            temperature=0.5,
        )
        reply_text = resp.output_text or "…"
    except Exception as e:
        print(f"[OpenAI ERROR] {type(e).__name__}: {e}", flush=True)
        reply_text = "目前服務有點忙，我稍後再回覆你一次。"

    print(f"Prompt: {user_text}")
    print(f"AI Response: {reply_text}")

    with ApiClient(CONFIGURATION) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=reply_text)]
            )
        )


def lambda_handler(event: dict, context):
    """
    Parameters
    ----------
    event: dict, required
        API Gateway Lambda Proxy Input Format

        Event doc: https://docs.aws.amazon.com/apigateway/latest/developerguide/set-up-lambda-proxy-integrations.html#api-gateway-simple-proxy-for-lambda-input-format

    context: object, required
        Lambda Context runtime methods and attributes

        Context doc: https://docs.aws.amazon.com/lambda/latest/dg/python-context-object.html

    Returns
    ------
    API Gateway Lambda Proxy Output Format: dict

        Return doc: https://docs.aws.amazon.com/apigateway/latest/developerguide/set-up-lambda-proxy-integrations.html
    """

    headers: dict = event.get("headers", {})
    signature: str = headers.get("X-Line-Signature") or headers.get("x-line-signature")

    body: str = event.get("body", "")

    WEBHOOK_HANDLER.handle(body, signature)

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({
            "message": "Line Bot",
        }),
    }
