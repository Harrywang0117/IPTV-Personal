import copy
from datetime import date, datetime
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pipeline as p


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 24, 17, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.config = json.loads((p.ROOT / "config.json").read_text())
        self.spec = self.config["sources"][0]

    def test_txt_extension_may_contain_m3u_and_header_catchup(self):
        text = '\ufeff#EXTM3U catchup="append" catchup-source="?start={utc}"\n'
        text += '#EXTINF:-1 tvg-logo="https://example.com/a,b.png" group-title="央视",CCTV1\n'
        text += '#EXTVLCOPT:http-user-agent=CustomPlayer\nhttps://example.com/stream\n'
        entry = p.parse_playlist(text)[0]
        self.assertEqual(entry.name, "CCTV1")
        self.assertEqual(entry.attrs["tvg-logo"], "https://example.com/a,b.png")
        self.assertEqual(entry.attrs["catchup-source"], "?start={utc}")
        self.assertIn("CustomPlayer", entry.options[0])

    def test_txt_lines(self):
        text = "卫视,#genre#\n浙江卫视,https://example.com/a$主线#https://backup.example.com/b$备线\n"
        entries = p.parse_playlist(text)
        self.assertEqual([e.url for e in entries], ["https://example.com/a", "https://backup.example.com/b"])
        self.assertEqual(entries[0].group, "卫视")

    def test_private_and_client_only_entries_are_rejected(self):
        for url in ["http://127.0.0.1:9978/a", "http://192.168.1.1/a", "http://localhost/a", "file:///tmp/a", "http://[::1]/a"]:
            self.assertFalse(p.public_url(url), url)

    def test_explicit_month_day_overrides_stale_relative_label(self):
        self.assertEqual(p.event_date("体育-昨天10-20", self.now.date()), date(2026, 10, 20))
        entry = p.Entry("足球 A VS B 20:00", "体育-昨天10-20", "https://example.com/a")
        self.assertEqual(p.prepare([entry], self.spec, self.config, self.now), [])

    def test_december_january_rollover(self):
        self.assertEqual(p.event_date("体育-明天01-01", date(2026, 12, 31)), date(2027, 1, 1))
        self.assertEqual(p.event_date("体育-昨天12-31", date(2027, 1, 1)), date(2026, 12, 31))

    def test_invalid_date_is_rejected(self):
        self.assertIsNone(p.event_date("体育-昨天02-30", self.now.date()))

    def test_explicit_year_wins(self):
        self.assertEqual(p.event_date("体育-今天2025-10-24", self.now.date()), date(2025, 10, 24))

    def test_today_future_is_upcoming_and_event_catchup_removed(self):
        entry = p.Entry("足球 A VS B 20:00", "体育-今天10-24", "https://example.com/a", {"catchup": "append"})
        entry = p.prepare([entry], self.spec, self.config, self.now)[0]
        self.assertTrue(entry.upcoming)
        self.assertNotIn("catchup", entry.attrs)
        self.assertIn("待开赛", p.display_group(entry, self.now))

    def test_no_shared_event_tvg_id_merge(self):
        a = p.Entry("比赛A 回放", "", "https://example.com/a", {"tvg-id": "same"}, event_date=self.now.date())
        b = p.Entry("比赛B 回放", "", "https://example.com/b", {"tvg-id": "same"}, event_date=self.now.date())
        self.assertNotEqual(a.identity, b.identity)

    def test_channel_normalization(self):
        a = p.Entry("CCTV-1 HD", "央视", "https://example.com/a")
        b = p.Entry("CCTV1综合", "央视", "https://example.com/b")
        self.assertEqual(a.identity, b.identity)

    def test_cctv4_regional_schedules_not_merged(self):
        a = p.Entry("CCTV4中文国际", "央视", "https://example.com/a")
        b = p.Entry("CCTV4欧洲", "央视", "https://example.com/b")
        c = p.Entry("CCTV4美洲", "央视", "https://example.com/c")
        self.assertEqual(len({a.identity, b.identity, c.identity}), 3)

    def test_limit_is_applied_after_decode(self):
        entries = [p.Entry("CCTV1", "央视", f"https://source{i}.example.com/a") for i in range(5)]
        def probe(entry):
            return {"ok": "source0" not in entry.url, "reason": "decoded" if "source0" not in entry.url else "decode_failed"}
        kept, stats = p.select_bucket(entries, self.config, probe)
        self.assertEqual(len(kept), 2)
        self.assertTrue(all(e.verified for e in kept))
        self.assertEqual(stats["decode_failed"], 1)
        self.assertIn("source1", kept[0].url)

    def test_distinct_hosts_are_prioritized(self):
        entries = [p.Entry("A", "", url) for url in ["https://one.example/a", "https://one.example/b", "https://two.example/a"]]
        self.assertIn("two.example", p.candidate_order(entries, 2)[1].url)

    def test_duplicate_urls_not_counted_as_backup(self):
        entry = p.Entry("CCTV1", "央视", "https://example.com/a")
        self.assertEqual(len(p.candidate_order([entry, copy.deepcopy(entry)], 6)), 1)

    def test_upcoming_not_tested_or_labelled_verified(self):
        entry = p.Entry("足球A", "", "https://example.com/a", event_date=self.now.date()+p.timedelta(days=1), upcoming=True)
        with patch("builtins.print"):
            kept, _ = p.select_bucket([entry], self.config, lambda e: self.fail("future event was probed"))
        self.assertFalse(kept[0].verified)
        self.assertIn("明天预告", p.render_m3u([kept], self.now, []))

    def test_no_verified_yesterday_fails_gate(self):
        cfg = dict(self.config, minimum_channels=0)
        entries = [[p.Entry("A", "", f"https://example.com/{delta}", event_date=self.now.date()+p.timedelta(days=delta), upcoming=True)] for delta in [-1,0,1]]
        self.assertIn("no_verified_events_2026-10-23", p.quality_errors(entries, cfg, self.now))

    def test_channel_drop_fails_gate(self):
        cfg = dict(self.config, minimum_channels=0)
        self.assertIn("channel_count_dropped", p.quality_errors([], cfg, self.now, previous_channels=100))

    def test_default_one_line_backup_two(self):
        entries = [[p.Entry("CCTV1", "央视", f"https://source{i}.example.com/a", verified=True) for i in range(2)]]
        main = p.parse_playlist(p.render_m3u(entries, self.now, []))
        backup = p.parse_playlist(p.render_m3u(entries, self.now, [], single=False))
        self.assertEqual(len(main), 1)
        self.assertEqual(len(backup), 2)
        self.assertIn("#EXTVLCOPT:http-user-agent=" + p.UA, main[0].options)

    def test_redirect_or_headers_without_frames_never_pass(self):
        prober = object.__new__(p.Prober)
        prober.config = self.config
        prober.hls_options = []
        result = subprocess.CompletedProcess([], 0, "frame=0\nout_time_us=3000000\n", "")
        with patch("pipeline.subprocess.run", return_value=result):
            self.assertFalse(prober.decode(p.Entry("A", "", "https://example.com/a"), 1)["ok"])

    def test_decoded_frames_with_duration_pass(self):
        prober = object.__new__(p.Prober)
        prober.config = self.config
        prober.hls_options = []
        result = subprocess.CompletedProcess([], 0, "frame=75\nout_time_us=3000000\n", "")
        with patch("pipeline.subprocess.run", return_value=result):
            self.assertTrue(prober.decode(p.Entry("A", "", "https://example.com/a"), 1)["ok"])

    def test_decode_total_timeout(self):
        prober = object.__new__(p.Prober)
        prober.config = self.config
        prober.hls_options = []
        with patch("pipeline.subprocess.run", side_effect=subprocess.TimeoutExpired("ffmpeg", 1)):
            result = prober.decode(p.Entry("A", "", "https://example.com/a"), 1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "timeout")

    def test_mp4_replay_retries_without_hls_only_options(self):
        prober = object.__new__(p.Prober)
        prober.config = self.config
        prober.hls_options = ["-allowed_extensions", "ALL"]
        failed = subprocess.CompletedProcess([], 1, "", "Option allowed_extensions not found.")
        passed = subprocess.CompletedProcess([], 0, "frame=75\nout_time_us=3000000\n", "")
        with patch("pipeline.subprocess.run", side_effect=[failed, passed]) as run:
            result = prober.decode(p.Entry("回放", "", "https://example.com/a"), 18)
        self.assertTrue(result["ok"])
        self.assertIn("-allowed_extensions", run.call_args_list[0].args[0])
        self.assertNotIn("-allowed_extensions", run.call_args_list[1].args[0])
        self.assertLessEqual(run.call_args_list[1].kwargs["timeout"], 18)

    def test_continuous_flv_is_not_playlist(self):
        result = subprocess.CompletedProcess([], 0, b"FLV" + b"x"*100, b"")
        with patch("pipeline.subprocess.run", return_value=result):
            with self.assertRaisesRegex(ValueError, "media"):
                p.fetch_playlist(self.spec, 1)

    def test_failed_run_preserves_accepted_subscription(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "public"
            out.mkdir()
            (out / "all.m3u").write_text("previous accepted output")
            config = dict(self.config, sources=[self.spec])
            with patch("pipeline.fetch_playlist", side_effect=ValueError("offline")), patch("builtins.print"):
                code = p.run(config, out, root / "report.json", self.now, probe=lambda e: {})
            self.assertEqual(code, 2)
            self.assertEqual((out / "all.m3u").read_text(), "previous accepted output")
            self.assertFalse(json.loads((root / "report.json").read_text())["accepted"])


if __name__ == "__main__":
    unittest.main()
