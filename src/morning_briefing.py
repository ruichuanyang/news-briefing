#!/usr/bin/env python3
"""AI-first daily news briefing: search -> verify/select -> write -> TTS -> ServerChan."""
import argparse
import base64
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from openai import OpenAI

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "topics.json"


def require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"缺少环境变量 {name}")
    return value


def chat_client() -> OpenAI:
    # 北京地域的兼容接口。工作空间专属地址可通过 secret 覆盖，须以 /v1 结尾。
    # GitHub Actions exposes an unset optional Secret as an empty string.
    # Treat that exactly like an absent variable so the documented default works.
    base_url = os.getenv("DASHSCOPE_BASE_URL") or "https://dashscope.aliyuncs.com/compatible-mode/v1"
    return OpenAI(api_key=require("DASHSCOPE_API_KEY"), base_url=base_url)


def ask_model(client: OpenAI, system: str, user: str, web: bool) -> str:
    kwargs = {}
    if web:
        # 百炼要求思考模式下的联网搜索使用流式响应；逐块取回最终正文。
        kwargs["extra_body"] = {
            "enable_search": True,
            "search_options": {"forced_search": True, "search_strategy": "agent", "enable_source": True},
        }
        kwargs["stream"] = True
    result = client.chat.completions.create(
        model=os.getenv("DASHSCOPE_MODEL", "qwen3.6-flash"),
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0.25 if web else 0.65,
        **kwargs,
    )
    if web:
        content = "".join(
            chunk.choices[0].delta.content or ""
            for chunk in result
            if chunk.choices and chunk.choices[0].delta.content
        )
    else:
        content = result.choices[0].message.content
    if not content:
        raise RuntimeError("模型没有返回内容")
    return content.strip()


def collect_research(client: OpenAI, topics: list[dict], now: datetime) -> str:
    topic_text = "\n".join(f"- {x['name']}：{x['instructions']}" for x in topics)
    return ask_model(
        client,
        "你是一名严谨的中文新闻研究编辑。你必须主动联网搜索，不能以记忆或想象填补事实。"
        "先检索当日及过去36小时的信息，优先官方、原始论文、交易所、公司公告和主流媒体。"
        "交叉核查日期、数字、名称。无法证实、来源不明、八卦传言一律舍弃。",
        f"现在是 {now:%Y-%m-%d %H:%M}（{now.tzname()}）。为以下栏目做研究备忘：\n{topic_text}\n\n"
        "每个栏目最多保留2条最有价值的事实。每条写成：标题｜发生/发布的准确时间｜2-3句事实｜来源名称与URL。"
        "没有可靠新内容就明确写‘今日无可靠更新’，绝不凑数。价格必须说明品种、币种、时间和数据源。",
        web=True,
    )


def write_script(client: OpenAI, research: str, target_chars: int, now: datetime) -> str:
    return ask_model(
        client,
        "你是中文广播新闻节目的资深主编。把研究备忘写成一篇自然、克制、适合早晨收听的完整口播稿。"
        "事实只可来自备忘；不确定就不说。绝不编造标题、来源、数字或因果。",
        f"播出日期：{now:%Y年%m月%d日}。目标长度 {target_chars} 个汉字上下（上限 {target_chars + 120}），"
        "正常语速约6分钟，绝不超过10分钟。\n\n"
        "写作要求：\n1. 用一个简洁开场串起全篇，再按‘市场与科技—Dota2—文娱—深圳与科学’自然过渡，"
        "信息少的栏目可合并，不要逐栏报菜单。\n2. 新闻联播播报感：短句、具体、平稳；解释为什么值得关注，"
        "但不要夸张、营销或机械罗列。\n3. 保留必要的时间、价格、单位；英文名首次出现可括注。\n"
        "4. 文末附‘资料来源’小节，每条只列来源名和URL，供读者核验。\n"
        "5. 只输出可直接发送的 Markdown 稿件，不要写创作说明。\n\n研究备忘：\n" + research,
        web=False,
    )


def find_audio_url(value, key_hint=""):
    if isinstance(value, dict):
        for key, item in value.items():
            hit = find_audio_url(item, key)
            if hit:
                return hit
    elif isinstance(value, list):
        for item in value:
            hit = find_audio_url(item, key_hint)
            if hit:
                return hit
    elif isinstance(value, str) and value.startswith("http") and ("audio" in key_hint.lower() or key_hint in {"url", "file_url"}):
        return value
    return None


def synthesize(text: str) -> str:
    """用百炼北京地域 CosyVoice v3 合成语音，返回可播放的临时音频 URL（24h 有效）。

    官方说明：CosyVoice 非实时语音合成仅在北京地域可用，且必须使用业务空间专属
    Endpoint；voice/format/sample_rate 放在 input 中；非流式响应直接包含音频 URL，
    无需 X-DashScope-Async 轮询。
    """
    key = require("DASHSCOPE_API_KEY")
    workspace_id = os.getenv("DASHSCOPE_WORKSPACE_ID", "").strip()
    if not workspace_id:
        raise RuntimeError("缺少 DASHSCOPE_WORKSPACE_ID：CosyVoice 语音合成需要百炼北京地域业务空间 ID")
    voice = os.getenv("DASHSCOPE_TTS_VOICE") or "longanyang"
    api_root = f"https://{workspace_id}.cn-beijing.maas.aliyuncs.com/api/v1"
    endpoint = f"{api_root}/services/audio/tts/SpeechSynthesizer"
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    payload = {
        "model": "cosyvoice-v3-flash",
        "input": {"text": text, "voice": voice, "format": "mp3", "sample_rate": 24000},
    }
    try:
        # CosyVoice 非流式合成耗时随字数近似线性增长：600 字约 72 秒，
        # 1300 字晨报约需 2~2.5 分钟，故读取超时放宽到 420 秒。
        response = requests.post(endpoint, headers=headers, json=payload, timeout=420)
        response.raise_for_status()
    except requests.HTTPError as exc:
        detail = ""
        try:
            detail = response.text
        except Exception:
            pass
        raise RuntimeError(f"语音合成请求失败（HTTP {response.status_code}）：{detail[:1500]}") from exc
    data = response.json()
    audio = (data.get("output") or {}).get("audio", {}).get("url") or find_audio_url(data)
    if audio:
        return audio
    # 兼容个别返回 task_id 的异步形态：轮询任务状态取 URL。
    task_id = (data.get("output") or {}).get("task_id") or data.get("task_id")
    if task_id:
        for _ in range(60):
            time.sleep(2)
            task = requests.get(f"{api_root}/tasks/{task_id}", headers=headers, timeout=30)
            task.raise_for_status()
            tp = task.json()
            status = (tp.get("output") or {}).get("task_status")
            if status == "SUCCEEDED":
                audio = find_audio_url(tp)
                if audio:
                    return audio
                raise RuntimeError(f"语音任务成功但未找到音频URL：{tp}")
            if status in {"FAILED", "CANCELED"}:
                raise RuntimeError(f"语音合成任务失败：{tp}")
        raise RuntimeError("语音合成轮询超时")
    raise RuntimeError(f"语音合成响应中未找到音频URL：{data}")


def spoken_version(script: str) -> str:
    """来源链接留给微信阅读，不让播音员逐字念 Markdown 和 URL。"""
    text = re.split(r"(?:^|\n)#+\s*资料来源", script, maxsplit=1)[0]
    text = re.sub(r"!?(?:\[([^\]]+)\]\([^)]*\))", r"\1", text)
    text = re.sub(r"[`*_#>]", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def push(title: str, script: str, audio_url: str, player_url: str | None = None) -> None:
    sendkey = require("SERVERCHAN_SENDKEY")
    listen = player_url or audio_url
    extra = ""
    if player_url and player_url != audio_url:
        extra = f"\n\n[备用下载链接（播放页打不开时用）]({audio_url})"
    desp = f"[▶ 点击打开播放页收听本期晨报]({listen}){extra}\n\n---\n\n{script}"
    response = requests.post(f"https://sctapi.ftqq.com/{sendkey}.send", data={"title": title, "desp": desp}, timeout=45)
    response.raise_for_status()
    body = response.json()
    if body.get("code") != 0:
        raise RuntimeError(f"方糖推送失败：{body}")


def download_audio(url: str) -> bytes:
    """下载百炼返回的临时音频字节，用于二次托管到可播放页面（同时规避 24h 过期）。"""
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    return r.content


def build_player_html(date: str, title: str, mp3_url: str, script: str) -> str:
    """生成一个手机端友好的播报页：内嵌 <audio> 播放器，点开即播，不再强制下载。"""
    safe = html.escape(script)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{html.escape(title)}</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
         background: #f5f6f8; color: #1f2329; }}
  .wrap {{ max-width: 680px; margin: 0 auto; padding: 24px 18px 48px; }}
  .card {{ background: #fff; border-radius: 18px; padding: 22px; box-shadow: 0 6px 24px rgba(0,0,0,.06); }}
  h1 {{ font-size: 20px; margin: 0 0 4px; }}
  .date {{ color: #8a9099; font-size: 13px; margin-bottom: 14px; }}
  audio {{ width: 100%; margin-top: 6px; }}
  .tip {{ color: #8a9099; font-size: 12.5px; margin-top: 10px; line-height: 1.6; }}
  .script {{ white-space: pre-wrap; line-height: 1.85; font-size: 15.5px; margin-top: 22px;
            padding-top: 18px; border-top: 1px solid #eef0f3; }}
</style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1>📻 {html.escape(title)}</h1>
      <div class="date">每日 AI 晨报 · {date}</div>
      <audio controls preload="auto" src="{mp3_url}"></audio>
      <p class="tip">点击播放即可收听。需要离线收藏可长按音频选择“下载”。</p>
    </div>
    <div class="script">{safe}</div>
  </div>
</body>
</html>
"""


REPO = os.getenv("GITHUB_REPOSITORY") or "ruichuanyang/news-briefing"


def _gh_api(method: str, path: str, body: dict | None = None) -> dict:
    """用 gh CLI 调用 GitHub API（ Actions 中由 GITHUB_TOKEN 自动鉴权）。"""
    cmd = ["gh", "api", f"--method", method, f"repos/{REPO}/{path}"]
    tmp_name = None
    if body is not None:
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(body, tmp, ensure_ascii=False)
        tmp.close()
        tmp_name = tmp.name
        cmd += ["--input", tmp_name]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True)
    finally:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
    if out.returncode != 0:
        raise RuntimeError(f"gh {method} {path} 失败: {out.stderr.strip()[:400]}")
    return json.loads(out.stdout) if out.stdout.strip() else {}


def publish_to_ghpages(date: str, mp3_bytes: bytes, html: str) -> str:
    """把 MP3 与播放页发布到 gh-pages 分支，返回 jsDelivr 播放页 URL（https，国内可达）。"""
    token = os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")
    if not token:
        raise RuntimeError("缺少 GH_TOKEN，无法发布播放页")
    os.environ["GH_TOKEN"] = token
    # 确保 gh-pages 分支存在
    try:
        exists = _gh_api("GET", "branches/gh-pages").get("name") == "gh-pages"
    except Exception:
        exists = False
    if not exists:
        base = _gh_api("GET", "git/refs/heads/main")["object"]["sha"]
        _gh_api("POST", "git/refs", {"ref": "refs/heads/gh-pages", "sha": base})
    mp3_b64 = base64.b64encode(mp3_bytes).decode()
    html_b64 = base64.b64encode(html.encode("utf-8")).decode()
    for path, content in [(f"audio/{date}.mp3", mp3_b64), (f"audio/{date}.html", html_b64)]:
        sha = None
        try:
            sha = _gh_api("GET", f"contents/{path}?ref=gh-pages").get("sha")
        except Exception:
            pass
        body = {"message": f"audio {date}", "content": content, "branch": "gh-pages"}
        if sha:
            body["sha"] = sha
        _gh_api("PUT", f"contents/{path}", body)
    return f"https://cdn.jsdelivr.net/gh/{REPO}@gh-pages/audio/{date}.html"


def prune_old_audio(date: str, keep_days: int = 14) -> None:
    """清理 14 天前的音频，避免 gh-pages 无限膨胀。"""
    try:
        listing = _gh_api("GET", "contents/audio?ref=gh-pages")
        cutoff = (datetime.now(ZoneInfo("Asia/Shanghai")).date() - timedelta(days=keep_days)).strftime("%Y%m%d")
        for item in listing:
            name = item.get("name", "")
            if name.endswith(".mp3") and name[:8] < cutoff:
                sha = item.get("sha")
                if sha:
                    _gh_api("DELETE", f"contents/audio/{name}",
                            {"message": f"prune {name}", "sha": sha, "branch": "gh-pages"})
    except Exception as exc:
        print(f"清理旧音频跳过：{exc}", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只生成文字，不合成或推送")
    args = parser.parse_args()
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    now = datetime.now(ZoneInfo(config.get("timezone", "Asia/Shanghai")))
    client = chat_client()
    research = collect_research(client, config["topics"], now)
    script = write_script(client, research, int(config.get("target_chars", 1300)), now)
    result = {"generated_at": now.isoformat(), "research": research, "script": script}
    if not args.dry_run:
        audio_url = synthesize(spoken_version(script))
        result["audio_url"] = audio_url
        player_url = None
        date = now.strftime("%Y%m%d")
        try:
            mp3_bytes = download_audio(audio_url)
            mp3_url = f"https://cdn.jsdelivr.net/gh/{REPO}@gh-pages/audio/{date}.mp3"
            html = build_player_html(date, f"{config.get('edition_name', '每日早报')}｜{now:%m月%d日}", mp3_url, script)
            player_url = publish_to_ghpages(date, mp3_bytes, html)
            result["player_url"] = player_url
            result["mp3_url"] = mp3_url
            prune_old_audio(date)
            print(f"播放页已发布：{player_url}")
        except Exception as exc:
            print(f"发布播放页失败，回退到原始音频链接：{exc}", file=sys.stderr)
        push(f"{config.get('edition_name', '每日早报')}｜{now:%m月%d日}", script, audio_url, player_url)
    (ROOT / "run-output.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print("完成" if not args.dry_run else "文字稿生成完成（dry-run）")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"失败：{exc}", file=sys.stderr)
        sys.exit(1)
