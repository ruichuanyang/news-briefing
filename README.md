# 每日新闻简报机器人（免费 · 微信推送 · AI 朗读）

每天自动抓你关心的新闻和行情，用 AI 念成一段语音，推送到你**个人微信**。
**全程免费、不需要服务器、不需要你长期开电脑。**

---

## 一、这套东西是怎么跑的（看懂这个就不慌）

```
每天早上 07:30（GitHub 自动运行，你电脑关着也跑）
  → 抓新闻/行情（美股、比特币、水贝金价银价、AI/DOTA2/单机新闻）
  → 生成简报文字 + 用微软免费语音念成 mp3
  → 音频传到 GitHub 免费网页空间
  → Server酱 把消息推到你微信（含简报 + 音频链接）
你上车 → 点微信里的链接 → 听一路
```

所有零件都是免费的：GitHub Actions（定时运行）、Edge TTS（微软免费朗读）、
Server酱（免费推微信）、GitHub Pages（免费存音频）。

---

## 二、部署步骤（全程网页操作，不用装软件）

### 第 1 步：注册 GitHub 账号
1. 打开 https://github.com ，点右上角 **Sign up**（注册）。
2. 填邮箱 → 设密码 → 取一个用户名 → 验证邮箱。免费，约 5 分钟。

### 第 2 步：新建一个仓库（放这些文件）
1. 登录后，点页面右上角 **+** → **New repository**。
2. Repository name 随便起，比如 `news-briefing`（用小写英文/数字）。
3. 选 **Public**（公开，GitHub Pages 免费需要公开）。
4. 勾 **Add a README file**（随便，后面会覆盖）。
5. 点 **Create repository**。

### 第 3 步：把本项目的文件传上去
最简单的方式——把本项目文件夹里的这几个文件**拖拽上传**到你的仓库：
- `main.py`
- `config.txt`
- `requirements.txt`
- `.github/workflows/daily.yml`（连同里面的文件夹一起传）

> 在 GitHub 仓库页面点 **Add file → Upload files**，把这几个文件拖进去，
> 写一句说明（比如“初始化”），点 **Commit changes** 即可。

### 第 4 步：开启 GitHub Pages（用来存音频）
1. 进仓库 **Settings → Pages**。
2. Source 选 **Deploy from a branch**，Branch 选 **gh-pages**，目录选 **/ (root)**。
3. 点 **Save**。第一次运行后会出现这个分支，不用急，跑一次脚本就有了。

### 第 5 步：注册 Server酱（拿到推微信的钥匙）
1. 打开 https://sct.ftqq.com ，用**微信扫码**登录。
2. 登录后页面会给你一串 **SendKey**（类似 `SCTxxxxx`）。
3. 微信里会关注一个“Server酱”服务号，以后消息就推到这里。

### 第 6 步：把钥匙填进仓库（避免泄露）
1. 进仓库 **Settings → Secrets and variables → Actions → New repository secret**。
2. Name 填 `SERVERCHAN_KEY`，Value 粘贴第 5 步的 SendKey，点 **Add secret**。
3. （可选）再建一个 secret：`VOICE`，值填 `yunxi` 或 `xiaoxiao` 切换音色。

### 第 7 步：开启自动运行 + 测一次
1. 进仓库 **Actions** 标签，如果提示启用，点 **I understand… enable**。
2. 点左侧 **每日新闻简报** → 右上 **Run workflow** → 点绿色运行。
3. 等 1–2 分钟，去微信看有没有收到消息。收到就成功了！

之后每天 07:30 会自动跑，无需任何操作。

---

## 三、怎么自定义“想听的内容”

打开仓库里的 **`config.txt`**，像改记事本一样：
- 不想听某一项，把那一行删掉，或在前面加 `#`。
- 想加新主题（比如“英超”“基金”），直接加一行关键词即可，
  新闻类会自动去搜最近两天的相关新闻。
- 改完点 **Commit changes** 保存，第二天生效。

> 默认已经配好你最初说的 6 项：AI、DOTA2、单机游戏、美股、比特币、黄金白银。

---

## 四、常见问题

- **微信收不到？** 检查 Server酱 SendKey 是否填对；去 sct.ftqq.com 看“发送记录”有没有报错。
- **音频点不开？** 确认第 4 步 Pages 已开启，且至少成功跑过一次（生成了 gh-pages 分支）。
- **某天新闻是空的？** 公开源偶尔抽风，脚本会自动跳过该项，不影响其他内容；第二天再试。
- **想换朗读声音？** 在 Secrets 里加 `VOICE` = `xiaoxiao`（女声）即可。
- **想改推送时间？** 改 `daily.yml` 里的 `cron`，示例 `30 23 * * *` = 北京 07:30；
  公式：北京时间 = UTC 时间 + 8 小时。

---

## 五、成本

**0 元。** 没有任何付费环节。GitHub / Server酱 / 微软 TTS / Pages 均有免费额度，日用远用不完。
