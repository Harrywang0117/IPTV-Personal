# 我的 IPTV 订阅

以 [Harrywang0117/IPTV](https://github.com/Harrywang0117/IPTV) 为基底，为 APTV 和支持 M3U 的安卓播放器整理少量可用线路。

- 主订阅只放本轮通过检测的赛事入口；昨天、今天、明天的完整赛程另存为 `schedule.md` / `schedule.json`，试看、登录、会员、待测和未开赛状态明确标注。
- 每频道默认一条、备用版最多两条。上次通过的入口优先复测，失效后才尝试其他来源，不因新源出现而整体换线。
- 每小时第 37 分钟刷新赛事；当天首次成功时也检测普通电视频道。任务延迟或跨日缺明日赛程时保留原文件，后续小时继续重试。
- 赛事回放至少二十分钟，并实际解码开头、50% 和 80% 位置各五秒。直播或无法确认总时长的入口每次解码十五秒，同一入口在三小时内两次通过、间隔至少二十分钟后才进入订阅。
- 识别已知咪咕试看跳转、登录和会员提示；HTTP 成功或短片段可解码不单独视为正赛可播。
- 每天北京时间 04:13 从公开体育索引和原项目列表发现新咪咕入口；检查现有源和候选源，新源至少两次相隔六小时通过三天目录及两个不同媒体入口的检测才入池。每日任务通常意味着观察至少一天。
- 新源只提供赛事候选，仍需逐条通过主订阅验收。连续两次巡检失败退出动态池，超过七十二小时未通过也不再使用；固定种子源继续复查以发现恢复。
- 三天赛程缺失、普通频道过少或骤减时拒绝发布。发布前独立核对文件与逐条检测证据，避免把预告或待测入口混入订阅。
- 今天保持一个分组，各日期按 `HH:MM` 排序。至少两个源支持时纠正同一内容编号的明显跨赛事分类冲突，保留播放地址。

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
| `public/all.m3u` | 推荐：电视频道和通过检测的赛事，每频道一条线路 |
| `public/all-backup.m3u` | 最多两条同名线路；APTV 请放入聚合配置以合并同名频道 |
| `public/events.m3u` | 只看通过检测的咪咕赛事 / 回放 |
| `public/channels.m3u` | 只看电视频道，不含赛事预告 |
| `public/all.txt` | 备用 TXT 格式；不能完整表达 M3U 的请求头和回看配置 |
| `public/schedule.md` / `public/schedule.json` | 三天完整赛程和限制状态，不含未验证播放链接 |
| `state/upstreams.json` / `reports/discovery.json` | 上游发现、观察期、入池和退出依据 |
| `public/status.json` | 最近一次成功发布的时间与验证统计 |

只添加推荐订阅或备用版其中之一，避免重复导入。播放器也需要开启订阅自动刷新。

## 验收范围

完整赛程见 [schedule.md](https://github.com/Harrywang0117/IPTV-Personal/blob/main/public/schedule.md)。明天及今天未开赛的场次只列在赛程中，不进入播放订阅。主订阅赛事可能明显少于完整赛程，这表示其他条目未通过要求，不会以完整目录冒充可观看。

检测只能证明生成机器在检测时能读取选定位置的视频，不能自动确认标题、画面双方或完整正赛内容，也不能保证播放器网络全场稳定。已知试看域名直接排除，但不能保证识别所有上游的权限限制方式。回放三段抽查不等于逐段检测整场。明确标为回放但无法核实时长的入口直接留在目录，不能改按直播标准放行。

`status.json` 分开记录三天列出数量、已验证、待观察、试看、会员、登录和失败数量；`state/event-evidence.json` 保存入口指纹与检测位置、时间，不保存跳转后的签名地址。`--events-only` 复用上次通过的普通频道并保留原 `channel_validation_at`。普通频道目前仍按三秒实际解码验收，每天重测一次。

新源“入池”只表示有资格提供候选，不代表它每场比赛都可播。两个公开索引不是全网搜索引擎，发现范围和每轮检查数量有限；索引可在 `config.json` 的 `discovery.feeds` 增补。候选只提取公开根入口加九位内容编号的格式，不扫描 IP 段、不使用带账号密码或令牌的地址，也不部署新的转播服务。

当前默认电视频道候选包含咪咕普通频道，以及原项目的央视、卫视、体育、港澳台和少儿分类。更多地方台 / 轮播可在 `config.json` 的 `base_groups` 中按需加入。没有通过解码的普通频道不进入新发布文件。

## 本地运行与维护

需要 Python 3.10+、FFmpeg 和 curl，没有额外 Python 依赖：

```sh
python3 -m unittest discover -s tests -v
python3 pipeline.py
python3 verify_publication.py
python3 discover.py
python3 pipeline.py --auto
python3 pipeline.py --events-only
python3 pipeline.py --events-only --check-event '阿斯顿维拉VS曼城'
```

定时与手动更新共用一个并发组。生成报告保存在 `reports/latest.json`，每日探查保存在 `reports/discovery.json`，无论通过或失败，云端都保留十四天验收报告。发布任务提交 `public/`、该报告和 `state/` 中的公开源清单缓存，推送冲突会失败，不强推覆盖人工修改。缓存中的赛事日期仍须属于昨天、今天或明天，不缓存跳转后的签名播放地址。新发现的入口须先经过观察期；仅抓取已配置公开索引中的明确入口，不跟随索引广告或任意网页链接。视频检测有总时限，不下载完整直播流。

`--auto` 按订阅时区和上次普通频道验收时间决定检测范围，避免定时任务延迟后错过当天全量更新。只有明天赛程缺失时返回状态码 3，自动任务将它记为“本轮未发布，等待上游”，继续保留原订阅；不会把它当作已经取得三天清单。排队任务执行时读取最新 `main`，减少旧版本推送冲突。

配置中的源站不是由本项目运营；可替换为你信任且可访问的同格式源。节目单来自配置里的独立 EPG 服务，节目单可用性不属于视频解码验收。

## 来源

参见 [NOTICE.md](NOTICE.md) 与 [GPL v3 许可证](LICENSE)。

GitHub 定时任务的延迟与六十天无活动停用规则见 [官方说明](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)。视频读取超时选项见 [FFmpeg 文档](https://www.ffmpeg.org/ffmpeg-protocols.html)；本项目还用进程总时限约束每次验证，避免持续流一直占住更新任务。
