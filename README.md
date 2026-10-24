# 我的 IPTV 订阅

以 [Harrywang0117/IPTV](https://github.com/Harrywang0117/IPTV) 为基底，为 APTV 和支持 M3U 的安卓播放器整理少量可用线路。

- 咪咕昨天回放、今天赛事、明天预告独立抓取，不依赖原仓库几段任务全部完成。
- 默认每频道一条线路；备用版每频道最多两条。先尝试不同服务器，解码成功后才选入。
- 每天北京时间 / 台北时间 00:37、06:37、12:37、18:37 更新；GitHub 定时任务可能延迟。
- 更新前实际解码约三秒视频及存在的音轨。HTTP 200、302、拿到 M3U8 均不算可播放验证。
- 今天的赛事统一放在“今天”分组；明天单独列为预告。赛前条目未计入已验证，开赛后的更新才测试它们。
- 没有昨天与今天的可解码赛事、没有明天赛程、频道过少或骤减时，拒绝发布并保留上次验收文件。失败原因在 Actions 报告里。

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

“已验证”只表示生成机器当时能够解码出连续画面。上游可能有地区、运营商、并发及回放保留期限制；这不能替代电视端持续播放，也不能自动证明画面与赛事标题完全一致。比赛开始前没有真实直播可验证，不承诺预告当下可看。

主订阅保留原始播放入口，不把短时跳转得到的签名地址固化。正常频道保留 M3U 中的台标、EPG ID、请求头、回看模板；赛事使用独立 ID，避免不同回放因上游共用 `tvg-id` 被播放器误合并。原始分组中的月日优先于“昨天 / 今天 / 明天”文字，过时备份不会冒充新赛程。

当前默认电视频道候选包含咪咕普通频道，以及原项目的央视、卫视、体育、港澳台和少儿分类。更多地方台 / 轮播可在 `config.json` 的 `base_groups` 中按需加入。没有通过解码的普通频道不进入新发布文件。

## 本地运行与维护

需要 Python 3.10+、FFmpeg 和 curl，没有额外 Python 依赖：

```sh
python3 -m unittest discover -s tests -v
python3 pipeline.py
```

定时与手动更新共用一个并发组。生成报告保存在 `reports/latest.json`，无论通过或失败，云端都保留十四天验收报告。发布任务只提交 `public/` 和该报告，推送冲突会失败，不强推覆盖人工修改。三个入口之外不会批量抓取 TVBox 客户端接口或下载完整直播流。

配置中的源站不是由本项目运营；可替换为你信任且可访问的同格式源。节目单来自配置里的独立 EPG 服务，节目单可用性不属于视频解码验收。

## 来源

参见 [NOTICE.md](NOTICE.md) 与 [GPL v3 许可证](LICENSE)。

GitHub 定时任务的延迟与六十天无活动停用规则见 [官方说明](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)。视频读取超时选项见 [FFmpeg 文档](https://www.ffmpeg.org/ffmpeg-protocols.html)；本项目还用进程总时限约束每次验证，避免持续流一直占住更新任务。
