#!/usr/bin/env python3
"""一次性诊断：验证智谱 GLM-4-Flash web_search 工具的真实行为（原始 HTTP 响应结构）。"""
import json
import os
import requests

KEY = os.environ["ZHIPU_API_KEY"]
URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
H = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}


def show(label, obj, limit=4000):
    print(f"\n===== {label} =====")
    print(json.dumps(obj, ensure_ascii=False, indent=2)[:limit])


TOOLS_FULL = [{
    "type": "web_search",
    "web_search": {
        "enable": "True",
        "search_engine": "search_pro",
        "search_result": "True",
        "search_query": "深圳水贝黄金 足金 批发价 元/克 白银 今日价格",
        "count": 5,
        "search_recency_filter": "noLimit",
        "content_size": "high",
    },
}]

# TEST 1: 非流式 + 完整 web_search 参数
body = {
    "model": "glm-4-flash",
    "messages": [{
        "role": "user",
        "content": "今天是2026年8月24日。请联网搜索：深圳水贝市场今日黄金足金批发价、白银银料价各是多少元/克？给出具体数字、日期与来源。",
    }],
    "tools": TOOLS_FULL,
    "tool_choice": "auto",
}
try:
    r = requests.post(URL, headers=H, json=body, timeout=120)
    print("TEST1 HTTP", r.status_code)
    show("TEST1 response", r.json())
except Exception as e:
    print("TEST1 error:", repr(e))

# TEST 2: 流式 + 相同参数，观察 chunk 结构
body2 = dict(body, stream=True)
try:
    r = requests.post(URL, headers=H, json=body2, timeout=120, stream=True)
    print("\nTEST2 HTTP", r.status_code)
    cnt = 0
    for line in r.iter_lines(decode_unicode=True):
        if not line:
            continue
        cnt += 1
        if cnt <= 10:
            print("SSE:", line[:700])
    print("TEST2 total SSE lines:", cnt)
except Exception as e:
    print("TEST2 error:", repr(e))
