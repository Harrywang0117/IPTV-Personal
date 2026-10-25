import copy
from datetime import date, datetime
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
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
        started = p.replace(entry, upcoming=False)
        self.assertEqual(p.display_group(entry, self.now), p.display_group(started, self.now))
        self.assertEqual(p.display_group(entry, self.now), "咪咕·今天 10-24")

    def test_no_shared_event_tvg_id_merge(self):
        a = p.Entry("比赛A 回放", "", "https://example.com/a", {"tvg-id": "same"}, event_date=self.now.date())
        b = p.Entry("比赛B 回放", "", "https://example.com/b", {"tvg-id": "same"}, event_date=self.now.date())
        self.assertNotEqual(a.identity, b.identity)

    def test_migu_content_majority_repairs_wrong_fixture_without_changing_url(self):
        correct = [p.Entry("UFC 正赛 21:30", "体育-昨天10-23", f"https://source{i}.example/968627135",
                           {"tvg-id": "UFC333"}, source=source, event_date=date(2026,10,23))
                   for i,source in enumerate(["migu-primary", "migu-alt", "migu-backup"])]
        wrong = p.Entry("英超 A VS B 21:30", "体育-今天10-24", correct[1].url, {"tvg-id": "A VS B"},
                        source="migu-alt", event_date=self.now.date(), upcoming=True)
        fixed, changes = p.reconcile_migu_metadata(correct+[wrong])
        self.assertEqual(fixed[-1].identity, correct[0].identity)
        self.assertEqual(fixed[-1].url, wrong.url)
        self.assertFalse(fixed[-1].upcoming)
        self.assertEqual(fixed[-1].source, "migu-alt")
        self.assertEqual(len(changes), 1)

    def test_repeated_rows_from_one_source_do_not_outvote_another(self):
        a = p.Entry("比赛A 19:00", "", "https://one.example/968627135", {"tvg-id": "A"},
                    source="migu-primary", event_date=self.now.date())
        b = p.replace(a, name="比赛B 19:00", source="migu-alt", attrs={"tvg-id": "B"})
        original = [a]+[copy.deepcopy(b) for _ in range(5)]
        fixed, changes = p.reconcile_migu_metadata(original)
        self.assertEqual(changes, [])
        self.assertEqual([e.identity for e in fixed], [e.identity for e in original])

    def test_shared_schedule_id_does_not_merge_different_migu_content(self):
        entries = [p.Entry(name, "", f"https://{source}.example/{content}", {"tvg-id": "same_schedule"},
                           source=source, event_date=self.now.date())
                   for content,name in [("968627135","斯诺克 比赛A 回放"),("968627136","斯诺克 比赛B 回放")]
                   for source in ["migu-primary","migu-alt"]]
        fixed, changes = p.reconcile_migu_metadata(entries)
        self.assertEqual(changes, [])
        self.assertEqual(len({e.identity for e in fixed}), 2)

    def test_long_running_same_fixture_is_not_forced_to_majority_date(self):
        yesterday = p.Entry("BWF 1号场地", "", "https://one.example/968627135", {"tvg-id": "BWF"},
                            source="migu-primary", event_date=self.now.date()-p.timedelta(days=1))
        today = p.replace(yesterday, source="migu-backup", event_date=self.now.date())
        entries = [yesterday,p.replace(yesterday,source="migu-alt"),today]
        fixed, changes = p.reconcile_migu_metadata(entries)
        self.assertEqual(changes, [])
        self.assertEqual(fixed[-1].event_date, self.now.date())

    def test_same_competition_fresh_match_title_is_not_rewritten_by_stale_majority(self):
        old = p.Entry("CBA A VS B 19:00", "", "https://one.example/968627135", {"tvg-id": "A VS B"},
                      source="migu-primary", event_date=self.now.date())
        new = p.replace(old, name="CBA C VS D 19:00", attrs={"tvg-id": "C VS D"}, source="migu-backup")
        fixed, changes = p.reconcile_migu_metadata([old,p.replace(old,source="migu-alt"),new])
        self.assertEqual(changes, [])
        self.assertEqual(fixed[-1].name,new.name)

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

    def test_catalog_presence_does_not_require_cloud_video_reachability(self):
        cfg = dict(self.config, minimum_channels=0)
        entries = [[p.Entry("A", "", f"https://example.com/{delta}", event_date=self.now.date()+p.timedelta(days=delta), upcoming=True)] for delta in [-1,0,1]]
        self.assertEqual(p.quality_errors(entries, cfg, self.now), [])

    def test_failed_sports_checks_preserve_catalogue_but_not_verified_status(self):
        entry = p.Entry("CBA A VS B 19:25", "", "https://example.com/a", event_date=self.now.date())
        kept, stats = p.select_bucket([entry], self.config, lambda e: {"ok": False, "reason": "timeout"})
        self.assertEqual(len(kept), 1)
        self.assertFalse(kept[0].verified)
        self.assertEqual(kept[0].check_status, "timeout")
        self.assertEqual(stats["timeout"], 1)
        self.assertIn("本轮未测通", p.display_name(kept[0]))

    def test_budget_skipped_sports_are_not_misreported_as_failed(self):
        entry = p.Entry("英超 A VS B 19:00", "", "https://example.com/a", event_date=self.now.date())
        kept, _ = p.select_bucket([entry], self.config, lambda e: {"ok": False, "reason": "event_budget"})
        self.assertEqual(kept[0].check_status, "not_checked")
        self.assertNotIn("本轮未测通", p.display_name(kept[0]))

    def test_unchecked_sports_catalogue_does_not_claim_decoded(self):
        entry = p.Entry("中超 A VS B 15:00", "", "https://example.com/a", event_date=self.now.date())
        kept, stats = p.select_bucket([entry], self.config, lambda e: self.fail("unsampled event probed"), check_event=False)
        self.assertEqual(len(kept), 1)
        self.assertFalse(kept[0].verified)
        self.assertEqual(stats["event_not_checked"], 1)

    def test_event_source_priority_does_not_change_tv_priority(self):
        entries = [p.Entry("比赛 19:00", "", f"https://{source}.example/a", source=source,
                           event_date=self.now.date()) for source in ["migu-primary", "migu-backup"]]
        kept, _ = p.select_bucket(entries, self.config, None, check_event=False)
        self.assertEqual(kept[0].source, "migu-backup")
        tv = [p.replace(e, name="CCTV1", event_date=None) for e in entries]
        kept, _ = p.select_bucket(tv, self.config, lambda e: {"ok": True, "reason": "decoded"})
        self.assertEqual(kept[0].source, "migu-primary")

    def test_membership_failure_is_visible_in_event_title(self):
        entry = p.Entry("CBA A VS B 19:25", "", "https://example.com/a", event_date=self.now.date())
        kept, _ = p.select_bucket([entry], self.config, lambda e: {"ok": False, "reason": "membership_required"})
        self.assertIn("源提示需会员", p.display_name(kept[0]))
        self.assertFalse(kept[0].verified)

    def test_login_failure_is_visible_and_does_not_imply_membership(self):
        entry = p.Entry("英超 A VS B 19:00", "", "https://example.com/a", event_date=self.now.date())
        kept, _ = p.select_bucket([entry], self.config, lambda e: {"ok": False, "reason": "login_required"})
        rendered = p.render_m3u([kept], self.now, [])
        self.assertIn("源提示需登录", rendered)
        self.assertNotIn("源提示需会员", rendered)

    def test_restricted_candidate_does_not_taint_working_backup(self):
        entries = [p.Entry("英超 A VS B 19:00", "", f"https://source{i}.example/a", event_date=self.now.date()) for i in range(2)]
        kept, _ = p.select_bucket(entries, self.config, lambda e: {"ok": "source1" in e.url,
                               "reason": "decoded" if "source1" in e.url else "login_required"})
        self.assertTrue(kept[0].verified)
        self.assertIn("source1", kept[0].url)
        self.assertNotIn("源提示需登录", p.parse_playlist(p.render_m3u([kept], self.now, []))[0].name)

    def test_active_match_commentaries_are_sampled_before_old_leagues(self):
        entries = [p.Entry(name, "", f"https://example.com/{i}", {"tvg-id": match}, event_date=self.now.date())
                   for i, (name, match) in enumerate([("中超 C VS D 10:00", "old"),
                         ("英超 A VS B 中文 16:00", "live"), ("英超 A VS B 原声 16:15", "live")])]
        keys = p.event_sample_keys([[e] for e in entries], 2, self.now)
        self.assertEqual(keys, {entries[1].identity, entries[2].identity})

    def test_distinct_active_matches_before_repeated_commentary(self):
        entries = [p.Entry(name, "", f"https://example.com/{i}", {"tvg-id": match}, event_date=self.now.date())
                   for i, (name, match) in enumerate([("CBA A VS B 中文 16:00", "one"),
                         ("CBA A VS B 原声 16:00", "one"), ("英超 C VS D 16:15", "two")])]
        keys = p.event_sample_keys([[e] for e in entries], 2, self.now)
        self.assertEqual(keys, {entries[0].identity, entries[2].identity})

    def test_access_hint_does_not_follow_video_redirect(self):
        opener = unittest.mock.MagicMock()
        response = opener.open.return_value.__enter__.return_value
        response.headers = {"Content-Type": "application/json;charset=UTF-8"}
        with patch("pipeline.urllib.request.build_opener", return_value=opener):
            response.read1.return_value = "由于版权原因，请登录后观看".encode()
            self.assertEqual(p.access_hint("https://example.com/a", 1), "login_required")
            response.read1.return_value = "开通钻石会员即可免费畅看哦".encode()
            self.assertEqual(p.access_hint("https://example.com/a", 1), "membership_required")
            response.headers = {"Content-Type": "video/mp2t"}
            response.read1.reset_mock()
            self.assertFalse(p.access_hint("https://example.com/a", 1))
            response.read1.assert_not_called()

    def test_trial_redirect_is_not_treated_as_full_playback(self):
        redirect = p.urllib.error.HTTPError("https://source.example/a", 302, "Found",
                     {"Location": "http://gslbtrialmgsplive.miguvideo.com/stream"}, None)
        opener = unittest.mock.MagicMock()
        opener.open.side_effect = redirect
        with patch("pipeline.urllib.request.build_opener", return_value=opener):
            self.assertEqual(p.access_hint("https://source.example/a", 1), "trial_stream")
        opener.open.assert_called_once()

    def test_trial_domain_check_does_not_match_other_domains_or_paths(self):
        self.assertTrue(p.trial_url("https://gslbtrialmgsplive.miguvideo.com/a"))
        self.assertFalse(p.trial_url("https://gslbmgsplive.miguvideo.com/a"))
        self.assertFalse(p.trial_url("https://gslbtrialmgsplive.miguvideo.com.example.org/a"))
        self.assertFalse(p.trial_url("https://example.org/gslbtrialmgsplive.miguvideo.com"))

    def test_trial_stream_is_rejected_before_short_decode_can_pass(self):
        prober = object.__new__(p.Prober)
        prober.config = self.config
        prober.deadline = prober.event_deadline = time.monotonic()+10
        prober.hosts = {"source.example": threading.Semaphore(1)}
        prober.lock = threading.Lock()
        prober.migu_hosts = set()
        entry = p.Entry("NBA A VS B 07:00", "", "https://source.example/a", event_date=self.now.date())
        with patch("pipeline.access_hint", return_value="trial_stream"), patch.object(prober, "decode") as decode:
            decode.return_value = {"ok": True, "reason": "decoded", "frames": 75}
            result = prober(entry)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "trial_stream")
        decode.assert_not_called()

    def test_trial_fallback_is_labelled_and_not_verified(self):
        entry = p.Entry("NBA A VS B 07:00", "", "https://source.example/a", event_date=self.now.date())
        kept, _ = p.select_bucket([entry],self.config,lambda e:{"ok":False,"reason":"trial_stream"})
        self.assertFalse(kept[0].verified)
        self.assertIn("仅试看",p.display_name(kept[0]))
        self.assertIn("仅试看",p.display_name(p.replace(kept[0],name="无时间赛事")))

    def test_recent_public_source_cache_survives_fetch_failure(self):
        text = '#EXTM3U\n#EXTINF:-1 group-title="体育-今天10-24",比赛 19:00\nhttps://example.com/live\n'
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            with patch("pipeline.fetch_playlist", return_value=text):
                _, meta = p.source_playlist(self.spec, self.config, state, self.now)
            self.assertFalse(meta["cached"])
            cache = next(state.glob("*.json"))
            original = cache.read_bytes()
            with patch("pipeline.fetch_playlist", side_effect=ValueError("offline")):
                value, meta = p.source_playlist(self.spec, self.config, state, self.now+p.timedelta(hours=2))
                self.assertEqual(value, text)
                self.assertTrue(meta["cached"])
                self.assertEqual(meta["fetch_error"], "offline")
                with self.assertRaises(ValueError):
                    p.source_playlist(self.spec, self.config, state, self.now+p.timedelta(hours=25))
            self.assertEqual(cache.read_bytes(), original)

    def test_invalid_playlist_does_not_overwrite_source_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            text = '#EXTM3U\n#EXTINF:-1 group-title="体育-今天10-24",比赛\nhttps://example.com/live\n'
            with patch("pipeline.fetch_playlist", return_value=text):
                p.source_playlist(self.spec, self.config, state, self.now)
            with patch("pipeline.fetch_playlist", return_value="<html>error</html>"):
                value, meta = p.source_playlist(self.spec, self.config, state, self.now)
            self.assertTrue(meta["cached"])
            self.assertEqual(value, text)

    def test_tv_only_response_preserves_previous_sports_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            text = '#EXTM3U\n#EXTINF:-1 group-title="体育-今天10-24",比赛\nhttps://example.com/live\n'
            with patch("pipeline.fetch_playlist", return_value=text):
                p.source_playlist(self.spec, self.config, state, self.now)
            tv_only = '#EXTM3U\n#EXTINF:-1 group-title="央视",CCTV1\nhttps://example.com/tv\n'
            with patch("pipeline.fetch_playlist", return_value=tv_only):
                value, meta = p.source_playlist(self.spec, self.config, state, self.now)
            self.assertTrue(meta["cached"])
            self.assertEqual(meta["fetch_error"], "source_has_no_current_events")
            self.assertEqual(value, text)

    def test_cached_source_supplements_events_without_old_tv_candidates(self):
        text = '#EXTM3U\n#EXTINF:-1 group-title="央视",CCTV1\nhttps://example.com/tv\n'
        for word, suffix in [("昨天", "10-23"), ("今天", "10-24"), ("明天", "10-25")]:
            text += f'#EXTINF:-1 group-title="体育-{word}{suffix}",比赛 00:01\nhttps://example.com/{word}\n'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = dict(self.config, sources=[self.spec], minimum_channels=0)
            with patch("pipeline.fetch_playlist", return_value=text):
                p.source_playlist(self.spec, cfg, root / "state", self.now)
            def probe(entry):
                self.assertIsNotNone(entry.event_date, "cached TV entered the scan")
                return {"ok": True, "reason": "decoded"}
            with patch("pipeline.fetch_playlist", side_effect=ValueError("offline")), patch("builtins.print"):
                code = p.run(cfg, root / "public", root / "report.json", self.now, probe=probe)
            self.assertEqual(code, 0)
            status = json.loads((root / "public/status.json").read_text())
            self.assertEqual(status["channels"], 0)
            self.assertTrue(status["sources"][0]["cached"])
            self.assertIn("some_source_catalogues_from_cache", status["warnings"])

    def test_reformat_preserves_event_epg_identity(self):
        entry = p.Entry("CBA A VS B 19:25", "", "https://example.com/a", event_date=self.now.date())
        first = p.parse_playlist(p.render_m3u([[entry]], self.now, []))[0]
        first.event_date = entry.event_date
        second = p.parse_playlist(p.render_m3u([[first]], self.now, []))[0]
        self.assertEqual(first.attrs["tvg-id"], second.attrs["tvg-id"])

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

    def test_cctv_epg_id_matches_configured_xmltv_service(self):
        entry = p.Entry("CCTV1", "央视", "https://example.com/a", {"tvg-id": "CCTV1综合"})
        output = p.parse_playlist(p.render_m3u([[entry]], self.now, []))[0]
        self.assertEqual(output.attrs["tvg-id"], "CCTV1")
        self.assertEqual(p.epg_id(p.Entry("CCTV4欧洲", "央视", "https://example.com/b")), "CCTV4欧洲")

    def test_events_sorted_numerically_and_unknown_time_last(self):
        entries = [p.Entry(name, "", "https://example.com/a", event_date=self.now.date())
                   for name in ["赛事C 23:30", "赛事B 10:00", "赛事A 9:55", "无时间"]]
        ordered = sorted(entries, key=p.rank)
        self.assertEqual([e.name for e in ordered], ["赛事A 9:55", "赛事B 10:00", "赛事C 23:30", "无时间"])
        rendered = p.parse_playlist(p.render_m3u([[e] for e in entries], self.now, []))
        self.assertEqual([e.name for e in rendered], ["09:55 赛事A", "10:00 赛事B", "23:30 赛事C", "无时间"])

    def test_event_time_prefix_is_not_duplicated(self):
        entry = p.Entry("09:55 赛事A", "", "https://example.com/a", event_date=self.now.date())
        self.assertEqual(p.display_name(entry), entry.name)

    def test_displayed_clock_wins_over_time_inside_title(self):
        entry = p.Entry("19:00 赛事A 昨日精彩回顾 02:00", "", "https://example.com/a", event_date=self.now.date())
        self.assertEqual(p.event_clock(entry.name), 19*60)
        self.assertEqual(p.display_name(entry), entry.name)

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

    def test_hls_probe_mime_rejection_gets_explicit_hls_retry(self):
        prober = object.__new__(p.Prober)
        prober.config = self.config
        prober.hls_options = ["-allowed_extensions", "ALL"]
        failed = subprocess.CompletedProcess([], 1, "", "Not detecting m3u8/hls with non standard extension and non standard mime type")
        passed = subprocess.CompletedProcess([], 0, "frame=75\nout_time_us=3000000\n", "")
        with patch("pipeline.subprocess.run", side_effect=[failed, passed]) as run:
            result = prober.decode(p.Entry("回放", "", "https://example.com/a"), 18)
        self.assertTrue(result["ok"])
        self.assertIn("hls", run.call_args_list[1].args[0])
        self.assertLessEqual(run.call_args_list[1].kwargs["timeout"], 18)

    def test_continuous_flv_is_not_playlist(self):
        result = subprocess.CompletedProcess([], 0, b"FLV" + b"x"*100, b"")
        with patch("pipeline.subprocess.run", return_value=result):
            with self.assertRaisesRegex(ValueError, "media"):
                p.fetch_playlist(self.spec, 1)

    @unittest.skipUnless(shutil.which("curl"), "curl required")
    def test_actual_continuous_response_has_total_deadline(self):
        class Stream(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "video/x-flv")
                self.end_headers()
                try:
                    self.wfile.write(b"FLV")
                    while True:
                        self.wfile.write(b"x" * 4096)
                        self.wfile.flush()
                        time.sleep(0.02)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Stream)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        started = time.monotonic()
        try:
            # Only the fixture's loopback URL bypasses the production URL guard.
            with patch("pipeline.public_url", return_value=True):
                with self.assertRaises(ValueError):
                    p.fetch_playlist({"url": f"http://127.0.0.1:{server.server_port}"}, 0.4)
            self.assertLess(time.monotonic() - started, 3)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

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

    def test_missing_tomorrow_waits_before_video_checks_and_keeps_publication(self):
        text = '#EXTM3U\n'
        for word, suffix in [("昨天", "10-23"), ("今天", "10-24")]:
            text += f'#EXTINF:-1 group-title="体育-{word}{suffix}",比赛 00:01\nhttps://example.com/{word}\n'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "public"
            out.mkdir()
            old_status = json.dumps({"accepted": True, "channels": 10,
                                     "channel_validation_at": "2026-10-23T22:00:00+08:00"})
            (out / "status.json").write_text(old_status)
            (out / "all.m3u").write_text("previous accepted subscription")
            cfg = dict(self.config, sources=[self.spec])
            with patch("pipeline.fetch_playlist", return_value=text), patch("builtins.print"), \
                    patch("pipeline.Prober", side_effect=AssertionError("video validation started too early")):
                code = p.run(cfg, out, root / "report.json", self.now)
            self.assertEqual(code, 3)
            report = json.loads((root / "report.json").read_text())
            self.assertTrue(report["deferred"])
            self.assertFalse(report["accepted"])
            self.assertEqual(report["errors"], ["missing_events_2026-10-25"])
            self.assertEqual(report["probe"], {})
            self.assertEqual(report["previous_channels_retained"], 10)
            self.assertEqual((out / "status.json").read_text(), old_status)
            self.assertEqual((out / "all.m3u").read_text(), "previous accepted subscription")

    def test_missing_today_is_failure_instead_of_normal_upstream_wait(self):
        text = '#EXTM3U\n#EXTINF:-1 group-title="体育-昨天10-23",比赛 00:01\nhttps://example.com/a\n'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("pipeline.fetch_playlist", return_value=text), patch("builtins.print"), \
                    patch("pipeline.Prober", side_effect=AssertionError("video checks started")):
                code = p.run(dict(self.config, sources=[self.spec]), root / "public", root / "report.json", self.now)
            self.assertEqual(code, 2)
            self.assertFalse(json.loads((root / "report.json").read_text())["deferred"])

    def test_daily_full_update_uses_validation_day_in_subscription_timezone(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            self.assertTrue(p.full_update_due(out, self.now))
            cases = [("2026-10-23T22:00:00+08:00", True, True),
                     ("2026-10-24T01:00:00+08:00", True, False),
                     ("2026-10-23T18:00:00+00:00", True, False),
                     ("2026-10-24T01:00:00+08:00", False, True),
                     ("2026-10-24T01:00:00", True, True),
                     ("bad_timestamp", True, True)]
            for timestamp, accepted, due in cases:
                with self.subTest(timestamp=timestamp, accepted=accepted):
                    (out / "status.json").write_text(json.dumps({"accepted": accepted, "channel_validation_at": timestamp}))
                    self.assertEqual(p.full_update_due(out, self.now), due)

    def test_events_only_keeps_complete_days_and_prior_tv_validation_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "public"
            cfg = dict(self.config, sources=[self.spec], minimum_channels=1, event_samples_per_day=0)
            old_time = "2026-10-24T08:00:00+08:00"
            tv = p.Entry("CCTV1", "央视", "https://example.com/tv", verified=True)
            p.publish(out, [[tv]], {"accepted": True, "channels": 1, "generated_at": old_time}, self.now, cfg)
            text = '#EXTM3U\n'
            for word, suffix in [("昨天", "10-23"), ("今天", "10-24"), ("明天", "10-25")]:
                text += f'#EXTINF:-1 group-title="体育-{word}{suffix}" tvg-id="A VS B",CBA A VS B 全场回放 00:01\nhttps://example.com/{word}\n'
            checked = []
            def probe(entry):
                checked.append(entry.url)
                return {"ok": False, "reason": "timeout"}
            with patch("pipeline.fetch_playlist", return_value=text), patch("builtins.print"):
                code = p.run(cfg, out, root / "report.json", self.now,
                             probe=probe, events_only=True, check_event=["A VS B", "another match"])
            self.assertEqual(code, 0)
            report = json.loads((out / "status.json").read_text())
            self.assertEqual(report["channel_validation_at"], old_time)
            self.assertEqual(report["channels"], 1)
            self.assertEqual(set(checked), {"https://example.com/昨天", "https://example.com/今天"})
            self.assertEqual(report["requested_event_check"], ["A VS B", "another match"])
            self.assertEqual([day["listed"] for day in report["events"].values()], [1,1,1])
            self.assertTrue(all(day["verified"] == 0 for day in report["events"].values()))
            self.assertIn("restricted_and_unverified_events_only_in_schedule", report["warnings"])
            self.assertEqual(p.parse_playlist((out / "events.m3u").read_text()), [])
            self.assertEqual(len(json.loads((out / "schedule.json").read_text())["events"]), 3)
            self.assertIn("https://example.com/tv", (out / "channels.m3u").read_text())

    def test_empty_selector_still_checks_all_started_events_under_strict_policy(self):
        text = '#EXTM3U\n'
        for word, suffix in [("昨天", "10-23"), ("今天", "10-24"), ("明天", "10-25")]:
            text += f'#EXTINF:-1 group-title="体育-{word}{suffix}",比赛 00:01\nhttps://example.com/{word}\n'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = dict(self.config, sources=[self.spec], minimum_channels=0, event_samples_per_day=0)
            with patch("pipeline.fetch_playlist", return_value=text), patch("builtins.print"):
                code = p.run(cfg, root / "public", root / "report.json", self.now,
                             probe=lambda e: {"ok": True, "reason": "decoded", "kind": "vod"}, check_event=[""])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads((root / "report.json").read_text())["requested_event_check"], [])


if __name__ == "__main__":
    unittest.main()
