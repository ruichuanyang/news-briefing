#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日新闻简报机器人 —— 免费、无需服务器、推送到微信(Server酱) + AI朗读(Edge TTS)
抓取源全部无需 API Key：
  - 新闻：Google News RSS 关键词搜索（免费）
  - 美股：Yahoo Finance 公开行情接口（免费）
  - 比特币：CoinGecko 公开接口（免费）
  - 黄金/白银：水贝价页面抓取 + 新浪基础金价兜底（免费）
朗读：Edge TTS 微软免费在线语音
推送：Server酱（免费）-> 个人微信；音频托管在 GitHub Pages（免费）
"""

import os
import re
import asyncio
import datetime
import email.utils
import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
VOICE_MAP = {
    "yunxi": "zh-CN-YunxiNeural",      # 云希 男声（温和）
    "xiaoxiao": "zh-CN-XiaoxiaoNeural" # 晓晓 女声（甜美）
}


def load_config(path="config.txt"):
    topics, voice = [], "yunxi"
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.lower().startswith("voice="):
                voice = line.split("=", 1)[1].strip() or "yunxi"
            else:
                topics.append(line)
    return topics, voice


def fetch_news(query, max_items=4):
    """Google News RSS 关键词搜索，返回 [(title, source, link)]（近两天）"""
    url = ("https://news.google.com/rss/search?q="
           + requests.utils.quote(query)
           + "&hl=zh-CN&gl=CN&ceid=CN:zh-Hans")
    try:
        r = requests.get(url, headers=UA, timeout=20)
        items = re.findall(r"<item>(.*?)</item>", r.text, re.S)
        out, now = [], datetime.datetime.now(datetime.timezone.utc)
        for it in items:
            t = re.search(r"<title>(.*?)</title>", it, re.S)
            l = re.search(r"<link>(.*?)</link>", it, re.S)
            d = re.search(r"<pubDate>(.*?)</pubDate>", it, re.S)
            s = re.search(r"<source[^>]*>(.*?)</source>", it, re.S)
            if not (t and l):
                continue
            title = re.sub(r"\s*-\s*[^_-]+$", "", t.group(1).strip())
            link = l.group(1).strip()
            src = s.group(1).strip() if s else ""
            if d:
                try:
                    pdt = email.utils.parsedate_to_datetime(d.group(1).strip())
                    if pdt.tzinfo is None:
                        pdt = pdt.replace(tzinfo=datetime.timezone.utc)
                    if (now - pdt).days > 2:
                        continue
                except Exception:
                    pass
            out.append((title, src, link))
            if len(out) >= max_items:
                break
        return out
    except Exception as e:
        print(f"[news] {query} 获取失败: {e}")
        return []


def fetch_stocks():
    """Yahoo Finance 三大指数，返回 [(name, price, chg%)]"""
    syms = {"标普500": "^GSPC", "纳斯达克": "^IXIC", "道琼斯": "^DJI"}
    res = []
    for name, sym in syms.items():
        try:
            u = (f"https://query1.finance.yahoo.com/v8/finance/chart/"
                 f"{requests.utils.quote(sym)}?range=1d&interval=1d")
            j = requests.get(u, headers=UA, timeout=20).json()
            m = j["chart"]["result"][0]["meta"]
            price = m.get("regularMarketPrice")
            prev = m.get("chartPreviousClose") or m.get("previousClose")
            chg = (price / prev - 1) * 100 if prev else 0
            res.append((name, price, chg))
        except Exception as e:
            print(f"[stock] {name} 失败: {e}")
    return res


def fetch_btc():
    """CoinGecko，返回 (usd, cny, chg%) 或 None"""
    try:
        u = ("https://api.coingecko.com/api/v3/simple/price"
             "?ids=bitcoin&vs_currencies=usd,cny&include_24hr_change=true")
        j = requests.get(u, headers=UA, timeout=20).json()["bitcoin"]
        return j["usd"], j.get("cny", 0), j.get("usd_24h_change", 0)
    except Exception as e:
        print(f"[btc] 失败: {e}")
        return None


def fetch_metals():
    """水贝金价/银价抓取 + 新浪基础金价兜底"""
    gold_sb = silver_sb = base_gold = ""
    try:
        html = requests.get("http://www.huangjinjiage.cn/golden/85544.html",
                            headers=UA, timeout=20).text
        m = re.search(r"水贝黄金\s*黄金价格\s*([\d.]+)元/克", html)
        if m:
            gold_sb = m.group(1)
        m = re.search(r"水贝白银价格[：: ]*([\d.]+)元/克", html)
        if not m:
            m = re.search(r"水贝\s*([\d.]+)\s*元/克", html)
        if m:
            silver_sb = m.group(1)
    except Exception as e:
        print(f"[metal-scrape] 失败: {e}")
    try:
        s = requests.get("https://hq.sinajs.cn/list=hf_AUTD",
                         headers={**UA, "Referer": "https://finance.sina.com.cn"},
                         timeout=20).text
        mm = re.search(r'"(.*?)"', s)
        if mm:
            parts = mm.group(1).split(",")
            if len(parts) > 1 and parts[1]:
                base_gold = parts[1]
    except Exception as e:
        print(f"[metal-sina] 失败: {e}")
    return gold_sb, silver_sb, base_gold


def build_briefing(topics):
    bj = datetime.timezone(datetime.timedelta(hours=8))
    today = datetime.datetime.now(bj).strftime("%Y年%m月%d日")
    md = [f"# 📰 每日新闻简报 · {today}\n"]
    speech = [f"今天是{today}的每日新闻简报。"]
    news_topics = [t for t in topics if t not in ("美股", "比特币", "黄金白银")]

    for t in news_topics:
        items = fetch_news(t)
        md.append(f"## 📌 {t}")
        if not items:
            md.append("_今日暂无相关新闻，或抓取受限。_\n")
            continue
        for title, src, link in items:
            md.append(f"- [{title}]({link})" + (f" — {src}" if src else ""))
            speech.append(f"{t}方面：{title}。")
        md.append("")

    if "美股" in topics:
        md.append("## 💰 美股")
        speech.append("美股方面。")
        for name, price, chg in fetch_stocks():
            md.append(f"- {name} 收报 {price:,.2f}，{chg:+.2f}%")
            speech.append(f"{name}收报约{price:.0f}点，"
                          f"{'上涨' if chg >= 0 else '下跌'}百分之{abs(chg):.2f}。")
        md.append("")

    if "比特币" in topics:
        b = fetch_btc()
        if b:
            usd, cny, chg = b
            md.append("## ₿ 比特币")
            md.append(f"- 比特币 ≈ ${usd:,.0f}（≈¥{cny:,.0f}），24h {chg:+.2f}%")
            speech.append(f"比特币约{usd:.0f}美元，约等于{cny:.0f}人民币，"
                          f"二十四小时{'上涨' if chg >= 0 else '下跌'}百分之{abs(chg):.2f}。")
            md.append("")

    if "黄金白银" in topics:
        g, s, base = fetch_metals()
        md.append("## 🪙 黄金 / 白银（水贝价）")
        if g:
            md.append(f"- 水贝足金（基础价，不含工费）：约 {g} 元/克")
        if s:
            md.append(f"- 水贝足银（≥99.99%）：约 {s} 元/克")
        if base:
            md.append(f"- 国内基础金价（新浪）：约 {base} 元/克（水贝首饰价≈基础价+工费）")
        if not (g or s or base):
            md.append("_今日金价获取失败，请稍后手动查看。_")
        parts = []
        if g:
            parts.append(f"水贝足金约{g}元每克")
        if s:
            parts.append(f"水贝足银约{s}元每克")
        if parts:
            speech.append("黄金白银方面。" + "，".join(parts) + "。")
        md.append("")

    md.append("> 本简报由免费自动化生成，数据来自公开源，仅供参考，不构成投资或消费建议。")
    return "\n".join(md), "\n".join(speech)


async def tts(text, out_path, voice_key):
    import edge_tts
    voice = VOICE_MAP.get(voice_key, VOICE_MAP["yunxi"])
    await edge_tts.Communicate(text, voice).save(out_path)


def push_serverchan(title, desp, key):
    if not key:
        print("[push] 未配置 SERVERCHAN_KEY，跳过推送")
        return
    try:
        r = requests.post(f"https://sctapi.ftqq.com/{key}.send",
                          data={"title": title, "desp": desp}, timeout=20)
        print("[push] Server酱:", r.json().get("message", r.status_code))
    except Exception as e:
        print(f"[push] 失败: {e}")


def main():
    os.makedirs("public", exist_ok=True)
    topics, voice = load_config()
    md, speech = build_briefing(topics)
    with open("public/briefing.md", "w", encoding="utf-8") as f:
        f.write(md)

    mp3 = "public/briefing.mp3"
    try:
        asyncio.run(tts(speech, mp3, voice))
        print("[tts] 生成成功:", mp3)
    except Exception as e:
        print(f"[tts] 失败: {e}")

    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if repo and "/" in repo:
        owner, name = repo.split("/", 1)
        audio_url = f"https://{owner}.github.io/{name}/briefing.mp3"
    else:
        audio_url = "(见仓库 public/briefing.mp3)"
    key = os.environ.get("SERVERCHAN_KEY", "")
    desp = md + f"\n\n🎧 **点击收听朗读版**：[{audio_url}]({audio_url})"
    push_serverchan("📰 每日新闻简报", desp, key)
    print("DONE")


if __name__ == "__main__":
    main()
