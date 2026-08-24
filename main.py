#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日新闻播报 bot —— v9（全量 LLM 口播版）

核心架构变更（相对 v8.1）：
v8.1 的问题：每个板块独立调用 LLM 或规则引擎，再硬拼接成一篇。
结果就是"拼凑感强、语气不统一、过渡生硬、听得让人困"。

v9 的做法：
1) 先抓完所有板块的全部数据（财经/游戏/娱乐/国际/母婴/AI/物理），
   存成结构化"原料"。
2) 一次性把全部原料 + 用户话题偏好 + 已转中文的财经数字，
   喂给 LLM，让它像**真人早间电台主播**一样从头到尾写一整篇连贯口播。
3) 单次 LLM 失败 → 回退 v8.1 逐段方案 → 再失败 → 回退 v7 纯规则引擎。
4) 同一份文字同时写 md 和喂 TTS（逐字一致）。

保留 v8.1 的所有数据源改进：
- DOTA2 Liquipedia 专项源 + Google News 补充 + 中文过滤 + 广告过滤
- clean_for_tts 剥《》""·—_等符号
- STRAY_RE 不误删 A股/X平台/B站

硬约束：
- 文字版 == 音频版，逐字一致。
- 朗读不含来源词/网址/HTML标签/读音标点/书名号/下划线。
- 不点名"第几条"，不堆"接下来的"，写成连贯文章。
- 数字读法：0.83% → "百分之零点八三"。
"""

import os
import re
import html
import json
import asyncio
import datetime
import requests
import xml.etree.ElementTree as ET
from urllib.parse import quote

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

VOICE_MAP = {
    "yunxi": "zh-CN-YunxiNeural",
    "yunyang": "zh-CN-YunyangNeural",
    "yunjian": "zh-CN-YunjianNeural",
    "yunhao": "zh-CN-YunhaoNeural",
    "xiaoxiao": "zh-CN-XiaoxiaoNeural",
    "xiaoyi": "zh-CN-XiaoyiNeural",
    "yunxia": "zh-CN-YunxiaNeural",
    "xiaochen": "zh-CN-XiaochenNeural",
    "xiaohan": "zh-CN-XiaohanNeural",
    "xiaomeng": "zh-CN-XiaomengNeural",
    "xiaorui": "zh-CN-XiaoruiNeural",
    "xiaomo": "zh-CN-XiaomoNeural",
}

# ==================== 文本清洗 ====================

CLICKBAIT = re.compile(r"(震惊|突发|速看|重磅|全网|疯传|不敢相信|万万没想到|"
                       r"点击(此处|这里|查看)|阅读原文|关注我们|扫码|福利|限时|"
                       r"夺宝|娱乐网址|博彩|彩票|澳门|现金)", re.I)

TAG_RE = re.compile(r"<\s*/?[a-zA-Z][^>]*>")
ATTR_RE = re.compile(r"(?i)\b(?:href|target|rel|src|alt|style|color|"
                     r"width|height|class|id|title|align)\s*=\s*[^ \n<>,]*")
HEX_RE = re.compile(r"#\s*[0-9a-fA-F]{3,8}")
URL_RE = re.compile(r"https?://\S+|www\.\S+")

STRAY_RE = re.compile(
    r"(?<=[\s>「『（(，“。、])(?:/?a|/?font|/?span|/?div|/?br|/?img|/?p|"
    r"/?b|/?i|/?u|/?strong|/?em|/?table|/?tr|/?td|/?li|/?ul|/?ol|/?h[1-6])\b"
    r"(?=[\s<」』）).，、:：])",
    flags=re.I,
)

SPAM_RE = re.compile(
    r"(?i)(亚博|博彩|彩票|赌博|开户|注册送|充值返|客服|加微信|"
    r"扫码领|限时优惠|点击领取|免费领取|代理|招商|加盟|"
    r"欢迎你|真没有|网游命|黄宫官网)", re.I
)

_CN_CHAR_RE = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")


def has_chinese(text, min_ratio=0.3):
    if not text:
        return False
    cn = len(_CN_CHAR_RE.findall(text))
    total = len(text.replace(" ", ""))
    if total == 0:
        return False
    return (cn / total) >= min_ratio


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
    s = re.sub(r"[<>]", " ", s)
    s = re.sub(r"(?i)\b_?blank\b|\b_self\b", " ", s)
    s = re.sub(r"\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}\b", " ", s, flags=re.I)
    s = re.sub(r"\s*/\s*", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def clean_for_tts(text):
    """朗读前最后一道清洗：剥掉会被 Edge TTS 误读的符号/残留。"""
    if not text:
        return ""
    text = html.unescape(text)
    text = TAG_RE.sub(" ", text)
    text = ATTR_RE.sub(" ", text)
    text = HEX_RE.sub(" ", text)
    text = URL_RE.sub(" ", text)
    text = STRAY_RE.sub(" ", text)
    text = re.sub(r"[<>]", " ", text)
    text = re.sub(r"[《》「」『』“”‘’]", "", text)
    text = text.replace("·", "").replace("—", "").replace("–", "").replace("…", "")
    text = re.sub(r"[_*#]+", "", text)
    text = text.replace(",", "，").replace(";", "；").replace(":", "：")
    text = re.sub(r"\s+", " ", text).strip()
    return text


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


# ==================== 数字中文播报 ====================

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


# ==================== 数据源 ====================

def fetch_stocks():
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
    try:
        g = requests.get("https://api.gold-api.com/price/XAU", timeout=15).json()
        s = requests.get("https://api.gold-api.com/price/XAG", timeout=15).json()
        fx = requests.get("https://open.er-api.com/v6/latest/USD", timeout=15).json()
        usdcny = float(fx["rates"]["CNY"])
        g_cny_g = float(g["price"]) / 31.1035 * usdcny
        s_cny_g = float(s["price"]) / 31.1035 * usdcny
        base = round(g_cny_g)
        shuibei = base + 15
        return (str(base), str(shuibei), str(round(s_cny_g, 1)))
    except Exception as e:
        print(f"[metal] 失败: {e}")
        return None


def _google_news(q, max_items=8):
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
            if not title or CLICKBAIT.search(title) or SPAM_RE.search(title):
                continue
            if not has_chinese(title):
                continue
            out.append((title, desc))
            if len(out) >= max_items:
                break
        return out
    except Exception as e:
        print(f"[news] {q} 失败: {e}")
        return []


def fetch_news(q, max_items=8):
    return _google_news(q, max_items=max_items)


def fetch_dota2_news(max_items=8):
    out = []
    try:
        url = ("https://liquipedia.net/dota2/api.php?action=feedrecentchanges"
               "&feedformat=atom&hours=48&limit=50")
        r = requests.get(url, headers=UA, timeout=20)
        feed = re.findall(r"<title>(.*?)</title>", r.text, flags=re.S)
        for t in feed:
            t = clean_text(t)
            if not t or t.lower().startswith("mediawiki") or "recent changes" in t.lower():
                continue
            if CLICKBAIT.search(t) or SPAM_RE.search(t):
                continue
            if not has_chinese(t):
                continue
            out.append((t, ""))
            if len(out) >= max_items:
                break
        print(f"[dota2] Liquipedia 抓到 {len(out)} 条")
    except Exception as e:
        print(f"[dota2] Liquipedia 失败: {e}")

    if len(out) < 3:
        extra = _google_news("DOTA2 比赛 夺冠 战队 TI 国际邀请赛", max_items=8)
        seen = {t for t, _ in out}
        for t, d in extra:
            if t not in seen:
                out.append((t, d))
                seen.add(t)
            if len(out) >= max_items:
                break
    return out[:max_items]


def fetch_weibo_hot(max_items=8):
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
        if not t or CLICKBAIT.search(t) or SPAM_RE.search(t) or t in seen:
            continue
        if not has_chinese(t):
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


# ==================== LLM 全量写稿（v9 核心）====================

LLM_ENDPOINTS = [
    "https://models.inference.ai.azure.com",
    "https://models.github.ai/inference",
]


def call_llm(messages, model, timeout=60):
    """调用 GitHub Models 免费推理接口，双端点轮询。"""
    import openai
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise RuntimeError("无 GITHUB_TOKEN")
    last_err = None
    for endpoint in LLM_ENDPOINTS:
        try:
            client = openai.OpenAI(api_key=token, base_url=endpoint)
            resp = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=0.5,
                max_tokens=2000,
                timeout=timeout,
            )
            text = resp.choices[0].message.content.strip()
            print(f"[llm] 成功 endpoint={endpoint} model={model} 输出{len(text)}字")
            return text
        except Exception as e:
            last_err = e
            print(f"[llm] endpoint={endpoint} 失败: {e}")
            continue
    raise RuntimeError(f"所有 LLM 端点均失败: {last_err}")


SYSTEM_PROMPT_V9 = (
    "你是一个中文早间电台的新闻主播，听众正在开车上班路上听你的节目。\n\n"
    "写作要求：\n"
    "1. 把下面提供的所有原始信息，改写成一篇**前后连贯、一气呵成**的口播稿。"
    "   不要分板块、不要列条目、不要出现'第一条''第二点''接下来'之类的机械标记。\n"
    "2. 像真人聊天一样自然：有长短句变化、有语气起伏、偶尔可以加一句个人感慨或幽默点评。"
    "   段落之间用自然的过渡串起来（比如'说完这个，再看看科技圈最近在忙什么'），"
    "   不要生硬地切题。\n"
    "3. 信息取舍：从候选里挑**真正值得说**的 3~5 条重点（优先选大事件、有影响力的消息），"
    "   跳过边角料和广告。如果某个板块今天确实没什么大事，可以一句话带过或者干脆不提——"
    "   不要为了凑数硬塞没营养的内容。\n"
    "4. 绝对禁止：来源媒体名（新浪/腾讯/新华网等）、网址、HTML 标签、书名号《》。\n"
    "5. 数字和百分比照原样用（已转为中文读法），不要自己换算。\n"
    "6. 总长度控制在 400~600 字，信息密度高但不啰嗦。\n"
    "7. 开头用'早上好，欢迎收听今天的新闻播报'，结尾用'感谢您的收听，祝您一天好心情，明天同一时间再会'。\n\n"
    "只输出口播正文，不要任何解释、前缀、Markdown 格式。"
)


def build_full_prompt(topics, raw_data):
    """把所有原始数据组装成一次性的用户提示词。"""
    parts = []

    # 用户想听的话题
    parts.append(f"【听众关注的话题】{', '.join(topics)}")

    # 财经数据（已转中文，直接可用）
    finance_lines = []
    if raw_data.get("stocks"):
        for name, price, chg in raw_data["stocks"]:
            direction = "上涨" if chg >= 0 else "下跌"
            finance_lines.append(
                f"{name}收盘{num2cn(round(price))}点，{direction}百分之{num2cn(round(abs(chg), 2))}")
    if raw_data.get("btc"):
        usd, cny, chg = raw_data["btc"]
        direction = "上涨" if chg >= 0 else "下跌"
        finance_lines.append(
            f"比特币约{num2cn(round(usd))}美元（合人民币约{num2cn(round(cny))}元），"
            f"过去二十四小时{direction}百分之{num2cn(round(abs(chg), 2))}")
    if raw_data.get("metals"):
        base, shuibei, silver = raw_data["metals"]
        finance_lines.append(
            f"国内基础金价每克约{num2cn(base)}元，水贝足金批发价每克约{num2cn(shuibei)}元，"
            f"足银每克约{num2cn(silver)}元")
    if finance_lines:
        parts.append(f"【财经行情数据】{'；'.join(finance_lines)}")

    # 各板块新闻候选
    section_map = {
        "dota2": ("DOTA2 / 电竞", raw_data.get("dota2", [])),
        "games": ("单机游戏 / 游戏", raw_data.get("games", [])),
        "entertainment": ("微博热搜 / 娱乐", raw_data.get("entertainment", [])),
        "international": ("国际新闻", raw_data.get("international", [])),
        "mom": ("母婴 / 孕期知识", raw_data.get("mom", [])),
        "ai": ("AI / 人工智能 / 科技", raw_data.get("ai", [])),
        "physics": ("物理学 / 科学前沿", raw_data.get("physics", [])),
        "other_finance": ("其他财经", raw_data.get("other_finance", [])),
    }

    for key, (label, items) in section_map.items():
        if items:
            lines = []
            for i, (t, d) in enumerate(items[:10], 1):
                entry = f"  {i}. {t}"
                if d and len(d) >= 10:
                    entry += f"（{d[:120]}）"
                lines.append(entry)
            parts.append(f"【{label}】（以下为候选，请挑重要的说）\n" + "\n".join(lines))

    return "\n\n".join(parts)


def write_article_v9(topics, raw_data, model):
    """v9 核心一次性写稿：把全部原料交给 LLM 写一整篇连贯口播。"""
    user_prompt = build_full_prompt(topics, raw_data)
    return call_llm(
        [{"role": "system", "content": SYSTEM_PROMPT_V9},
         {"role": "user", "content": user_prompt}],
        model,
    )


# ==================== 回退方案（v8.1 逐段 + v7 纯规则）====================

CATS = [
    ("游戏", ["DOTA2", "单机游戏", "电竞", "Steam", "游戏"]),
    ("娱乐", ["微博热搜", "热搜", "娱乐", "娱乐八卦"]),
    ("国际", ["国际", "全球", "海外"]),
    ("母婴", ["母婴", "母婴知识", "孕期", "育儿"]),
    ("AI",   ["AI", "人工智能", "大模型", "科技"]),
    ("物理", ["物理", "物理学", "物理学前沿", "科学前沿"]),
]

OPENERS = {
    "游戏": {"DOTA2": "电竞这块先说 DOTA2，今天有几条值得一聊的消息。",
             "单机游戏": "单机游戏这边，",
             "_default": "游戏行业还有这些动向："},
    "国际": {"_default": "再把视野放到海外，今天国际上有几个看点。"},
    "AI": {"_default": "人工智能领域，最近动作不少，挑重要的说。"},
}

FIN_OPENER = {
    "A股": "回到国内，A股方面，", "港股": "港股这边，", "基金": "基金市场，",
    "原油": "原油方面，", "外汇": "外汇市场，", "债券": "债券市场，",
    "_default": "财经方面还有这些消息：",
}

ITEM_TRANS = [
    "另外，", "与此同时，", "值得关注的是，", "视线转到，", "另一边，",
    "此外，", "紧随其后的消息是，", "同样受到瞩目的还有，", "再把目光投向，",
    "说来也巧，", "与之呼应的是，", "还有一则，", "不妨也看看，",
    "接力这条消息，", "同一时间，", "镜头给到，", "顺着这个话题，",
    "也来关注，", "继续看，", "补充一条，", "同一赛道上，", "另一边传来，",
]
SUMMARY_LEAD = ["据了解，", "据相关报道，", "消息显示，", "有报道提到，", "资料显示，"]


def make_sentence(title, desc, idx):
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
    items = [(clean_text(t), clean_text(d)) for t, d in items]
    items = [(t, d) for t, d in items
             if t and not SPAM_RE.search(t) and has_chinese(t) and len(t) >= 5]
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


def compose_fallback(topics, raw_data):
    """v8.1 逐段回退方案：每个板块独立生成再拼接。"""
    bj = datetime.timezone(datetime.timedelta(hours=8))
    now = datetime.datetime.now(bj)
    today = f"{now.year}年{now.month}月{now.day}日"
    weekday = "星期" + "一二三四五六日"[now.weekday()]

    parts = [(
        f"早上好，欢迎收听今天的新闻播报。今天是{today}，{weekday}，"
        f"我们为您梳理了财经、游戏、娱乐、国际、母婴、科技与科学方面"
        f"值得关注的动态，下面一起听。")]

    # 财经
    parts.append(narrate_finance(
        raw_data.get("stocks", []),
        raw_data.get("btc"),
        raw_data.get("metals"),
    ))
    for t in topics:
        if t in ("美股", "比特币", "黄金白银"):
            continue
        if t in ("A股", "港股", "基金", "原油", "外汇", "债券"):
            parts.append(narrate(
                raw_data.get("other_finance", []) or fetch_news(t, max_items=6),
                FIN_OPENER.get(t, FIN_OPENER["_default"])))

    # 非财经
    for cat, keys in CATS:
        matched = [t for t in topics if t in keys]
        if not matched:
            continue
        if cat == "游戏":
            for t in matched:
                if t == "DOTA2":
                    items = raw_data.get("dota2", [])
                elif t == "单机游戏":
                    items = raw_data.get("games", [])
                else:
                    items = raw_data.get("games", []) or fetch_news(map_topic(t), max_items=6)
                parts.append(narrate(items, OPENERS["游戏"].get(t, OPENERS["游戏"]["_default"])))
        elif cat == "娱乐":
            parts.append(narrate(
                raw_data.get("entertainment", []),
                "娱乐方面，今天微博热搜榜上有几个话题热度很高，我们来聊几个。"))
        elif cat == "母婴":
            parts.append(narrate(
                raw_data.get("mom", []),
                "忙碌之中，也给准妈妈们留一段实用提醒："))
        elif cat == "物理":
            parts.append(narrate(
                raw_data.get("physics", []),
                "最后，用一则科学前沿的消息收个尾。"))
        elif cat in OPENERS:
            for t in matched:
                key = {"国际": "international", "ai": "ai"}.get(cat, cat)
                items = raw_data.get(key, []) or fetch_news(map_topic(t), max_items=6)
                parts.append(narrate(items, OPENERS[cat].get(t, OPENERS[cat]["_default"])))

    parts.append(
        "以上就是今天的新闻播报。感谢您的收听，祝您一天好心情，"
        "我们明天同一时间再会。")
    return "".join(parts)


# ==================== 主流程 ====================

def build_briefing(topics, llm_on=True, llm_model="gpt-4o-mini"):
    """v9 主流程：抓全量数据 → 单次 LLM 写整篇 → 失败逐级回退。"""

    # ===== Phase 1：抓取所有数据 =====
    print("[phase1] 开始抓取所有数据...")
    raw_data = {}

    # 财经
    raw_data["stocks"] = fetch_stocks() if "美股" in topics else []
    raw_data["btc"] = fetch_btc() if "比特币" in topics else None
    raw_data["metals"] = fetch_metals() if "黄金白银" in topics else None

    # 其他财经
    other_fin_topics = [t for t in topics if t in ("A股","港股","基金","原油","外汇","债券")]
    if other_fin_topics:
        raw_data["other_finance"] = []
        for t in other_fin_topics:
            raw_data["other_finance"] += fetch_news(t, max_items=4)

    # 游戏
    if any(t in topics for t in ("DOTA2","单机游戏","电竞","Steam","游戏")):
        if "DOTA2" in topics:
            raw_data["dota2"] = fetch_dota2_news()
        if "单机游戏" in topics:
            raw_data["games"] = fetch_news("单机游戏 新作 发售", max_items=6)
        if not raw_data.get("games") and any(t in topics for t in ("电竞","Steam","游戏")):
            raw_data["games"] = raw_data.get("games", []) or fetch_news("游戏 行业 新作", max_items=6)

    # 娱乐
    if any(t in topics for t in ("微博热搜","热搜","娱乐","娱乐八卦")):
        raw_data["entertainment"] = fetch_weibo_hot()

    # 国际
    if any(t in topics for t in ("国际","全球","海外")):
        raw_data["international"] = fetch_news("国际 全球 新闻", max_items=8)

    # 母婴
    if any(t in topics for t in ("母婴","母婴知识","孕期","育儿")):
        raw_data["mom"] = fetch_mom_tips()

    # AI
    if any(t in topics for t in ("AI","人工智能","大模型","科技")):
        raw_data["ai"] = fetch_news("人工智能 AI 大模型 发布 科技", max_items=8)

    # 物理
    if any(t in topics for t in ("物理","物理学","物理学前沿","科学前沿")):
        raw_data["physics"] = fetch_news("物理学 科研 突破 研究发现", max_items=6)

    total_items = sum(len(v) if isinstance(v, list) else 1 for v in raw_data.values() if v)
    print(f"[phase1] 数据抓取完成，共 {total_items} 条候选")

    # ===== Phase 2：LLM 一次性写整篇 =====
    text = ""
    if llm_on:
        try:
            text = write_article_v9(topics, raw_data, llm_model)
            if text and len(text) > 80:
                print(f"[v9] LLM 全量写稿成功，{len(text)} 字")
            else:
                print("[v9] LLM 返回过短，视为异常")
                text = ""
        except Exception as e:
            print(f"[v9] LLM 全量写稿失败: {e}")
            text = ""

    # ===== Phase 3：回退 —— 逐段方案 =====
    if not text:
        print("[fallback] 回退到逐段方案...")
        text = compose_fallback(topics, raw_data)

    # ===== Phase 4：输出 =====
    speech_clean = clean_for_tts(text)
    return text, speech_clean


# ==================== TTS / 配置 / 推送 ====================

async def tts(text, out_path, voice_key, rate="+0%"):
    import edge_tts
    voice = VOICE_MAP.get(voice_key, VOICE_MAP["yunxi"])
    await edge_tts.Communicate(text, voice, rate=rate).save(out_path)


def load_config(path="config.txt"):
    topics, voice, rate = [], "yunxi", "+0%"
    llm_on, llm_model = True, "gpt-4o-mini"
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
            elif low.startswith("llm="):
                llm_on = line.split("=", 1)[1].strip().lower() in ("on", "1", "true", "yes", "开")
            elif low.startswith("llm_model="):
                llm_model = line.split("=", 1)[1].strip() or "gpt-4o-mini"
            else:
                topics.append(line)
    return topics, voice, rate, llm_on, llm_model


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
    topics, voice, rate, llm_on, llm_model = load_config()
    md, speech = build_briefing(topics, llm_on=llm_on, llm_model=llm_model)

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
