"""Compare identical public VOD entrypoints without publishing or keeping signed URLs."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import os
import socket
import subprocess
import tempfile
import time
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import pipeline as p


def redact(text):
    return re.sub(r'https?://[^\s<>"\']+', '[url]', text)[-1800:]


def duration_result(result):
    duration = None
    try:
        value = float(json.loads(result.stdout).get('format', {}).get('duration', 0))
        if value > 0:
            duration = round(value, 3)
    except (ValueError, TypeError):
        pass
    return {'returncode': result.returncode, 'duration_seconds': duration,
            'error': redact(result.stderr) if result.returncode or duration is None else ''}


def metadata(entry, profile, hls_options, timeout=10):
    protocols = 'http,https,tcp,tls,crypto' + (',httpproxy' if profile == 'proxy_protocol' else '')
    cmd = ['ffprobe', '-v', 'error', '-rw_timeout', '5000000', '-protocol_whitelist', protocols,
           '-user_agent', p.UA]
    if profile in ('all_extensions', 'forced_hls'):
        cmd += hls_options
    if profile == 'forced_hls':
        cmd += ['-f', 'hls']
    cmd += ['-show_entries', 'format=duration', '-of', 'json', entry.url]
    started = time.monotonic()
    try:
        result = duration_result(subprocess.run(cmd, capture_output=True, text=True, timeout=timeout))
    except subprocess.TimeoutExpired as error:
        stderr = error.stderr or ''
        if isinstance(stderr, bytes):
            stderr = stderr.decode('utf-8', 'replace')
        result = {'returncode': None, 'duration_seconds': None, 'error': redact(stderr), 'total_timeout': True}
    return {'profile': profile, 'seconds': round(time.monotonic()-started, 2), **result}


def transport(url, long_connect=False):
    """Follow normal redirects; retain only media type and CDN host, never query tokens."""
    with tempfile.TemporaryDirectory(prefix='iptv-transport-') as tmp:
        body = Path(tmp)/'body'
        headers = Path(tmp)/'headers'
        cmd = ['curl', '--silent', '--show-error', '--location', '--max-redirs', '5',
               '--proto', '=http,https', '--proto-redir', '=http,https',
               '--connect-timeout', '15' if long_connect else '5', '--max-time', '25' if long_connect else '12',
               '--max-filesize', '65536', '--range', '0-1023', '--user-agent', p.UA,
               '--output', str(body), '--dump-header', str(headers),
               '--write-out', '%{http_code}\n%{content_type}\n%{url_effective}', url]
        started = time.monotonic()
        try:
            response = subprocess.run(cmd, capture_output=True, text=True, timeout=28)
            values = response.stdout.splitlines()
            head = body.read_bytes()[:2048] if body.exists() else b''
            kind = ('hls' if head.lstrip().startswith(b'#EXTM3U') else 'flv' if head.startswith(b'FLV') else
                    'mp4' if head[4:8] == b'ftyp' else 'json' if head.lstrip().startswith((b'{', b'[')) else 'other')
            result = {'returncode': response.returncode, 'status': values[0] if values else '',
                      'content_type': values[1] if len(values)>1 else '',
                      'final_host': urlparse(values[2]).hostname if len(values)>2 else None,
                      'media_prefix': kind, 'error': redact(response.stderr)}
        except subprocess.TimeoutExpired:
            result = {'returncode': None, 'error': 'process_total_timeout', 'media_prefix': 'other'}
        return {'connect_timeout': 15 if long_connect else 5, 'seconds':round(time.monotonic()-started,2), **result}


def tcp_check(host):
    if not host or not host.endswith('.miguvideo.com'):
        return []
    result = []
    for timeout in (5, 15):
        started = time.monotonic()
        try:
            with socket.create_connection((host, 80), timeout=timeout):
                item = {'ok': True}
        except OSError as error:
            item = {'ok': False, 'error': type(error).__name__, 'errno': error.errno}
        result.append({'timeout':timeout,'seconds':round(time.monotonic()-started,2),**item})
        if item['ok']:
            break
    return result


def validate_targets(data):
    entries = []
    if not 1 <= len(data['targets']) <= 6:
        raise ValueError('one_to_six_targets_required')
    for item in data['targets']:
        url = item['url']; parsed = urlparse(url)
        if not p.public_url(url) or parsed.query or parsed.fragment or not re.fullmatch(r'/\d{9}', parsed.path):
            raise ValueError('only_public_unsigned_content_entrypoints_allowed')
        entries.append(p.Entry(item['name'], '', url, source=item['source']))
    return entries


def run(target_path, output, environment):
    target_bytes = target_path.read_bytes()
    entries = validate_targets(json.loads(target_bytes))
    config = json.loads((p.ROOT/'config.json').read_text())
    probe = p.Prober(dict(config, decode_seconds=5))
    def check(entry):
        print('vod_diagnostic_start '+entry.source+' '+entry.name, flush=True)
        hint = p.access_hint(entry.url, 2)
        quick = transport(entry.url)
        network = [quick]
        if quick['media_prefix'] == 'other':
            network.append(transport(entry.url, long_connect=True))
        reachable = any(r.get('status','').startswith('2') and r['returncode'] in (0,63) for r in network)
        profiles = ['baseline','all_extensions','forced_hls','proxy_protocol'] if reachable else ['baseline','proxy_protocol']
        variants = [metadata(entry, profile, probe.hls_options) for profile in profiles]
        row = {'name':entry.name,'source':entry.source,'url':entry.url,'access_hint':hint,'transport':network,
               'metadata':variants,'cdn_tcp':tcp_check(network[-1].get('final_host'))}
        if reachable:
            row['production_decode'] = probe.decode(entry, 25)
        print('vod_diagnostic_end '+entry.source+' '+entry.name, flush=True)
        return row
    started = datetime.now(ZoneInfo(config['timezone']))
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(check, entries))
    versions = {}
    for command in ('ffmpeg','ffprobe'):
        result = subprocess.run([command,'-version'],capture_output=True,text=True,timeout=5)
        versions[command] = result.stdout.splitlines()[0]
    report = {'started_at':started.isoformat(timespec='seconds'),
              'finished_at':datetime.now(started.tzinfo).isoformat(timespec='seconds'),
              'environment':environment,'versions':versions,'hls_options':probe.hls_options,
              'proxy_environment_keys':[key for key in ('http_proxy','https_proxy','HTTP_PROXY','HTTPS_PROXY','ALL_PROXY') if os.environ.get(key)],
              'targets_sha256':hashlib.sha256(target_bytes).hexdigest(),'samples':results}
    p.atomic_write(output,json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print('vod_diagnostic_complete targets='+str(len(results)),flush=True)
    return report


if __name__ == '__main__':
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('--targets',type=Path,default=p.ROOT/'diagnostic-targets.json')
    parser.add_argument('--out',type=Path,default=p.ROOT/'reports/vod-comparison.json')
    parser.add_argument('--environment',choices=['local','github'],default='local')
    args=parser.parse_args()
    run(args.targets,args.out,args.environment)
