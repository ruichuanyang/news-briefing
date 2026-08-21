#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日新闻播报 bot —— 口播模式（重制版 v6）

要点：
1. 所有新闻用 Google News 中文 RSS（hl=zh-CN），天然无 HTML、无英文标题。
2. clean_text 多道清洗：去 HTML 标签 + 去残留标签碎片(a href= /a font color= #6f6f6f)
   + 去网址 + 去广告词，保证「朗读绝不念出代码/符号/网址」。
3. 数据只抓一次，文字版(md)与音频版(speech)用同一份数据渲染，逐字一致。
4. 免费：Edge TTS 朗读 + Server酱推微信 + GitHub Actions 定时。
"""

import os
import re
import html
import asyncio
import datetime
import requests
import xml.etree.ElementTree as ET
from urllib.parse import quote

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

VOICE_MAP = {
    "yunxi": "zh-CN-YunxiNeural",        # 云希 男声（温和，默认）
    "yunyang": "zh-CN-YunyangNeural",    # 云扬 男声（新闻播报腔）
    "yunjian": "zh-CN-YunjianNeural",    # 云健 男声（体育解说感）
    "yunhao": "zh-CN-YunhaoNeural",      # 云浩 男声（轻松）
    "xiaoxiao": "zh-CN-XiaoxiaoNeural",  # 晓晓 女声（甜美）
    "xiaoyi": "zh-CN-XiaoyiNeural",      # 晓伊 女声
    "yunxia": "zh-CN-YunxiaNeural",      # 云夏 女声（轻快）
    "xiaochen": "zh-CN-XiaochenNeural",  # 晓辰 女声（沉稳）
    "xiaohan": "zh-CN-XiaohanNeural",    # 晓涵 女声（小说感）
    "xiaomeng": "zh-CN-XiaomengNeural",  # 晓梦 女声（可爱）
    "xiaorui": "zh-CN-XiaoruiNeural",    # 晓睿 女声（知性严谨）
    "xiaomo": "zh-CN-XiaomoNeural",      # 晓墨 女声
}

# 广告/营销词，命中则整段剔除或替换
CLICKBAIT = re.compile(r"(震惊|突发|速看|重磅|全网|疯传|不敢相信|万万没想到|"
                       r"点击(此处|这里|查看)|阅读原文|关注我们|扫码|福利|限时|"
                       r"夺宝|娱乐网址|博彩|彩票|澳门|现金)", re.I)

TAG_RE = re.compile(r"<\s*/?[a-zA-Z][^>]*>")                       # <a ...> </a> 等完整标签
ATTR_RE = re.compile(r"(?i)\b(?:href|target|rel|src|alt|style|color|"
                     r"width|height|class|id|title|align)\s*=\s*[^ \n<>,]*")  # href= target= color=
HEX_RE = re.compile(r"#\s*[0-9a-fA-F]{3,8}")                       # #6f6f6f
URL_RE = re.compile(r"https?://\S+|www\.\S+")
STRAY_RE = re.compile(r"(?i)\b(?:/?(?:a|font|span|div|br|img|p|b|i|"
                      r"u|strong|em|table|tr|td|li|ul|ol|h[1-6]))\b")


def clean_text(s):
    """彻底清洗：去 HTML、去标签碎片、去网址、去广告、压缩空白。"""
    if not s:
        return ""
    s = html.unescape(s or "")
    s = TAG_RE.sub(" ", s)          # 1) 完整 HTML 标签（带尖括号）
    s = ATTR_RE.sub(" ", s)         # 2) 残留属性碎片 href= target= color=
    s = HEX_RE.sub(" ", s)          # 3) 十六进制颜色
    s = URL_RE.sub(" ", s)          # 4) 网址
    s = CLICKBAIT.sub(" ", s)       # 5) 广告词
    s = STRAY_RE.sub(" ", s)        # 6) 孤立标签名 /a /font a font ...
    s = s.replace(" /a", "").replace("/a", "")
    s = s.replace(" /font", "").replace("/font", "")
    s = re.sub(r"[<>]", " ", s)     # 7) 任何剩漏的尖括号
    s = re.sub(r"(?i)\b_?blank\b|\b_self\b", " ", s)        # 8) _blank / _self
    s = re.sub(r"\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}\b", " ", s, flags=re.I)  # 9) 域名
    s = re.sub(r"\s*/\s*", " ", s)  # 10) 孤立斜杠
    s = re.sub(r"\s+", " ", s).strip()
    return s


# ---------- 数字中文播报 ----------
DIGITS = "零一二三四五六七八九"
UNITS = ["", "十", "百", "千"]


def _group4(n):
    s, pos = "", 0
    while n > 0:
        d = n % 10
        if d != 0:
            s = DIGITS[d] + UNITS[pos] + s
        elif s and s[0] != "零":
            s = "零" + s
        n //= 10
        pos += 1
    return s.lstrip("零")


def _int2cn(num):
    if num == 0:
        return "零"
    s = ""
    if num >= 100000000:
        s += _int2cn(num // 100000000) + "亿"
        num %= 100000000
        if num == 0:
            return s
        if num < 10000000:
            s += "零"
    if num >= 10000:
        s += _group4(num // 10000) + "万"
        num %= 10000
        if num == 0:
            return s
        if num < 1000:
            s += "零"
    s += _group4(num)
    return s


def num2cn(x):
    try:
        x = float(x)
    except Exception:
        return str(x)
    neg = x < 0
    x = abs(x)
    intpart = int(x)
    frac = x - intpart
    s = _int2cn(intpart)
    if frac and frac > 0:
        dec = f"{frac:.2f}".split(".")[1].rstrip("0")
        if dec:
            s += "点" + "".join(DIGITS[int(c)] for c in dec)
    return ("负" if neg else "") + s


def count2cn(n):
    return {1: "一", 2: "两", 3: "三", 4: "四", 5: "五"}.get(n, num2cn(n))


# ---------- 数据源（全部免费、无需 key）----------

def fetch_stocks():
    syms = [("道琼斯", "^DJI"), ("标普五百", "^GSPC"), ("纳斯达克", "^IXIC")]
    out = []
    for name, sym in syms:
        try:
            r = requests.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
                f"?range=1d&interval=1d", timeout=15)
            j = r.json()["chart"]["result"][0]
            price = j["meta"]["regularMarketPrice"]
            prev = j["meta"].get("chartPreviousClose") or j["meta"].get("previousClose")
            chg = (price - prev) / prev * 100 if prev else 0
            out.append((name, price, chg))
        except Exception as e:
            print(f"[stock] {sym} 失败: {e}")
    return out


def fetch_btc():
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price"
            "?ids=bitcoin&vs_currencies=usd,cny&include_24hr_change=true",
            timeout=15)
        d = r.json()["bitcoin"]
        return (d["usd"], d["cny"], d["usd_24h_change"])
    except Exception as e:
        print(f"[btc] 失败: {e}")
        return None


def fetch_metals():
    """水贝金价/银价：用新浪基础金价(AU9999,元/克) + 伦敦银换算，估算水贝批发价。"""
    try:
        hd = {"Referer": "https://finance.sina.com.cn/"}
        gold = float(requests.get("https://hq.sinajs.cn/list=AU9999",
                                  headers=hd, timeout=15).text.split(",")[1])
        ag = float(requests.get("https://hq.sinajs.cn/list=hf_AG",
                                headers=hd, timeout=15).text.split(",")[1])
        usdcny = float(requests.get("https://hq.sinajs.cn/list=USDCNY",
                                    headers=hd, timeout=15).text.split(",")[1])
        ag_cny = ag / 31.1035 * usdcny
        shuibei_gold = gold + 18  # 水贝足金批发价≈基础金价+约18元工费
        return (str(round(shuibei_gold)), str(round(ag_cny, 1)), str(round(gold)))
    except Exception as e:
        print(f"[metal] 失败: {e}")
        return None


def fetch_news(q, max_items=3):
    """Google News 中文 RSS，天然无 HTML、无英文标题。"""
    try:
        url = (f"https://news.google.com/rss/search?q={quote(q)}"
               f"&hl=zh-CN&gl=CN&ceid=CN:zh-Hans")
        r = requests.get(url, headers=UA, timeout=20)
        root = ET.fromstring(r.content)
        out = []
        for it in root.findall(".//item"):
            title = clean_text(it.findtext("title", ""))
            desc = clean_text(it.findtext("description", ""))
            src = clean_text(it.findtext("source", ""))
            link = it.findtext("link", "") or ""
            if not title:
                continue
            # 去掉标题末尾 " - 来源" 这类尾巴
            if src and title.endswith(f" - {src}"):
                title = title[: -(len(src) + 3)].strip()
            out.append((title, desc, src, link))
            if len(out) >= max_items:
                break
        return out
    except Exception as e:
        print(f"[news] {q} 失败: {e}")
        return []


def fetch_weibo_hot(max_items=3):
    try:
        r = requests.get("https://api.vvhan.com/api/hotlist/wbHot", timeout=15)
        data = r.json().get("data", [])
        out = []
        for d in data[:max_items]:
            t = clean_text(d.get("title", ""))
            if t and not CLICKBAIT.search(t):
                out.append((t, "", "微博热搜", ""))
        return out
    except Exception as e:
        print(f"[weibo] 失败: {e}")
        return []


MOM_TIPS = [
    "孕早期(前十二周)是胎儿器官形成的关键期，应避免烟酒、谨慎用药，并在医生指导下补充叶酸。",
    "孕二十八周后可以每天固定时间数胎动，两小时内明显少于十次，或较平时明显减少，应及时就医检查。",
    "孕期补钙首选饮食，牛奶、豆制品、深绿色蔬菜都是好来源；必要时在医生指导下服用钙剂。",
    "孕妇感冒多为病毒引起，不建议自行服用复方感冒药，应多休息、多喝水，症状重时及时就医。",
    "孕中晚期建议左侧卧位休息，有助于改善子宫胎盘血流，缓解下肢水肿。",
    "妊娠期糖尿病筛查一般在孕二十四到二十八周进行，确诊后需通过饮食和运动控制血糖。",
    "临近预产期要留意临产信号：规律宫缩、见红、破水，其中破水后应平躺并尽快就医。",
    "母乳喂养对宝宝免疫力和母婴情感联结都有好处，产后尽早开奶、按需哺乳通常更顺利。",
    "产后四十二天建议回医院复查，评估子宫恢复和伤口愈合情况，不要因为感觉良好就忽视。",
    "孕期体重增长应适度，整个孕期一般增加十一到十六公斤较为合适，增长过快需警惕妊娠糖尿病。",
    "缺铁易导致孕期贫血，可多吃红肉、动物肝脏、瘦肉，搭配维C丰富的食物促进铁吸收。",
    "胎心监护一般在孕三十二周后开始，能反映胎儿宫内状况，产检时按医生建议进行即可。",
]


def fetch_mom_tips(max_items=1):
    day = datetime.datetime.now().day
    tip = MOM_TIPS[day % len(MOM_TIPS)]
    return [(tip, "", "母婴小知识", "")]


# ---------- 口播渲染 ----------

CATS = [
    ("财经", ["美股", "比特币", "黄金白银", "A股", "港股", "基金", "原油", "外汇", "债券"]),
    ("游戏", ["DOTA2", "单机游戏", "电竞", "Steam", "游戏"]),
    ("娱乐", ["微博热搜", "热搜", "娱乐", "娱乐八卦"]),
    ("国际", ["国际", "全球", "海外"]),
    ("母婴", ["母婴", "母婴知识", "孕期", "育儿"]),
    ("AI",   ["AI", "人工智能", "大模型", "科技"]),
    ("物理", ["物理", "物理学", "物理学前沿", "科学前沿"]),
]

TRANS = {
    "财经": "先来关注财经要闻。",
    "游戏": "接下来，我们把目光转向游戏圈。",
    "娱乐": "听完游戏圈的消息，我们再把目光投向国内娱乐圈。",
    "国际": "接下来，把视线转向国际，看看海外都在关注什么。",
    "母婴": "忙碌之余，也来听一段孕期实用小知识。",
    "AI": "接下来，来关注人工智能领域的最新动态。",
    "物理": "节目的最后，我们再用一则科学前沿消息收尾。",
}


def speak_stocks():
    rows = fetch_stocks()
    if not rows:
        return "美股行情暂时获取不到，稍后再看。"
    trend = "集体上涨" if sum(c for _, _, c in rows) >= 0 else "集体下跌"
    parts = [f"首先关注美股，三大指数{trend}"]
    for name, price, chg in rows:
        t = "上涨" if chg >= 0 else "下跌"
        parts.append(f"{name}收报{num2cn(price)}点，{t}百分之{num2cn(abs(chg))}")
    return "，".join(parts) + "。"


def speak_btc():
    d = fetch_btc()
    if not d:
        return "比特币行情暂时获取不到。"
    usd, cny, chg = d
    trend = "上涨" if chg >= 0 else "下跌"
    return (f"加密货币方面，比特币最新报{num2cn(usd)}美元，"
            f"折合人民币约{num2cn(cny)}元，过去二十四小时{trend}百分之{num2cn(abs(chg))}。")


def speak_metals():
    m = fetch_metals()
    if not m:
        return "贵金属行情暂时获取不到。"
    gold_sb, silver_sb, base = m
    return (f"贵金属方面，水贝足金基础价约每克{num2cn(gold_sb)}元，"
            f"足银约每克{num2cn(silver_sb)}元，国内基础金价每克{num2cn(base)}元。")


def speak_news_items(topic, items):
    if not items:
        return f"关于{topic}，今天暂未检索到可用消息。"
    parts = [f"为您播报{topic}方面的消息{count2cn(len(items))}条。"]
    links = ["首先", "第二条", "第三条", "第四条", "第五条"]
    for i, (title, desc, src, link) in enumerate(items):
        body = clean_text(desc if len(desc) >= 10 else title)  # 朗读层再洗一道
        body = body.rstrip("。！？.!?；;，, ") + "。"
        kw = links[i] if i < len(links) else f"第{count2cn(i + 1)}条"
        parts.append(f"{kw}，{body}")
    return "".join(parts)


def speak_weibo(items):
    if not items:
        return "今天微博热搜暂未检索到热门话题。"
    parts = ["为您播报今天微博热搜榜上的热门话题。"]
    links = ["首先", "此外", "还有", "另外"]
    for i, (title, desc, src, link) in enumerate(items):
        body = clean_text(title).rstrip("。！？.!?；;，, ") + "。"
        kw = links[i] if i < len(links) else "此外"
        parts.append(f"{kw}，{body}")
    return "".join(parts)


def build_briefing(topics):
    bj = datetime.timezone(datetime.timedelta(hours=8))
    now = datetime.datetime.now(bj)
    today = f"{now.year}年{now.month}月{now.day}日"
    weekday = "星期" + "一二三四五六日"[now.weekday()]

    speech = [f"早上好，今天是{today}，{weekday}。欢迎收听每日新闻播报。"]

    for cat, keys in CATS:
        matched = [t for t in topics if t in keys]
        if not matched:
            continue
        speech.append(TRANS[cat])
        if cat == "财经":
            if "美股" in matched:
                speech.append(speak_stocks())
            if "比特币" in matched:
                speech.append(speak_btc())
            if "黄金白银" in matched:
                speech.append(speak_metals())
            for t in matched:
                if t in ("美股", "比特币", "黄金白银"):
                    continue
                speech.append(speak_news_items(t, fetch_news(t)))
        elif cat == "娱乐":
            speech.append(speak_weibo(fetch_weibo_hot()))
        elif cat == "母婴":
            speech.append(speak_news_items("母婴", fetch_mom_tips()))
        elif cat == "物理":
            speech.append(speak_news_items(
                "科学前沿", fetch_news("物理学 科研 突破 研究发现", max_items=1)))
        else:
            for t in matched:
                q = t
                if t == "AI":
                    q = "人工智能 AI 大模型"
                elif t == "DOTA2":
                    q = "DOTA2 电竞"
                elif t == "单机游戏":
                    q = "单机游戏 发售"
                elif t == "国际":
                    q = "国际 全球 新闻"
                speech.append(speak_news_items(t, fetch_news(q)))

    speech.append("以上就是今天的全部内容。祝您一天顺利，我们明天再见。")
    text = "".join(speech)
    return text, text  # 文字版 == 音频版，逐字一致


async def tts(text, out_path, voice_key, rate="+0%"):
    import edge_tts
    voice = VOICE_MAP.get(voice_key, VOICE_MAP["yunxi"])
    await edge_tts.Communicate(text, voice, rate=rate).save(out_path)


def load_config(path="config.txt"):
    topics, voice, rate = [], "yunxi", "+0%"
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.lower().startswith("voice="):
                voice = line.split("=", 1)[1].strip() or "yunxi"
            elif line.lower().startswith("rate="):
                rate = line.split("=", 1)[1].strip() or "+0%"
            else:
                topics.append(line)
    return topics, voice, rate


def push(text, audio_url=None):
    key = os.environ.get("SERVERCHAN_KEY", "")
    if not key:
        print("[push] 未配置 SERVERCHAN_KEY，跳过推送")
        return
    url = f"https://sctapi.ftqq.com/{key}.send"
    desp = text
    if audio_url:
        desp += f"\n\n🔊 收听音频：{audio_url}"
    try:
        r = requests.post(url, data={"title": "每日新闻播报", "des": desp,
                                     "desp": desp}, timeout=15)
        print("[push]", r.json())
    except Exception as e:
        print(f"[push] 失败: {e}")


def main():
    os.makedirs("public", exist_ok=True)
    topics, voice, rate = load_config()
    md, speech = build_briefing(topics)
    with open("public/briefing.md", "w", encoding="utf-8") as f:
        f.write(md)

    mp3 = "public/briefing.mp3"
    try:
        asyncio.run(tts(speech, mp3, voice, rate))
        print("[tts] 生成成功:", mp3)
    except Exception as e:
        print(f"[tts] 失败: {e}")

    repo = os.environ.get("GITHUB_REPOSITORY", "")
    audio_url = ""
    if repo and "/" in repo:
        u, rp = repo.split("/", 1)
        audio_url = f"https://{u}.github.io/{rp}/briefing.mp3"
    push(speech, audio_url)


if __name__ == "__main__":
    main()
