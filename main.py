#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日新闻播报 bot —— v8（LLM 润色版）

相对 v7 的改动：
1) 数据源升级：DOTA2 改用专项源（Liquipedia 近期变动）补 Google News，
   各新闻板块候选数提高到 6~8 条，给"挑重点"留足原料，解决边角料/漏大新闻。
2) 新增「LLM 润色层」（默认开启）：用 GitHub Models 免费模型（GPT-4o-mini / gpt-4.1）
   从候选里挑出真正重要、值得一听的 3 条左右，改写成前后连贯、能听下去的口播。
   完全免费、零密钥——复用 GitHub Actions 自带的 GITHUB_TOKEN（需 models: read 权限）。
3) 失败安全：LLM 调用失败 / 没配 token / 返回异常 → 自动回退到 v7 规则引擎，
   绝不让你早上收不到简报。
4) 朗读清洗：新增 clean_for_tts()，在最终文本上剥掉《》""·—等会被 Edge TTS
   误读的符号，并修复旧版 STRAY_RE 误删 "A股/X平台" 等单独字母的问题。

硬约束（任何接手者都必须遵守）：
- 文字版（md）与音频版（speech）用同一份字符串，逐字一致——先润色拼好整篇，再写 md、再喂 TTS。
- 朗读绝不出现：来源词、网址、a href=/<font> 等 HTML 标签、被念出来的标点符号/书名号。
- 不点名"第一条/第二条"，不堆机械过渡词，写成能听下去的一篇文章。
- 数字读法：0.83% → "百分之零点八三"（num2cn 已校准）。
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

# 广告/标题党词（不是来源词，用于候选过滤）
CLICKBAIT = re.compile(r"(震惊|突发|速看|重磅|全网|疯传|不敢相信|万万没想到|"
                       r"点击(此处|这里|查看)|阅读原文|关注我们|扫码|福利|限时|"
                       r"夺宝|娱乐网址|博彩|彩票|澳门|现金)", re.I)

# 标签 + 属性 + 十六进制色值 + 网址
TAG_RE = re.compile(r"<\s*/?[a-zA-Z][^>]*>")
ATTR_RE = re.compile(r"(?i)\b(?:href|target|rel|src|alt|style|color|"
                     r"width|height|class|id|title|align)\s*=\s*[^ \n<>,]*")
HEX_RE = re.compile(r"#\s*[0-9a-fA-F]{3,8}")
URL_RE = re.compile(r"https?://\S+|www\.\S+")

# 残留标签碎片：只在「看起来像 HTML 标签」的上下文才剥，避免误删 A股 / X平台 / B站
# 匹配：<...> 已由上 TAG_RE 处理；这里补「成对标签名」如 </a> <font> 的裸词，
# 但限定它前后是空白/标点/行首行尾，而不是中文字之间（A 字后面是中文字就不算）。
STRAY_RE = re.compile(
    r"(?<=[\s>「『（(，“。、])(?:/?a|/?font|/?span|/?div|/?br|/?img|/?p|"
    r"/?b|/?i|/?u|/?strong|/?em|/?table|/?tr|/?td|/?li|/?ul|/?ol|/?h[1-6])\b"
    r"(?=[\s<」』）).，、:：])",
    flags=re.I,
)


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
    """朗读前最后一道清洗：剥掉会被 Edge TTS 误读的符号/残留，保证"听得干净"。

    只动「影响语音」的东西，不动数字、不动文字内容：
    - 《》""''「」『』 书名引号 → 去掉（避免读作"左书名号"）
    - · — – … 等符号 → 去掉或替换（避免读作"间隔号/破折号"）
    - 仍兜底：残留的 < > 标签、网址、HTML 实体
    """
    if not text:
        return ""
    text = html.unescape(text)
    # 先清标签/网址（兜底）
    text = TAG_RE.sub(" ", text)
    text = ATTR_RE.sub(" ", text)
    text = HEX_RE.sub(" ", text)
    text = URL_RE.sub(" ", text)
    text = STRAY_RE.sub(" ", text)
    text = re.sub(r"[<>]", " ", text)
    # 书名号、引号、方头括号 → 直接去掉（不要留白导致粘连，用空）
    text = re.sub(r"[《》「」『』“”‘’]", "", text)
    # 间隔号 · 去掉；破折号/连接号 — – 去掉；省略号 … 去掉
    text = text.replace("·", "").replace("—", "").replace("–", "").replace("…", "")
    # 中文顿号、逗号、句号、问号、感叹号、冒号、分号 都是合法断句标点，Edge TTS 不读，保留
    # 英文标点转中文或去掉（避免读成 "comma"）
    text = text.replace(",", "，").replace(";", "；").replace(":", "：")
    text = re.sub(r"\s+", " ", text).strip()
    return text


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


def _google_news(q, max_items=6):
    """Google News 中文 RSS；标题剥来源。返回 [(title, desc), ...]。"""
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


def fetch_news(q, max_items=6):
    """对外新闻抓取：直接用 Google News RSS（候选数提到 6 条）。"""
    return _google_news(q, max_items=max_items)


def fetch_dota2_news(max_items=8):
    """DOTA2 专项源：优先 Liquipedia 近 48 小时变动页（赛事/战队权威），
    不足或失败时补 Google News『DOTA2 比赛 夺冠 战队 TI』。
    返回 [(title, desc), ...]，标题已清洗去来源。"""
    out = []
    try:
        # Liquipedia 的「近期变动」页面，含战队、赛事、选手动态
        url = ("https://liquipedia.net/dota2/api.php?action=feedrecentchanges"
               "&feedformat=atom&hours=48&limit=50")
        r = requests.get(url, headers=UA, timeout=20)
        feed = re.findall(r"<title>(.*?)</title>", r.text, flags=re.S)
        for t in feed:
            t = clean_text(t)
            if not t or t.lower().startswith("mediawiki") or "recent changes" in t.lower():
                continue
            if CLICKBAIT.search(t):
                continue
            out.append((t, ""))
            if len(out) >= max_items:
                break
        print(f"[dota2] Liquipedia 抓到 {len(out)} 条")
    except Exception as e:
        print(f"[dota2] Liquipedia 失败: {e}")

    # 不足则补 Google News 专项查询（夺冠/战队/TI 等关键词，命中大新闻概率更高）
    if len(out) < 4:
        extra = _google_news("DOTA2 比赛 夺冠 战队 TI", max_items=8)
        seen = {t for t, _ in out}
        for t, d in extra:
            if t not in seen:
                out.append((t, d))
                seen.add(t)
            if len(out) >= max_items:
                break
        print(f"[dota2] 补 Google News 后共 {len(out)} 条")
    return out[:max_items]


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


# ---------------- LLM 润色层（v8 新增）----------------

def call_llm(messages, model, timeout=40):
    """调用 GitHub Models 免费推理接口（OpenAI 兼容）。

    认证：复用 GitHub Actions 自带的 GITHUB_TOKEN（需 workflow 配 models: read）。
    本地没有 token 时直接抛异常，由上层回退到规则引擎。
    免费档限制：gpt-4o-mini 约 15 请求/分、150 请求/天、8K 入 / 4K 出 token。
    """
    import openai
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise RuntimeError("无 GITHUB_TOKEN，跳过 LLM 润色")
    client = openai.OpenAI(
        api_key=token,
        base_url="https://models.github.ai/inference",
    )
    resp = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0.4,
        max_tokens=900,
        timeout=timeout,
    )
    return resp.choices[0].message.content.strip()


SYS_PROMPT = (
    "你是一个中文新闻简报的口播编辑。用户开车时收听，要求："
    "1) 从提供的候选新闻里挑出【最值得听、最重要】的 3 条左右，跳过边角料、广告、标题党；"
    "2) 用自然流畅、前后连贯的中文口播写成一段（不要分点、不要出现'第一条/第二条'、"
    "不要出现'接下来的'等机械过渡词），像真人主播在说话；"
    "3) 不要出现任何来源媒体名、网址、HTML 标签或书名号；"
    "4) 数字和百分比照原样保留（如 0.83%），不要自己改成中文读法；"
    "5) 总字数控制在 200 字以内，信息密度高、不啰嗦。"
    "只输出改写后的口播正文，不要任何解释、前缀或 Markdown 格式。"
)


def polish_news_with_llm(cat_name, candidates, model):
    """给定板块名 + 候选新闻，让模型挑重点并改写成口播。失败抛异常由上层处理。"""
    if not candidates:
        raise RuntimeError("无候选可润色")
    lines = []
    for i, (t, d) in enumerate(candidates[:8], 1):
        line = f"{i}. 标题：{t}"
        if d:
            line += f"；摘要：{d}"
        lines.append(line)
    user = (f"板块：{cat_name}。以下是抓到的候选新闻（已去来源、去广告），"
            f"请挑重点改写成口播：\n" + "\n".join(lines))
    out = call_llm(
        [{"role": "system", "content": SYS_PROMPT},
         {"role": "user", "content": user}],
        model,
    )
    if not out or len(out) < 10:
        raise RuntimeError("LLM 返回过短，疑似异常")
    return out


# ---------------- 规则引擎（v7 保留，作为 LLM 失败回退）----------------

CATS = [
    ("财经", ["美股", "比特币", "黄金白银", "A股", "港股", "基金", "原油", "外汇", "债券"]),
    ("游戏", ["DOTA2", "单机游戏", "电竞", "Steam", "游戏"]),
    ("娱乐", ["微博热搜", "热搜", "娱乐", "娱乐八卦"]),
    ("国际", ["国际", "全球", "海外"]),
    ("母婴", ["母婴", "母婴知识", "孕期", "育儿"]),
    ("AI",   ["AI", "人工智能", "大模型", "科技"]),
    ("物理", ["物理", "物理学", "物理学前沿", "科学前沿"]),
]

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


def compose_news_rule(cat, matched):
    """v7 规则引擎拼装一个非财经板块（娱乐/国际/母婴/AI/物理/游戏）。
    作为 LLM 不可用时的回退。"""
    if cat == "游戏":
        parts = []
        for t in matched:
            if t == "DOTA2":
                items = fetch_dota2_news()
            elif t == "单机游戏":
                items = fetch_news("单机游戏 新作 发售", max_items=6)
            else:
                items = fetch_news(map_topic(t), max_items=6)
            parts.append(narrate(items, OPENERS["游戏"].get(t, OPENERS["游戏"]["_default"])))
        return "".join(parts)
    if cat == "娱乐":
        return narrate(fetch_weibo_hot(),
                       "娱乐方面，今天微博热搜榜上有几个话题热度很高，我们来聊几个。")
    if cat == "母婴":
        return narrate(fetch_mom_tips(),
                       "忙碌之中，也给准妈妈们留一段实用提醒：")
    if cat == "物理":
        return narrate(fetch_news("物理学 科研 突破 研究发现", max_items=6),
                       "最后，用一则科学前沿的消息收个尾。")
    # 国际 / AI
    if cat in OPENERS:
        parts = []
        for t in matched:
            q = map_topic(t)
            parts.append(narrate(fetch_news(q, max_items=6),
                                 OPENERS[cat].get(t, OPENERS[cat]["_default"])))
        return "".join(parts)
    return ""


# ---------------- 文章体组装 ----------------

def build_briefing(topics, llm_on=True, llm_model="gpt-4o-mini"):
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
                speech.append(narrate(
                    fetch_news(t, max_items=6),
                    FIN_OPENER.get(t, FIN_OPENER["_default"])))
            continue

        # —— 非财经板块：优先 LLM 润色，失败回退规则引擎 ——
        if cat == "游戏":
            candidates = []
            for t in matched:
                if t == "DOTA2":
                    candidates += fetch_dota2_news()
                elif t == "单机游戏":
                    candidates += fetch_news("单机游戏 新作 发售", max_items=6)
                else:
                    candidates += fetch_news(map_topic(t), max_items=6)
        elif cat == "娱乐":
            candidates = fetch_weibo_hot()
        elif cat == "母婴":
            candidates = fetch_mom_tips()
        elif cat == "物理":
            candidates = fetch_news("物理学 科研 突破 研究发现", max_items=6)
        else:  # 国际 / AI
            candidates = []
            for t in matched:
                candidates += fetch_news(map_topic(t), max_items=6)

        seg = ""
        if llm_on:
            try:
                seg = polish_news_with_llm(cat, candidates, llm_model)
                print(f"[llm][{cat}] 润色成功，{len(seg)} 字")
            except Exception as e:
                print(f"[llm][{cat}] 不可用，回退规则引擎: {e}")
                seg = ""
        if not seg:
            seg = compose_news_rule(cat, matched)
        speech.append(seg)

    speech.append(
        "以上就是今天的新闻播报。感谢您的收听，祝您一天好心情，"
        "我们明天同一时间再会。")

    # 关键：文字版与音频版共用同一个字符串
    text = "".join(speech)
    # 朗读前清洗（剥书名号/引号/间隔号等会被 TTS 误读的符号 + 兜底标签网址）
    speech_clean = clean_for_tts(text)
    return text, speech_clean


# ---------------- TTS / 配置 / 推送 ----------------

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

    # 文字版（md）：保留完整版（含书名号等，便于阅读）
    with open("public/briefing.md", "w", encoding="utf-8") as f:
        f.write(md)

    # 音频版（speech）：已做朗读清洗
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
