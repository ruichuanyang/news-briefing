#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日新闻简报机器人 —— 免费、无需服务器、推送到微信(Server酱) + AI朗读(Edge TTS)
抓取源全部无需 API Key：
  - 新闻：Google News RSS 关键词搜索（免费，抓标题+摘要）
  - 美股：Yahoo Finance 公开行情接口（免费）
  - 比特币：CoinGecko 公开接口（免费）
  - 黄金/白银：水贝价页面抓取 + 新浪基础金价兜底（免费）
朗读：Edge TTS 微软免费在线语音
推送：Server酱（免费）-> 个人微信；音频托管在 GitHub Pages（免费）

口播模式：模仿新闻联播/财经新闻，有开场白、逻辑过渡词、收尾；
念摘要不念标题；播报顺序固定为：财经 -> 游戏 -> 娱乐 -> 国际 -> 母婴 -> AI -> 物理。

一致性保证：所有数据只抓取一次（gather），文字版与音频版用同一份数据渲染，
推送文字 = 口播稿正文（去除新闻网址），与朗读逐字一致。
"""

import os
import re
import html
import asyncio
import datetime
import email.utils
import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
VOICE_MAP = {
    # 男声
    "yunxi":    "zh-CN-YunxiNeural",     # 云希 温和（默认）
    "yunyang":  "zh-CN-YunyangNeural",   # 云扬 新闻播报感
    "yunjian":  "zh-CN-YunjianNeural",   # 云健 浑厚稳重
    "yunfeng":  "zh-CN-YunfengNeural",   # 云枫 成熟磁性
    "yunhao":   "zh-CN-YunhaoNeural",    # 云皓 沉稳播报
    "yunjie":   "zh-CN-YunjieNeural",    # 云杰 睿智
    "yunxia":   "zh-CN-YunxiaNeural",    # 云夏 少年感
    # 女声
    "xiaoxiao": "zh-CN-XiaoxiaoNeural",  # 晓晓 甜美
    "xiaoyi":   "zh-CN-XiaoyiNeural",    # 晓伊 活泼
    "xiaochen": "zh-CN-XiaochenNeural",  # 晓辰 成熟干练
    "xiaohan":  "zh-CN-XiaohanNeural",   # 晓涵 温婉
    "xiaomeng": "zh-CN-XiaomengNeural",  # 晓梦 青春
    "xiaomo":   "zh-CN-XiaomoNeural",    # 晓墨 知性
    "xiaorui":  "zh-CN-XiaoruiNeural",   # 晓睿 学术
    "xiaoshuang": "zh-CN-XiaoshuangNeural",  # 晓双 童声
    "xiaoxuan": "zh-CN-XiaoxuanNeural",  # 晓萱 甜美
    "xiaoyan":  "zh-CN-XiaoyanNeural",   # 晓颜 专业
    "xiaoyou":  "zh-CN-XiaoyouNeural",   # 晓悠 温柔
}

# 播报顺序分类（财经 -> 游戏 -> 娱乐 -> 国际 -> 母婴 -> AI -> 物理；每类内部按 config 里的顺序）
CATS = [
    ("财经", ["美股", "比特币", "黄金白银", "A股", "港股", "基金", "原油", "外汇", "债券"]),
    ("游戏", ["DOTA2", "单机游戏", "电竞", "Steam", "游戏"]),
    ("娱乐", ["微博热搜", "热搜", "娱乐八卦", "娱乐圈", "八卦", "明星"]),
    ("国际", ["X热点", "推特热点", "Twitter", "国际热点", "海外"]),
    ("母婴", ["母婴小知识", "母婴知识", "孕期", "育儿", "母婴", "孕妇", "宝宝"]),
    ("AI",   ["AI", "人工智能", "大模型", "ChatGPT", "AIGC"]),
    ("物理", ["物理前沿", "前沿科学", "物理学", "科学前沿", "物理"]),
]

DIG = "零一二三四五六七八九"
CN_UNITS = ["", "十", "百", "千"]
CN_BIG = ["", "万", "亿"]


def int2cn(n):
    """整数转中文（支持亿以内），如 52759 -> 五万二千七百五十九"""
    if n == 0:
        return "零"
    segs = []
    while n > 0:
        segs.append(n % 10000)
        n //= 10000
    res = ""
    for i in range(len(segs) - 1, -1, -1):
        seg = segs[i]
        if seg == 0:
            continue
        part = ""
        for pos in range(3, -1, -1):
            d = seg // (10 ** pos) % 10
            if d == 0:
                if part and not part.endswith(("零", "十")):
                    part += "零"
            else:
                part += DIG[d] + CN_UNITS[pos]
        part = part.strip("零")
        if i < len(segs) - 1 and seg < 1000 and res and not res.endswith("零"):
            res += "零"
        res += part + CN_BIG[i]
    return res


def num2cn(x):
    """数字转中文朗读，如 52759.21 -> 五万二千七百五十九点二一"""
    if isinstance(x, float):
        s = f"{x:.2f}"
    else:
        s = str(x)
    s = s.replace(",", "").strip()
    if "." in s:
        i, d = s.split(".", 1)
        d = d.rstrip("0")
        if d:
            return int2cn(int(i)) + "点" + "".join(DIG[int(c)] for c in d)
        return int2cn(int(i))
    return int2cn(int(s) if s else 0)


# 常见营销/夸张前缀词（念摘要时去除）
CLICKBAIT = re.compile(
    r"^(重磅|突发|震惊|官宣|刚刚|快讯|速看|必看|注意|警惕|揭秘|爆|绝了|疯了|"
    r"狂|大动作|一夜之间|终于|定了|来了|好消息|坏消息|收藏|扩散|转发|"
    r"[!！:：\s]*)+"
)


def clean_text(s):
    """清洗文本：去 HTML/转义/网址/无用符号/广告前缀；保留中文标点与书名号《》。"""
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = html.unescape(s)
    # 去网址（避免文字显示网址、音频念出链接）
    s = re.sub(r"https?://\S+", "", s)
    s = re.sub(r"www\.\S+", "", s)
    # 去无用符号（保留中文标点 。，、；：？！《》 与中文文字）
    s = re.sub(r"[【】()（）〔〕\[\]<>]", " ", s)
    s = s.replace('"', " ").replace("'", " ").replace("`", " ")
    s = s.replace("「", " ").replace("」", " ").replace("『", " ").replace("』", " ")
    # 去常见广告/尾巴词（不限定行尾）
    s = re.sub(r"(阅读全文|查看详情|Read full article|点击.*?查看)", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = CLICKBAIT.sub("", s)
    s = re.sub(r"(来源|编辑)[：: ].*?$", "", s).strip()
    s = re.sub(r"\s+", " ", s).strip()
    return s


def trim_body(body):
    """新闻正文收尾规范化：去掉末尾标点后统一补一个句号"""
    return body.rstrip("。！？.!?；;，, 、…") + "。"


def load_config(path="config.txt"):
    topics, voice, rate = [], "yunxi", "+0%"
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            low = line.lower()
            if low.startswith("voice="):
                voice = line.split("=", 1)[1].strip() or "yunxi"
            elif low.startswith("rate="):
                rate = line.split("=", 1)[1].strip() or "+0%"
            else:
                topics.append(line)
    return topics, voice, rate


def classify(topics):
    """按播报顺序分类"""
    order, used = [], set()
    for cat, keys in CATS:
        ts = [t for t in topics if t not in used
              and any(k.lower() in t.lower() for k in keys)]
        if ts:
            order.append((cat, ts))
            used.update(ts)
    rest = [t for t in topics if t not in used]
    if rest:
        order.append(("其他", rest))
    return order


def fetch_news(query, max_items=3):
    """Google News RSS 关键词搜索，返回 [(title, desc, src, link)]（近两天）"""
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
            de = re.search(r"<description>(.*?)</description>", it, re.S)
            if not (t and l):
                continue
            title = clean_text(re.sub(r"\s*-\s*[^_-]+$", "", t.group(1).strip()))
            link = l.group(1).strip()
            src = clean_text(s.group(1).strip()) if s else ""
            desc = clean_text(de.group(1).strip()) if de else ""
            if d:
                try:
                    pdt = email.utils.parsedate_to_datetime(d.group(1).strip())
                    if pdt.tzinfo is None:
                        pdt = pdt.replace(tzinfo=datetime.timezone.utc)
                    if (now - pdt).days > 2:
                        continue
                except Exception:
                    pass
            out.append((title, desc, src, link))
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
        html_txt = requests.get("http://www.huangjinjiage.cn/golden/85544.html",
                                headers=UA, timeout=20).text
        m = re.search(r"水贝黄金\s*黄金价格\s*([\d.]+)元/克", html_txt)
        if m:
            gold_sb = m.group(1)
        m = re.search(r"水贝白银价格[：: ]*([\d.]+)元/克", html_txt)
        if not m:
            m = re.search(r"水贝\s*([\d.]+)\s*元/克", html_txt)
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


def fetch_weibo_hot(max_items=3):
    """微博热搜：第三方免费接口 vvhan 取实时热搜榜，失败则用娱乐八卦兜底"""
    try:
        r = requests.get("https://api.vvhan.com/api/hotlist/wbHot",
                         headers=UA, timeout=20)
        j = r.json()
        data = j.get("data") or []
        out = []
        for it in data:
            title = clean_text(str(it.get("title", "")))
            if not title:
                continue
            hot = it.get("hot") or 0
            desc = f"该话题今日热度约{num2cn(int(hot))}" if hot else ""
            out.append((title, desc, "微博热搜", it.get("url", "")))
            if len(out) >= max_items:
                break
        if out:
            return out
    except Exception as e:
        print(f"[weibo] 失败: {e}")
    return fetch_news("娱乐 八卦", max_items)


def fetch_x_trending(max_items=3):
    """国际热点：优先 Google News 中文国际新闻（中文朗读友好），失败再用 Reddit 英文兜底"""
    items = fetch_news("国际 新闻 世界", max_items)
    if items:
        return items
    try:
        r = requests.get("https://www.reddit.com/r/worldnews/top.json?t=day&limit=6",
                         headers={**UA, "Accept": "application/json"}, timeout=20)
        j = r.json()
        out = []
        for ch in j.get("data", {}).get("children", []):
            d = ch.get("data", {})
            title = clean_text(str(d.get("title", "")))
            if not title or title.startswith("["):
                continue
            out.append((title, "", "Reddit",
                        f"https://www.reddit.com{d.get('permalink', '')}"))
            if len(out) >= max_items:
                break
        if out:
            return out
    except Exception as e:
        print(f"[x] 失败: {e}")
    return []


# 孕期母婴小知识库（按日期轮换，保证每天不重复；仅科普，具体请遵医嘱）
MOM_TIPS = [
    ("孕早期叶酸补充",
     "孕前三个月到孕早期，建议每天补充四百微克叶酸，有助于预防胎儿神经管缺陷，具体剂量请遵医嘱。",
     "孕期科普", ""),
    ("孕期体重管理",
     "孕期体重并非越长越好。孕中晚期每周增长约零点四到零点五公斤较为适宜，具体目标因人而异，建议定期产检并咨询医生。",
     "孕期科普", ""),
    ("孕期补铁防贫血",
     "孕中晚期铁需求量增大，容易出现缺铁性贫血。可适量吃红肉、动物肝脏等富铁食物，必要时在医生指导下补充铁剂。",
     "孕期科普", ""),
    ("孕期睡眠建议",
     "孕晚期建议尽量左侧卧睡，可减轻子宫对大血管的压迫，改善胎盘供血；用孕妇枕托住腹部和腰部会更舒适。",
     "孕期科普", ""),
    ("孕吐缓解小方法",
     "孕早期孕吐常见，可少食多餐、避免空腹，起床前先吃两片苏打饼干，通常孕十二周后会逐渐缓解，严重时需及时就医。",
     "孕期科普", ""),
    ("DHA 与胎儿大脑发育",
     "孕期可适量补充 DHA，每周吃两到三次深海鱼是不错的选择，也可以选用藻油 DHA 补充剂，建议咨询医生后使用。",
     "孕期科普", ""),
    ("孕期补钙",
     "孕中晚期每日钙需求约一千毫克，奶制品、豆制品、绿叶菜都是好来源，必要时按医嘱补充钙片，预防抽筋和骨量流失。",
     "孕期科普", ""),
    ("孕期适度运动",
     "没有禁忌症的前提下，孕期散步、孕妇瑜伽、游泳等适度运动有助于控制体重和改善情绪，运动强度以不气喘为宜。",
     "孕期科普", ""),
    ("妊娠糖尿病筛查",
     "孕二十四到二十八周通常要做口服葡萄糖耐量试验，筛查妊娠期糖尿病，检查前请按要求空腹并遵循医生安排。",
     "孕期科普", ""),
    ("数胎动",
     "孕二十八周后可以每天固定时间数胎动，两小时内胎动明显少于十次，或较平时明显减少，应及时就医检查。",
     "孕期科普", ""),
]


def fetch_mom_tips(max_items=1):
    """母婴小知识：内置知识库按日期轮换，保证每天不重复"""
    today = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    n = len(MOM_TIPS)
    picks = [MOM_TIPS[(today.toordinal() + i) % n] for i in range(min(max_items, n))]
    return [tuple(p) for p in picks]


def fetch_physics(max_items=2):
    """物理学前沿：Phys.org 物理学 RSS（科普向），失败则 arXiv 量子物理 RSS 兜底"""
    try:
        r = requests.get("https://phys.org/rss-feed/phys/", headers=UA, timeout=20)
        items = re.findall(r"<item>(.*?)</item>", r.text, re.S)
        out = []
        for it in items:
            t = re.search(r"<title>(.*?)</title>", it, re.S)
            l = re.search(r"<link>(.*?)</link>", it, re.S)
            de = re.search(r"<description>(.*?)</description>", it, re.S)
            if not t:
                continue
            title = clean_text(t.group(1).strip())
            desc = clean_text(de.group(1).strip()) if de else ""
            if len(desc) > 90:
                desc = desc[:90]
            out.append((title, desc, "Phys.org", (l.group(1).strip() if l else "")))
            if len(out) >= max_items:
                break
        if out:
            return out
    except Exception as e:
        print(f"[phys] 失败: {e}")
    try:
        r = requests.get("https://export.arxiv.org/rss/quant-ph",
                         headers=UA, timeout=20)
        items = re.findall(r"<item>(.*?)</item>", r.text, re.S)
        out = []
        for it in items:
            t = re.search(r"<title>(.*?)</title>", it, re.S)
            if t:
                out.append((clean_text(t.group(1).strip()), "", "arXiv", ""))
            if len(out) >= max_items:
                break
        return out
    except Exception as e:
        print(f"[phys-arxiv] 失败: {e}")
    return []


def fetch_for(topic, max_items=3):
    """按主题分发到专用数据源，统一返回 [(title, desc, src, link)]"""
    if topic in ("微博热搜", "热搜", "娱乐八卦", "娱乐"):
        return fetch_weibo_hot(max_items)
    if topic in ("X热点", "推特热点", "Twitter", "国际热点", "海外"):
        return fetch_x_trending(max_items)
    if topic in ("母婴小知识", "母婴知识", "孕期", "育儿", "母婴", "孕妇", "宝宝"):
        return fetch_mom_tips(max_items)
    if topic in ("物理前沿", "前沿科学", "物理学", "科学前沿", "物理"):
        return fetch_physics(max_items)
    return fetch_news(topic, max_items)


def gather(topics):
    """一次性抓取所有数据并缓存，供文字版与音频版共用，保证两者一致。"""
    return {
        "stocks": fetch_stocks(),
        "btc": fetch_btc(),
        "metals": fetch_metals(),
        "news": {t: fetch_for(t) for t in topics},
    }


def cat_transition(cat):
    """板块过渡语（逻辑连贯词）"""
    return {
        "财经": "先来关注财经要闻。",
        "游戏": "接下来，我们把目光转向游戏圈。",
        "娱乐": "听完游戏圈的消息，我们再把目光投向国内娱乐圈。",
        "国际": "接下来，把视线转向国际，看看海外都在关注什么。",
        "母婴": "忙碌之余，也来听一段孕期实用小知识。",
        "AI": "接下来，来关注人工智能领域的最新动态。",
        "物理": "节目的最后，我们再用一则物理学前沿消息收尾。",
        "其他": "另外，还有几条消息带给大家。",
    }.get(cat, "接下来请看其他消息。")


def speak_price(price):
    """股价朗读：整数加'点'，带小数的自带'点'"""
    s = num2cn(price)
    if isinstance(price, float) and price != int(price):
        return s
    return s + "点"


def count2cn(n):
    """条数朗读：2 念'两'（两条/两次），其余用中文数字"""
    return "两" if n == 2 else num2cn(n)


def speak_news_items(topic, items):
    """把一个新闻主题变成口播段落（念摘要，不念标题广告/链接/来源）"""
    if not items:
        return f"关于{topic}，今天暂未检索到可用消息。"
    # 母婴小知识：直接念知识正文，不套新闻模板
    if topic in ("母婴小知识", "母婴知识", "孕期", "育儿", "母婴", "孕妇", "宝宝"):
        title, desc, src, link = items[0]
        body = trim_body(clean_text(desc if len(desc) >= 12 else title))
        return f"给您带来一段{topic}。{body}"
    # 微博热搜：口语化播报热门话题
    if topic in ("微博热搜", "热搜", "娱乐八卦", "娱乐"):
        parts = ["为您播报今天微博热搜榜上的热门话题。"]
        links = ["首先", "此外", "还有", "另外", "最后"]
        for i, (title, desc, src, link) in enumerate(items):
            body = trim_body(clean_text(desc if len(desc) >= 12 else title))
            kw = links[i] if i < len(links) else "此外"
            parts.append(f"{kw}，{body}")
        return "".join(parts)
    # 国际热点：避免念"X热点"这类生硬词
    if topic in ("X热点", "推特热点", "Twitter", "国际热点", "海外"):
        parts = [f"为您播报今天的国际热点消息{count2cn(len(items))}条。"]
        links = ["首先", "第二条", "第三条", "第四条", "第五条"]
        for i, (title, desc, src, link) in enumerate(items):
            body = trim_body(clean_text(desc if len(desc) >= 12 else title))
            kw = links[i] if i < len(links) else f"第{count2cn(i + 1)}条"
            parts.append(f"{kw}，{body}")
        return "".join(parts)
    # 物理前沿：只有一两条，用"带来一条消息"收尾更自然
    if topic in ("物理前沿", "前沿科学", "物理学", "科学前沿", "物理"):
        title, desc, src, link = items[0]
        body = trim_body(clean_text(desc if len(desc) >= 12 else title))
        return f"为您带来一条物理学前沿消息。{body}"
    parts = [f"为您播报{topic}方面的消息{count2cn(len(items))}条。"]
    links = ["首先", "第二条", "第三条", "第四条", "第五条"]
    for i, (title, desc, src, link) in enumerate(items):
        body = trim_body(clean_text(desc if len(desc) >= 12 else title))
        kw = links[i] if i < len(links) else f"第{count2cn(i + 1)}条"
        parts.append(f"{kw}，{body}")
    return "".join(parts)


def speak_finance(ts, data):
    """财经板块：先行情（美股/比特币/黄金白银），后财经新闻（使用已抓取的数据）"""
    segs = []
    stocks = data.get("stocks") or []
    if "美股" in ts and stocks:
        order = {"道琼斯": 0, "标普500": 1, "纳斯达克": 2}
        rows = sorted(stocks, key=lambda r: order.get(r[0], 3))
        trends = [1 if c >= 0 else -1 for _, _, c in rows]
        if all(t == 1 for t in trends):
            summary = "三大指数集体收涨"
        elif all(t == -1 for t in trends):
            summary = "三大指数集体收跌"
        else:
            summary = "三大指数涨跌不一"
        segs.append(f"首先关注美股，{summary}。")
        names = {"道琼斯": "道琼斯", "标普500": "标普五百", "纳斯达克": "纳斯达克"}
        parts = []
        for name, price, chg in rows:
            trend = "上涨" if chg >= 0 else "下跌"
            parts.append(f"{names.get(name, name)}收报{speak_price(price)}，"
                         f"{trend}百分之{num2cn(abs(chg))}")
        segs.append("，".join(parts) + "。")
    if "比特币" in ts:
        b = data.get("btc")
        if b:
            usd, cny, chg = b
            trend = "上涨" if chg >= 0 else "下跌"
            segs.append(f"加密货币方面，比特币最新报{num2cn(usd)}美元，"
                        f"折合人民币约{num2cn(int(cny))}元，"
                        f"过去二十四小时{trend}百分之{num2cn(abs(chg))}。")
    if "黄金白银" in ts:
        g, s, base = data.get("metals") or ("", "", "")
        parts = []
        if g:
            parts.append(f"水贝足金基础价每克{num2cn(float(g))}元")
        if s:
            parts.append(f"足银约{num2cn(float(s))}元每克")
        if base:
            parts.append(f"国内基础金价每克{num2cn(float(base))}元")
        if parts:
            segs.append("贵金属方面，" + "，".join(parts) + "。")
    # 财经板块里的新闻类主题（如 基金、A股）
    news_ts = [t for t in ts if t not in ("美股", "比特币", "黄金白银")]
    for t in news_ts:
        segs.append(speak_news_items(t, data["news"].get(t, [])))
    return "".join(segs)


def build_briefing(topics):
    bj = datetime.timezone(datetime.timedelta(hours=8))
    now = datetime.datetime.now(bj)
    today = f"{now.year}年{now.month}月{now.day}日"
    weekday = "星期" + "一二三四五六日"[now.weekday()]

    # 所有数据只抓取一次
    data = gather(topics)

    # 口播稿各部分（列表元素之间在文字版换行、在音频版无缝拼接）
    parts = [f"早上好，今天是{today}，{weekday}。欢迎收听每日新闻播报。"]
    ordered = classify(topics)
    for cat, ts in ordered:
        parts.append(cat_transition(cat))
        if cat == "财经":
            fin = speak_finance(ts, data)
            parts.append(fin if fin else "财经数据今日暂未获取成功。")
        else:
            for t in ts:
                parts.append(speak_news_items(t, data["news"].get(t, [])))
    parts.append("以上就是今天的全部内容。祝您一天顺利，我们明天再见。")

    speech = "".join(parts)                       # 连续朗读
    md = "\n\n".join(p for p in parts if p)       # 与音频逐字一致，仅换行便于阅读
    return md, speech


async def tts(text, out_path, voice_key, rate="+0%"):
    import edge_tts
    voice = VOICE_MAP.get(voice_key, VOICE_MAP["yunxi"])
    await edge_tts.Communicate(text, voice, rate=rate).save(out_path)


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
    topics, voice, rate = load_config()
    md, speech = build_briefing(topics)
    with open("public/briefing.md", "w", encoding="utf-8") as f:
        f.write(md)
    with open("public/speech.txt", "w", encoding="utf-8") as f:
        f.write(speech)

    mp3 = "public/briefing.mp3"
    try:
        asyncio.run(tts(speech, mp3, voice, rate))
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
