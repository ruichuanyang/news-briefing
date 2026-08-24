#!/usr/bin/env python3
"""隔离诊断 CosyVoice v3 非流式接口：分别用短/中文本测试，打印状态码与耗时。"""
import os
import time

import requests

key = os.environ["DASHSCOPE_API_KEY"]
ws = os.environ["DASHSCOPE_WORKSPACE_ID"].strip()
voice = os.environ.get("DASHSCOPE_TTS_VOICE") or "longanyang"
api_root = f"https://{ws}.cn-beijing.maas.aliyuncs.com/api/v1"
endpoint = f"{api_root}/services/audio/tts/SpeechSynthesizer"
headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def try_tts(label: str, text: str, timeout: int) -> None:
    payload = {
        "model": "cosyvoice-v3-flash",
        "input": {"text": text, "voice": voice, "format": "mp3", "sample_rate": 24000},
    }
    t0 = time.time()
    try:
        r = requests.post(endpoint, headers=headers, json=payload, timeout=timeout)
        dt = time.time() - t0
        snippet = r.text[:500].replace("\n", " ")
        print(f"[{label}] HTTP {r.status_code} in {dt:.1f}s | body={snippet!r}")
    except Exception as exc:
        dt = time.time() - t0
        print(f"[{label}] EXCEPTION after {dt:.1f}s: {exc!r}")


if __name__ == "__main__":
    print(f"voice={voice} endpoint={endpoint}")
    print("=== 短文本（约 12 字）===")
    try_tts("short", "这是一条诊断测试。", 60)
    print("=== 中文本（约 600 字）===")
    try_tts("medium", ("今天我们来关注几条重要新闻。" * 40), 180)
