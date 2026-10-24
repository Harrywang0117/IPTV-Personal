"""Short, redacted environment comparison; does not publish subscriptions."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys
import urllib.error
import urllib.request
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import pipeline as p


def main():
    config = json.loads((p.ROOT / "config.json").read_text())
    probe = p.Prober(config)
    now = datetime.now(ZoneInfo(config["timezone"]))
    samples, sources = [], []
    for spec in config["sources"]:
        if not spec.get("events"):
            continue
        try:
            entries = p.parse_playlist(p.fetch_playlist(spec, config["fetch_timeout"]), spec["id"])
            sources.append({"id": spec["id"], "entries": len(entries),
                            "csl_today": sum("中超" in e.name and "今天" in e.group for e in entries)})
            filters = {
                "yesterday": lambda e: "昨天" in e.group,
                "today_replay": lambda e: "今天" in e.group and "NBA" in e.name and "回放" in e.name,
                "today_csl": lambda e: "今天" in e.group and "中超" in e.name,
                "cctv1": lambda e: e.name == "CCTV1综合",
            }
            for kind, predicate in filters.items():
                sample = next((e for e in entries if predicate(e)), None)
                if sample:
                    samples.append((kind, sample))
        except Exception as error:
            sources.append({"id": spec["id"], "error": type(error).__name__})

    def test(sample):
        kind, entry = sample
        return {"source": entry.source, "kind": kind, "title": entry.name, **probe(entry)}

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(test, samples))

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    def transport_test(sample):
        kind, entry = sample
        opener = urllib.request.build_opener(NoRedirect)
        try:
            response = opener.open(urllib.request.Request(entry.url, headers={"User-Agent": p.UA}), timeout=5)
            response.close()
            return {"kind": kind, "error": "no_redirect"}
        except urllib.error.HTTPError as response:
            location = response.headers.get("Location", "")
        except Exception as error:
            return {"kind": kind, "error": type(error).__name__}
        parsed = urlsplit(location)
        if not (parsed.hostname or "").endswith(".miguvideo.com"):
            return {"kind": kind, "error": "not_migu_cdn"}
        checks = []
        for scheme in ("http", "https"):
            url = urlunsplit((scheme, parsed.netloc, parsed.path, parsed.query, parsed.fragment))
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": p.UA}), timeout=5) as response:
                    head = response.read(128)
                    is_media = head.lstrip().startswith(b"#EXTM3U") or head.startswith(b"FLV")
                    item = {"scheme": scheme, "status": response.status, "media_prefix": is_media}
                if is_media:
                    item["decode"] = probe(p.replace(entry, url=url))
                checks.append(item)
            except Exception as error:
                checks.append({"scheme": scheme, "error": type(error).__name__})
        return {"kind": kind, "cdn_host": parsed.hostname, "checks": checks}

    transport_samples = [s for s in samples if s[1].source == "migu-primary" and s[0] in ("yesterday", "cctv1")]
    with ThreadPoolExecutor(max_workers=2) as pool:
        transports = list(pool.map(transport_test, transport_samples))
    ffmpeg = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=5)
    report = {"at": now.isoformat(), "python": sys.version.split()[0],
              "ffmpeg": ffmpeg.stdout.splitlines()[0], "hls_options": probe.hls_options,
              "sources": sources, "samples": results, "transports": transports}
    p.atomic_write(Path("reports/diagnostic.json"), json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
