# 我的 IPTV 订阅

以 [Harrywang0117/IPTV](https://github.com/Harrywang0117/IPTV) 为基底，为 APTV 和支持 M3U 的安卓播放器整理少量可用线路。

- 咪咕昨天回放、今天赛事、明天预告独立抓取，不依赖原仓库几段任务全部完成。
- 普通电视频道默认一条已解码线路，备用版最多两条；咪咕赛事保留完整清单，已测通线路优先，未抽测或检测失败不会让整场赛事消失。
- 每天北京时间 / 台北时间 00:37 全量更新，06:37、12:37、18:37 刷新赛事；GitHub 定时任务可能延迟。
- 普通电视频道逐条解码；赛事按日期和比赛抽测，另行报告检测状态。HTTP 200、302、拿到 M3U8 均不算解码通过。
- 今天的赛事统一放在“今天”分组；明天单独列为预告。赛前条目未计入已验证，开赛后的更新才测试它们。
- 三天清单缺失、普通频道过少或骤减时拒绝发布，保留上次文件。云端到咪咕 CDN 超时会记警告与未验证状态，不能因此删掉赛事清单。
- 三个日期组各自按时间排序，名称以 `HH:MM` 开头；无时间条目放最后。明确返回会员提示的已抽测条目会标明。

## 订阅文件

APTV 和安卓播放器推荐直接添加：

```text
https://raw.githubusercontent.com/Harrywang0117/IPTV-Personal/main/public/all.m3u
```

只看体育：

```text
https://raw.githubusercontent.com/Harrywang0117/IPTV-Personal/main/public/events.m3u
```

备用版：[all-backup.m3u](https://raw.githubusercontent.com/Harrywang0117/IPTV-Personal/main/public/all-backup.m3u)。更新状态见 [status.json](https://raw.githubusercontent.com/Harrywang0117/IPTV-Personal/main/public/status.json) 和 [Actions](https://github.com/Harrywang0117/IPTV-Personal/actions)。

| 文件 | 内容 |
| --- | --- |
| `public/all.m3u` | 推荐：电视频道和三天赛事，每频道一条线路 |
| `public/all-backup.m3u` | 最多两条同名线路；APTV 请放入聚合配置以合并同名频道 |
| `public/events.m3u` | 只看咪咕赛事 / 回放 / 预告 |
| `public/channels.m3u` | 只看电视频道，不含赛事预告 |
| `public/all.txt` | 备用 TXT 格式；不能完整表达 M3U 的请求头和回看配置 |
| `public/status.json` | 最近一次成功发布的时间与验证统计 |

只添加推荐订阅或备用版其中之一，避免重复导入。播放器也需要开启订阅自动刷新。

## 验收范围

赛事清单完整与比赛可以观看是两种验收。`status.json` 分别记录场次、解说条目、解码通过、未抽测、未测通及会员提示；未验证条目不会被当成测通。源站可能只在直播期间提供正赛，结束后转为宣传片；媒体能解码也不能证明是那场比赛。需要会员的内容不能由本项目保证可播。

“解码通过”只表示生成机器当时能够解码出约三秒视频及存在的音轨。它不能替代电视端持续播放，也不能自动核实赛事标题。比赛开始前没有真实直播可验证。`--events-only` 复用上次通过验收的普通频道，并保留其 `channel_validation_at`，不会冒充当次重测。

主订阅保留原始播放入口，不把短时跳转得到的签名地址固化。正常频道保留 M3U 中的台标、EPG ID、请求头、回看模板；赛事使用独立 ID，避免不同回放因上游共用 `tvg-id` 被播放器误合并。原始分组中的月日优先于“昨天 / 今天 / 明天”文字，过时备份不会冒充新赛程。

当前默认电视频道候选包含咪咕普通频道，以及原项目的央视、卫视、体育、港澳台和少儿分类。更多地方台 / 轮播可在 `config.json` 的 `base_groups` 中按需加入。没有通过解码的普通频道不进入新发布文件。

## 本地运行与维护

需要 Python 3.10+、FFmpeg 和 curl，没有额外 Python 依赖：

```sh
python3 -m unittest discover -s tests -v
python3 pipeline.py
python3 pipeline.py --events-only
```

定时与手动更新共用一个并发组。生成报告保存在 `reports/latest.json`，无论通过或失败，云端都保留十四天验收报告。发布任务只提交 `public/` 和该报告，推送冲突会失败，不强推覆盖人工修改。只读取配置中的公开入口，不批量抓取 TVBox 客户端接口或下载完整直播流。

配置中的源站不是由本项目运营；可替换为你信任且可访问的同格式源。节目单来自配置里的独立 EPG 服务，节目单可用性不属于视频解码验收。

## 来源

参见 [NOTICE.md](NOTICE.md) 与 [GPL v3 许可证](LICENSE)。

GitHub 定时任务的延迟与六十天无活动停用规则见 [官方说明](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)。视频读取超时选项见 [FFmpeg 文档](https://www.ffmpeg.org/ffmpeg-protocols.html)；本项目还用进程总时限约束每次验证，避免持续流一直占住更新任务。
