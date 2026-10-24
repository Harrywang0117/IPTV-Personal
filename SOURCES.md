# 咪咕公开源调查

2026-10-24 核对。以下均是公开列表及入口，不代表由本项目运营，也不保证会员节目可以观看。

| 配置 | 来处 | 本轮观察 |
| --- | --- | --- |
| `migu-primary` | [原项目咪咕备份](https://github.com/Harrywang0117/IPTV/tree/main/tvbox/live) | 三天日期完整，昨日 CBA 5 场/17 条、今日中超 8 场/27 条；本机可解码部分真实回放 |
| `migu-alt` | [jn950/live 的公开列表](https://github.com/jn950/live/blob/main/tv/holive.m3u) | 文件快照较旧，但其公开入口仍返回当天三天赛事；可作补充，存在直播结束后只播放宣传片的情况 |
| `migu-backup` | [Lianyuge/tv-auto 的公开页面](https://github.com/Lianyuge/tv-auto/blob/main/index.html) | 当天清单仍在更新，连接可用性有波动；替换原本多项失败的备用入口 |

另外检查了多个公开列表中的入口，部分超时、DNS 失败、拒绝连接或并非播放列表，没有加入。三个已接入入口对上述 CBA 和中超的场次信息一致；源完整性与可观看权限仍应分开。

[develop202/migu_video](https://github.com/develop202/migu_video)、[akiralereal/iptv](https://github.com/akiralereal/iptv)、[showlo0327/migu-m3u](https://github.com/showlo0327/migu-m3u) 是可自托管的软件项目，不能把它们当成另一个现成、可匿名稳定使用的赛事源。本轮没有为它们部署服务或配置个人账号。

赛事时刻取自源中的标注，未逐场替换为官方开球时间。完整清单不保证上游没有漏赛；后续发现具体缺场应继续比较原始源与发布结果。

今晚维拉对曼城的原默认源明确要求登录，两组备用源曾输出正确赛前画面；22:00 后复查旧、新入口均未测通，部分提示会员或登录。现优先尝试两个备用源，但不能据此保证无会员观看这场正赛或回放。
