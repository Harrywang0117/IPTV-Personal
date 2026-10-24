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


class Prober:
    def __init__(self, config):
        self.config = config
        self.deadline = time.monotonic() + config["budget_seconds"]
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
        remaining = self.deadline - time.monotonic()
        if remaining <= 0 or not semaphore.acquire(timeout=max(0, remaining)):
            return {"ok": False, "reason": "budget", "frames": 0}
        try:
            remaining = self.deadline - time.monotonic()
            if remaining < 1:
                return {"ok": False, "reason": "budget", "frames": 0}
            return self.decode(entry, min(self.config["probe_timeout"], remaining))
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


def select_bucket(entries, config, probe):
    lines, stats = [], Counter()
    for entry in candidate_order(entries, config["candidate_limit"]):
        if entry.upcoming:
            lines.append(entry)
            stats["upcoming_lines"] += 1
        else:
            result = probe(entry)
            stats[result["reason"]] += 1
            if result["ok"]:
                lines.append(replace(entry, verified=True))
        if len(lines) >= config["max_lines"]:
            break
    return lines, stats


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
    title = re.sub(r"\s*\d{1,2}:\d{2}\s*$", "", entry.name).strip()
    title = re.sub(r"^\d{1,2}:\d{2}\s+", "", title)
    return f"{hour:02d}:{minute:02d} {title}"


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
                attrs["tvg-id"] = "event-" + hashlib.sha256(repr(entry.identity).encode()).hexdigest()[:16]
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
        elif delta <= 0 and sum(e.verified for e in items) < config["minimum_verified_per_started_day"]:
            errors.append(f"no_verified_events_{day}")
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


def run(config, out_dir, report_path, now, fixtures=None, probe=None):
    candidates, sources = [], []
    for spec in config["sources"]:
        started = time.monotonic()
        print(f"source_start id={spec['id']}", flush=True)
        try:
            text = ((fixtures / (spec["id"] + ".m3u")).read_text(encoding="utf-8") if fixtures
                    else fetch_playlist(spec, config["fetch_timeout"]))
            parsed = parse_playlist(text, spec["id"])
            if not parsed:
                raise ValueError("empty_playlist")
            selected = prepare(parsed, spec, config, now)
            candidates.extend(selected)
            sources.append({"id": spec["id"], "ok": True, "parsed": len(parsed), "selected": len(selected)})
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            reason = type(error).__name__
            if isinstance(error, ValueError) and re.fullmatch(r"[a-z_]+(?:\d+)?", str(error)):
                reason = str(error)
            elif isinstance(error, subprocess.TimeoutExpired):
                reason = "source_total_timeout"
            sources.append({"id": spec["id"], "ok": False, "error": reason})
        print(f"source_end id={spec['id']} ok={sources[-1]['ok']} seconds={time.monotonic()-started:.1f}", flush=True)
    grouped = defaultdict(list)
    for entry in candidates:
        grouped[entry.identity].append(entry)
    probe = probe or Prober(config)
    buckets, stats, finished = [], Counter(), 0
    ordered = sorted(grouped.values(), key=lambda b: rank(b[0]))
    print(f"probe_start channels_and_events={len(ordered)}", flush=True)
    with ThreadPoolExecutor(max_workers=config["workers"]) as pool:
        futures = [pool.submit(select_bucket, entries, config, probe) for entries in ordered]
        for future in as_completed(futures):
            lines, counts = future.result()
            finished += 1
            stats.update(counts)
            if lines:
                buckets.append(lines)
            if finished % 20 == 0 or finished == len(ordered):
                print(f"probe_progress done={finished}/{len(ordered)} accepted={len(buckets)} counts={dict(stats)}", flush=True)
    previous = {}
    try:
        previous = json.loads((out_dir / "status.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    errors = quality_errors(buckets, config, now, previous.get("channels", 0))
    if stats["budget"]:
        errors.append("probe_budget_exhausted")
    by_day = {}
    for delta in (-1, 0, 1):
        day = now.date() + timedelta(days=delta)
        entries = [b[0] for b in buckets if b[0].event_date == day]
        by_day[day.isoformat()] = {"listed": len(entries), "verified": sum(e.verified for e in entries),
                                  "upcoming": sum(e.upcoming for e in entries)}
    report = {"generated_at": now.isoformat(timespec="seconds"),
              "finished_at": datetime.now(now.tzinfo).isoformat(timespec="seconds"), "accepted": not errors,
              "errors": errors, "sources": sources, "channels": sum(not b[0].event_date for b in buckets),
              "events": by_day, "probe": dict(stats), "max_lines": config["max_lines"],
              "verification": "decoded_video_and_optional_audio_at_generator_location",
              "decode_seconds": config["decode_seconds"],
              "upcoming_note": "赛前条目未验证可播；开赛后重测。解码成功不证明画面对应赛事或电视端持续播放。"}
    atomic_write(report_path, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    if errors:
        print("quality_gate_rejected " + ",".join(errors), flush=True)
        return 2
    publish(out_dir, buckets, report, now, config)
    print(f"published channels={report['channels']} events={by_day}", flush=True)
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--out", type=Path, default=ROOT / "public")
    parser.add_argument("--report", type=Path, default=ROOT / "reports/latest.json")
    parser.add_argument("--fixtures", type=Path, help="Use saved source lists, still decode actual streams")
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    if not 1 <= config["max_lines"] <= 2 or config["candidate_limit"] < config["max_lines"]:
        parser.error("max_lines must be 1 or 2; candidate_limit must cover max_lines")
    for name in ("ffmpeg", "curl"):
        if not shutil.which(name):
            parser.error(name + " is required")
    return run(config, args.out, args.report, datetime.now(ZoneInfo(config["timezone"])), args.fixtures)


if __name__ == "__main__":
    raise SystemExit(main())
