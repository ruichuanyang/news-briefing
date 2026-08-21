# 每日新闻播报 bot（口播版 · 重制 v6）

每天自动抓取新闻/行情 → 生成「新闻联播式」口播稿 → AI 朗读成 mp3 → 推送到微信。
全程免费：GitHub Actions 定时 + Edge TTS 朗读 + Server酱推微信。

## 部署（一次性）
1. 新建一个 **Public** 仓库，把这 5 个文件传上去（根目录，不要套文件夹）：
   `main.py` `config.txt` `requirements.txt` `README.md` `.github/workflows/daily.yml`
2. 注册 Server酱（https://sct.ftqq.com）拿到 SendKey。
3. 仓库 **Settings → Secrets and variables → Actions → New repository secret**：
   Name 填 `SERVERCHAN_KEY`，Value 填你的 SendKey。
4. 仓库 **Settings → Pages**：Source 选 `Deploy from a branch`，Branch 选 `gh-pages`、目录 `/ (root)`，Save。
   （`gh-pages` 分支在第一次跑 Actions 后自动出现，没出现就先跑一次再做这步。）
5. 仓库 **Actions** → 点「每日新闻简报」→ 右上 **Run workflow** 跑一次测试。

## 每天自动运行
`daily.yml` 里 `30 23 * * *` = 北京时间每天早上 07:30。不用开电脑。

## 改你想听的（网页改 config.txt 即可）
- 增删主题：删/加一行（如 `DOTA2`、`国际`、`母婴知识`）。
- 换声音：改 `voice=` 那行的代号（文件里有清单）。
- 调语速：改 `rate=`（如 `rate=-10%` 慢一点）。

改完保存（Commit changes），第二天生效；想立刻听就 Actions 跑一次。

## 关键设计
- 新闻全部来自 Google News 中文 RSS，天然无 HTML、无英文标题。
- 朗读前经多道清洗，绝不会念出 `a href=`、`<font>`、网址、广告词。
- 文字版与音频版用同一份数据，逐字一致。
