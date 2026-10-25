"""Bounded picture samples for Actions artifacts; no semantic match recognition."""
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict
from datetime import datetime
import html
import json
from pathlib import Path
import subprocess
import time
import uuid

import pipeline as p


def capture(entry, offset, path, prober, timeout):
    user_agent, referer = p.UA, None
    for option in entry.options:
        if option.startswith('#EXTVLCOPT:http-user-agent='):
            user_agent = option.split('=', 1)[1]
        elif option.startswith(('#EXTVLCOPT:http-referrer=', '#EXTVLCOPT:http-referer=')):
            referer = option.split('=', 1)[1]
    if any(c in user_agent + (referer or '') for c in '\r\n'):
        return 'invalid_headers'
    cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
           '-rw_timeout', '5000000', '-protocol_whitelist', 'http,https,tcp,tls,crypto',
           '-user_agent', user_agent]
    if referer:
        cmd += ['-referer', referer]
    tail = (['-ss', str(offset)] if offset else []) + [
        '-threads', '1', '-i', entry.url, '-map', '0:v:0', '-frames:v', '1',
        '-vf', 'scale=640:360:force_original_aspect_ratio=decrease',
        '-q:v', '5', '-threads', '1', '-update', '1', str(path)]
    started = time.monotonic()
    options = prober.hls_options
    try:
        for attempt in range(3):
            remaining = timeout - (time.monotonic() - started)
            if remaining < .1:
                return 'timeout'
            result = subprocess.run(cmd + options + tail, capture_output=True, text=True, timeout=remaining)
            if result.returncode == 0 and path.exists():
                data = path.read_bytes()
                if data.startswith(b'\xff\xd8') and data.endswith(b'\xff\xd9'):
                    return 'captured'
            if attempt < 2 and 'Option' in result.stderr and 'not found' in result.stderr:
                options = []
            elif attempt < 2 and 'Not detecting m3u8/hls with non standard extension' in result.stderr:
                options = prober.hls_options + ['-f', 'hls']
            else:
                return 'capture_failed'
    except subprocess.TimeoutExpired:
        return 'timeout'
    except OSError:
        return 'capture_failed'
    return 'capture_failed'


def audit_frames(buckets, config, prober, directory, now, requested=()):
    settings = config.get('frame_audit', {})
    deadline = time.monotonic() + min(60, settings.get('budget_seconds', 60))
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    entries = [b[0] for b in buckets if b[0].event_date and not b[0].upcoming
               and (b[0].verified or b[0].check_status == 'observation_pending')]
    entries.sort(key=lambda e: (e.event_date != now.date(), not e.verified, p.rank(e)))
    explicit = [e for e in entries if any(t in e.name for t in requested)]
    groups = defaultdict(list)
    for entry in entries:
        if entry not in explicit:
            groups[(entry.event_date, entry.name.split()[0])].append(entry)
    priorities = {'CBA': 0, 'NBA': 1, '中超': 2}
    keys = sorted(groups, key=lambda k: (priorities.get(k[1], 3), k[0] != now.date(), k))
    # Rotate diagnostics, without changing the playlist or its selected route.
    for key in keys:
        shift = int(now.timestamp() // 3600) % len(groups[key])
        groups[key] = groups[key][shift:] + groups[key][:shift]
    entries = explicit + [groups[key][i] for i in range(max((len(v) for v in groups.values()), default=0))
                          for key in keys if i < len(groups[key])]
    # One route per match; cap work rather than multiplying every commentary.
    selected, seen = [], set()
    for entry in entries:
        match = (entry.event_date, entry.name.split()[0], entry.attrs.get('tvg-id') or entry.name)
        if match not in seen:
            seen.add(match)
            selected.append(entry)
        if len(selected) >= min(6, settings.get('max_entries', 6)):
            break
    run_id = uuid.uuid4().hex[:12]
    jobs = []
    for entry in selected:
        offsets = entry.evidence.get('sample_offsets', [0])[:3]
        for offset in offsets:
            filename = f'{run_id}-{p.event_key(entry)}-{entry.fingerprint}-{offset}.jpg'
            jobs.append((entry, offset, filename))

    def one(job):
        entry, offset, filename = job
        remaining = deadline - time.monotonic()
        status = capture(entry, offset, directory / filename, prober, min(10, remaining)) if remaining > .5 else 'budget'
        return {'title': entry.name, 'event_date': str(entry.event_date), 'source': entry.source,
                'route_fingerprint': entry.fingerprint, 'offset_seconds': offset, 'status': status,
                'captured_at': datetime.now(now.tzinfo).isoformat(timespec='seconds'),
                'file': filename if status == 'captured' else None}

    with ThreadPoolExecutor(max_workers=3) as pool:
        records = list(pool.map(one, jobs))
    report = {'captured': sum(r['status'] == 'captured' for r in records), 'attempted': len(records),
              'entries_sampled': len(selected), 'black_detection_available': bool(getattr(prober, 'black_detection', False)),
              'semantic_match_checked': False, 'records': records,
              'note': '截图为有限抽样；黑屏检查覆盖本轮视频解码片段。未自动识别球队、比分、会员提示或画面与标题是否一致；截图失败不会冒充播放检测失败。'}
    (directory / 'manifest.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    body = ['<!doctype html><meta charset="utf-8"><title>赛事画面抽样</title>',
            '<style>body{font:16px sans-serif;max-width:1000px;margin:24px auto}img{max-width:100%}figure{display:inline-block;width:46%;vertical-align:top;margin:1%}</style>',
            '<h1>赛事画面抽样</h1><p>' + html.escape(report['note']) + '</p>']
    for record in records:
        caption = f"{record['event_date']} {record['title']} · {record['offset_seconds']}秒 · {record['status']}"
        body.append('<figure>' + (f'<img src="{record["file"]}" loading="lazy">' if record['file'] else '')
                    + '<figcaption>' + html.escape(caption) + '</figcaption></figure>')
    (directory / 'index.html').write_text('\n'.join(body), encoding='utf-8')
    return report
