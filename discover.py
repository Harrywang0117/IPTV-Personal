#!/usr/bin/env python3
"""Bounded discovery of advertised public Migu mirrors, never IP-range scans."""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import hashlib
import html
import ipaddress
import json
from pathlib import Path
import re
import socket
import subprocess
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import pipeline as p

ROOT_PATTERN = re.compile(r'https?://[^\s<>"\'\\/]+/\d{9}(?!\d|[/?&#])')


def safe_root(url, resolve=False):
    try:
        u = urlparse(url)
        if not p.public_url(url) or u.path not in ('', '/') or u.query or u.fragment:
            return False
        if resolve:
            addresses = socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == 'https' else 80), type=socket.SOCK_STREAM)
            if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
                return False
        return True
    except (ValueError, OSError):
        return False


def extract_roots(text):
    result = set()
    for match in ROOT_PATTERN.finditer(html.unescape(text)):
        u = urlparse(match[0])
        root = u.scheme + '://' + u.netloc
        if safe_root(root):
            result.add(root)
    return sorted(result)


def read_feed(url, timeout=18):
    if not p.public_url(url):
        raise ValueError('invalid_feed')
    # Configured public documents only. Do not crawl links, execute scripts, or
    # follow redirects to unknown hosts. The byte and total-time limits apply.
    result = subprocess.run(['curl', '--silent', '--show-error', '--fail', '--max-redirs', '0',
                             '--proto', '=http,https', '--connect-timeout', '5', '--max-time', str(timeout),
                             '--max-filesize', str(8*1024*1024), '--user-agent', p.UA, url], capture_output=True, timeout=timeout+2)
    if result.returncode:
        raise ValueError('discovery_feed_curl_'+str(result.returncode))
    if not result.stdout:
        raise ValueError('discovery_feed_empty')
    if len(result.stdout) > 8*1024*1024:
        raise ValueError('discovery_feed_too_large')
    return result.stdout.decode('utf-8', 'replace')


def active_sources(path, now, config):
    registry = json.loads(path.read_text())
    seeds = {s['url'].rstrip('/') for s in config['sources']}
    sources = []
    for item in registry.get('sources', []):
        if item.get('status') != 'active' or item['url'] in seeds or not safe_root(item['url']):
            continue
        try:
            age = (now-datetime.fromisoformat(item['last_qualified_at'])).total_seconds()
            if not 0 <= age <= config.get('discovery', {}).get('active_max_age_hours', 72)*3600:
                continue
        except (KeyError, ValueError, TypeError):
            continue
        sources.append({'id': item['id'], 'url': item['url'], 'events': True, 'channels': False})
    return sources[:config.get('discovery', {}).get('max_active', 6)]


def observe(item, observation, now, settings):
    item = dict(item)
    history = [o for o in item.get('observations', [])
               if (now-datetime.fromisoformat(o['at'])).total_seconds() <= 7*86400]
    history.append({'at': now.isoformat(), **observation})
    item['observations'] = history[-8:]
    item['last_checked_at'] = now.isoformat()
    if observation['qualified']:
        item['consecutive_failures'] = 0
        item['last_qualified_at'] = now.isoformat()
        successes = [datetime.fromisoformat(o['at']) for o in history if o['qualified']]
        repeated = any((now-t).total_seconds() >= settings.get('promotion_hours', 6)*3600 for t in successes[:-1])
        item['status'] = 'active' if repeated or item.get('seed') else 'probation'
    else:
        item['consecutive_failures'] = item.get('consecutive_failures', 0)+1
        if item.get('status') == 'active' and item['consecutive_failures'] < 2:
            item['status'] = 'active'
        else:
            item['status'] = 'degraded' if item.get('seed') else 'quarantined'
    return item


def sample_entries(entries, now, limit):
    """Cover both active matches and VOD; one restricted league cannot dominate."""
    active, vod, other = [], [], []
    for entry in entries:
        start = p.kickoff(entry, now.tzinfo)
        if start and 0 <= (now-start).total_seconds() <= 4*3600 and '回放' not in entry.name:
            active.append(entry)
        elif '回放' in entry.name:
            vod.append(entry)
        else:
            other.append(entry)
    selected, seen = [], set()
    # Try two distinct ongoing matches and two replays before broadening.
    for group in (active, vod):
        matches = set()
        for entry in sorted(group, key=p.rank):
            match = entry.attrs.get('tvg-id') or entry.name
            if entry.url not in seen and match not in matches:
                selected.append(entry); seen.add(entry.url); matches.add(match)
            if len(matches) == 2:
                break
    for entry in sorted(active+vod+other, key=p.rank):
        if entry.url not in seen:
            selected.append(entry); seen.add(entry.url)
    return selected[:limit]


def check_source(item, config, now):
    spec = {'id': item['id'], 'url': item['url'], 'events': True}
    try:
        if not safe_root(item['url'], resolve=True):
            raise ValueError('invalid_candidate_root')
        # Candidate roots must serve a catalogue themselves. Redirecting roots
        # are quarantined, rather than letting an advertised URL reach a LAN.
        text = read_feed(item['url'], config['fetch_timeout'])
        entries = p.prepare(p.parse_playlist(text, item['id']), spec, config, now)
        events = [e for e in entries if e.event_date]
        missing = p.missing_event_days(events, now)
        started = [e for e in events if not e.upcoming and urlparse(e.url).netloc == urlparse(item['url']).netloc
                   and not urlparse(e.url).query and re.fullmatch(r'/\d{9}', urlparse(e.url).path)]
        prober = p.Prober(dict(config, budget_seconds=150, event_probe_budget_seconds=150, per_host=1))
        results = []
        for entry in sample_entries(started, now, config.get('discovery', {}).get('samples_per_source', 4)):
            result = prober(entry)
            results.append({'event_id': p.event_key(entry), 'ok': result['ok'], 'reason': result['reason'],
                            'kind': result.get('kind'), 'duration_seconds': result.get('duration_seconds')})
        passed = [r for r in results if r['ok']]
        return {'qualified': not missing and len(passed) >= 2, 'catalogue_entries': len(events),
                'missing_dates': [str(d) for d in missing], 'samples': results,
                'capabilities_observed': dict(Counter(r['kind'] for r in passed)), 'cached': False}
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        return {'qualified': False, 'error': p.source_error(error)}


def run(config, registry_path, report_path, now, feeds=None, checker=None):
    settings = config.get('discovery', {})
    try:
        registry = json.loads(registry_path.read_text())
    except (OSError, ValueError):
        registry = {'sources': []}
    by_url = {s['url']: s for s in registry.get('sources', []) if safe_root(s.get('url', ''))}
    for spec in config['sources']:
        if spec['id'].startswith('migu-') and safe_root(spec['url']):
            by_url.setdefault(spec['url'].rstrip('/'), {'url': spec['url'].rstrip('/'), 'id': spec['id'],
                                                      'first_seen_at': now.isoformat(), 'status': 'seed', 'seed': True})
    feed_reports = []
    # Public candidates found outside the runner remain candidates only. This
    # lets a blocked index recover without pretending its current fetch worked.
    candidate_file = settings.get('candidate_file')
    if candidate_file:
        for candidate in json.loads((p.ROOT/candidate_file).read_text()).get('candidates', []):
            root = candidate['url'].rstrip('/')
            if not safe_root(root) or (root not in by_url and len(by_url) >= settings.get('max_registry',100)):
                continue
            item = by_url.setdefault(root, {'url':root, 'id':'migu-discovered-'+hashlib.sha256(root.encode()).hexdigest()[:10],
                                            'first_seen_at':now.isoformat(), 'status':'new'})
            item['discovered_from'] = sorted(set(item.get('discovered_from', [])+[candidate['found_in']]))
    for url in settings.get('feeds', []):
        try:
            text = feeds[url] if feeds is not None else read_feed(url)
            roots = extract_roots(text)
            feed_reports.append({'url': url, 'ok': True, 'candidate_roots': len(roots)})
            for root in roots:
                if root not in by_url and len(by_url) >= settings.get('max_registry', 100):
                    continue
                item = by_url.setdefault(root, {'url': root, 'id': 'migu-discovered-'+hashlib.sha256(root.encode()).hexdigest()[:10],
                                                'first_seen_at': now.isoformat(), 'status': 'new'})
                item['discovered_from'] = sorted(set(item.get('discovered_from', [])+[url]))
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
            feed_reports.append({'url': url, 'ok': False, 'error': p.source_error(error)})
    # Always monitor admitted sources; rotate the others oldest-check first.
    ordered = sorted(by_url.values(), key=lambda s: (0 if s.get('seed') or s['status']=='active' else 1,
                                                     s.get('last_checked_at', ''), s['url']))
    selected = ordered[:settings.get('checks_per_run', 16)]
    checker = checker or check_source
    def checked(item):
        print('discovery_source_start id='+item['id'], flush=True)
        result = checker(item, config, now)
        print('discovery_source_end id='+item['id']+' qualified='+str(result['qualified']), flush=True)
        return result
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(checked, selected))
    changes = []
    for item, result in zip(selected, results):
        updated = observe(item, result, now, settings)
        by_url[item['url']] = updated
        if updated['status'] != item['status']:
            changes.append({'source': item['id'], 'from': item['status'], 'to': updated['status'],
                            'reason': 'qualified_observation' if result['qualified'] else 'failed_validation'})
    registry = {'updated_at': now.isoformat(), 'sources': sorted(by_url.values(), key=lambda s: s['id'])}
    report = {'generated_at': now.isoformat(), 'feeds': feed_reports, 'checked': len(selected),
              'registered': len(by_url), 'status_counts': dict(Counter(i['status'] for i in by_url.values())),
              'changes': changes, 'note': '发现不等于可播；新源至少两次相隔六小时通过三天目录和两条媒体检测才入池。节目仍须逐条通过发布验收。'}
    p.atomic_write(registry_path, json.dumps(registry, ensure_ascii=False, indent=2)+'\n')
    p.atomic_write(report_path, json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if any(r['ok'] for r in feed_reports) else 2


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, default=p.ROOT/'config.json')
    parser.add_argument('--registry', type=Path, default=p.ROOT/'state/upstreams.json')
    parser.add_argument('--report', type=Path, default=p.ROOT/'reports/discovery.json')
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    return run(config, args.registry, args.report, datetime.now(ZoneInfo(config['timezone'])))


if __name__ == '__main__':
    raise SystemExit(main())
