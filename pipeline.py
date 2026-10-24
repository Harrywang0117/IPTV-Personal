#!/usr/bin/env python3
"""Generate small IPTV playlists; only decoded media is labelled verified."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time as day_time, timedelta
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from vendor.upstream import channel_key, ADULT_RE, NOTICE_WORDS

ROOT = Path(__file__).resolve().parent
ATTR = re.compile(r'([\w-]+)="([^"]*)"')
DAY_WORDS = {"昨天": -1, "昨日": -1, "今天": 0, "今日": 0, "明天": 1, "明日": 1}
CATCHUP = ("catchup", "catchup-source", "catchup-days", "catchup-correction")
UA = "okhttp/3.15"


def normalized_channel(name):
    # CCTV4's Europe/America feeds have different schedules.
    if re.search(r"CCTV[\s-]*4", name, re.I):
        for region in ("欧洲", "美洲"):
            if region in name:
                return "CCTV4" + region
    return channel_key(name)


@dataclass
class Entry:
    name: str
    group: str
    url: str
    attrs: dict = field(default_factory=dict)
    options: list = field(default_factory=list)
    source: str = ""
    event_date: date | None = None
    upcoming: bool = False
    verified: bool = False
    check_status: str = "not_checked"

    @property
    def identity(self):
        # Event tvg-id can be shared by unrelated replays. Keep the full title.
        if self.event_date:
            return (self.event_date.isoformat(), re.sub(r"\s+", " ", self.name).strip())
        return ("tv", normalized_channel(self.name))

    @property
    def fingerprint(self):
        value = self.url + "\n" + "\n".join(self.options)
        return hashlib.sha256(value.encode()).hexdigest()[:20]


def public_url(url):
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in ("http", "https") or not host or parsed.username or parsed.password:
            return False
        if host == "localhost" or host.endswith((".local", ".localhost")):
            return False
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            return "." in host
    except ValueError:
        return False


def split_extinf(line):
    quoted = False
    for i, char in enumerate(line):
        if char == '"':
            quoted = not quoted
        elif char == "," and not quoted:
            return line[:i], line[i + 1:].strip()
    return line, ""


def parse_playlist(text, source=""):
    header, entries, info, options = {}, [], None, []
    group = ""
    is_m3u = "#EXTINF:" in text
    for raw in text.lstrip("\ufeff").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTM3U"):
            header.update(ATTR.findall(line))
        elif line.startswith("#EXTINF:"):
            meta, name = split_extinf(line)
            attrs = dict(ATTR.findall(meta))
            info = (name or attrs.get("tvg-name", ""), attrs)
            options = []
        elif line.startswith(("#EXTVLCOPT:", "#KODIPROP:")):
            options.append(line)
        elif line.startswith("#"):
            continue
        elif is_m3u:
            if info and public_url(line):
                name, attrs = info
                for key in CATCHUP:
                    if key not in attrs and key in header:
                        attrs[key] = header[key]
                entries.append(Entry(name, attrs.get("group-title", ""), line,
                                     attrs, options.copy(), source))
            info = None
        elif line.endswith(",#genre#"):
            group = line[:-len(",#genre#")]
        elif "," in line:
            name, urls = line.split(",", 1)
            for url in re.split(r"#(?=https?://)", urls):
                # TVBox appends a $line label; do not strip query strings.
                url = re.sub(r"\$[^?&/]*$", "", url.strip())
                if public_url(url):
                    entries.append(Entry(name.strip(), group, url, source=source))
    return entries


def event_date(group, today):
    """Prefer absolute dates, then month/day, then relative words."""
    full = re.search(r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})", group)
    if full:
        try:
            return date(*map(int, full.groups()))
        except ValueError:
            return None
    partial = re.search(r"(?<!\d)(\d{1,2})[-/.](\d{1,2})(?!\d)", group)
    if partial:
        month, day = map(int, partial.groups())
        candidates = []
        for year in (today.year - 1, today.year, today.year + 1):
            try:
                candidates.append(date(year, month, day))
            except ValueError:
                pass
        return min(candidates, key=lambda d: abs((d - today).days)) if candidates else None
    for word, delta in DAY_WORDS.items():
        if word in group:
            return today + timedelta(days=delta)
    return None


def event_clock(name):
    """Return a valid clock in minutes; source lists normally put it last."""
    prefix = re.match(r"^\s*(\d{1,2}):(\d{2})\s+", name)
    if prefix:
        hour, minute = map(int, prefix.groups())
        if hour < 24 and minute < 60:
            return hour * 60 + minute
    matches = re.findall(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)", name)
    for hour, minute in reversed(matches):
        hour, minute = int(hour), int(minute)
        if hour < 24 and minute < 60:
            return hour * 60 + minute
    return None


def kickoff(entry, timezone):
    clock = event_clock(entry.name)
    if clock is None:
        return None
    hour, minute = divmod(clock, 60)
    return datetime.combine(entry.event_date, day_time(hour, minute), timezone)


def prepare(entries, spec, config, now):
    selected = []
    for entry in entries:
        if not entry.name or ADULT_RE.search(entry.name + "|" + entry.group):
            continue
        if any(word in entry.name for word in NOTICE_WORDS):
            continue
        has_day = any(word in entry.group for word in DAY_WORDS)
        is_event = has_day or bool(re.search(r"体育.*20\d\d[-/.]", entry.group))
        if is_event:
            if not spec.get("events"):
                continue
            dated = event_date(entry.group, now.date())
            if dated is None or abs((dated - now.date()).days) > 1:
                continue
            entry = replace(entry, event_date=dated)
            start = kickoff(entry, now.tzinfo)
            entry.upcoming = dated > now.date() or (dated == now.date() and start is not None and start > now)
            # Channel EPG IDs and append-catchup templates do not describe event VODs.
            entry.attrs = {k: v for k, v in entry.attrs.items() if k not in CATCHUP}
        else:
            if spec["id"] == "base" and entry.group not in config["base_groups"]:
                continue
            normalized = normalized_channel(entry.name)
            if normalized.startswith("CGTN"):
                continue
            entry.name = normalized or entry.name
            entry.group = ("央视" if normalized.startswith("CCTV") else entry.group)
        selected.append(entry)
    return selected


def fetch_playlist(spec, timeout):
    if not public_url(spec["url"]):
        raise ValueError("invalid_source_url")
    cmd = ["curl", "--silent", "--show-error", "--fail", "--location",
           "--proto", "=http,https", "--proto-redir", "=http,https",
           "--connect-timeout", "5", "--max-time", str(timeout),
           "--max-filesize", str(8 * 1024 * 1024), "--user-agent", UA, spec["url"]]
    result = subprocess.run(cmd, capture_output=True, timeout=timeout + 2)
    if result.returncode:
        raise ValueError(f"source_fetch_failed_curl_{result.returncode}")
    body = result.stdout
    if len(body) > 8 * 1024 * 1024 or body.startswith((b"FLV", b"\x47")):
        raise ValueError("source_is_media_or_too_large")
    text = body.decode("utf-8-sig", "replace")
    if "#EXTINF:" not in text and ",#genre#" not in text:
        raise ValueError("source_is_not_playlist")
    return text


def source_error(error):
    if isinstance(error, ValueError) and re.fullmatch(r"[a-z_]+(?:\d+)?", str(error)):
        return str(error)
    return "source_total_timeout" if isinstance(error, subprocess.TimeoutExpired) else type(error).__name__


def source_playlist(spec, config, state_dir, now):
    """Retain public source lists when the runner cannot reach a mirror."""
    cache_path = state_dir / (hashlib.sha256(spec["url"].encode()).hexdigest()[:16] + ".json")
    try:
        text = fetch_playlist(spec, config["fetch_timeout"])
        if not parse_playlist(text, spec["id"]):
            raise ValueError("empty_playlist")
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        if spec.get("events"):
            try:
                cached = json.loads(cache_path.read_text())
                age = (now - datetime.fromisoformat(cached["fetched_at"])).total_seconds() / 3600
                if (cached["url"] == spec["url"] and 0 <= age <= config.get("source_cache_hours", 24)
                        and parse_playlist(cached["text"], spec["id"])):
                    return cached["text"], {"cached": True, "cache_age_hours": round(age, 2),
                                            "fetch_error": source_error(error)}
            except (OSError, ValueError, TypeError, KeyError):
                pass
        raise
    if spec.get("events"):
        atomic_write(cache_path, json.dumps({"url": spec["url"], "fetched_at": now.isoformat(),
                                            "text": text}, ensure_ascii=False) + "\n")
    return text, {"cached": False}


class Prober:
    def __init__(self, config):
        self.config = config
        self.deadline = time.monotonic() + config["budget_seconds"]
        self.event_deadline = time.monotonic() + config.get("event_probe_budget_seconds", 180)
        self.hosts = defaultdict(lambda: threading.Semaphore(config["per_host"]))
        self.lock = threading.Lock()
        help_text = subprocess.run(["ffmpeg", "-hide_banner", "-h", "demuxer=hls"],
                                   capture_output=True, text=True, timeout=5).stdout
        self.hls_options = ["-allowed_extensions", "ALL"]
        if "extension_picky" in help_text:
            self.hls_options += ["-extension_picky", "0"]

    def __call__(self, entry):
        with self.lock:
            semaphore = self.hosts[urlparse(entry.url).hostname]
        deadline = min(self.deadline, self.event_deadline) if entry.event_date else self.deadline
        budget_reason = "event_budget" if entry.event_date else "budget"
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not semaphore.acquire(timeout=max(0, remaining)):
            return {"ok": False, "reason": budget_reason, "frames": 0}
        try:
            remaining = deadline - time.monotonic()
            if remaining < 1:
                return {"ok": False, "reason": budget_reason, "frames": 0}
            result = self.decode(entry, min(self.config["probe_timeout"], remaining))
            if entry.event_date and result["reason"] == "decode_failed":
                hint = access_hint(entry.url, min(2, max(0, deadline-time.monotonic())))
                if hint:
                    result["reason"] = hint
            return result
        finally:
            semaphore.release()

    def decode(self, entry, timeout):
        user_agent, referer = UA, None
        for option in entry.options:
            if option.startswith("#EXTVLCOPT:http-user-agent="):
                user_agent = option.split("=", 1)[1]
            elif option.startswith(("#EXTVLCOPT:http-referrer=", "#EXTVLCOPT:http-referer=")):
                referer = option.split("=", 1)[1]
        if any(c in user_agent + (referer or "") for c in "\r\n"):
            return {"ok": False, "reason": "invalid_headers", "frames": 0}
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
               "-rw_timeout", "5000000", "-protocol_whitelist", "http,https,tcp,tls,crypto",
               "-user_agent", user_agent]
        if referer:
            cmd += ["-referer", referer]
        tail = ["-threads", "1", "-i", entry.url, "-t", str(self.config["decode_seconds"]),
                "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn", "-threads", "1",
                "-f", "null", "-progress", "pipe:1", "-"]
        started = time.monotonic()
        try:
            result = subprocess.run(cmd + self.hls_options + tail, capture_output=True,
                                    text=True, timeout=timeout)
            # An event may redirect to MP4 rather than HLS. HLS-only options
            # cause FFmpeg to reject valid MP4s before decoding any frames.
            if self.hls_options and "Option" in result.stderr and "not found" in result.stderr:
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    return {"ok": False, "reason": "timeout", "frames": 0}
                result = subprocess.run(cmd + tail, capture_output=True, text=True, timeout=remaining)
            if "Not detecting m3u8/hls with non standard extension" in result.stderr:
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    return {"ok": False, "reason": "timeout", "frames": 0}
                result = subprocess.run(cmd + self.hls_options + ["-f", "hls"] + tail,
                                        capture_output=True, text=True, timeout=remaining)
            frames = max([int(v) for v in re.findall(r"^frame=(\d+)$", result.stdout, re.M)] or [0])
            micros = max([int(v) for v in re.findall(r"^out_time_us=(\d+)$", result.stdout, re.M)] or [0])
            ok = result.returncode == 0 and frames >= 5 and micros >= (self.config["decode_seconds"] - 0.25) * 1e6
            reason = "decoded" if ok else "decode_failed"
            if not ok:
                for code in ("403", "404", "410", "451", "500", "502", "503"):
                    if re.search(r"\b" + code + r"\b", result.stderr):
                        reason = "http_" + code
                        break
            return {"ok": ok, "reason": reason, "frames": frames,
                    "seconds": round(time.monotonic() - started, 2),
                    "diagnostic": "" if ok else re.sub(r"https?://[^\s]+", "[url]", result.stderr[-1200:])}
        except subprocess.TimeoutExpired:
            return {"ok": False, "reason": "timeout", "frames": 0}


def candidate_order(entries, limit):
    # Try distinct servers first. Several mirrors of one URL add no redundancy.
    unique, seen = [], set()
    for entry in entries:
        if entry.fingerprint not in seen:
            seen.add(entry.fingerprint)
            unique.append(entry)
    first, rest, hosts = [], [], set()
    for entry in unique:
        host = urlparse(entry.url).netloc
        (rest if host in hosts else first).append(entry)
        hosts.add(host)
    return (first + rest)[:limit]


def access_hint(url, timeout):
    if timeout < 0.1:
        return False
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    try:
        with urllib.request.build_opener(NoRedirect).open(
                urllib.request.Request(url, headers={"User-Agent": UA}), timeout=timeout) as response:
            if "application/json" not in response.headers.get("Content-Type", ""):
                return False
            text = response.read(1024).decode("utf-8", "replace")
            if "会员" in text and any(word in text for word in ("开通", "订购", "购买")):
                return "membership_required"
            if "登录" in text and any(word in text for word in ("请", "需要", "后观看")):
                return "login_required"
            return False
    except (OSError, urllib.error.URLError):
        return False


def select_bucket(entries, config, probe, check_event=True):
    lines, fallback, stats = [], [], Counter()
    if entries[0].event_date:
        priority = {source: i for i, source in enumerate(config.get("event_source_priority", []))}
        entries = sorted(entries, key=lambda e: priority.get(e.source, len(priority)))
    for entry in candidate_order(entries, config["candidate_limit"]):
        if entry.upcoming:
            lines.append(entry)
            stats["upcoming_lines"] += 1
        elif entry.event_date and not check_event:
            lines.append(entry)
            stats["event_not_checked"] += 1
        else:
            result = probe(entry)
            stats[result["reason"]] += 1
            if result["ok"]:
                lines.append(replace(entry, verified=True, check_status="decoded"))
            elif entry.event_date:
                status = "not_checked" if result["reason"] in ("event_budget", "budget") else result["reason"]
                fallback.append(replace(entry, check_status=status))
        if len(lines) >= config["max_lines"]:
            break
    # A sports catalogue must not lose entire matches because a cloud runner
    # cannot reach the CDN. Preserve unverified entries, with separate status.
    if entries[0].event_date:
        fallback.sort(key=lambda e: e.check_status in ("membership_required", "login_required"))
        lines += fallback[:max(0, config["max_lines"]-len(lines))]
    return lines, stats


def event_sample_keys(ordered, limit_per_day, now=None):
    by_day = defaultdict(list)
    for entries in ordered:
        entry = entries[0]
        if entry.event_date and not entry.upcoming:
            by_day[entry.event_date].append(entry)
    selected = set()
    priorities = ("中超", "CBA", "NBA", "斯诺克")
    for entries in by_day.values():
        def active(entry):
            clock = event_clock(entry.name)
            return bool(now and entry.event_date == now.date() and clock is not None
                        and 0 <= now.hour*60+now.minute-clock <= 240)
        entries.sort(key=lambda e: (0 if active(e) else 1, next((i for i, league in enumerate(priorities)
                                        if league in e.name), len(priorities)), rank(e)))
        seen, primary, alternate = set(), [], []
        for entry in entries:
            match = entry.attrs.get("tvg-id") or entry.name
            if match not in seen:
                primary.append(entry)
                seen.add(match)
            elif active(entry):
                alternate.append(entry)
        # Cover distinct active matches before spending slots on commentary.
        choices = [e for e in primary if active(e)] + alternate + [e for e in primary if not active(e)]
        selected.update(e.identity for e in choices[:limit_per_day])
    return selected


def rank(entry):
    if entry.event_date:
        clock = event_clock(entry.name)
        return (0, entry.event_date.isoformat(), clock if clock is not None else 1440, entry.name)
    return (1, entry.group, re.sub(r"\d+", lambda m: m[0].zfill(4), entry.name))


def display_name(entry):
    if not entry.event_date:
        return entry.name
    clock = event_clock(entry.name)
    if clock is None:
        return entry.name
    hour, minute = divmod(clock, 60)
    membership = entry.check_status == "membership_required" or entry.attrs.get("_membership_hint", False)
    suffix = ("（源提示需会员）" if membership else "（源提示需登录）" if entry.check_status == "login_required"
              else "（本轮未测通）" if entry.check_status not in ("not_checked", "last_accepted", "decoded") else "")
    if re.match(r"^\s*\d{1,2}:\d{2}\s+", entry.name):
        name = re.sub(r"^\s*\d{1,2}:\d{2}", f"{hour:02d}:{minute:02d}", entry.name)
        return name + (suffix if suffix and suffix not in name else "")
    title = re.sub(r"\s*\d{1,2}:\d{2}\s*$", "", entry.name).strip()
    title = re.sub(r"^\d{1,2}:\d{2}\s+", "", title)
    return f"{hour:02d}:{minute:02d} {title}{suffix}"


def display_group(entry, now):
    if not entry.event_date:
        return entry.group or "电视频道"
    delta = (entry.event_date - now.date()).days
    label = {-1: "昨天回放", 0: "今天", 1: "明天预告"}.get(delta, entry.event_date.isoformat())
    return f"咪咕·{label} {entry.event_date:%m-%d}"


def safe_attr(value):
    return str(value).replace('"', "'").replace("\r", " ").replace("\n", " ")


def epg_id(entry):
    # The configured XMLTV service uses CCTV1, not Migu's CCTV1综合.
    if entry.name == "CCTV16 4K":
        return "CCTV16"
    if re.fullmatch(r"CCTV(?:[1-9]|1[0-7]|5\+|4K|4欧洲|4美洲)", entry.name):
        return entry.name
    return entry.attrs.get("tvg-id", "")


def render_m3u(buckets, now, epg, single=True):
    lines = ['#EXTM3U x-tvg-url="' + ",".join(epg) + '"']
    for bucket in sorted(buckets, key=lambda b: rank(b[0])):
        for entry in bucket[:1] if single else bucket:
            attrs = {k: v for k, v in entry.attrs.items() if k.startswith("tvg-") or k in CATCHUP}
            name = display_name(entry)
            attrs["tvg-name"] = name
            attrs["group-title"] = display_group(entry, now)
            if entry.event_date:
                existing = entry.attrs.get("tvg-id", "")
                attrs["tvg-id"] = existing if re.fullmatch(r"event-[0-9a-f]{16}", existing) else "event-" + hashlib.sha256(repr(entry.identity).encode()).hexdigest()[:16]
            elif epg_id(entry):
                attrs["tvg-id"] = epg_id(entry)
            meta = " ".join(f'{k}="{safe_attr(v)}"' for k, v in attrs.items())
            options = entry.options.copy()
            if not any(o.startswith("#EXTVLCOPT:http-user-agent=") for o in options):
                options.append("#EXTVLCOPT:http-user-agent=" + UA)
            lines += [f"#EXTINF:-1 {meta},{safe_attr(name)}", *options, entry.url]
    return "\n".join(lines) + "\n"


def render_txt(buckets, now):
    groups = defaultdict(list)
    for bucket in sorted(buckets, key=lambda b: rank(b[0])):
        entry = bucket[0]
        # TXT cannot faithfully carry custom headers/catchup; M3U is recommended.
        groups[display_group(entry, now)].append(f"{display_name(entry).replace(',', '，')},{entry.url}")
    return "\n".join(line for group, entries in groups.items()
                     for line in [group + ",#genre#", *entries]) + "\n"


def quality_errors(buckets, config, now, previous_channels=0):
    channels = sum(not bucket[0].event_date for bucket in buckets)
    errors = []
    if channels < config["minimum_channels"]:
        errors.append("too_few_verified_channels")
    if previous_channels and channels < previous_channels * config["minimum_channel_ratio"]:
        errors.append("channel_count_dropped")
    events = [b[0] for b in buckets if b[0].event_date]
    for delta in (-1, 0, 1):
        day = now.date() + timedelta(days=delta)
        items = [e for e in events if e.event_date == day]
        if not items:
            errors.append(f"missing_events_{day}")
    return errors


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, encoding="utf-8", delete=False) as handle:
        handle.write(content)
        temporary = Path(handle.name)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def publish(out_dir, buckets, report, now, config):
    """Prepare every output before replacing any accepted subscription file."""
    events = [b for b in buckets if b[0].event_date]
    channels = [b for b in buckets if not b[0].event_date]
    artifacts = {
        "all.m3u": render_m3u(buckets, now, config["epg"]),
        "all-backup.m3u": render_m3u(buckets, now, config["epg"], single=False),
        "events.m3u": render_m3u(events, now, config["epg"]),
        "channels.m3u": render_m3u(channels, now, config["epg"]),
        "all.txt": render_txt(buckets, now),
        "status.json": json.dumps(report, ensure_ascii=False, indent=2) + "\n",
    }
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".staging-", dir=out_dir.parent) as staging:
        for name, content in artifacts.items():
            (Path(staging) / name).write_text(content, encoding="utf-8")
        out_dir.mkdir(parents=True, exist_ok=True)
        for name in artifacts:
            os.replace(Path(staging) / name, out_dir / name)


def run(config, out_dir, report_path, now, fixtures=None, probe=None, events_only=False, check_event=None):
    previous = {}
    try:
        previous = json.loads((out_dir / "status.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    if events_only and not previous.get("accepted"):
        raise ValueError("events_only_requires_previous_accepted_channels")
    candidates, sources = [], []
    for spec in config["sources"]:
        if events_only and not spec.get("events"):
            continue
        started = time.monotonic()
        print(f"source_start id={spec['id']}", flush=True)
        try:
            if fixtures:
                text, source_meta = (fixtures / (spec["id"] + ".m3u")).read_text(encoding="utf-8"), {"fixture": True}
            else:
                text, source_meta = source_playlist(spec, config, out_dir.parent / "state", now)
            parsed = parse_playlist(text, spec["id"])
            if not parsed:
                raise ValueError("empty_playlist")
            selected = prepare(parsed, spec, config, now)
            candidates.extend(selected)
            sources.append({"id": spec["id"], "ok": True, "parsed": len(parsed), "selected": len(selected), **source_meta})
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            sources.append({"id": spec["id"], "ok": False, "error": source_error(error)})
        print(f"source_end id={spec['id']} ok={sources[-1]['ok']} seconds={time.monotonic()-started:.1f}", flush=True)
    grouped = defaultdict(list)
    for entry in candidates:
        if not events_only or entry.event_date:
            grouped[entry.identity].append(entry)
    probe = probe or Prober(config)
    buckets, stats, finished = [], Counter(), 0
    ordered = sorted(grouped.values(), key=lambda b: rank(b[0]))
    samples = event_sample_keys(ordered, config.get("event_samples_per_day", 12), now)
    if check_event:
        samples.update(b[0].identity for b in ordered if b[0].event_date and not b[0].upcoming
                       and check_event in b[0].name)
    ordered.sort(key=lambda b: (0 if b[0].identity in samples else 1 if b[0].event_date else 2, rank(b[0])))
    print(f"probe_start channels_and_events={len(ordered)}", flush=True)
    with ThreadPoolExecutor(max_workers=config["workers"]) as pool:
        futures = [pool.submit(select_bucket, entries, config, probe,
                               not entries[0].event_date or entries[0].identity in samples) for entries in ordered]
        for future in as_completed(futures):
            lines, counts = future.result()
            finished += 1
            stats.update(counts)
            if lines:
                buckets.append(lines)
            if finished % 20 == 0 or finished == len(ordered):
                print(f"probe_progress done={finished}/{len(ordered)} accepted={len(buckets)} counts={dict(stats)}", flush=True)
    if events_only:
        saved_channels = defaultdict(list)
        for entry in parse_playlist((out_dir / "all-backup.m3u").read_text(), "last-accepted"):
            if not entry.group.startswith("咪咕·"):
                saved_channels[entry.identity].append(replace(entry, verified=True, check_status="last_accepted"))
        buckets.extend(saved_channels.values())
    errors = quality_errors(buckets, config, now, previous.get("channels", 0))
    if stats["budget"]:
        errors.append("probe_budget_exhausted")
    by_day = {}
    for delta in (-1, 0, 1):
        day = now.date() + timedelta(days=delta)
        entries = [b[0] for b in buckets if b[0].event_date == day]
        by_day[day.isoformat()] = {"listed": len(entries), "verified": sum(e.verified for e in entries),
                                  "upcoming": sum(e.upcoming for e in entries),
                                  "not_checked": sum(e.check_status == "not_checked" and not e.upcoming for e in entries),
                                  "membership_required": sum(e.check_status == "membership_required" or e.attrs.get("_membership_hint", False) for e in entries),
                                  "login_required": sum(e.check_status == "login_required" for e in entries),
                                  "failed_checks": sum(not e.verified and e.check_status not in ("not_checked", "last_accepted") for e in entries)}
    coverage = {}
    for league in ("CBA", "中超"):
        matches = [b[0] for b in buckets if b[0].event_date and b[0].name.startswith(league+" ")]
        coverage[league] = {}
        for day in sorted(set(e.event_date for e in matches)):
            items = [e for e in matches if e.event_date == day]
            coverage[league][day.isoformat()] = {
                "matches": len(set(e.attrs.get("tvg-id") or e.name for e in items)),
                "entries": len(items), "decoded_entries": sum(e.verified for e in items)}
    warnings = []
    if any(not b[0].verified and not b[0].upcoming for b in buckets if b[0].event_date):
        warnings.append("sports_catalog_contains_unverified_entries")
    if stats["membership_required"]:
        warnings.append("some_sports_entries_require_membership")
    if stats["login_required"]:
        warnings.append("some_sports_entries_require_login")
    if any(source.get("cached") for source in sources):
        warnings.append("some_source_catalogues_from_cache")
    report = {"generated_at": now.isoformat(timespec="seconds"),
              "finished_at": datetime.now(now.tzinfo).isoformat(timespec="seconds"), "accepted": not errors,
              "errors": errors, "warnings": warnings, "sources": sources, "channels": sum(not b[0].event_date for b in buckets),
              "events": by_day, "probe": dict(stats), "max_lines": config["max_lines"],
              "event_policy": "complete_catalog_with_sampled_playback_checks", "competition_coverage": coverage,
              "requested_event_check": check_event,
              "mode": "events_only" if events_only else "full",
              "channel_validation_at": (previous.get("channel_validation_at") or previous.get("generated_at")) if events_only else now.isoformat(timespec="seconds"),
              "verification": "decoded_video_and_optional_audio_at_generator_location",
              "decode_seconds": config["decode_seconds"],
              "upcoming_note": "赛前条目未验证可播；开赛后重测。解码成功不证明画面对应赛事或电视端持续播放。"}
    atomic_write(report_path, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    if errors:
        print("quality_gate_rejected " + ",".join(errors), flush=True)
        return 2
    for warning in warnings:
        print("::warning::" + warning, flush=True)
    publish(out_dir, buckets, report, now, config)
    print(f"published channels={report['channels']} events={by_day}", flush=True)
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--out", type=Path, default=ROOT / "public")
    parser.add_argument("--report", type=Path, default=ROOT / "reports/latest.json")
    parser.add_argument("--fixtures", type=Path, help="Use saved source lists, still decode actual streams")
    parser.add_argument("--events-only", action="store_true", help="Refresh the complete sports catalogue; reuse last accepted TV channels")
    parser.add_argument("--check-event", help="Also test every started commentary whose title contains this text")
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    if not 1 <= config["max_lines"] <= 2 or config["candidate_limit"] < config["max_lines"]:
        parser.error("max_lines must be 1 or 2; candidate_limit must cover max_lines")
    for name in ("ffmpeg", "curl"):
        if not shutil.which(name):
            parser.error(name + " is required")
    return run(config, args.out, args.report, datetime.now(ZoneInfo(config["timezone"])), args.fixtures,
               events_only=args.events_only, check_event=args.check_event)


if __name__ == "__main__":
    raise SystemExit(main())
