#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 live下载器.py 每天备份到 tvbox/live/ 的直播源，按用途拆成多个精简的 M3U 订阅（iptv/*.m3u），
并生成 iptv/README.md 列出所有订阅地址和主播平台的实时地址。

APTV 在单个订阅里不合并同名频道；把订阅加进「聚合配置」后，同名频道会合并成一个频道，
其余线路作为备选、播放失败时自动切换。所以同一频道的多条线路用完全相同的名字，
按质量从好到差连续排列（排第一的就是默认线路）。

规则：
1. 只用最近 2 天内下载成功过的源（livelist.txt 第二列日期）
2. 去掉成人内容（源名、源内成人条目占比、分组名、频道名四层）、超过 12000 条的超大列表、公告类条目
3. 主播直播间（B站/快手/抖音/Twitch 等）白天大多已下播，不做快照，只在 README 里列出实时地址
4. 体育赛事只收咪咕的（咪咕「体育-今天/明天/昨天」按北京时间分今天/明天/昨天、按开赛时间排序）；
   所有订阅在写文件前都会逐条探测线路（见下方「线路探测」）：能播的排前面，明确失效的直接删，
   测不了的（超时/403/5xx）保留；咪咕代理 302 → miguvideo 算能播；
   其它来源的「赛事」条目不可靠（token 会过期、常有把 CCTV4 当斯诺克播的），不再收录
5. 电视频道按名称合并（CCTV-1 / CCTV1综合 / CCTV1 HD 视为同一频道），至少 2 个源收录才保留；
   外文频道一律不收（用户确认不要国际分类，国际体育也去掉）
6. 虎牙/斗鱼/YY 的「一起看/轮播」房间是每天变化的快照，不再生成订阅，只在 README 列出平台实时地址；
   其它轮播/回放/片名放进影视轮播
7. 每个频道最多 10 条线路：被越多源收录的地址越靠前，IPv6 地址放后面
8. 保留台标、节目单 id、回看（源头部的 catchup 下放到每条线路）和 UA 配置
"""

import argparse
import difflib
import ipaddress
import json
import os
import re
import socket
import ssl
import threading
import time
import unicodedata
from collections import Counter, defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

try:                # 只在探测时需要；没装 requests 也不影响生成订阅（自动跳过探测）
    import requests
except ImportError:
    requests = None
else:
    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except Exception:
        pass

REPO_ROOT = Path(__file__).resolve().parent.parent
LIVELIST_PATH = REPO_ROOT / "livelist.txt"
LIVE_DIR = REPO_ROOT / "tvbox" / "live"
LIVES_JSON = REPO_ROOT / "tvbox" / "海量直播线路.json"   # 用来按 URL 取回源的原始名称（文件名已去掉 🔞 等符号）
OUTPUT_DIR = REPO_ROOT / "iptv"
REPO_SLUG = os.environ.get("GITHUB_REPOSITORY", "Harrywang0117/IPTV")
RAW_BASE = f"https://raw.githubusercontent.com/{REPO_SLUG}/main/iptv/"
BEIJING = timezone(timedelta(hours=8))

MAX_AGE_DAYS = 2            # 源文件最近几天内更新过才用
MAX_SOURCE_CHANNELS = 12000 # 单个源超过这么多条，视为音乐/点播超大列表
ADULT_SOURCE_RATIO = 0.2    # 源内成人条目占比超过这个值，整个源不要
MIN_SOURCES = 2             # 电视频道至少在几个源里出现
MIN_SOURCES_VOD = 2         # 影视轮播条目至少在几个源里出现
MAX_LINES = 10              # 每个频道最多保留几条线路
EPG_COUNT = 3               # 头部写入几个节目单地址（取各源里最常用的）
# ---- 线路探测（生成订阅前逐条测试，见方案「改动 2」） ----
# 判定：ok = 200 且内容是 m3u8（#EXTM3U 开头）/视频数据（video/*、FLV/TS 头）/咪咕代理 302 → miguvideo；
#       dead = DNS 失败、连接被拒、404/410、HTML 错误页或「请登录后观看」类文字、咪咕代理 200 报错、
#              局域网/本机地址、协议拼写错误；
#       unknown = 超时、403/451、5xx、连接重置、rtmp/rtsp/p2p 等测不了的协议。
# unknown 不删：GitHub 的机器在海外，国内源常常只给国内 IP，按 dead 处理会误删好源。
# 本地调试可 IPTV_PROBE_EVENTS=0 关闭整个测试流程（沿用旧变量名）。
PROBE_ENABLED = os.environ.get("IPTV_PROBE_EVENTS", "1") != "0"
PROBE_TIMEOUT = 5            # 单地址探测总时长上限（秒）
PROBE_MAX_BYTES = 4096       # 首字节探测：只读前 4KB 就停，绝不下载完整视频流
PROBE_REDIRECTS = 3          # 跟随跳转最多几次
PROBE_WORKERS = 64           # 总并发
PROBE_HOST_WORKERS = 4       # 同一台服务器最多同时几个请求（防止被封，反而把好源测成坏的）
PROBE_HOST_FASTDEAD = 3      # 同 host 前 N 个都是 DNS 失败/连接被拒 → 这台剩余地址直接判 dead
PROBE_BUDGET = 1200          # 总时长预算（秒，20 分钟），可用 IPTV_PROBE_BUDGET 覆盖
PROBE_SAFE_MIN_OK = 200      # 安全阀：整体 ok 少于这么多条……
PROBE_SAFE_MIN_RATIO = 0.10  # ……或 ok 占比低于这个值，本轮完全不过滤（判定是网络本身出问题）
PROBE_UA = "okhttp/3.15"     # 咪咕代理等只认 TVBox 指纹
LOCAL_NETS_V4 = tuple(ipaddress.ip_network(n) for n in
                      ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
LOCAL_NETS_V6 = tuple(ipaddress.ip_network(n) for n in ("::1/128", "fc00::/7", "fe80::/10"))
# 能测的只有 http(s)；这些是真实存在但没法用 HTTP 探测的协议 → unknown（不删）
TESTABLE_SCHEMES = ("http", "https")
UNTESTABLE_SCHEMES = {"rtmp", "rtmps", "rtmpt", "rtmpe", "rtsp", "rtp", "mms", "mmsh", "mmst",
                      "p2p", "udp", "igmp", "ftp", "ftps", "ws", "wss", "file"}
HTML_HEAD_RE = re.compile(rb"^\s*(<!doctype\s+html|<html)", re.I)
LOGIN_HINTS = ("请登录", "登录后观看", "请先登录", "链接请求出错", "请求出错", "已失效", "不存在",
               "无权限", "没有权限", "节目已下架")

ALL_FILE = "all.m3u"      # 11 个订阅按频道名合并成一个，给电视/其它播放器一个链接即用

FILES = {   # 订阅文件 → 说明；每个控制在 3000 条线路以内，APTV 加载不卡
    "sports-events.m3u": "体育赛事·咪咕（今天/明天/昨天）",
    "sports.m3u": "体育频道",
    "cctv.m3u": "央视卫视",
    "hmt.m3u": "港澳台",
    "local-north.m3u": "地方台·华北东北西北",
    "local-east.m3u": "地方台·华东",
    "local-south.m3u": "地方台·中南西南",
    "kids.m3u": "少儿（频道 + 动画轮播）",
    "theme.m3u": "专题频道",
    "vod-movie.m3u": "影视轮播（电影/电视剧/纪录片）",
    "vod-show.m3u": "综艺轮播（综艺/相声小品/MV/其他）",
}
DEDUPE_ENABLED = os.environ.get("IPTV_DEDUPE", "1") != "0"   # 本地调试可关掉跨订阅去重
# 同一个播放地址只保留在优先级最高的订阅里，避免 APTV 把多个订阅一起聚合后出现重复线路。
# 顺序按「频道原本的主分类」排：赛事 > 央视/港澳台/地方/少儿/专题/轮播 > 体育（体育是交叉视图，放最后）。
DEDUPE_PRIORITY = ["sports-events.m3u", "cctv.m3u", "hmt.m3u",
                   "local-north.m3u", "local-east.m3u", "local-south.m3u",
                   "kids.m3u", "theme.m3u", "vod-movie.m3u", "vod-show.m3u",
                   "sports.m3u"]

REGION_NORTH = set("北京 天津 河北 山西 内蒙古 辽宁 吉林 黑龙江 陕西 甘肃 青海 宁夏 新疆".split())
REGION_EAST = set("上海 江苏 浙江 安徽 福建 江西 山东".split())
PROVINCES = ("北京 天津 河北 山西 内蒙古 辽宁 吉林 黑龙江 上海 江苏 浙江 安徽 福建 江西 山东 河南 湖北 湖南 "
             "广东 广西 海南 重庆 四川 贵州 云南 西藏 陕西 甘肃 青海 宁夏 新疆").split()
PLATFORMS = [("虎牙", r"虎牙"), ("斗鱼", r"斗鱼"), ("YY", r"YY"), ("B站", r"B站|哔哩|BL$")]
GROUP_ORDER = (["今天", "明天", "昨天",
                "央视", "央视付费", "卫视", "CGTN",
                "央视体育", "咪咕体育", "地方体育", "港澳台体育",
                "香港", "澳门", "台湾", "少儿频道", "动画轮播",
                "春晚", "4K超清", "NewTV", "iHOT", "SiTV", "电影频道", "剧场频道", "纪录频道", "音乐频道",
                "其他数字", "购物", "直播中国", "其他频道"]
               + [f"地方·{p}" for p in PROVINCES]
               + ["地方·其他", "电影轮播", "电视剧轮播", "综艺晚会", "相声小品", "纪录片", "演唱会·MV", "其他轮播"])

ADULT_RE = re.compile(
    r"🔞|18\+|成人|福利|伦理|倫理|情色|色情|三级|三級|18禁|探花|麻豆|精东|一本道|约炮|私密|国产精品|欧美精品|中色|小姐姐"
    r"|\bAV\b|porn|xxx|\bsex|fuck|\bfap\b|hentai|brazzers|playboy|hustler|milf|\banal\b|nude|naked|erotic|adult|babes"
    r"|hot ?guys|hot ?girls|взросл|эрот|美女展示|美女直播|美女热舞|热舞|黑丝|丝袜|TikTok美女", re.I)
# 按源的原始名称判断：「一起看」、轮播是 24 小时房间；其它直播平台是主播间
PLATFORM_VOD_RE = re.compile(r"一起看|轮播")
STREAMER_RE = re.compile(r"虎牙|斗鱼|B站|哔哩|BL$|抖音|快手|YY|Twitch|TikTok|百战|酷狗|来疯|直播间", re.I)
# 汇总源里转载的平台房间（原分组带这些字样）不是电视频道，归轮播
ROOM_GROUP_RE = re.compile(r"一起看|斗鱼|虎牙|YY|原创|轮播|解说|歌星|金曲|点歌|K歌")
MIRROR_OVERLAP = 0.6        # 两个源的地址重合度（相对较小的一个）超过这个值，算同一个来源的镜像
NOTICE_WORDS = ("公众号", "更新", "提示", "失效", "关注", "微信", "QQ", "TG", "Tg", "群", "购买",
                "联系", "广告", "仅供", "说明", "声明", "订阅", "赞助", "打赏", "官网", "网址", "刷新就是新内容",
                # 源站自带的广告/水印频道（「主页地址」「切莫相信」「无界接口」这类）
                "免费分享", "主页", "微博", "无界", "切莫", "谨防", "禁止", "接口")
NOT_STREAM_EXT = (".mp4", ".mov", ".mkv", ".avi", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".txt", ".json")

# ---- 体育赛事 ----
MIGU_RE = re.compile(r"咪咕")       # 赛事订阅只用咪咕的赛事源（其它来源的赛事 ID 不可靠）
SPORT_RE = re.compile(r"体育|體育|赛事|賽事|比赛|足球|篮球|籃球|联赛|聯賽|英超|西甲|意甲|德甲|法甲|欧冠|中超|NBA|CBA|NFL|NHL|MLB"
                      r"|斯诺克|台球|网球|排球|乒乓|羽毛球|F1|UFC|拳击|搏击|甲$|乙$|超$", re.I)
ESPORT_RE = re.compile(r"游戏|电竞|電競|英雄联盟|王者荣耀|和平精英|第五人格|LOL|DOTA|CS ?2|CSGO|VALORANT|无畏契约", re.I)
MATCH_RE = re.compile(r"(?<![A-Z])VS(?![A-Z])|对阵|全场回放|\d{1,2}:\d{2}|(?<!\d)\d{1,3}-\d{1,3}(?!\d)", re.I)
DAY_WORDS = {"昨天": -1, "昨日": -1, "今天": 0, "今日": 0, "明天": 1, "明日": 1}
DAY_RE = re.compile("|".join(DAY_WORDS))
DATE_RE = re.compile(r"(20\d{2})-(\d{1,2})-(\d{1,2})")
TIME_RE = re.compile(r"(\d{1,2}):(\d{2})")
OUT_OF_RANGE = 99

# ---- 轮播/回放/分集：按场次、分集或编号的条目 ----
VOD_ENTRY_RE = re.compile(r"回放|重播|点播|全场|预告|(?<![A-Z])VS(?![A-Z])|\d{1,2}:\d{2}$|20\d{2}-\d{2}-\d{2}"
                          r"|[一-鿿]\d{3,}$|[（(][上中下][)）]$", re.I)
VOD_RULES = [   # 用 源名 + 原分组 + 名称 判断，顺序即优先级
    ("动画轮播", r"动画|動畫|动漫|動漫|卡通|少儿|儿童|国漫|日漫|蜡笔小新|熊出没|喜羊羊|猫和老鼠|海绵宝宝|小猪佩奇"),
    ("相声小品", r"相声|相聲|小品|二人转|评书|曲艺|本山|德云"),
    ("演唱会·MV", r"演唱会|演唱會|MV|歌曲|歌星|金曲|音乐|音樂|点歌|DJ|KTV"),
    ("综艺晚会", r"综艺|綜藝|晚会|晚會|春晚|歌手|乘风|脱口秀|真人秀|选秀"),
    ("纪录片", r"纪录|紀錄|纪实|紀實|自然|动物|野生|探索|地理|科普|历史|歷史|人文|宇宙"),
    ("电视剧轮播", r"剧|劇"),
    ("电影轮播", r"电影|電影|影院|片|院线|动作|喜剧|科幻|恐怖|武侠|功夫|警匪|战争|影视"),
]

# ---- 电视频道分类 ----
TW_RE = (r"TVBS|台视|台視|臺視|中视|中視|华视|華視|民视|民視|东森|東森|中天|三立|纬来|緯來|八大|年代|寰宇|龙华|龍華|爱尔达|愛爾達"
         r"|大愛|大爱|公视|公視|好消息|靖天|靖洋|非凡|镜新闻|鏡新聞|中旺|大立|唯心|采昌|博斯|台湾|台灣|ELTA|MOMO|龍祥|龙祥|華藝|华艺")
MO_RE = r"澳门|澳門|澳视|澳視|澳亚|澳亞|莲花|蓮花"
HK_RE = (r"凤凰|鳳凰|翡翠|TVB|明珠|无线|無線|J2|VIUTV|(?<![A-Z])HOY|RTHK|(?<![A-Z])NOW|有线新闻|有線新聞|有线财经|有線財經"
         r"|香港|亚洲电视|亞洲電視|(?<![A-Z])ATV(?![A-Z])|华丽台|華麗台|欢喜台|歡喜台|千禧|天映|面包台|(?<![A-Z])MYTV|港台|耀才")
SPORT_CH_RE = (r"体育|體育|足球|篮球|籃球|NBA|CBA|赛事|ESPN|劲爆|高尔夫|网球|搏击|五星|SPORT|ELEVEN|DAZN|BEIN|EUROSPORT"
               r"|台球|斯诺克|博斯|竞赛|乒羽|武搏")
INTL_SPORT_RE = re.compile(r"DAZN|ELEVEN|BEIN|ESPN|EUROSPORT|SKY ?SPORT|FOX ?SPORT|NBA ?TV|NFL|NHL|MLB", re.I)
KIDS_RE = r"少儿|卡通|动画|动漫|儿童|兒童|炫动|哈哈|嘉佳|优漫|卡酷|青少|幼教|优优宝贝|宝贝"
THEME_RULES = [
    ("春晚", r"春晚|春节联欢"),
    ("4K超清", r"(^|[一-鿿])[48]K(?![A-Z])"),
    ("NewTV", r"^NEWTV"),
    ("iHOT", r"^IHOT"),
    ("SiTV", r"^SITV|都市剧场|欢笑剧场|游戏风云|生活时尚|金色学堂|法治天地|七彩戏剧|极速汽车|东方财经|全纪实"),
    ("电影频道", r"电影|電影|影院|CHC|影迷|院线"),
    ("剧场频道", r"剧场|劇場|电视剧|戏剧|戲劇|剧集|戏曲|戲曲"),
    ("纪录频道", r"纪实|紀實|纪录|紀錄|探索|发现|發現|地理|DISCOVERY|NATGEO|动物|求索"),
    ("音乐频道", r"音乐|音樂|MTV|CHANNELV|KTV|点歌"),
    ("其他数字", r"CIBN|黑莓|梨园|乐游|熊猫频道|华数|百视通|BESTV|书法|书画|国学|财富天下|睛彩"),
    ("购物", r"购物|好易购|聚鲨|快乐购"),
]
# 中文名像地方台：以台名常见后缀结尾（「徐州1」这类「地名+数字」多数是点播轮播，不收）
LOCAL_SUFFIX = ("新闻|综合|公共|都市|经济|生活|文体|文艺|科教|教育|文旅|农村|农业|娱乐|法制|法治"
                r"|民生|影视|旅游|购物|休闲|时尚|资讯|文化|频道|电视台|电视|台|[一二三四五六七八九]套|\d套"
                # 按 10-19 的真实数据补充：+频道 后缀大多已被 channel_key 去掉，只剩行业词
                r"|农科|农牧|城市|商务|自贸|陶瓷|紫砂|书法|书画|国学|海洋|藏语|十八"
                r"|经视|教科|政法|女性|导视|服务|财经|文娱|融媒|信息|气象|党建|乡村|农民|垄上|北方|留学|外语|汽摩|置业")
LOCAL_RE = re.compile(r"^(?=.*[一-鿿]).*(" + LOCAL_SUFFIX + r")$")
# 外语台（西里尔文/日文假名/韩文/阿拉伯文/泰文）——国内没人看，和国际体育一样不收
FOREIGN_SCRIPT_RE = re.compile(r"[\u0400-\u04FF\u3040-\u30FF\uAC00-\uD7AF\u0600-\u06FF\u0E00-\u0E7F]")
# 带国名的境外频道（「俄罗斯RT」「美国之音」这类名字里有中文、但并不是国内频道）
FOREIGN_COUNTRY_RE = re.compile(
    r"俄罗斯|俄国|日本|韩国|美国|英国|法国|德国|意大利|西班牙|葡萄牙|荷兰|波兰|乌克兰|白俄罗斯"
    r"|伊拉克|伊朗|罗马尼亚|朝鲜(?!语|族)|越南|泰国|印度|土耳其|阿拉伯|以色列|巴西|阿根廷|墨西哥|加拿大|澳大利亚|新西兰"
    r"|美国之音|VOA|BBC|CNN|NHK|KBS|MBC|阿里郎|JIMJAM", re.I)
# 「咪视界1」「咪咕视频-3」这类只有编号的咪咕付费频道，名字没有任何信息，留着只会污染订阅
PLACEHOLDER_RE = re.compile(r"^(咪视界|咪咕视频|咪咕直播)\d+$")
JUNK_NAME_RE = re.compile(r"^(测试|未知|演示)\d*$")
CJK_RE = re.compile(r"[一-鿿]")
# 名字里带这些词的才当「频道」兜底，节目名/片名/电台回轮播
CHANNEL_NAME_RE = re.compile(r"频道|頻道|电视|電視|卫视|衛視|TV|台$|直播|新闻|新聞|体育|體育|财经|財經|纪录|紀錄|少儿|少兒"
                             r"|购物|購物|影视|影視|音乐|音樂|剧场|劇場|电影|電影|旅游|旅遊|生活|综合|綜合|导视|導視", re.I)


def look_like_channel(key, name):
    """兜底判断：像电视频道（后缀像台名/带频道字样）才放进「其他频道」"""
    return bool(CJK_RE.search(name) and (LOCAL_RE.search(key) or CHANNEL_NAME_RE.search(name)))

HEADER_ATTRS = ("catchup", "catchup-source", "catchup-days")
ENTRY_ATTRS = ("tvg-id", "tvg-logo")
ATTR_RE = re.compile(r'([\w-]+)="([^"]*)"')


# 英文名的央视付费频道 → 中文名，才能和中文条目合并成一条多线路频道
CCTV_PAY_EN = {
    "STORMFOOTBALL": "风云足球",
    "WEAPON&TECHNOLOGY": "兵器科技",
    "NOSTALGIATHEATER": "怀旧剧场",
    "THEFIRSTTHEATER": "第一剧场",
    "GOLF&TENNIS": "高尔夫网球",
    "BILLIARDS": "央视台球",
    "HEALTH": "卫生健康",
    "STORMMUSIC": "风云音乐",
    "STORMTHEATER": "风云剧场",
}


def channel_key(name):
    """频道名 → 合并用的键：NFKC、去空白/符号/括号注释/清晰度后缀，CCTV 只看台号（CCTV4K 不并进 CCTV4）。"""
    n = unicodedata.normalize("NFKC", name).upper()
    n = re.sub(r"\[.*?\]|\(.*?\)|【.*?】|[\s\-_·•|]", "", n)
    n = re.sub(r"^央视网", "", n)                    # 央视网CCTV11 → CCTV11
    n = n.replace("娛樂", "娱乐")
    # 央视 4K/8K：CCTV16 4K / CCTV4K真4K / cctv4k-av3a / cctv8k-av3a 统一成 3 个频道
    if "CCTV" in n and "16" in n and "4K" in n:
        return "CCTV16 4K"
    if "CCTV" in n and "8K" in n:
        return "CCTV8K"
    if "CCTV" in n and "4K" in n:
        return "CCTV4K"
    if re.search(r"CCTV.*少儿|^央视少儿", n):
        return "CCTV14"
    for en, zh in CCTV_PAY_EN.items():
        if n == "CCTV" + en:
            return "CCTV" + zh
    if re.search(r"CCTV.*台球|^央视台球", n):
        return "CCTV央视台球"
    if re.search(r"央视高网|CCTV.*高网|高尔夫网球", n):
        return "CCTV高尔夫网球"
    if "兵器" in n:
        return "CCTV兵器科技"
    if re.search(r"文化精品|央视文化$|央视精品", n):
        return "CCTV文化精品"
    if "中学生" in n:
        return "CCTV中学生"
    if "老故事" in n:
        return "CCTV老故事"
    if "发现之旅" in n:
        return "CCTV发现之旅"
    m = re.match(r"CCTV(5\+|\d{1,2}\+?)(?![0-9]*K)", n)
    if m:
        return "CCTV" + m.group(1)
    m = re.match(r"(CETV|中国教育)0?([1-5])(?!\d)", n)
    if m:
        return "CETV" + m.group(2)
    return re.sub(r"(超清|超高清|高清|标清|蓝光|FHD|UHD|HD|1080P|720P|频道)$", "", n) or n


def is_notice(name, url):
    path = url.split("?", 1)[0].lower()
    return any(w in name for w in NOTICE_WORDS) or path.endswith(NOT_STREAM_EXT)


def sport_event_offset(name, group, today):
    """体育赛事返回相对今天的天数（-1/0/1；超出范围返回 OUT_OF_RANGE），不是赛事返回 None。"""
    text = name + "|" + group
    if ESPORT_RE.search(text) or not SPORT_RE.search(text) or not MATCH_RE.search(name):
        return None
    m = DAY_RE.search(group)
    if m:
        return DAY_WORDS[m.group(0)]
    m = DATE_RE.search(name)
    if m:
        try:
            offset = (datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date() - today).days
        except ValueError:
            return None
        return offset if -1 <= offset <= 1 else OUT_OF_RANGE
    return 0   # 没有日期信息的一般是正在直播的比赛


def province_of(name, groups):
    for p in PROVINCES:
        if p in name:
            return p
    hits = Counter()
    for g, n in groups.items():
        for p in PROVINCES:
            if p in g:
                hits[p] += n
    return hits.most_common(1)[0][0] if hits else None


def first_match(rules, text, default=None):
    for label, pattern in rules:
        if re.search(pattern, text):
            return label
    return default


def classify_channel(key, name, groups):
    """电视频道 → [(文件, 分组)]；不像电视直播的返回 []（交给兜底「其他频道」）。"""
    if "中央电视塔" in key or "中央电视塔" in name:
        return [("theme.m3u", "直播中国")]           # 中央电视塔的景区机位，不是央视
    sport = re.search(SPORT_CH_RE, key)
    if key.startswith("CGTN"):
        return [("cctv.m3u", "CGTN")]
    if re.search(r"^(CCTV|CETV)|央视|中央(电视(?!塔)|新影)|中国教育", key):
        main = "央视" if re.match(r"CCTV(?:\d{1,2}\+?|[48]K|16 4K)$|CETV|中国教育", key) else "央视付费"
        return [("cctv.m3u", main)] + ([("sports.m3u", "央视体育")] if sport else [])
    region = first_match([("台湾", TW_RE), ("澳门", MO_RE), ("香港", HK_RE)], key)
    if region:
        return [("hmt.m3u", region)] + ([("sports.m3u", "港澳台体育")] if sport else [])
    if re.search(r"卫视|衛視", key):
        return [("cctv.m3u", "卫视")]
    if FOREIGN_COUNTRY_RE.search(name):               # 境外频道不收（和国际体育一致）
        # 名字像影视条目（「印度片系列」）的交给轮播规则，别当境外频道丢掉
        if re.search(r"系列|合集|电影|電影|剧|劇|片|剧场|劇場|回放|重播|完整版|全集", name):
            return []
        return [(None, "外文")]
    if re.search(r"(?<![A-Z])(FOX|HITS|WWE)(?![A-Z])", key):
        return [("theme.m3u", "其他频道")]            # 境外但带中文名的频道，不塞进地方台
    if sport:
        if not CJK_RE.search(key) or INTL_SPORT_RE.search(key):
            return [(None, "国际体育")]             # 国际体育（beIN/ESPN/DAZN 等）按用户要求不再收录
        sub = "咪咕体育" if "咪咕" in key else "地方体育"
        return [("sports.m3u", sub)]
    if re.search(KIDS_RE, key):
        return [("kids.m3u", "少儿频道")]
    theme = first_match(THEME_RULES, key)
    if theme:
        return [("theme.m3u", theme)]
    if any("直播中国" in g for g in groups):
        return [("theme.m3u", "直播中国")]
    if LOCAL_RE.search(key):
        return [("local.m3u", f"地方·{province_of(name, groups) or '其他'}")]
    if not CJK_RE.search(key):
        return [(None, "外文")]                 # 体育以外的外文频道不收
    return []


def route(fname, group):
    """分类结果 → 最终订阅文件（大类再按地区/类型拆开）"""
    if fname == "local.m3u":
        prov = group.split("·", 1)[1]
        return ("local-north.m3u" if prov in REGION_NORTH
                else "local-east.m3u" if prov in REGION_EAST else "local-south.m3u")
    if fname == "vod.m3u":
        return "vod-movie.m3u" if group in ("电影轮播", "电视剧轮播", "纪录片") else "vod-show.m3u"
    return fname


def parse_m3u(text):
    """返回 (头部属性, [(名称, 分组, 属性, 附加选项行, 地址)])"""
    header, entries = {}, []
    info, opts = None, []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#EXTM3U"):
            for k, v in ATTR_RE.findall(line):
                header.setdefault(k, v)
        elif line.startswith("#EXTINF"):
            attrs = dict(ATTR_RE.findall(line.split(",", 1)[0]))
            info = (line.rsplit(",", 1)[-1].strip(), attrs)
            opts = []
        elif line.startswith(("#EXTVLCOPT", "#KODIPROP")):
            opts.append(line)
        elif line.startswith("#"):
            continue
        elif "://" in line and info:
            name, attrs = info
            entries.append((name, attrs.get("group-title", ""), attrs, opts, line))
            info = None
    return header, entries


def parse_txt(text):
    """TVBox txt：「分组,#genre#」+「频道名,地址[#地址2][$备注]」"""
    entries, group = [], ""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith(",#genre#"):
            group = line[:-len(",#genre#")]
            continue
        if "," not in line:
            continue
        name, urls = line.split(",", 1)
        for url in urls.split("#"):
            url = url.split("$", 1)[0].strip()
            if "://" in url:
                entries.append((name.strip(), group, {}, [], url))
    return {}, entries


LEGACY_HEADER_RE = re.compile(r'#EXTM3U\r?\n#EXTINF:-1 tvg-name="[^"\n]*",[^\n]*\r?\n(?![a-zA-Z][\w+.-]*://)')


def strip_legacy_header(text):
    """旧版 live下载器 会在每个文件开头插入「#EXTM3U + 一行没有地址的 #EXTINF」，去掉它"""
    m = LEGACY_HEADER_RE.match(text)
    return text[m.end():] if m else text


def load_lives(lives_json):
    if not lives_json.is_file():
        return []
    return json.loads(lives_json.read_text(encoding="utf-8")).get("lives", [])


def load_sources(livelist_path, live_dir, lives, today):
    """读 livelist.txt，返回最近更新过的源：[(文件名, 源原始名称, UA, 头部属性, 条目)]"""
    names = {l["url"]: l["name"] for l in lives}
    cutoff = (today - timedelta(days=MAX_AGE_DAYS)).strftime("%Y%m%d")
    sources, seen = [], set()
    for row in livelist_path.read_text(encoding="utf-8").splitlines():
        parts = row.split("|")
        if len(parts) < 6 or parts[0] in seen or parts[1] < cutoff:
            continue
        seen.add(parts[0])
        path = live_dir / parts[0]
        if not path.is_file():
            continue
        text = strip_legacy_header(path.read_text(encoding="utf-8", errors="replace"))
        header, entries = parse_m3u(text) if "#EXTINF" in text else parse_txt(text)
        ua = "" if parts[5] in ("", "null") else parts[5]
        sources.append((parts[0], names.get(parts[3], parts[0]), ua, header, entries))
    return sources


def streamer_links(lives):
    """主播平台的实时地址（原始订阅地址，打开就是当前在播的房间）"""
    out, seen = [], set()
    for l in lives:
        url = l.get("url", "")
        if (STREAMER_RE.search(l["name"]) and not PLATFORM_VOD_RE.search(l["name"])
                and url.startswith("http") and "127.0.0.1" not in url and url not in seen):
            seen.add(url)
            out.append((l["name"], url))
    return out


def new_bucket():
    return {"names": Counter(), "attrs": defaultdict(Counter), "groups": Counter(), "texts": Counter(),
            "sources": set(), "families": set(), "lines": {}, "order": None}


def add_line(bucket, fname, fam, source, name, group, attrs, opts, url, header, ua, order):
    bucket["names"][name] += 1
    bucket["groups"][group] += 1
    bucket["texts"][source] += 1
    bucket["sources"].add(fname)
    bucket["families"].add(fam)
    if bucket["order"] is None:
        bucket["order"] = order
    for k in ENTRY_ATTRS:
        if attrs.get(k):
            bucket["attrs"][k][attrs[k]] += 1
    if url not in bucket["lines"]:
        catchup = {k: attrs.get(k) or header.get(k, "") for k in HEADER_ATTRS}
        ua_opt = [f"#EXTVLCOPT:http-user-agent={ua}"] if ua and not any("user-agent" in o for o in opts) else []
        bucket["lines"][url] = (catchup, opts + ua_opt)


def merge_bucket(dst, src):
    for k in ("names", "groups", "texts"):
        dst[k].update(src[k])
    dst["sources"] |= src["sources"]
    dst["families"] |= src["families"]
    dst["order"] = min(x for x in (dst["order"], src["order"]) if x is not None)
    for k, c in src["attrs"].items():
        dst["attrs"][k].update(c)
    for url, v in src["lines"].items():
        dst["lines"].setdefault(url, v)


def mirror_families(sources):
    """地址高度重合的源（镜像、互相转载）归为同一个来源，返回 {文件名: 来源 id}"""
    urls = {s[0]: {e[4] for e in s[4]} for s in sources}
    fam = {}
    for f in sorted(urls, key=lambda f: -len(urls[f])):
        fam[f] = next((g for g in fam if fam[g] == g and urls[f]
                       and len(urls[f] & urls[g]) / min(len(urls[f]), len(urls[g])) >= MIRROR_OVERLAP), f)
    return fam


def build(sources, today):
    """返回 (节目单地址列表, {文件: [(分组, 排序键, 显示名, 属性, [((回看属性, 选项行), 地址)])]}, 统计)"""
    stats = Counter()
    family = mirror_families(sources)
    epg = Counter()
    tv, vod, platform = defaultdict(new_bucket), defaultdict(new_bucket), defaultdict(new_bucket)
    events = defaultdict(new_bucket)            # (相对天数, 赛事名) → bucket（只放咪咕）
    url_sources = defaultdict(set)
    order = 0

    for fname, source, ua, header, entries in sources:
        if ADULT_RE.search(source):
            stats["跳过成人源"] += 1
            continue
        is_platform_vod = bool(PLATFORM_VOD_RE.search(source))
        if not is_platform_vod and STREAMER_RE.search(source):
            stats["主播直播间源（改用实时地址）"] += 1
            continue
        if len(entries) > MAX_SOURCE_CHANNELS:
            stats["跳过超大源"] += 1
            continue
        adult_n = sum(1 for e in entries if ADULT_RE.search(e[0] + "|" + e[1]))
        if entries and adult_n / len(entries) > ADULT_SOURCE_RATIO:
            stats["跳过成人源"] += 1
            continue
        stats["使用的源"] += 1
        for u in (header.get("x-tvg-url") or header.get("url-tvg") or "").split(","):
            if u.strip():
                epg[u.strip()] += 1
        for name, group, attrs, opts, url in entries:
            order += 1
            if ADULT_RE.search(name + "|" + group):
                stats["跳过成人频道"] += 1
                continue
            if is_notice(name, url) or "更新时间" in group:
                stats["跳过公告条目"] += 1
                continue
            url_sources[url].add(family[fname])
            if is_platform_vod:
                bucket = platform[(first_match(PLATFORMS, source, "其他"), channel_key(name))]
            else:
                offset = sport_event_offset(name, group, today)
                if offset is not None and not (MIGU_RE.search(source) or MIGU_RE.search(fname) or MIGU_RE.search(group)):
                    # 非咪咕的赛事来源（潇雨/裤佬等）token 会过期，第二天就 404/403，不再收录
                    stats["跳过非咪咕赛事"] += 1
                    continue
                elif offset == OUT_OF_RANGE:
                    stats["跳过不在昨天到明天的赛事"] += 1
                    continue
                elif offset is not None:
                    bucket = events[(offset, re.sub(r"\s+", " ", name).strip())]
                elif VOD_ENTRY_RE.search(name) or VOD_ENTRY_RE.search(group) or ROOM_GROUP_RE.search(group):
                    bucket = vod[channel_key(name)]
                else:
                    bucket = tv[channel_key(name)]
            add_line(bucket, fname, family[fname], source, name, group, attrs, opts, url, header, ua, order)
    platform_keys = {key for _, key in platform}
    for key in [k for k in vod if k in tv]:
        merge_bucket(tv[key], vod.pop(key))     # 同一频道有的源放在轮播分组里，线路并到一处

    def pick_lines(bucket):
        urls = sorted(bucket["lines"], key=lambda u: (u.startswith(("http://[", "https://[")),
                                                     -len(url_sources[u])))[:MAX_LINES]
        return [(bucket["lines"][u], u) for u in urls]

    def attrs_of(bucket):
        return {k: c.most_common(1)[0][0] for k, c in bucket["attrs"].items()}

    def popular(key, b):
        return (-len(b["families"]), -len(b["sources"]), b["order"], key)

    out = defaultdict(list)

    def emit_tv(key, b, placements):
        """把电视频道按分类结果写进 out；返回 False 表示被过滤掉了"""
        name = b["names"].most_common(1)[0][0]
        if placements[0][0] is None:
            stats["跳过国际体育" if placements[0][1] == "国际体育" else "跳过外文频道"] += 1
            return False
        # 国内频道按源的个数计（咪咕这类只有一家、但有多个镜像的也要保住）
        if len(b["sources"]) < MIN_SOURCES:
            stats["跳过收录源太少的频道"] += 1
            return False
        for fname, group in placements:
            m = re.match(r"CCTV(\d+)(\+?)", key)
            sk = (0, int(m.group(1)), m.group(2), key) if group in ("央视", "央视体育") and m else popular(key, b)
            out[fname].append((group, sk, name, attrs_of(b), pick_lines(b)))
        return True

    for key, b in tv.items():
        if key in platform_keys:
            stats["与平台轮播重复"] += 1
            continue
        name = b["names"].most_common(1)[0][0]
        placements = classify_channel(key, name, b["groups"])
        if not placements:
            merge_bucket(vod[key], b)   # 先并进轮播桶，下面再用轮播规则/频道规则二次判断
            continue
        emit_tv(key, b, placements)

    def is_vod_entry(bucket, name):
        """轮播桶里的条目：原名/原分组本身带场次、分集、轮播信号 → 确实是影视轮播"""
        return (VOD_ENTRY_RE.search(name) is not None
                or any(ROOM_GROUP_RE.search(g) or VOD_ENTRY_RE.search(g) for g in bucket["groups"]))

    for key, b in vod.items():
        if key in platform_keys:
            stats["与平台轮播重复"] += 1
            continue
        name = b["names"].most_common(1)[0][0]
        group = first_match(VOD_RULES, " ".join(list(b["texts"]) + list(b["groups"]) + [name]))
        if group is None:                       # 没命中轮播规则：可能是被错放进轮播的电视频道
            if not is_vod_entry(b, name):
                placements = classify_channel(key, name, b["groups"])
                if placements:
                    emit_tv(key, b, placements)
                    continue
                if (PLACEHOLDER_RE.search(key) or JUNK_NAME_RE.search(name) or FOREIGN_SCRIPT_RE.search(name)
                        or FOREIGN_COUNTRY_RE.search(name) or not CJK_RE.search(name)):
                    stats["跳过无法归类的频道"] += 1
                    continue
                if re.search(r"「[^」]*(B站|斗鱼|虎牙|抖音|快手)[^」]*」|^(B站|斗鱼|虎牙|抖音|快手)", name):
                    stats["跳过无法归类的频道"] += 1      # 平台房间改用 README 实时地址
                    continue
                prov = province_of(name, b["groups"])   # 名字/分组里带省份的城市台，回地方台文件
                if prov:
                    group, fname = f"地方·{prov}", "local.m3u"
                elif look_like_channel(key, name):
                    group, fname = "其他频道", "theme.m3u"   # 像频道但名录不出来，兜底放专题订阅
                else:
                    group, fname = "其他轮播", "vod.m3u"     # 节目名/片名/电台，交回轮播
            else:
                group, fname = "其他轮播", "vod.m3u"
        else:
            fname = "kids.m3u" if group == "动画轮播" else "vod.m3u"
        if len(b["families"]) < MIN_SOURCES_VOD:
            stats["跳过收录源太少的轮播"] += 1
            continue
        out[fname].append((group, popular(key, b), name, attrs_of(b), pick_lines(b)))

    stats["平台轮播房间（改用实时地址）"] = len(platform)   # 不再生成快照订阅，只在 README 列实时地址

    words = {-1: "昨天", 0: "今天", 1: "明天"}
    for (offset, name), b in events.items():
        label = f"{words[offset]} {(today + timedelta(days=offset)):%m-%d}"
        t = TIME_RE.search(name)
        sk = ((int(t.group(1)), int(t.group(2))) if t else (99, 0), b["order"])
        out["sports-events.m3u"].append((label, sk, name, attrs_of(b), pick_lines(b)))

    # APTV 聚合靠「同名且连续」合并线路：同一赛事名在不同日期（今天/明天）出现时加日期后缀，
    # 否则两个不同的场次会被当成一个频道、线路也不连续。
    ev_rows = out["sports-events.m3u"]
    name_count = Counter(r[2] for r in ev_rows)
    if any(c > 1 for c in name_count.values()):
        out["sports-events.m3u"] = [
            (g, sk, (f"{name}（{g.split(' ')[0]}）" if name_count[name] > 1 else name), attrs, lines)
            for g, sk, name, attrs, lines in ev_rows
        ]

    def group_rank(label):
        base = label.split(" ", 1)[0]
        if base in GROUP_ORDER:
            return (GROUP_ORDER.index(base), label)
        plat = [p for p, _ in PLATFORMS] + ["其他"]
        head, _, tail = label.partition("·")
        return (len(GROUP_ORDER) + (plat.index(head) if head in plat else len(plat)), tail != "一起看", label)

    routed = defaultdict(list)
    for fname, items in out.items():
        for item in items:
            routed[route(fname, item[0])].append(item)
    for fname in routed:
        routed[fname].sort(key=lambda r: (group_rank(r[0]), r[1]))
    return [u for u, _ in epg.most_common(EPG_COUNT)], routed, stats


class ProbeResult:
    """单个地址的判定结果。

    kind: ok / dead / unknown；measured=False 表示没真正测到（超出总预算），不能改动失效天数；
    fast_dead=True 只给「域名解析失败 / 连接被拒」用：同一台服务器前 3 个都是这种，整台剩余判 dead。
    """

    __slots__ = ("kind", "elapsed", "reason", "measured", "fast_dead")

    def __init__(self, kind, elapsed=0.0, reason="", measured=True, fast_dead=False):
        self.kind = kind
        self.elapsed = elapsed
        self.reason = reason
        self.measured = measured
        self.fast_dead = fast_dead

    def __repr__(self):
        return f"ProbeResult({self.kind}, {self.elapsed:.2f}s, {self.reason!r}, measured={self.measured})"


def dedupe_cross_file(out):
    """跨订阅去重：同一个播放地址只保留在 DEDUPE_PRIORITY 最靠前的文件里。

    这样 APTV 把多个订阅加进同一个「聚合配置」时，同名频道合并后不会出现重复线路；
    某个频道如果线路全被前面的订阅占用了，就不再输出（它的内容在那里已经有了）。
    返回 {文件名: 去掉的线路数}。
    """
    seen, dropped = set(), Counter()
    order = [f for f in DEDUPE_PRIORITY if f in out] + [f for f in out if f not in DEDUPE_PRIORITY]
    for fname in order:
        items = out.get(fname)
        if not items:
            continue
        new_items = []
        for group, sk, name, attrs, lines in items:
            keep = []
            for line in lines:
                url = line[1]
                if url in seen:
                    dropped[fname] += 1
                    continue
                seen.add(url)
                keep.append(line)
            if keep:
                new_items.append((group, sk, name, attrs, keep))
        out[fname] = new_items
    return dropped


def probe_budget():
    """探测总预算（秒），可用 IPTV_PROBE_BUDGET 覆盖（本地测试方便）"""
    raw = os.environ.get("IPTV_PROBE_BUDGET", "").strip()
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            print(f"  [warn] IPTV_PROBE_BUDGET={raw!r} 不是数字，改用默认 {PROBE_BUDGET}s")
    return float(PROBE_BUDGET)


def _looks_like_http_typo(scheme):
    """hhttp:// / htpp:// 这类明显是 http 拼错的协议 → dead"""
    return max(difflib.SequenceMatcher(None, scheme, good).ratio()
               for good in TESTABLE_SCHEMES) >= 0.6


def is_local_host(host):
    """局域网或本机地址（127.*、10.*、172.16-31.*、192.168.*、localhost，以及 IPv6 回环/链路本地）"""
    h = (host or "").strip().lower().strip("[]")
    if not h:
        return True
    if h == "localhost" or h.endswith(".localhost") or h.endswith(".local") or h == "localhost.localdomain":
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    if ip.is_unspecified:
        return True
    nets = LOCAL_NETS_V4 if ip.version == 4 else LOCAL_NETS_V6
    return any(ip in net for net in nets)


def static_verdict(url):
    """不联网就能判定的地址 → ProbeResult；需要联网探测的返回 None"""
    m = re.match(r"^([A-Za-z][A-Za-z0-9+.\-]*)://", url or "")
    if not m:
        return ProbeResult("dead", reason="地址格式错误")
    scheme = m.group(1).lower()
    if scheme not in TESTABLE_SCHEMES:
        if scheme in UNTESTABLE_SCHEMES:
            return ProbeResult("unknown", reason="不支持的协议")   # rtmp/rtsp/p2p/rtmps/ftps 等测不了，不删
        if _looks_like_http_typo(scheme):
            return ProbeResult("dead", reason="协议拼写错误")
        return ProbeResult("unknown", reason="不支持的协议")
    try:
        parsed = urlparse(url)
    except ValueError:
        return ProbeResult("dead", reason="地址格式错误")
    if not parsed.hostname:
        return ProbeResult("dead", reason="地址没有主机名")
    if is_local_host(parsed.hostname):
        return ProbeResult("dead", reason="局域网或本机地址")
    return None


class _ProbeTimeout(Exception):
    """探测总时长已到"""


def _looks_like_media(head):
    """FLV 文件头；MPEG-TS 每 188 字节一个 0x47 同步字节（文本里不会出现 NUL）"""
    if head[:4] == b"FLV\x01":
        return True
    return (len(head) > 376 and head[0] == head[188] == head[376] == 0x47
            and b"\x00" in head[:377])


def _read_head(resp, deadline):
    """流式读取响应体，最多前 4KB 就停（绝不下载完整视频流）"""
    buf = bytearray()
    for chunk in resp.iter_content(chunk_size=2048):
        if chunk:
            buf.extend(chunk)
            if len(buf) >= PROBE_MAX_BYTES:
                break
        if time.monotonic() > deadline:
            raise _ProbeTimeout()
    return bytes(buf[:PROBE_MAX_BYTES])


def _judge_200(resp, deadline):
    """HTTP 200/206 → 看 Content-Type + 开头几个字节判定能不能播，返回 (kind, reason, fast_dead)"""
    ctype = (resp.headers.get("Content-Type") or "").lower()
    if ctype.startswith("video/") and not any(k in ctype for k in ("mpegurl", "scpls")):
        return ("ok", "视频流 Content-Type", False)      # 不读 body，立即关掉
    body = _read_head(resp, deadline)
    head = body.lstrip(b"\xef\xbb\xbf \t\r\n")
    if head.startswith(b"#EXTM3U"):                     # m3u8（不管 Content-Type 是什么）
        return ("ok", "m3u8 播放列表", False)
    if _looks_like_media(body):                         # FLV / MPEG-TS 视频头
        return ("ok", "FLV/TS 视频头", False)
    if not body.strip():
        return ("unknown", "200 空响应", False)
    if "text/html" in ctype or HTML_HEAD_RE.match(body[:512]) or b"<html" in body[:1024].lower():
        return ("dead", "HTML 错误页", False)
    text = body[:1024].decode("utf-8", "ignore")
    if any(w in text for w in LOGIN_HINTS):
        return ("dead", "提示登录/已失效", False)       # 咪咕等代理的 200 报错
    return ("dead", "200 但不是 m3u8/视频", False)


def _probe_http(url, timeout):
    """流式 GET 首字节探测，返回 (kind, reason, fast_dead)；网络异常由 _classify_exc 分类"""
    deadline = time.monotonic() + timeout
    current = url
    for _ in range(PROBE_REDIRECTS + 1):
        left = max(0.05, deadline - time.monotonic())
        resp = requests.get(current, headers={"User-Agent": PROBE_UA},
                            timeout=(min(3.0, left), min(float(PROBE_TIMEOUT), left)),
                            allow_redirects=False, stream=True, verify=False)
        try:
            code = resp.status_code
            if 300 <= code < 400:
                loc = (resp.headers.get("Location") or "").strip()
                if "miguvideo" in loc.lower():          # 咪咕代理能播的判定
                    return ("ok", "咪咕代理 302 → miguvideo", False)
                if not loc:
                    return ("unknown", f"HTTP {code} 没有 Location", False)
                current = urljoin(current, loc)
                continue
            if code in (404, 410):
                return ("dead", f"HTTP {code}", False)
            if code in (403, 451):
                return ("unknown", f"HTTP {code}", False)
            if code >= 500:
                return ("unknown", f"HTTP {code}", False)
            if code in (200, 206):
                return _judge_200(resp, deadline)
            return ("unknown", f"HTTP {code}", False)
        finally:
            resp.close()
    return ("unknown", "跳转次数过多", False)


def _walk_exceptions(exc, depth=0):
    """展开 requests/urllib3 的异常链（原始异常藏在 reason/args/__cause__/__context__ 里）"""
    if exc is None or depth > 8:
        return
    yield exc
    for attr in ("reason", "__cause__", "__context__"):
        child = getattr(exc, attr, None)
        if isinstance(child, BaseException):
            yield from _walk_exceptions(child, depth + 1)
    for arg in getattr(exc, "args", ()) or ():
        if isinstance(arg, BaseException):
            yield from _walk_exceptions(arg, depth + 1)


def _classify_exc(exc):
    """网络异常 → (kind, reason, fast_dead)"""
    chain = list(_walk_exceptions(exc))
    texts = " ".join(f"{type(e).__name__}: {e}" for e in chain).lower()
    for e in chain:
        if isinstance(e, socket.gaierror):
            return ("dead", "域名解析失败", True)
        if isinstance(e, ConnectionRefusedError):
            return ("dead", "连接被拒", True)
        if isinstance(e, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return ("unknown", "连接被重置", False)
        if isinstance(e, (socket.timeout, TimeoutError)):
            return ("unknown", "超时", False)
        if isinstance(e, ssl.SSLError):
            return ("unknown", "SSL 错误", False)
    if requests is not None:
        if isinstance(exc, requests.exceptions.Timeout):
            return ("unknown", "超时", False)
        if isinstance(exc, requests.exceptions.SSLError):
            return ("unknown", "SSL 错误", False)
        if isinstance(exc, (requests.exceptions.InvalidURL, requests.exceptions.MissingSchema)):
            return ("dead", "地址格式错误", False)
    if any(w in texts for w in ("name or service not known", "nodename nor servname", "failed to resolve",
                                "nameresolutionerror", "getaddrinfo", "no address associated",
                                "temporary failure in name resolution")):
        return ("dead", "域名解析失败", True)
    if "connection refused" in texts or "econnrefused" in texts:
        return ("dead", "连接被拒", True)
    if ("connection reset" in texts or "econnreset" in texts or "remote end closed" in texts
            or "remotedisconnected" in texts or "connection aborted" in texts
            or "connection broken" in texts or "chunkedencodingerror" in texts):
        return ("unknown", "连接被重置", False)
    if "timeout" in texts or "timed out" in texts:
        return ("unknown", "超时", False)
    if "ssl" in texts or "certificate" in texts:
        return ("unknown", "SSL 错误", False)
    return ("unknown", f"请求异常 {type(exc).__name__}", False)


def _probe_network(url, timeout):
    """后台线程跑 _probe_http，主线程到点直接放弃（慢速流靠循环内计时中止不了）"""
    box = {}

    def worker():
        try:
            box["result"] = _probe_http(url, timeout)
        except BaseException as e:      # 异常交给 _classify_exc 分类
            box["error"] = e

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return ("unknown", "超时", False)
    if "error" in box:
        return _classify_exc(box["error"])
    return box["result"]


def probe_url(url, timeout=None):
    """探测单个地址 → ProbeResult"""
    timeout = PROBE_TIMEOUT if timeout is None else max(0.05, float(timeout))
    verdict = static_verdict(url)
    if verdict is not None:
        return verdict
    started = time.monotonic()
    kind, reason, fast_dead = _probe_network(url, timeout)
    return ProbeResult(kind, time.monotonic() - started, reason, measured=True, fast_dead=fast_dead)


def _host_of(url):
    try:
        return (urlparse(url).netloc or "").lower()
    except ValueError:
        return ""


def _order_by_host(urls):
    """按 host 轮转排序：线程池不会被某一台服务器的上千条地址占满，也配合同 host 并发限制"""
    queues = {}
    for u in urls:
        queues.setdefault(_host_of(u), []).append(u)
    out, pending = [], [deque(q) for q in queues.values()]
    while pending:
        rest = []
        for q in pending:
            out.append(q.popleft())
            if q:
                rest.append(q)
        pending = rest
    return out


def probe_urls(urls, budget=None):
    """并发探测一批地址（调用方负责去重），返回 {url: ProbeResult}"""
    budget = probe_budget() if budget is None else max(0.0, float(budget))
    deadline = time.monotonic() + budget
    lock = threading.Lock()
    host_state, host_sems = {}, {}
    results = {}

    def host_skip(url):
        return url, ProbeResult("dead", 0.0, "同服务器连续失效（解析失败/连接被拒）", measured=True)

    def task(url):
        verdict = static_verdict(url)       # 局域网、协议拼错、rtmp 等不用联网
        if verdict is not None:
            return url, verdict
        host = _host_of(url)
        with lock:
            hs = host_state.setdefault(host, {"probed": 0, "fast_dead": 0, "closed": False})
            if hs["closed"]:
                return host_skip(url)
            if time.monotonic() >= deadline:
                return url, ProbeResult("unknown", 0.0, "超出探测总预算", measured=False)
            sem = host_sems.setdefault(host, threading.Semaphore(PROBE_HOST_WORKERS))
        with sem:                           # 同一台服务器最多 PROBE_HOST_WORKERS 个并发
            with lock:
                if hs["closed"]:
                    return host_skip(url)
                left = deadline - time.monotonic()
                if left <= 0.05:
                    return url, ProbeResult("unknown", 0.0, "超出探测总预算", measured=False)
            res = probe_url(url, timeout=min(float(PROBE_TIMEOUT), left))
            with lock:
                hs["probed"] += 1
                if hs["probed"] <= PROBE_HOST_FASTDEAD and res.fast_dead:
                    hs["fast_dead"] += 1
                if hs["probed"] >= PROBE_HOST_FASTDEAD and hs["fast_dead"] >= PROBE_HOST_FASTDEAD:
                    hs["closed"] = True    # 前 3 个都是解析失败/连接被拒 → 整台剩余判 dead
            return url, res

    with ThreadPoolExecutor(max_workers=PROBE_WORKERS) as pool:
        for url, res in pool.map(task, _order_by_host(urls)):
            results[url] = res
    return results


def _probe_stats(out, results):
    """按文件统计 ok/unknown/明确失效（安全阀分支用，不改动 out）"""
    files = {}
    for fname, items in out.items():
        st = Counter()
        for _, _, _, _, lines in items:
            for _, url in lines:
                st["total"] += 1
                res = results.get(url)
                if res is None or not res.measured or res.kind == "unknown":
                    st["unknown"] += 1
                    if res is None or not res.measured:
                        st["skipped"] += 1
                elif res.kind == "ok":
                    st["ok"] += 1
                else:
                    st["deleted"] += 1
        files[fname] = dict(st)
    return files


def filter_by_probe(out, results):
    """按探测结果过滤/排序（原地改 out），返回各文件统计。

    频道内顺序：ok（响应时间升序）→ unknown（保持原顺序）；
    明确失效（404/410/HTTP 报错/登录提示/HTML 错误页/局域网/协议拼错）直接删；
    测不了的（超时/403/451/5xx/重置/rtmp 等）保留，避免把海外 IP 的国内源误删。
    频道线路全删光就不输出；同名线路仍各自连续。
    """
    files = {}
    for fname, items in out.items():
        st = Counter()
        new_items = []
        for group, sk, name, attrs, lines in items:
            ok_lines, kept_other = [], []
            for line in lines:
                st["total"] += 1
                url = line[1]
                res = results.get(url)
                if res is None or not res.measured or res.kind == "unknown":
                    st["unknown"] += 1
                    if res is None or not res.measured:
                        st["skipped"] += 1
                    kept_other.append(line)
                elif res.kind == "ok":
                    st["ok"] += 1
                    ok_lines.append((line, res.elapsed))
                else:
                    st["deleted"] += 1          # 明确失效：直接删
            ok_lines.sort(key=lambda x: x[1])   # ok 按响应时间从快到慢
            kept = [line for line, _ in ok_lines] + kept_other
            if kept:                            # 线路全删光的频道不输出
                new_items.append((group, sk, name, attrs, kept))
        out[fname] = new_items
        files[fname] = dict(st)
    return files


def trim_out(out, limit=3000):
    """每个文件控制在 3000 条线路以内：从尾部（优先级最低的频道）开始丢，返回 {文件: 丢弃条数}"""
    dropped = {}
    for fname, items in out.items():
        total = sum(len(item[4]) for item in items)
        if total <= limit:
            continue
        kept, n = [], 0
        for item in items:
            if n + len(item[4]) > limit:
                break
            kept.append(item)
            n += len(item[4])
        dropped[fname] = total - n
        out[fname] = kept
    return dropped


def probe_and_filter(out, today, budget=None):
    """对所有输出文件的所有线路做一次探测，按结果过滤/排序（原地改 out）。

    返回统计字典；触发安全阀时不做任何过滤。
    """
    started = time.monotonic()
    budget = probe_budget() if budget is None else max(0.0, float(budget))
    # 地址按订阅文件顺序收集（体育赛事在最前）：预算不够时优先保证赛事订阅被测到
    file_order = [f for f in FILES if f in out] + [f for f in out if f not in FILES]

    def collect(fnames):
        got, seen = [], set()
        for fname in fnames:
            for item in out.get(fname, []):
                for _, url in item[4]:
                    if url not in seen:
                        seen.add(url)
                        got.append(url)
        return got

    urls = collect(file_order)
    info = {"urls": len(urls), "ok": 0, "unknown": 0, "dead": 0, "skipped": 0,
            "elapsed": 0.0, "safety": False, "files": {}}
    if not urls:
        return info
    results = {}
    priority = collect(["sports-events.m3u"])
    if priority:
        results.update(probe_urls(priority, budget=budget))
        prio_set = set(priority)
        rest = [u for u in urls if u not in prio_set]
        if rest:            # 剩余预算留给其它订阅
            results.update(probe_urls(rest, budget=max(0.0, budget - (time.monotonic() - started))))
    else:
        results.update(probe_urls(urls, budget=budget))
    measured = [r for r in results.values() if r.measured]
    ok_n = sum(1 for r in measured if r.kind == "ok")
    info["ok"] = ok_n
    info["unknown"] = sum(1 for r in measured if r.kind == "unknown")
    info["dead"] = sum(1 for r in measured if r.kind == "dead")
    info["skipped"] = len(urls) - len(measured)
    ratio = ok_n / len(urls) if urls else 0.0      # 按方案：占比 = ok / 全部去重地址（不是只算测到的）
    if ok_n < PROBE_SAFE_MIN_OK or ratio < PROBE_SAFE_MIN_RATIO:
        # 安全阀：疑似 GitHub 网络本身出问题，这一轮完全不过滤，也不更新失效天数
        info["safety"] = True
        info["files"] = _probe_stats(out, results)
        print(f"::warning::线路探测整体只有 {ok_n} 条 ok（已测 {len(measured)}/{len(urls)} 条，"
              f"占比 {ratio * 100:.1f}%），判定为网络异常，本轮不做过滤")
        info["elapsed"] = time.monotonic() - started
        return info
    info["files"] = filter_by_probe(out, results)
    info["elapsed"] = time.monotonic() - started
    return info


def esc(v):
    return v.replace('"', "'")


def merge_items_by_name(items):
    """按频道名合并（等价于 APTV 的「聚合配置」）：同名频道合成一条，线路连续、地址去重。"""
    order, buckets = [], {}
    for group, sk, name, attrs, lines in items:
        b = buckets.get(name)
        if b is None:
            b = [group, sk, name, dict(attrs), []]
            buckets[name] = b
            order.append(name)
        seen = {u for _, u in b[4]}
        for line in lines:
            if line[1] not in seen:
                seen.add(line[1])
                b[4].append(line)
    return [buckets[name] for name in order]


def write_m3u(path, epg_urls, items):
    out = ['#EXTM3U x-tvg-url="' + ",".join(epg_urls) + '"' if epg_urls else "#EXTM3U"]
    for group, _, name, attrs, lines in items:
        for (catchup, opts), url in lines:
            a = [f'tvg-name="{esc(name)}"']
            a += [f'{k}="{esc(v)}"' for k, v in attrs.items()]
            a += [f'{k}="{esc(v)}"' for k, v in catchup.items() if v]
            a.append(f'group-title="{esc(group)}"')
            out.append(f'#EXTINF:-1 {" ".join(a)},{name}')
            out.extend(opts)
            out.append(url)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def write_readme(path, summary, streamers, now):
    lines = [
        "# 直播订阅",
        "",
        f"由 `python/直播聚合订阅.py` 每天自动生成，最近更新：{now:%Y-%m-%d %H:%M}（北京时间）。",
        "",
        "## 订阅地址",
        "",
        "| 订阅 | 频道 | 线路 | 测通率 | 地址 |",
        "|---|---:|---:|---:|---|",
    ]
    for fname, title, n_ch, n_lines, rate in summary:
        lines.append(f"| {title} | {n_ch} | {n_lines} | {rate} | {RAW_BASE}{fname} |")
    lines += [
        "",
        "「测通率」= 本轮实际测通的线路（ok）/ 该文件探测前的线路总数；测不了（超时、403、5xx）的保留，"
        "明确失效（404/410/服务器报错/登录提示/HTML 错误页）的直接删掉。",
        "",
        "同一条线路只会出现在一个订阅里（赛事 > 频道分类 > 体育，体育里的重复已归到对应频道订阅），"
        "把多个订阅一起加进「聚合配置」不会出现重复线路。",
        "",
        "最后一行 `all.m3u` 是把上面 11 个订阅按频道名合并成一个链接（约 1.5 万条线路），"
        "任何播放器/电视都能直接订阅；APTV/Apple TV 之间仍建议用「聚合配置」+ iCloud 同步。",
        "",
        "国内访问 raw.githubusercontent.com 不稳定时，在地址前加 `https://ghfast.top/`。",
        "",
        "**APTV 里显示成「一个频道 + 多条线路」**：先把订阅添加为「订阅配置」，再新建一个「聚合配置」只勾选这个订阅。"
        "同名频道会合并成一个，右上角显示备选数量，播放失败自动切换（超时在 设置-高级设置-播放超时时间 里改）。",
        "",
        "## 主播直播（实时地址，直接订阅）",
        "",
        "主播间只有开播时能看，快照没有意义；下面是原始的实时地址，打开就是当前在播的房间。"
        "虎牙/斗鱼/YY 的「一起看」轮播房间也在这些实时地址里，不再生成每日快照。",
        "",
        "| 名称 | 地址 |",
        "|---|---|",
    ]
    lines += [f"| {name} | {url} |" for name, url in streamers]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description="把直播备份拆成多个精简 M3U 订阅")
    ap.add_argument("--livelist", default=str(LIVELIST_PATH))
    ap.add_argument("--live-dir", default=str(LIVE_DIR))
    ap.add_argument("--lives-json", default=str(LIVES_JSON))
    ap.add_argument("--out-dir", default=str(OUTPUT_DIR))
    ap.add_argument("--force", action="store_true", help="兼容工作流参数")
    ap.add_argument("--debug", action="store_true", help="兼容工作流参数")
    args = ap.parse_args()

    now = datetime.now(BEIJING)
    lives = load_lives(Path(args.lives_json))
    sources = load_sources(Path(args.livelist), Path(args.live_dir), lives, datetime.now())
    epg_urls, out, stats = build(sources, now.date())
    deduped = dedupe_cross_file(out) if DEDUPE_ENABLED else Counter()   # 同一个地址只留在优先级最高的订阅里
    probe = None
    if PROBE_ENABLED and requests is not None:
        try:
            probe = probe_and_filter(out, now.date())
        except Exception as e:                  # 探测本身出错也不能影响生成订阅
            print(f"::warning::线路探测异常（{type(e).__name__}: {e}），本轮保留原样")
            probe = None
    elif PROBE_ENABLED:
        print("  [warn] 没有安装 requests，跳过线路探测")
    trimmed = trim_out(out)                     # 每个文件仍然控制在 3000 条以内

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"最近 {MAX_AGE_DAYS} 天内更新过的源: {len(sources)} 个（北京时间 {now:%Y-%m-%d %H:%M}）")
    for k in ("使用的源", "跳过成人源", "主播直播间源（改用实时地址）", "跳过超大源", "跳过成人频道", "跳过公告条目",
              "跳过不在昨天到明天的赛事", "跳过非咪咕赛事", "与平台轮播重复", "跳过无法归类的频道", "跳过国际体育", "跳过外文频道",
              "跳过收录源太少的频道", "跳过收录源太少的轮播", "平台轮播房间（改用实时地址）"):
        print(f"  {k}: {stats[k]}")
    if deduped:
        print(f"  跨订阅去重: 去掉 {sum(deduped.values())} 条重复线路（"
              + "，".join(f"{f} {n}" for f, n in sorted(deduped.items())) + "）")
    if probe:
        print(f"  线路探测: {probe['urls']} 个地址（去重后）用时 {probe['elapsed']:.1f}s；"
              f"ok {probe['ok']}，unknown {probe['unknown']}，明确失效 {probe['dead']}，超预算未测 {probe['skipped']}")
        if probe["safety"]:
            print("  安全阀: 本轮不做过滤（按原顺序输出）")
    if trimmed:
        print("  超过 3000 条已截断: " + "，".join(f"{f} 丢 {n} 条" for f, n in trimmed.items()))
    titles = dict(FILES, **{f: f for f in out if f not in FILES})   # 新增的大类按文件名直接输出
    output_names = dict(titles)
    output_names[ALL_FILE] = "全部频道（合并订阅）"
    stale = 0
    for old in out_dir.iterdir():
        if not old.is_file() or old.name in output_names:
            continue
        legacy = old.name.startswith("tv") and old.suffix in (".m3u8", ".txt")   # 原仓库 iptv/ 里的旧文件
        if old.suffix == ".m3u" or legacy:      # 只清理订阅和遗留文件，--out-dir 指到仓库根也不会误删 livelist.txt
            old.unlink()
            stale += 1
    summary = []
    for fname, title in titles.items():
        items = out.get(fname, [])
        write_m3u(out_dir / fname, epg_urls, items)
        n_lines = sum(len(i[4]) for i in items)
        fst = (probe or {}).get("files", {}).get(fname, {})
        rate = f"{fst.get('ok', 0) / fst['total'] * 100:.1f}%" if fst.get("total") else "—"
        summary.append((fname, title, len(items), n_lines, rate))
        groups = Counter(i[0] for i in items)
        detail = ""
        if probe and fst:
            detail = (f"  | ok {fst.get('ok', 0)}  unknown {fst.get('unknown', 0)}"
                      f"  已删除 {fst.get('deleted', 0)}  未测 {fst.get('skipped', 0)}  测通率 {rate}")
        print(f"  {fname:18s} {title:16s} 频道 {len(items):5d}  线路 {n_lines:6d}  "
              + "，".join(f"{g} {n}" for g, n in groups.items()) + detail)
    # 合并订阅：给电视/其它播放器一个链接（APTV 里更推荐用「聚合配置」，可 iCloud 同步）
    all_items = merge_items_by_name([item for fname in titles for item in out.get(fname, [])])
    if all_items:
        write_m3u(out_dir / ALL_FILE, epg_urls, all_items)
        total_lines = sum(len(i[4]) for i in all_items)
        if probe and probe.get("files"):
            ok_n = sum(v.get("ok", 0) for v in probe["files"].values())
            tot_n = sum(v.get("total", 0) for v in probe["files"].values())
            all_rate = f"{ok_n / tot_n * 100:.1f}%" if tot_n else "—"
        else:
            all_rate = "—"
        summary.append((ALL_FILE, "全部频道（合并订阅）", len(all_items), total_lines, all_rate))
        print(f"  {ALL_FILE:18s} 全部频道（合并订阅） 频道 {len(all_items):5d}  线路 {total_lines:6d}  测通率 {all_rate}")
    streamers = streamer_links(lives)
    write_readme(out_dir / "README.md", summary, streamers, now)
    print(f"  清理旧订阅文件: {stale} 个")
    print(f"  主播平台实时地址: {len(streamers)} 个（写在 README.md）")
    print(f"  节目单: {epg_urls}")
    print(f"-> {out_dir}/")


if __name__ == "__main__":
    main()
