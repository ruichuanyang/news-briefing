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

import asyncio
import edge_tts
import io
import requests
from openai import OpenAI

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "topics.json"

# 由 main() 在读取 topics.json 后写入，ask_model/synthesize 据此选择提供方。
LLM_PROVIDER = "zhipu"          # zhipu | qwen
LLM_MODEL = "glm-4-flash"       # 智谱 GLM-4-Flash 永久免费
TTS_CFG: dict = {"provider": "edge", "voice": "zh-CN-XiaoxiaoNeural"}


def require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"缺少环境变量 {name}")
    return value


def chat_client() -> OpenAI:
    if LLM_PROVIDER == "qwen":
        # 阿里百炼兼容接口（旧方案，保留作回退）。
        base_url = os.getenv("DASHSCOPE_BASE_URL") or "https://dashscope.aliyuncs.com/compatible-mode/v1"
        return OpenAI(api_key=require("DASHSCOPE_API_KEY"), base_url=base_url)
    # 默认智谱 GLM：OpenAI 兼容、永久免费、内置联网搜索、国内可达。
    base_url = os.getenv("ZHIPU_BASE_URL") or "https://open.bigmodel.cn/api/paas/v4"
    return OpenAI(api_key=require("ZHIPU_API_KEY"), base_url=base_url)


def ask_model(client: OpenAI, system: str, user: str, web: bool) -> str:
    kwargs = {}
    if web:
        if LLM_PROVIDER == "qwen":
            # 百炼要求思考模式下的联网搜索使用流式响应；逐块取回最终正文。
            kwargs["extra_body"] = {
                "enable_search": True,
                "search_options": {"forced_search": True, "search_strategy": "agent", "enable_source": True},
            }
        else:
            # 智谱内置联网搜索工具：自动检索并返回来源（标题/URL/摘要）。
            kwargs["tools"] = [{"type": "web_search", "web_search": {"enable": True, "search_result": True}}]
        kwargs["stream"] = True
    result = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0.25 if web else 0.65,
        timeout=180,  # 写稿/扩写单次调用最长 3 分钟，防止 GLM 挂起拖死整期（SDK 默认 600s）
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


def _parse_date(value: str):
    """把 '2026-08-24' / '2026/08/24' 解析为 datetime；失败返回 None。"""
    if not value:
        return None
    m = re.match(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", str(value))
    if not m:
        return None
    try:
        return datetime(int(m[1]), int(m[2]), int(m[3]))
    except ValueError:
        return None


def zhipu_search(query: str, count: int = 5, now: datetime | None = None) -> list[dict]:
    """智谱独立 Web Search API：直接返回结构化搜索结果，不经过 LLM 生成。

    走 /paas/v4/web_search（比 chat/completions+web_search 工具更快、更便宜，
    后者会让 GLM 额外生成一大段正文、容易超时，且结果在顶层 web_search 字段、
    流式模式丢失）。返回规范化后的 [{title, link, date, content}]。
    """
    key = require("ZHIPU_API_KEY")
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    payload = {
        "search_query": query[:70],
        "search_engine": "search_std",  # 改：基础版 ¥0.01/次，比 search_pro 便宜 3 倍
        "search_intent": False,
        "count": max(1, min(count, 10)),
        # 时效窗口收窄到近 7 天，避免把一个月前的旧文/赛前预告当新闻
        "search_recency_filter": "oneWeek",
        "content_size": "medium",
    }
    last_err = ""
    # 免费/赠金账户搜索 API 有较低 QPS，突发会 429；额度耗尽型 429 当月不恢复，只短退避重试 1 次。
    for attempt in range(5):
        try:
            response = requests.post(
                "https://open.bigmodel.cn/api/paas/v4/web_search",
                headers=headers, json=payload, timeout=20,
            )
            if response.status_code == 429:
                # 429 多为“月额度耗尽”（当月不会恢复）或瞬时 QPS 限流。
                # 只短退避重试 1 次防瞬时抖动；仍 429 立即放弃并切 Tavily 兜底，
                # 避免 9 个板块 × 15/30/45/60/75s 长退避把整期 45 分钟烧光。
                last_err = "HTTP 429 (rate limited)"
                if attempt >= 1:
                    break
                time.sleep(3)
                continue
            if response.status_code >= 500:
                last_err = f"HTTP {response.status_code}"
                time.sleep(2 * (attempt + 1))
                continue
            response.raise_for_status()
            data = response.json()
            results = []
            for item in data.get("search_result") or []:
                title = re.sub(r"\s+", " ", str(item.get("title", "") or "")).strip()
                content = re.sub(r"\s+", " ", str(item.get("content", "") or "")).strip()
                if not title and not content:
                    continue
                results.append({
                    "title": title,
                    "link": str(item.get("link", "") or "").strip(),
                    "date": str(item.get("publish_date", "") or "").strip(),
                    "content": content[:400],
                })
            if results:
                # ① 按发布日期倒序：搜索接口默认按“相关度”返回，未必是时间顺序，
                # 必须自己排，否则会选中陈旧的“赛前预告”（如赛事已结束却报“即将开打”）。
                results.sort(
                    key=lambda r: _parse_date(r["date"]) or datetime(2000, 1, 1),
                    reverse=True,
                )
                # ② 软时效：丢弃超过 14 天的明显旧闻（如去年的文章），但排序已保证优先用最新
                cutoff = (now.date() - timedelta(days=14)) if now else None
                if cutoff:
                    results = [
                        r for r in results
                        if (d := _parse_date(r["date"])) is None or d.date() >= cutoff
                    ]
                # 过滤后为空说明只有旧闻，直接视为无可靠更新（重试也不会变新）
                return results
            last_err = "empty result"
            time.sleep(3 * (attempt + 1))
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_err = str(exc)
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"智谱 web_search 失败（{last_err}）")


def tavily_search(query: str, count: int = 5, now: datetime | None = None) -> list[dict]:
    """Tavily 搜索 API 兜底：智谱搜索额度耗尽时自动启用。需 TAVILY_API_KEY 环境变量。
    返回与 zhipu_search 完全一致的规范化结构 [{title, link, date, content}]。"""
    key = os.getenv("TAVILY_API_KEY", "").strip()
    if not key:
        raise RuntimeError("Tavily 未配置（缺少 TAVILY_API_KEY 环境变量）")
    payload = {
        "api_key": key,
        "query": query[:400],
        "search_depth": "basic",
        "topic": "news",
        "days": 7,
        "max_results": max(1, min(count, 10)),
        "include_answer": False,
        "include_raw_content": False,
    }
    try:
        response = requests.post("https://api.tavily.com/search", json=payload, timeout=20)
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        raise RuntimeError(f"Tavily 请求失败：{exc}")
    results = []
    for item in data.get("results") or []:
        title = re.sub(r"\s+", " ", str(item.get("title", "") or "").strip())
        content = re.sub(r"\s+", " ", str(item.get("content", "") or "").strip())
        if not title and not content:
            continue
        results.append({
            "title": title,
            "link": str(item.get("url", "") or "").strip(),
            "date": str(item.get("published_date", "") or "").strip(),
            "content": content[:400],
        })
    if not results:
        raise RuntimeError("Tavily 返回空结果")
    # 与智谱一致：按发布日期倒序 + 14 天软时效过滤
    results.sort(key=lambda r: _parse_date(r["date"]) or datetime(2000, 1, 1), reverse=True)
    cutoff = (now.date() - timedelta(days=14)) if now else None
    if cutoff:
        results = [r for r in results if (d := _parse_date(r["date"])) is None or d.date() >= cutoff]
    return results


def web_search(query: str, count: int = 5, now: datetime | None = None) -> tuple[list[dict], str]:
    """多源搜索路由器：优先智谱 web_search，失败（429/网络错/空结果）自动切 Tavily。
    返回 (results, provider)，provider ∈ {'zhipu','tavily','none'}。"""
    try:
        res = zhipu_search(query, count=count, now=now)
        if res:
            print(f"[search] 命中智谱：{query[:32]}（{len(res)} 条）", file=sys.stderr)
            return res, "zhipu"
    except Exception as exc:
        print(f"[search] 智谱失败，尝试 Tavily 兜底：{exc}", file=sys.stderr)
    try:
        res = tavily_search(query, count=count, now=now)
        if res:
            print(f"[search] 命中 Tavily：{query[:32]}（{len(res)} 条）", file=sys.stderr)
            return res, "tavily"
    except Exception as exc:
        print(f"[search] Tavily 也失败：{exc}", file=sys.stderr)
    return [], "none"


def collect_research(client: OpenAI, topics: list[dict], now: datetime) -> tuple[str, dict]:
    provider_log: dict[str, str] = {}
    if LLM_PROVIDER == "qwen":
        # 旧路径：百炼 enable_search（保留回退）。
        topic_text = "\n".join(f"- {x['name']}：{x['instructions']}" for x in topics)
        research = ask_model(
            client,
            "你是一名严谨的中文新闻研究编辑。你必须主动联网搜索，不能以记忆或想象填补事实。"
            "先检索当日及过去36小时的信息，优先官方、原始论文、交易所、公司公告和主流媒体。"
            "交叉核查日期、数字、名称。无法证实、来源不明、八卦传言一律舍弃。",
            f"现在是 {now:%Y-%m-%d %H:%M}（{now.tzname()}）。为以下栏目做研究备忘：\n{topic_text}\n\n"
            "每个栏目最多保留2条最有价值的事实。每条写成：标题｜发生/发布的准确时间｜2-3句事实｜来源名称与URL。"
            "没有可靠新内容就明确写'今日无可靠更新'，绝不凑数。价格必须说明品种、币种、时间和数据源。",
            web=True,
        )
        for t in topics:
            provider_log[t["name"]] = "qwen-web"
        return research, provider_log
    # 智谱路径：逐栏目显式联网搜索，直接读取顶层 web_search 字段的真实结果，
    # 不再依赖模型自述（GLM-4-Flash 常忽略搜索结果、凭训练数据编造）。
    memo = []
    for topic in topics:
        name = topic["name"]
        query = (topic.get("search_query") or f"{name} 今日新闻").format(
            date=now.strftime("%Y年%m月%d日"), short=now.strftime("%m月%d日")
        )
        results, provider = web_search(query, count=int(topic.get("count", 5)), now=now)
        provider_log[name] = provider
        lines = [f"- {name}：（以下按发布时间从新到旧排列，优先采用最上方条目）"]
        if not results:
            lines.append("  - 今日无可靠更新｜--｜联网搜索未返回可用结果｜--")
        else:
            seen = set()
            kept = 0
            for r in results:
                key = (r["title"] or r["content"])[:40]
                if key in seen:
                    continue
                seen.add(key)
                lines.append(
                    f"  - {r['title'] or '（无标题）'}｜{r['date'] or '--'}｜{r['content'][:140]}｜{r['link'] or '--'}"
                )
                kept += 1
                if kept >= 2:
                    break
        memo.append("\n".join(lines))
    return "\n\n".join(memo)


def extract_sources(research: str) -> list[tuple[str, str]]:
    """从研究备忘中提取真实 URL，返回 [(域名, url)]，按出现顺序去重。"""
    urls: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in research.splitlines():
        if "｜" not in line or "http" not in line:
            continue
        link = line.split("｜")[-1].strip()
        if not link.startswith("http"):
            continue
        key = link.split("?")[0][:100]
        if key in seen:
            continue
        seen.add(key)
        domain = re.sub(r"^https?://(?:www\.)?", "", link).split("/")[0]
        domain = re.sub(r"\?.*$", "", domain)  # 去掉 ?tracking 参数，避免显示脏域名
        urls.append((domain or "来源", link))
    return urls


def postprocess_script(script: str, research: str) -> str:
    """兜底约束（GLM-4-Flash 指令遵循有限）：

    1. 剥离 ```markdown / ``` 代码围栏；
    2. 文末“资料来源”小节只保留研究备忘中真实出现的 URL，绝不保留模型编造的域名。
    """
    script = script.strip()
    # 兜底：扩写步骤的 user_prompt 偶被 GLM 回显为成稿首行“当前草稿：”，此处剥离，避免被 TTS 念出。
    script = re.sub(r"^\s*当前草稿[：:]\s*", "", script)
    script = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", script)
    script = re.sub(r"\s*```\s*$", "", script)
    # 截掉模型自带的资料来源部分（兼容 “# 资料来源” 与 “[资料来源]” 两种写法）
    head = re.split(r"(?:^|\n)[#\[]?\s*资料来源\s*\]?", script, maxsplit=1)[0].rstrip()
    # 移除正文里残留的来源标记（来源统一在文末列出）：
    #   [来源](url) / [来源](--) 空死链，以及 （来源：xxx）/ (来源：xxx) 中文括号标记
    head = re.sub(r"\[来源\]\([^)]*\)", "", head)
    head = re.sub(r"[（(]来源[：:][^）)]*[）)]", "", head)
    head = re.sub(r"[ \t]{2,}", " ", head)
    head = re.sub(r"\n{3,}", "\n\n", head).strip()
    sources = extract_sources(research)
    if not sources:
        return head
    lines = ["\n\n### 资料来源\n"]
    for domain, url in sources:
        lines.append(f"- {domain}: {url}")
    return head + "\n".join(lines)


# 未来时态哨兵：赛事“即将开打”与游戏“即将发售/上线”是同一类过期预告陷阱。
_BANNED_FUTURE = re.compile(
    r"即将开打|即将开赛|将开打|开赛在即|即将开赛|将开赛|即将举行|即将开"
    r"|即将发售|即将上线|即将公测|即将推出|即将开播|即将上映"
)
# 对应已发生结果的关键词（备忘里出现这些却用了未来时态 → 强制重生成）。
_CONCLUDED = re.compile(r"夺冠|冠军产生|决赛结束|最终夺冠|捧起冠军|问鼎|正式发售|已正式上线|正式上线|大结局|完结|已发售|已经上线")


def write_script(client: OpenAI, research: str, target_chars: int, now: datetime, topics: list[dict]) -> str:
    section_list = "、".join(t.get("name", "") for t in topics if t.get("name"))
    sys_prompt = (
        "你是“哥哥”的专属晨报主编。这是给“哥哥”一个人的每日播报，没有别的听众，"
        "语气要像亲密的人在清晨跟他说话——自然、有温度、信息充实，但不过分腻。\n"
        "开场直接叫“哥哥”，绝不要写“各位听众”“听众朋友们”“大家好”这类面向大众的称呼。\n"
        "硬性要求：\n"
        f"1. 覆盖全部栏目：{section_list}。每个栏目都必须报道；只有备忘中标明'今日无可靠更新'的栏目才可一句话带过。\n"
        "每个栏目展开 2-3 句：先说事实，再点出背景、影响或值得关注的原因；不要一句话带过。\n"
        "2. 事实只可来自备忘；时间、价格、数字、名称必须与备忘逐字一致，禁止补数、改数；宁可写'暂无数据'。\n"
        "3. 严禁罗列备忘原文：禁止出现'｜'竖线分隔、禁止'标题｜日期｜内容'式清单，全部改写成连贯的广播语言。\n"
        "4. 文末'资料来源'小节：只逐字复制备忘中真实出现的链接（URL）并附来源名；备忘里没有链接的栏目不列入，"
        "绝不编造域名或猜测网址。\n"
        "5. 优先采用日期最新的条目；备忘条目已按发布时间从新到旧排列，最上方即最新。\n"
        "6. 凡同一事件若同时出现'预热/前瞻/即将'与'已发生结果'，必须采用已发生结果；若最新结果已表明事件结束"
        "（如'夺冠''决赛结束''冠军产生''正式发售''已上线''大结局'），严禁使用'即将开打/即将开赛/即将发售/即将上线/即将公测'等未来时态表述。\n"
        "7. 正文中不要写'[来源]'、'（来源）'之类的来源标记，所有来源由系统在文末统一列出；正文只叙述事实。"
    )
    user_prompt = (
        f"播出日期：{now:%Y年%m月%d日}。目标长度 {target_chars} 个汉字上下（上限 {target_chars + 120}），"
        "正常语速约5分钟，绝不超过7分钟。\n\n"
        "写作要求：\n1. 用一个简洁开场直接叫“哥哥”串起全篇，各栏目之间自然过渡，不要逐栏报菜单。整体约5分钟（约{target_chars}字），内容要充实，每栏2-3句。"
        "其中黄金白银必须单独成段，且必须先用'元/克'报出深圳水贝或上海金交所的黄金/白银批发行情价（如'足金约X元/克'），国际金价（美元/盎司）只能作为补充；严禁只报国际美元价而漏掉元/克行情价；"
        "美股必须报出主要指数点位或涨跌幅数字。\n2. 新闻联播播报感：短句、具体、平稳；解释为什么值得关注，"
        "但不要夸张、营销或机械罗列。\n3. 保留必要的时间、价格、单位；英文名首次出现可括注。\n"
        "4. 不要写'资料来源'小节，来源链接会由系统自动附加到文末。\n"
        "5. 只输出可直接发送的 Markdown 稿件，不要写创作说明。\n\n研究备忘：\n" + research
    )
    script = ask_model(client, sys_prompt, user_prompt, web=False)
    # 兜底①：备忘已有“已发生结果”，成稿却写出未来时态——说明用了陈旧预告/预热，强制重生成一次
    if _BANNED_FUTURE.search(script) and _CONCLUDED.search(research):
        script = ask_model(
            client,
            sys_prompt + "\n⚠️ 上稿错误：你使用了'即将开打/即将发售/即将上线'等未来时态，但备忘中该事件已有明确结果。"
            "必须改用语已发生的事实（如'X队于X月X日夺冠''游戏已于X月X日发售'），严禁未来时态。",
            user_prompt,
            web=False,
        )
    # 兜底②：GLM 长度控制不可信，常写太短。以“正文净字数”（去掉 Markdown/来源段）判断是否达标，
    # 不足就循环补写（每栏补一句背景/影响），不加裁切，最多 3 轮。
    attempts = 0
    while _body_len(script) < target_chars and attempts < 3:
        script = _expand_script(client, script, target_chars, now)
        attempts += 1
    return script


def _body_len(script: str) -> int:
    """正文净字数：去掉资料来源段与 Markdown 符号后的可读字数（用于长度判定）。"""
    text = re.split(r"(?:^|\n)#+\s*资料来源", script, maxsplit=1)[0]
    text = re.sub(r"!?\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"[`*_#>]", "", text)
    return len(text.strip())


def _expand_script(client: OpenAI, script: str, target_chars: int, now: datetime) -> str:
    """把过短的草稿适度扩写：每个栏目只补一句背景/影响，整体仅增加约 200 字。"""
    sys_prompt = (
        "你是“哥哥”的专属晨报主编，口吻不变：开场叫“哥哥”，自然、有温度。\n"
        "下面是一版草稿。请在不改动任何已有事实的前提下，为【每个栏目】补充一句背景或影响分析"
        "（说明为什么值得关注），使播报更充实；总体只在当前基础上增加 150-250 字，不要大段重写，"
        f"不要超过 {target_chars} 字。保持 Markdown 格式，不写资料来源小节，不改变已有数据与结论。"
    )
    # 注意：user_prompt 不要带“当前草稿：”之类字面标签，否则 GLM 会把标签回显进成稿、
    # 被 TTS 原样念出。上下文已由 sys_prompt 说明“这是一版草稿”。
    return ask_model(client, sys_prompt, "现有草稿如下，请按系统要求扩写：\n" + script, web=False)


def personalize_script(script: str) -> str:
    """把面向大众的称呼统一替换为对“哥哥”的专属口吻（GLM 指令遵循有限，代码兜底）。"""
    s = script
    # 听众类称呼 → 哥哥
    s = re.sub(r"各位听众(?:朋友们?|朋友)?", "哥哥", s)
    s = re.sub(r"听众朋友们?", "哥哥", s)
    # 其他面向大众的开场 → 哥哥，
    s = re.sub(r"^(?:大家好|观众朋友们?|亲爱的听众|听众朋友)[，！!。.\s]*", "哥哥，", s.strip())
    return s.strip()


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


def synthesize(text: str) -> bytes:
    """合成语音，返回 MP3 字节（供后续托管到播放页）。

    提供方由 TTS_CFG['provider'] 决定：
      - 'edge'（默认）：微软 Edge TTS，完全免费、无需密钥；
      - 'cosyvoice'：阿里百炼 CosyVoice v3（旧方案，保留作回退）。
    """
    provider = (TTS_CFG or {}).get("provider", "edge")
    if provider == "cosyvoice":
        return _synthesize_cosyvoice(text)
    return _synthesize_edge(text, (TTS_CFG or {}).get("voice") or "zh-CN-XiaoxiaoNeural")


def _synthesize_cosyvoice(text: str) -> bytes:
    """阿里百炼 CosyVoice v3（北京地域业务空间专属 Endpoint），返回音频字节。"""
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
        return download_audio(audio)
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
                    return download_audio(audio)
                raise RuntimeError(f"语音任务成功但未找到音频URL：{tp}")
            if status in {"FAILED", "CANCELED"}:
                raise RuntimeError(f"语音合成任务失败：{tp}")
        raise RuntimeError("语音合成轮询超时")
    raise RuntimeError(f"语音合成响应中未找到音频URL：{data}")


def _synthesize_edge(text: str, voice: str) -> bytes:
    """微软 Edge TTS：免费、无需密钥。流式累积音频字节后返回。

    edge-tts >=7 的 stream() 产出字典（如 {"type":"audio","data":bytes}），
    旧版才是 (type, data) 元组；这里按字典解析，兼容 7.x。
    """
    async def _run() -> bytes:
        buf = bytearray()
        communicate = edge_tts.Communicate(text, voice)
        async for chunk in communicate.stream():
            if isinstance(chunk, dict) and chunk.get("type") == "audio":
                buf.extend(chunk.get("data") or b"")
        return bytes(buf)
    try:
        return asyncio.run(asyncio.wait_for(_run(), timeout=180))
    except asyncio.TimeoutError:
        raise RuntimeError("Edge TTS 合成超时（180s）")


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
    """把 MP3 与播放页发布到 gh-pages 分支，返回 GitHub Pages 播放页 URL（https，微信内可正常打开）。"""
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
    owner, _, name = REPO.partition("/")
    return f"https://{owner}.github.io/{name}/audio/{date}.html"


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
    # 读取提供方配置，写入模块级全局，供 chat_client/ask_model/synthesize 使用。
    llm_cfg = config.get("llm") or {}
    tts_cfg = config.get("tts") or {}
    globals()["LLM_PROVIDER"] = llm_cfg.get("provider", "zhipu")
    globals()["LLM_MODEL"] = llm_cfg.get("model") or ("glm-4-flash" if LLM_PROVIDER == "zhipu" else "qwen3.6-flash")
    globals()["TTS_CFG"] = tts_cfg or {"provider": "edge", "voice": "zh-CN-XiaoxiaoNeural"}
    now = datetime.now(ZoneInfo(config.get("timezone", "Asia/Shanghai")))
    client = chat_client()
    research, search_log = collect_research(client, config["topics"], now)
    script = personalize_script(
        postprocess_script(
            write_script(client, research, int(config.get("target_chars", 1500)), now, config["topics"]),
            research,
        )
    )
    title = f"{config.get('edition_name', '每日早报')}｜{now:%m月%d日}"
    result = {"generated_at": now.isoformat(), "research": research, "script": script, "llm": LLM_PROVIDER, "tts": TTS_CFG.get("provider"), "search_providers": search_log}
    if not args.dry_run:
        audio_bytes = synthesize(spoken_version(script))
        date = now.strftime("%Y%m%d")
        mp3_url = f"https://cdn.jsdelivr.net/gh/{REPO}@gh-pages/audio/{date}.mp3"
        player_url = None
        try:
            html = build_player_html(date, title, mp3_url, script)
            player_url = publish_to_ghpages(date, audio_bytes, html)
            result["player_url"] = player_url
            result["mp3_url"] = mp3_url
            prune_old_audio(date)
            print(f"播放页已发布：{player_url}")
        except Exception as exc:
            print(f"发布播放页失败，回退到音频直链：{exc}", file=sys.stderr)
        result["audio_url"] = mp3_url
        push(title, script, mp3_url, player_url)
    (ROOT / "run-output.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print("完成" if not args.dry_run else "文字稿生成完成（dry-run）")


if __name__ == "__main__":
    import traceback
    try:
        main()
    except Exception as exc:
        print(f"失败：{exc}", file=sys.stderr)
        traceback.print_exc()
        sys.exit(1)
