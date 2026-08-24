# AI 每日晨报

每天北京时间 07:00 由 GitHub Actions 启动：千问先联网检索、核验和筛选，再由千问写成一篇连贯的中文晨报；CosyVoice 将它合成为 MP3，最后通过方糖发到微信。设计目标是约 1,300 字、约 6 分钟，留出充足时间在 08:00 前送达。

## 成本与边界

- GitHub 私有仓库免费账户每月含 2,000 分钟 Actions；本任务通常每日只需数分钟。公开仓库的标准 Runner 则免费。
- 百炼联网搜索北京地域为 4 元/千次；本任务每天一次搜索会话。`cosyvoice-v3-flash` 为 0.8 元/万输入字符。按 1,300 字/天估算，语音约 **3.12 元/月**；模型与搜索通常不到 1 元/月，因此预留在 5 元预算内。
- 方糖免费账户每天 5 条，任务每天仅发送 1 条。
- GitHub 的定时工作流是尽力调度，不是 SLA；07:00 启动提供了约 1 小时缓冲。若未来要求严格的“必达 08:00”，应迁移到付费云函数/定时服务。
- 方糖消息中的音频使用百炼的临时签名链接，请在当天收听。文字稿会完整保留在微信消息中。

价格和产品能力以官方页面为准： [GitHub Actions](https://docs.github.com/en/billing/concepts/product-billing/github-actions)、[百炼模型与语音价格](https://help.aliyun.com/zh/model-studio/model-pricing)、[百炼联网搜索](https://help.aliyun.com/zh/model-studio/web-search/)、[方糖额度](https://sct.ftqq.com/docs/getting-started/faq/)。

## 部署

1. 将本目录新建为一个 GitHub 仓库并推送。建议先设为私有，避免公开你的兴趣主题。
2. 在阿里云百炼北京地域开通模型服务，创建 API Key，并确保 `qwen3.6-flash`、联网搜索和 CosyVoice 可调用。
3. 在仓库 **Settings → Secrets and variables → Actions** 添加以下 Secrets：

| Secret | 内容 |
| --- | --- |
| `DASHSCOPE_API_KEY` | 百炼北京地域 API Key |
| `SERVERCHAN_SENDKEY` | 你的方糖 Turbo SendKey |
| `DASHSCOPE_BASE_URL` | 可选；默认 `https://dashscope.aliyuncs.com/compatible-mode/v1`。若控制台给出工作空间专属 OpenAI 兼容地址，填该地址（以 `/v1` 结尾）。 |
| `DASHSCOPE_TTS_VOICE` | 可选；默认 `longanyang`。建议先在百炼声音设计中创建一次“沉稳、清晰、普通话标准的新闻播音男声”，把返回的 voice_id 填入这里。CosyVoice 创建音色免费。 |

4. 打开仓库 Actions，手动运行 **每日 AI 晨报** 一次。若成功收到消息，定时任务会自动继续。

## 修改你关注的话题

只编辑 [config/topics.json](config/topics.json)：修改、增加或删除 `topics` 数组的项目，然后提交。`instructions` 是给研究型 AI 的栏目编辑指令；它会在每天运行时立即生效，不需要改代码。

可以把 `target_chars` 调至 900–1,500。为保证月预算不超过 5 元，建议保持在 1,300 字或以下；它已远低于 10 分钟口播上限。

## 本地试运行

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export DASHSCOPE_API_KEY='你的百炼Key'
python src/morning_briefing.py --dry-run
```

`--dry-run` 不会合成语音或发送方糖消息，会把研究备忘和稿件写进 `run-output.json`，便于先检查文字质量。
