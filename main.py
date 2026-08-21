#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日新闻播报 bot —— 文章体重写版 (v7)

解决 three 个问题：
1) 数据源加固：美股改 stooq(带 Yahoo 兜底)，贵金属改 gold-api + 汇率接口，
   微博热搜多源兜底；任一路拿不到就「安静跳过」，不再出现"暂时获取不到"的破句。
2) 彻底去掉消息来源：标题里的"新浪新闻_手机新浪网""上海交通大学 新闻网"等
   后缀一律剥掉，全文/音频都不写不读。
3) 话术重写：按"写一篇能听下去的文章"来组织，板块之间自然衔接，
   不再满屏"接下来的"，条目之间用多变的自然连接词串成连贯叙述。
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

# ---------------- 文本清洗 ----------------
CLICKBAIT = re.compile(r"(震惊|突发|速看|重磅|全网|疯传|不敢相信|万万没想到|"
                       r"点击(此处|这里|查看)|阅读原文|关注我们|扫码|福利|限时|"
                       r"夺宝|娱乐网址|博彩|彩票|澳门|现金)", re.I)

TAG_RE = re.compile(r"<\s*/?[a-zA-Z][^>]*>")
ATTR_RE = re.compile(r"(?i)\b(?:href|target|rel|src|alt|style|color|"
                     r"width|height|class|id|title|align)\s*=\s*[^ \n<>,]*")
HEX_RE = re.compile(r"#\s*[0-9a-fA-F]{3,8}")
URL_RE = re.compile(r"https?://\S+|www\.\S+")
STRAY_RE = re.compile(r"(?i)\b(?:/?(?:a|font|span|div|br|img|p|b|i|"
                      r"u|strong|em|table|tr|td|li|ul|ol|h[1-6]))\b")


def clean_text(s):
    """彻底清洗：去 HTML、去标签碎片、去网址、去广告、压缩空白。"""
    if not s:
        return ""
    s = html.unescape(s or "")
    s = TAG_RE.sub(" ", s)
    s = ATTR_RE.sub(" ", s)
    s = HEX_RE.sub(" ", s)
    s = URL_RE.sub(" ", s)
    s = CLICKBAIT.sub(" ", s)
    s = STRAY_RE.sub(" ", s)
    s = s.replace(" /a", "").replace("/a", "")
    s = s.replace(" /font", "").replace("/font", "")
    s = re.sub(r"[<>]", " ", s)
    s = re.sub(r"(?i)\b_?blank\b|\b_self\b", " ", s)
    s = re.sub(r"\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}\b", " ", s, flags=re.I)
    s = re.sub(r"\s*/\s*", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


# 常见来源词（作为标题尾巴时一律剥除，绝不写出/读出）
SOURCE_FRAGS = [
    "新浪新闻", "手机新浪网", "新浪财经", "新浪网", "新浪科技", "新浪体育",
    "新闻网", "澎湃新闻", "澎湃", "腾讯网", "腾讯新闻", "腾讯科技", "腾讯体育",
    "网易新闻", "网易", "搜狐新闻", "搜狐网", "搜狐", "新华网", "新华每日电讯",
    "人民网", "央视新闻", "央广网", "中国新闻网", "环球网", "环球时报",
    "参考消息", "联合早报", "光明网", "海外网", "界面新闻", "第一财经",
    "证券时报", "财新网", "财新", "36氪", "钛媒体", "IT之家", "新京报",
    "北京日报", "上观新闻", "南方都市报", "中国青年网", "科技日报", "科学网",
    "北京大学", "清华大学", "上海交通大学 新闻网", "复旦大学", "浙江大学",
    "中国日报", "经济日报", "工人日报", "人民日报", "央视网", "观察者网",
    "凤凰网", "凤凰资讯", "东方网", "红星新闻", "封面新闻", "极目新闻",
    "上游新闻", "九派新闻", "潇湘晨报", "扬子晚报", "钱江晚报", "澎湃讯",
]


def strip_source(title, src):
    """去掉标题里夹带的消息来源，保证文章里不出现'新浪新闻'之类的字眼。"""
    title = (title or "").strip()
    if src:
        for sep in [" - ", " _ ", "｜", " | ", "—", " – ", " -- "]:
            suf = sep + src
            if title.endswith(suf):
                title = title[: -len(suf)].strip()
                break
    for frag in SOURCE_FRAGS:
        if title.endswith(frag):
            title = title[: -len(frag)].strip()
    return title.strip(" -_｜|—– \t")


# ---------------- 数字中文播报 ----------------
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
    s = ""
    if num >= 100000000:
        s = _int2cn(num // 100000000) + "亿"
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
    s = _int2cn(intpart) if intpart else ""
    if frac and frac > 0:
        dec = f"{frac:.2f}".split(".")[1].rstrip("0")
        if dec:
            s = (s if s else "零") + "点" + "".join(DIGITS[int(c)] for c in dec)
    if not s:
        s = "零"
    return ("负" if neg else "") + s


def count2cn(n):
    return {1: "一", 2: "两", 3: "三", 4: "四", 5: "五"}.get(n, num2cn(n))


# ---------------- 数据源（全部免费、无需 key）----------------

def fetch_stocks():
    """美股三大指数：stooq 日线算涨跌，失败则 Yahoo 兜底。"""
    syms = [("道琼斯", "^dji"), ("标普500", "^spx"), ("纳斯达克", "^ixic")]
    yahoo_map = {"^dji": "^DJI", "^spx": "^GSPC", "^ixic": "^IXIC"}
    out = []

    def _stooq(sym):
        r = requests.get(f"https://stooq.com/q/d/l/?s={sym}&i=d", timeout=15)
        lines = [l for l in r.text.strip().splitlines()
                 if l and not l.lower().startswith("date")]
        if len(lines) < 2:
            raise ValueError("no data")
        close = float(lines[-1].split(",")[4])
        prev = float(lines[-2].split(",")[4])
        return close, (close - prev) / prev * 100

    def _yahoo(sym):
        r = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/"
            f"{yahoo_map.get(sym, sym)}?range=1d&interval=1d", timeout=15)
        j = r.json()["chart"]["result"][0]
        price = j["meta"]["regularMarketPrice"]
        prev = j["meta"].get("chartPreviousClose") or j["meta"].get("previousClose")
        return price, (price - prev) / prev * 100

    for name, sym in syms:
        val = None
        for fn in (_stooq, _yahoo):
            try:
                val = fn(sym)
                break
            except Exception as e:
                print(f"[stock][{name}] {fn.__name__} 失败: {e}")
        if val:
            out.append((name, val[0], val[1]))
    return out


def fetch_btc():
    # 主源 coingecko；兜底 coinbase(美元现货) + er-api(美元兑人民币)
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price"
            "?ids=bitcoin&vs_currencies=usd,cny&include_24hr_change=true",
            timeout=15)
        d = r.json()["bitcoin"]
        return (d["usd"], d["cny"], d["usd_24h_change"])
    except Exception as e:
        print(f"[btc] coingecko 失败: {e}")
    try:
        usd = float(requests.get(
            "https://api.coinbase.com/v2/prices/BTC-USD/spot",
            timeout=15).json()["data"]["amount"])
        fx = requests.get("https://open.er-api.com/v6/latest/USD",
                          timeout=15).json()
        cny = usd * float(fx["rates"]["CNY"])
        return (usd, cny, 0.0)
    except Exception as e:
        print(f"[btc] coinbase 失败: {e}")
        return None


def fetch_metals():
    """贵金属：gold-api 拿国际金价/银价(美元/盎司)，再按汇率折算人民币克价。"""
    try:
        g = requests.get("https://api.gold-api.com/price/XAU", timeout=15).json()
        s = requests.get("https://api.gold-api.com/price/XAG", timeout=15).json()
        fx = requests.get("https://open.er-api.com/v6/latest/USD", timeout=15).json()
        usdcny = float(fx["rates"]["CNY"])
        g_cny_g = float(g["price"]) / 31.1035 * usdcny      # 国内基础金价 元/克
        s_cny_g = float(s["price"]) / 31.1035 * usdcny      # 足银 元/克
        base = round(g_cny_g)
        shuibei = base + 15                                 # 水贝足金批发≈基础+工费
        return (str(base), str(shuibei), str(round(s_cny_g, 1)))
    except Exception as e:
        print(f"[metal] 失败: {e}")
        return None


def fetch_news(q, max_items=3):
    """Google News 中文 RSS；标题剥来源，绝不带回'新浪新闻'等字眼。"""
    try:
        url = (f"https://news.google.com/rss/search?q={quote(q)}"
               f"&hl=zh-CN&gl=CN&ceid=CN:zh-Hans")
        r = requests.get(url, headers=UA, timeout=20)
        root = ET.fromstring(r.content)
        out = []
        for it in root.findall(".//item"):
            raw_title = it.findtext("title", "")
            raw_desc = it.findtext("description", "")
            raw_src = it.findtext("source", "")
            title = strip_source(clean_text(raw_title), clean_text(raw_src))
            desc = clean_text(raw_desc)
            if not title or CLICKBAIT.search(title):
                continue
            out.append((title, desc))
            if len(out) >= max_items:
                break
        return out
    except Exception as e:
        print(f"[news] {q} 失败: {e}")
        return []


def fetch_weibo_hot(max_items=5):
    """微博热搜：多源兜底。拿不到就返回空列表（安静跳过，不报错句）。"""
    candidates = []

    def _grab(getter):
        try:
            return getter()
        except Exception as e:
            print(f"[weibo] 源失败: {e}")
            return []

    def src_vvhan():
        r = requests.get("https://api.vvhan.com/api/hotlist/wbHot", timeout=12)
        return [clean_text(d.get("title", "")) for d in r.json().get("data", [])]

    def src_tenapi():
        r = requests.get("https://tenapi.cn/v2/weibohot", timeout=12)
        return [clean_text(d.get("title", "")) for d in r.json().get("data", [])]

    def src_official():
        r = requests.get("https://weibo.com/ajax/side/hotSearch",
                         headers=UA, timeout=12)
        return [clean_text(d.get("word", ""))
                for d in r.json().get("data", {}).get("realtime", [])]

    for src in (src_vvhan, src_tenapi, src_official):
        candidates = _grab(src)
        if candidates:
            break

    out, seen = [], set()
    for t in candidates:
        if not t or CLICKBAIT.search(t) or t in seen:
            continue
        seen.add(t)
        out.append((t, ""))
        if len(out) >= max_items:
            break
    return out


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


def fetch_mom_tips():
    day = datetime.datetime.now().day
    tip = MOM_TIPS[day % len(MOM_TIPS)]
    return [(tip, "")]


# ---------------- 文章体叙述 ----------------

CATS = [
    ("财经", ["美股", "比特币", "黄金白银", "A股", "港股", "基金", "原油", "外汇", "债券"]),
    ("游戏", ["DOTA2", "单机游戏", "电竞", "Steam", "游戏"]),
    ("娱乐", ["微博热搜", "热搜", "娱乐", "娱乐八卦"]),
    ("国际", ["国际", "全球", "海外"]),
    ("母婴", ["母婴", "母婴知识", "孕期", "育儿"]),
    ("AI",   ["AI", "人工智能", "大模型", "科技"]),
    ("物理", ["物理", "物理学", "物理学前沿", "科学前沿"]),
]

# 各板块开场白：直接切题，绝不用"接下来"
OPENERS = {
    "游戏": {
        "DOTA2": "电竞这块先说《DOTA2》，今天有几条值得一聊的消息。",
        "单机游戏": "单机游戏这边，",
        "_default": "游戏行业还有这些动向：",
    },
    "国际": {"_default": "再把视野放到海外，今天国际上有几个看点。"},
    "AI": {"_default": "人工智能领域，最近动作不少，挑重要的说。"},
}

FIN_OPENER = {
    "A股": "回到国内，A股方面，",
    "港股": "港股这边，",
    "基金": "基金市场，",
    "原油": "原油方面，",
    "外汇": "外汇市场，",
    "债券": "债券市场，",
    "_default": "财经方面还有这些消息：",
}

# 条目之间的自然连接词（多变、不重复、不出现"接下来的"）
ITEM_TRANS = [
    "另外，", "与此同时，", "值得关注的是，", "视线转到，", "另一边，",
    "此外，", "紧随其后的消息是，", "同样受到瞩目的还有，", "再把目光投向，",
    "说来也巧，", "与之呼应的是，", "还有一则，", "不妨也看看，",
    "接力这条消息，", "同一时间，", "镜头给到，", "顺着这个话题，",
    "也来关注，", "继续看，", "补充一条，", "同一赛道上，", "另一边传来，",
]

SUMMARY_LEAD = ["据了解，", "据相关报道，", "消息显示，", "有报道提到，", "资料显示，"]


def make_sentence(title, desc, idx):
    """把一条新闻写成完整、可朗读的句子；标题为主，摘要作补充。"""
    title = clean_text(title)
    if not title:
        return ""
    if not title[-1] in "。！？!?":
        title += "。"
    desc = clean_text(desc)
    if desc and len(desc) >= 12:
        overlap = sum(1 for w in desc[:6] if w in title[:10])
        if overlap < 4:
            if not desc[-1] in "。！？!?":
                desc += "。"
            lead = SUMMARY_LEAD[idx % len(SUMMARY_LEAD)]
            return f"{title}{lead}{desc}"
    return title


def narrate(items, opener):
    """把一组新闻串成连贯的一段话。空列表则安静跳过。"""
    items = [(clean_text(t), clean_text(d)) for t, d in items]
    items = [(t, d) for t, d in items if t]
    if not items:
        return ""
    parts = [opener]
    for i, (t, d) in enumerate(items):
        sent = make_sentence(t, d, i)
        if not sent:
            continue
        if i == 0:
            parts.append(sent)
        else:
            trans = ITEM_TRANS[(i - 1) % len(ITEM_TRANS)]
            parts.append(trans + sent)
    return "".join(parts)


def narrate_finance(stock_rows, btc, metals):
    segs = []
    if stock_rows:
        up = sum(c for _, _, c in stock_rows)
        mood = "整体走强" if up > 0 else ("集体承压" if up < 0 else "走势平稳")
        s = f"先说财经。隔夜美股三大指数{mood}，"
        for name, price, chg in stock_rows:
            t = "上涨" if chg >= 0 else "下跌"
            s += (f"{name}收报{num2cn(round(price))}点，"
                  f"{t}百分之{num2cn(round(abs(chg), 2))}；")
        segs.append(s.rstrip("；") + "。")
    if btc:
        usd, cny, chg = btc
        t = "上涨" if chg >= 0 else "下跌"
        segs.append(
            f"加密货币方面，比特币最新报{num2cn(round(usd))}美元，"
            f"约合人民币{num2cn(round(cny))}元，过去二十四小时"
            f"{t}百分之{num2cn(round(abs(chg), 2))}。")
    if metals:
        base, shuibei, silver = metals
        segs.append(
            f"贵金属这边，国内基础金价每克{num2cn(base)}元，"
            f"水贝足金批发价约每克{num2cn(shuibei)}元，"
            f"足银每克{num2cn(silver)}元。")
    if not segs:
        return ("财经方面，今天几路行情数据暂时没能及时拉取，"
                "建议您稍后打开行情软件查看。")
    return "".join(segs)


def map_topic(t):
    return {
        "DOTA2": "DOTA2 电竞 比赛",
        "单机游戏": "单机游戏 新作 发售",
        "游戏": "游戏 行业 新作",
        "国际": "国际 全球 新闻",
        "AI": "人工智能 AI 大模型 发布",
    }.get(t, t)


def build_briefing(topics):
    bj = datetime.timezone(datetime.timedelta(hours=8))
    now = datetime.datetime.now(bj)
    today = f"{now.year}年{now.month}月{now.day}日"
    weekday = "星期" + "一二三四五六日"[now.weekday()]

    speech = [(
        f"早上好，欢迎收听今天的新闻播报。今天是{today}，{weekday}，"
        f"我们为您梳理了财经、游戏、娱乐、国际、母婴、科技与科学方面"
        f"值得关注的动态，下面一起听。")]

    for cat, keys in CATS:
        matched = [t for t in topics if t in keys]
        if not matched:
            continue
        if cat == "财经":
            stock_rows = fetch_stocks() if "美股" in matched else []
            btc = fetch_btc() if "比特币" in matched else None
            metals = fetch_metals() if "黄金白银" in matched else None
            speech.append(narrate_finance(stock_rows, btc, metals))
            for t in matched:
                if t in ("美股", "比特币", "黄金白银"):
                    continue
                speech.append(narrate(fetch_news(t),
                                      FIN_OPENER.get(t, FIN_OPENER["_default"])))
        elif cat == "娱乐":
            speech.append(narrate(
                fetch_weibo_hot(),
                "娱乐方面，今天微博热搜榜上有几个话题热度很高，我们来聊几个。"))
        elif cat == "母婴":
            speech.append(narrate(
                fetch_mom_tips(),
                "忙碌之中，也给准妈妈们留一段实用提醒："))
        elif cat == "物理":
            speech.append(narrate(
                fetch_news("物理学 科研 突破 研究发现", max_items=1),
                "最后，用一则科学前沿的消息收个尾。"))
        else:
            for t in matched:
                q = map_topic(t)
                speech.append(narrate(
                    fetch_news(q),
                    OPENERS[cat].get(t, OPENERS[cat]["_default"])))

    speech.append(
        "以上就是今天的新闻播报。感谢您的收听，祝您一天好心情，"
        "我们明天同一时间再会。")
    text = "".join(speech)
    return text, text  # 文字版 == 音频版，逐字一致


# ---------------- TTS / 配置 / 推送 ----------------

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
