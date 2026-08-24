# 每日新闻播报 bot（口播版 · v8 · LLM 润色）

每天自动抓取新闻/行情 → **大模型挑重点 + 改写成口播** → Edge TTS 朗读成 mp3 → 推送到微信。
全程免费：GitHub Actions 定时 + GitHub Models 免费推理 + Edge TTS 朗读 + Server酱推微信。

## v8 相对 v7 的升级
1. **新闻不再边角料**：DOTA2 改用专项源（Liquipedia 近期变动）补 Google News，各板块候选数提到 6~8 条，给模型挑重点留足原料；模型会主动挑"真正大新闻"（如战队夺冠），不再漏掉重磅。
2. **口播更自然**：非财经板块（游戏/娱乐/国际/母婴/AI/物理）先过 GitHub Models 免费模型，挑 3 条左右写成连贯口播；不再像"念标题"。
3. **朗读更干净**：新增朗读清洗，剥掉《》""·—等会被 Edge TTS 误读的符号；并修复旧版误删 "A股/X平台" 等单独字母的问题。
4. **失败安全**：LLM 调用失败 / 没配 token / 返回异常 → 自动回退 v7 规则引擎，绝不让你早上收不到简报。

> 关于"大模型"的代价（你已知情）：免费档的提示词可能被用于模型改进（发的是公开新闻，敏感度低）；偶发 429 限流时自动回退规则引擎。GitHub Models 免费档有 150 次/天、8K 入/4K 出 token 上限，本机器人每天单次调用约 6~7 次，绰绰有余。

## 部署（一次性）
1. 新建一个 **Public** 仓库，把这 6 个文件传上去（根目录，不要套文件夹）：
   `main.py` `config.txt` `requirements.txt` `README.md` `.github/workflows/daily.yml`
2. 注册 Server酱（https://sct.ftqq.com）拿到 SendKey。
3. 仓库 **Settings → Secrets and variables → Actions → New repository secret**：
   Name 填 `SERVERCHAN_KEY`，Value 填你的 SendKey。
4. 仓库 **Settings → Pages**：Source 选 `Deploy from a branch`，Branch 选 `gh-pages`、目录 `/ (root)`，Save。
   （`gh-pages` 分支在第一次跑 Actions 后自动出现，没出现就先跑一次再做这步。）
5. 仓库 **Actions** → 点「每日新闻简报」→ 右上 **Run workflow** 跑一次测试。
   （v8 在 workflow 里已加 `models: read` 权限，无需你额外配置 LLM 密钥。）

## 每天自动运行
`daily.yml` 里 `30 23 * * *` = 北京时间每天早上 07:30。不用开电脑。

## 改你想听的（网页改 config.txt 即可）
- 增删主题：删/加一行（如 `DOTA2`、`国际`、`母婴知识`）。
- 换声音：改 `voice=` 那行的代号（文件里有清单）。
- 调语速：改 `rate=`（如 `rate=-10%` 慢一点）。
- **关掉 LLM 润色**：把 `llm=on` 改成 `llm=off`（退回纯规则引擎，等同 v7）。
- 换更强的模型：改 `llm_model=`（如 `gpt-4.1`）。

改完保存（Commit changes），第二天生效；想立刻听就 Actions 跑一次。

## 关键设计
- 数据源：美股(stooq/Yahoo)、贵金属(gold-api+汇率)、比特币(coingecko/coinbase)、DOTA2(Liquipedia+Google News)、微博热搜(vvhan)、新闻(Google News 中文 RSS)、母婴(内置知识库)。任一源失败自动跳过。
- 来源词（新浪/腾讯/各大高校新闻网等 80+ 个）全面剥离，文字与音频都不写不读。
- **文字版与音频版用同一份数据，逐字一致**（文字版保留书名号便于阅读，音频版经朗读清洗剥符号）。
- 朗读前经多道清洗，绝不会念出 `a href=`、`<font>`、网址、广告词、书名号；数字/百分比读法已校准（如 0.83% 读作"百分之零点八三"）。
