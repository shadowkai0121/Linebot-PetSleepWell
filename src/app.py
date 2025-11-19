from datetime import datetime, timezone
import json
import os
import time
import uuid
import boto3
import csv
import math
from decimal import Decimal

from linebot.v3 import (
    WebhookHandler
)
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    PushMessageRequest,
    TextMessage,
    QuickReply,
    QuickReplyItem,
    LocationAction
)
from linebot.v3.webhooks import (
    FollowEvent,
    MessageEvent,
    LocationMessageContent,
    TextMessageContent
)

from openai import OpenAI
from boto3.dynamodb.conditions import Key
import requests

TABLE_NAME = os.environ["TABLE_NAME"]
DYNAMO_DB = boto3.resource("dynamodb")
TABLE = DYNAMO_DB.Table(TABLE_NAME)

CHANNEL_SECRET = os.environ.get("CHANNEL_SECRET", "")
CHANNEL_ACCESS_TOKEN = os.environ.get("CHANNEL_ACCESS_TOKEN", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")

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

PROVIDERS = []
with open("providers.csv", newline="", encoding="utf-8") as f:
    reader = csv.DictReader(f)
    for row in reader:
        try:
            lat = Decimal(row.get("latitude") or row.get("lat") or 0)
            lng = Decimal(row.get("longitude") or row.get("lng") or 0)
            PROVIDERS.append({
                "name": row.get("名稱") or "",
                "tel": row.get("電話") or "",
                "addr": row.get("地址") or "",
                "site":  row.get("網址") or "",
                "line": row.get("LINE") or "",
                "lat": lat, "lng": lng
            })
        except Exception:
            pass


def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0088
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * \
        math.cos(math.radians(lat2))*math.sin(dlon/2)**2
    return 2 * R * math.asin(math.sqrt(a))


def find_nearest(lat, lng):
    best = None
    bestd = 1e18
    for p in PROVIDERS:
        d = haversine_km(lat, lng, p["lat"], p["lng"])
        if d < bestd:
            bestd = d
            best = (p, d)
    return best


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def day_str(iso_ts: str) -> str:
    return iso_ts[:10]  # 'YYYY-MM-DD'


def put_message(
    user_id: str,
    message_type: str,
    text: str | None = None,
    lat: Decimal | None = None,
    lng: Decimal | None = None,
    role: str = "user",
    origin: str = "line-user",
    reply_to: str | None = None,
    trace: dict | None = None,
    indexable: bool | None = None,
    ttl_days: int = 10
):
    ts = now_iso()
    ulid = uuid.uuid4().hex
    item = {
        "pk": f"USER#{user_id}",
        "sk": f"MSG#{ts}#{ulid}",
        "type": "message",
        "role": role,
        "origin": origin,
        "messageType": message_type,
        "ts": ts,
        "d": day_str(ts),
        "ttl": int(time.time()) + ttl_days * 24 * 3600
    }
    if text is not None:
        item["text"] = text
    if lat is not None:
        item["lat"] = lat
    if lng is not None:
        item["lng"] = lng
    if reply_to:
        item["replyTo"] = reply_to
    if trace:
        item["trace"] = trace
    if indexable is not None:
        item["indexable"] = indexable
    TABLE.put_item(Item=item)
    return item


def put_location(
    user_id: str,
    lat: Decimal,
    lng: Decimal,
    source: str = "quick-reply",
    ttl_days: int = 10
):
    ts = now_iso()
    # 歷史一筆
    TABLE.put_item(Item={
        "pk": f"USER#{user_id}",
        "sk": f"LOC#{ts}",
        "type": "location",
        "lat": lat, "lng": lng,
        "source": source,
        "ts": ts,
        "d": day_str(ts),
        "ttl": int(time.time()) + ttl_days * 24 * 3600
    })
    # 當前位置
    TABLE.put_item(Item={
        "pk": f"USER#{user_id}",
        "sk": "LOCATION_LATEST",
        "type": "location_current",
        "lat": lat, "lng": lng,
        "updatedAt": ts
    })


def get_recent_messages(user_id: str, limit: int = 20):
    # sk 以 MSG# 起頭的倒序
    resp = TABLE.query(
        KeyConditionExpression=Key("pk").eq(
            f"USER#{user_id}") & Key("sk").begins_with("MSG#"),
        ScanIndexForward=False,
        Limit=limit
    )
    return resp.get("Items", [])


def get_latest_location(user_id: str):
    # 先嘗試讀 LOCATION_LATEST
    res = TABLE.get_item(
        Key={"pk": f"USER#{user_id}", "sk": "LOCATION_LATEST"})
    if "Item" in res:
        return res["Item"]
    # 不存在就去歷史裡抓最新一筆
    resp = TABLE.query(
        KeyConditionExpression=Key("pk").eq(
            f"USER#{user_id}") & Key("sk").begins_with("LOC#"),
        ScanIndexForward=False, Limit=1
    )
    items = resp.get("Items", [])
    return items[0] if items else None


def get_places_comment(name: str, lat: Decimal, lng: Decimal) -> list:
    url = "https://places.googleapis.com/v1/places:searchText"

    payload = {
        "textQuery": name,
        "locationBias": {
            "circle": {
                "center": {
                    "latitude": lat,
                    "longitude": lng,
                },
                "radius": 1000.0
            }
        }
    }

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": GOOGLE_API_KEY,
        "X-Goog-FieldMask": "places.id,places.displayName"
    }

    resp = requests.post(url, json=payload, headers=headers)

    resp = resp.json()["places"][0]
    id = resp.get("id")

    url = f"https://places.googleapis.com/v1/places/{id}"
    headers["X-Goog-FieldMask"] = "id,rating,googleMapsUri,regularOpeningHours.weekdayDescriptions,reviews.rating,reviews.relativePublishTimeDescription,reviews.text.text"
    # headers["X-Goog-FieldMask"] = "*"
    params = {
        "languageCode": "zh-TW",
    }

    resp = requests.get(url, params=params, headers=headers)
    resp = resp.json()

    data = {
        "name": name,
        "rating": resp["rating"],
        "googleMapsUri": resp["googleMapsUri"],
        "regularOpeningHours": "\n".join(resp["regularOpeningHours"]["weekdayDescriptions"]),
        "reviews": [],
    }
    for review in resp["reviews"]:
        data["reviews"].append({
            "published": review["relativePublishTimeDescription"],
            "rating": review["rating"],
            "text": review["text"]["text"]
        })

    return data


COMMENT_INSTRUCTION = """
You are a professional recommendation-summary generator for Google Maps places. 
Your role is to read the structured JSON place data sent by the user and produce a 
concise, neutral, and informative summary in Traditional Chinese, approximately 
150 characters. Your summary is intended for consumers evaluating a service provider.

=== Core Responsibilities ===
1. Analyze and summarize the place using:
   - Name, rating, and opening hours
   - Review content (sentiment, themes, frequency)
   - Any additional information retrieved by Web Search

2. When the provided JSON does not contain enough information to form a reliable 
   150-character recommendation summary, or if certain aspects require confirmation 
   (e.g., service風評、價格透明度、企業背景、分店資訊、是否有爭議報導), 
   you MUST automatically call the `web.run` tool to search for additional information.
   Your search queries should generally be based on:
   - The place name (e.g., "<店名> 評價", "<店名> 喪禮", "<店名> 服務")
   - Related keywords inferred from the reviews if necessary.

3. After Web Search results return, integrate:
   - Key facts
   - Repeated patterns
   - High-signal information
   - Cross-source sentiment trends
   into a single objective, consumer-friendly summary.

4. Maintain neutrality:
   - If reviews include both praise and complaints, reflect this contrast.
   - Avoid exaggeration, speculation, or invented details.
   - Do not quote reviews directly or list bullet points.

5. Writing rules:
   - Traditional Chinese only
   - Single paragraph (no bullet points)
   - Around 150 characters (±10%)
   - Focus on: 服務態度、流程專業、價格透明度、環境品質、可信度
   - Do not include URLs, citations, or tool call details.

=== Output Format ===
Your final answer must follow this code block:

```
約200字的推薦摘要"
```
Only output valid Plain Text.
"""


@WEBHOOK_HANDLER.add(MessageEvent, message=LocationMessageContent)
def handle_location(event: MessageEvent):
    user_id = event.source.user_id
    loc = event.message
    lat = Decimal(str(loc.latitude))
    lng = Decimal(str(loc.longitude))
    addr = getattr(loc, "address", None)

    quick = f"已收到你的定位，我來幫你找附近的服務…"
    with ApiClient(CONFIGURATION) as api_client:
        api = MessagingApi(api_client)
        api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=quick)]
            )
        )

    try:
        put_location(user_id=user_id, lat=Decimal(str(lat)),
                     lng=Decimal(str(lng)), source="quick-reply")

        best = find_nearest(lat, lng)
        if best and best[0]:
            p, dkm = best
            info = get_places_comment(p['name'], p["lat"], p["lng"])

            msg = []
            msg.append("最近的業者")
            msg.append(f"名稱：{p['name']}")
            msg.append(f"電話：{p['tel']}")
            msg.append(f"Line：{p['line']}")
            msg.append(f"地址：{p['addr']}")
            msg.append(f"網站：{p['site']}")
            msg.append(f"服務時間：\n{info['regularOpeningHours']}")
            quick = "\n".join(msg)
        else:
            quick = "附近暫無資料。"

        with ApiClient(CONFIGURATION) as api_client:
            api = MessagingApi(api_client)
            api.push_message(PushMessageRequest(
                to=user_id,
                messages=[TextMessage(text=quick)]
            ))

        try:
            resp = OPENAI_CLIENT.responses.create(
                model="gpt-4o",
                instructions=COMMENT_INSTRUCTION,
                input=json.dumps(info, ensure_ascii=False),
                max_output_tokens=2048,
                temperature=0.8,
            )
            reply_text = resp.output_text.replace("```", "").strip() or "…"
        except Exception as e:
            print(f"[OpenAI ERROR] {type(e).__name__}: {e}", flush=True)
            reply_text = "目前有點忙，我稍後再回覆您一次。"

        with ApiClient(CONFIGURATION) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.push_message(
                PushMessageRequest(
                    to=user_id,
                    messages=[TextMessage(text=reply_text)]
                )
            )
    except Exception as e:
        print(f"[Location ERROR] {type(e).__name__}: {e}", flush=True)


@WEBHOOK_HANDLER.add(FollowEvent)
def handle_follow(event):
    with ApiClient(CONFIGURATION) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[
                    TextMessage(
                        text=(
                            "歡迎加入！\n"
                            "為了提供更準確的附近服務，請傳送你的目前位置"
                        ),
                        quick_reply=QuickReply(items=[
                            QuickReplyItem(
                                action=LocationAction(label="傳送目前位置")
                            )
                        ])
                    )
                ]
            )
        )


@WEBHOOK_HANDLER.add(MessageEvent, message=TextMessageContent)
def handle_message(event: MessageEvent):
    user_text = event.message.text
    if user_text[0] == '/':
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({
                "message": "Line Bot",
            }),
        }
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
        reply_text = "目前有點忙，我稍後再回覆您一次。"

    #
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
    signature: str = headers.get(
        "X-Line-Signature") or headers.get("x-line-signature")

    body: str = event.get("body", "")

    WEBHOOK_HANDLER.handle(body, signature)

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({
            "message": "Line Bot",
        }),
    }
