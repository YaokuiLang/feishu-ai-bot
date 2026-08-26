import os
import json
import requests
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import uvicorn

app = FastAPI()

# 从环境变量读取配置
APP_ID = os.getenv("FEISHU_APP_ID")
APP_SECRET = os.getenv("FEISHU_APP_SECRET")
VERIFICATION_TOKEN = os.getenv("FEISHU_VERIFICATION_TOKEN")
MODEL_API_URL = os.getenv("MODEL_API_URL")
MODEL_API_KEY = os.getenv("MODEL_API_KEY", "")
MODEL_NAME = os.getenv("MODEL_NAME", "deepseek-chat")

FEISHU_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"

_token_cache = {"token": None, "expire": 0}


def get_tenant_access_token():
    """获取飞书 tenant_access_token(带缓存)"""
    import time
    if _token_cache["token"] and time.time() < _token_cache["expire"]:
        return _token_cache["token"]
    resp = requests.post(FEISHU_TOKEN_URL, json={
        "app_id": APP_ID,
        "app_secret": APP_SECRET
    }, timeout=10)
    if resp.status_code != 200:
        raise Exception("获取 tenant_access_token 失败")
    data = resp.json()
    if data.get("code") != 0:
        raise Exception(data.get("msg", "未知错误"))
    _token_cache["token"] = data["tenant_access_token"]
    _token_cache["expire"] = time.time() + data.get("expire", 7200) - 300
    return _token_cache["token"]


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/feishu/events")
async def feishu_events(request: Request):
    """接收飞书事件回调(兼容 v1 与 v2 schema)"""
    body = await request.json()
    token = body.get("token") or (body.get("header") or {}).get("token")

    # URL 验证(v1 与 v2 两种格式)
    if body.get("type") == "url_verification":
        if token != VERIFICATION_TOKEN:
            raise HTTPException(status_code=403, detail="Invalid token")
        return JSONResponse({"challenge": body.get("challenge")})

    header = body.get("header") or {}
    if header.get("event_type") == "url_verification":
        if header.get("token") != VERIFICATION_TOKEN:
            raise HTTPException(status_code=403, detail="Invalid token")
        challenge = body.get("challenge") or (body.get("event") or {}).get("challenge")
        return JSONResponse({"challenge": challenge})

    # 事件回调校验 token
    if token and token != VERIFICATION_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid verification token")

    # 解析事件(v2 用 header.event_type + event;v1 用 event.type)
    event_type = header.get("event_type") or (body.get("event") or {}).get("type")
    if event_type not in ("im.message.receive_v1", "message"):
        return {"code": 0}

    event = body.get("event") or {}
    message = event.get("message") or {}
    chat_id = message.get("chat_id")
    chat_type = message.get("chat_type", "p2p")
    msg_type = message.get("message_type") or message.get("msg_type")
    content = message.get("content")  # JSON 字符串

    if msg_type == "text" and chat_id:
        try:
            text = json.loads(content).get("text", "")
        except (json.JSONDecodeError, AttributeError):
            text = content or ""
        # 群聊需 @ 机器人才回复;私聊直接回复
        if chat_type == "group":
            if "@" not in text and "机器人" not in text:
                return {"code": 0}
        if text.strip():
            reply = call_model(text)
            send_message(chat_id, reply)
    return {"code": 0}


def call_model(prompt):
    """调用 OpenAI 兼容模型接口"""
    if not MODEL_API_URL:
        return "模型接口未配置,请设置 MODEL_API_URL"
    headers = {"Content-Type": "application/json"}
    if MODEL_API_KEY:
        headers["Authorization"] = f"Bearer {MODEL_API_KEY}"
    payload = {
        "model": MODEL_NAME,
        "messages": [{"role": "user", "content": prompt}]
    }
    try:
        resp = requests.post(MODEL_API_URL, json=payload, headers=headers, timeout=60)
        if resp.status_code == 200:
            data = resp.json()
            return data.get("choices", [{}])[0].get("message", {}).get("content", "无回复内容")
        return f"模型接口异常,状态码 {resp.status_code}"
    except Exception as e:
        return f"调用模型失败: {str(e)}"


def send_message(chat_id, text):
    """通过飞书消息 API 发送文本消息"""
    access_token = get_tenant_access_token()
    url = "https://open.feishu.cn/open-apis/im/v1/messages"
    params = {"receive_id_type": "chat_id"}
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }
    payload = {
        "receive_id": chat_id,
        "msg_type": "text",
        "content": json.dumps({"text": text}, ensure_ascii=False)
    }
    resp = requests.post(url, json=payload, headers=headers, params=params, timeout=15)
    if resp.status_code != 200 or resp.json().get("code") != 0:
        print("发送消息失败:", resp.text)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 10000)))


@app.get("/")
async def root():
    return {"status": "ok", "service": "feishu-ai-bot"}
