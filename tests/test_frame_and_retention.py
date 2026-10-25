from datetime import datetime, timedelta
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import frame_audit as f
import pipeline as p
import verify_publication as v


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((p.ROOT / 'config.json').read_text())
        self.now = datetime(2026, 10, 25, 16, tzinfo=ZoneInfo('Asia/Shanghai'))
        self.entry = p.Entry('CBA A VS B 全场回放 08:00', '', 'https://source.example/968123456',
                             event_date=self.now.date(), verified=True, check_status='decoded',
                             evidence={'kind': 'vod', 'duration_seconds': 7200,
                                       'sample_offsets': [0, 3600, 5760], 'sample_seconds': 5})
        _, self.proof = p.qualify_events([[self.entry]], {}, self.now, self.config)

    def test_network_and_budget_reuse_real_timestamp_only_within_one_day(self):
        for reason in p.INCONCLUSIVE_EVENT_CHECKS:
            current = p.replace(self.entry, verified=False, check_status=reason, evidence={})
            accepted, proof = p.qualify_events([[current]], self.proof, self.now + timedelta(hours=2), self.config)
            self.assertEqual(len(accepted), 1, reason)
            self.assertEqual(current.check_status, 'recent_verified')
            item = next(iter(proof.values()))
            self.assertEqual(item['last_success_at'], self.now.isoformat())
            self.assertTrue(item['verification_reused'])
            self.assertIn('沿用近期检测', p.display_name(current))
        current = p.replace(self.entry, verified=False, check_status='timeout')
        self.assertEqual(p.qualify_events([[current]], self.proof, self.now + timedelta(hours=25), self.config)[0], [])

    def test_hard_negative_route_change_or_upcoming_cannot_reuse_old_success(self):
        for reason in ['trial_stream', 'membership_required', 'login_required', 'black_video',
                       'content_mismatch', 'http_404', 'http_410', 'decode_failed']:
            current = p.replace(self.entry, verified=False, check_status=reason)
            accepted, proof = p.qualify_events([[current]], self.proof, self.now + timedelta(hours=1), self.config)
            self.assertEqual((accepted, proof), ([], {}), reason)
        for current in [p.replace(self.entry, verified=False, check_status='timeout', upcoming=True),
                        p.replace(self.entry, verified=False, check_status='timeout', url='https://different.example/968123456')]:
            self.assertFalse(p.qualify_events([[current]], self.proof, self.now, self.config)[0])

    def test_fresh_success_is_preferred_over_retained_success(self):
        retained = p.replace(self.entry, verified=False, check_status='timeout', evidence={})
        fresh = p.replace(self.entry, url='https://fresh.example/968123456')
        accepted, _ = p.qualify_events([[retained, fresh]], self.proof, self.now + timedelta(hours=1), self.config)
        self.assertEqual(accepted[0][0].url, fresh.url)
        rendered = p.parse_playlist(p.render_m3u(accepted, self.now, [], single=False))
        self.assertEqual(len(rendered), 2)
        self.assertEqual(rendered[0].name, rendered[1].name)

    def test_vod_observation_cannot_instantly_qualify_new_live_stream(self):
        live = p.replace(self.entry, evidence={'kind': 'live', 'sample_seconds': 15})
        accepted, _ = p.qualify_events([[live]], self.proof, self.now + timedelta(hours=1), self.config)
        self.assertFalse(accepted)

    def test_independent_verifier_enforces_reuse_age_and_reason(self):
        now = self.now + timedelta(hours=2)
        current = p.replace(self.entry, verified=False, check_status='timeout', evidence={})
        accepted, proof = p.qualify_events([[current]], self.proof, now, self.config)
        catalogue = [[current]] + [[p.replace(current, event_date=now.date()+timedelta(days=d), verified=False, upcoming=True)] for d in [-1, 1]]
        report = {'accepted': True, 'channels': 0, 'generated_at': now.isoformat(),
                  'event_policy': 'qualified_only_with_separate_complete_schedule',
                  'events': {str(now.date()+timedelta(days=d)): {} for d in [-1, 0, 1]}, 'published_events': 1}
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'public'
            p.publish(out, accepted, report, now, self.config, catalogue)
            evidence_path = out.parent / 'state/event-evidence.json'
            p.atomic_write(evidence_path, json.dumps(proof))
            self.assertEqual(v.verify(out)['published_events'], 1)
            self.assertFalse((out / 'events-catalogue.m3u').exists())
            row = next(iter(proof.values()))
            row['last_attempt_reason'] = 'membership_required'
            p.atomic_write(evidence_path, json.dumps(proof))
            with self.assertRaisesRegex(AssertionError, 'unsafe_evidence_reuse'):
                v.verify(out)
            row['last_attempt_reason'] = 'timeout'
            row['last_success_at'] = (now-timedelta(hours=25)).isoformat()
            p.atomic_write(evidence_path, json.dumps(proof))
            with self.assertRaisesRegex(AssertionError, 'expired_or_weak'):
                v.verify(out)

    def test_black_video_rejected_only_when_all_three_windows_are_black(self):
        prober = object.__new__(p.Prober)
        prober.config = self.config
        metadata = subprocess.CompletedProcess([], 0, '{"format":{"duration":"7200"}}', '')
        black = {'ok': True, 'picture_quality': {'checked': True, 'almost_all_black': True}}
        visible = {'ok': True, 'picture_quality': {'checked': True, 'almost_all_black': False}}
        with patch('pipeline.subprocess.run', return_value=metadata):
            with patch.object(prober, 'decode', side_effect=[black, black, black]):
                self.assertEqual(prober.event_check(self.entry, 45)['reason'], 'black_video')
            with patch.object(prober, 'decode', side_effect=[black, visible, visible]):
                self.assertTrue(prober.event_check(self.entry, 45)['ok'])

    def test_decode_connection_timeout_is_inconclusive_and_http_failure_is_specific(self):
        prober = object.__new__(p.Prober)
        prober.config, prober.hls_options = self.config, []
        for stderr, reason in [('Connection to tcp://public.example:80 failed: Connection timed out', 'network_timeout'),
                               ('HTTP error 404 Not Found', 'http_404')]:
            result = subprocess.CompletedProcess([], 1, '', stderr)
            with patch('pipeline.subprocess.run', return_value=result):
                self.assertEqual(prober.decode(self.entry, 10)['reason'], reason)


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg required for actual picture check')
class ActualPictureTests(unittest.TestCase):
    def test_actual_black_and_moving_video_and_artifact(self):
        config = json.loads((p.ROOT / 'config.json').read_text())
        now = datetime.now(ZoneInfo('Asia/Shanghai'))
        prober = p.Prober(config)
        self.assertTrue(prober.black_detection)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, source in [('black', 'color=c=black:s=160x90:r=10:d=2'),
                                 ('moving', 'testsrc2=s=160x90:r=10:d=2')]:
                subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i', source,
                                '-threads', '1', '-c:v', 'mpeg4', '-movflags', '+faststart', '-y', str(root / (name+'.mp4'))], check=True, timeout=10)
            server = ThreadingHTTPServer(('127.0.0.1', 0), partial(QuietHandler, directory=str(root)))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                for name, expected in [('black', True), ('moving', False)]:
                    entry = p.Entry(name, '', f'http://127.0.0.1:{server.server_port}/{name}.mp4', event_date=now.date())
                    checked = prober.decode(entry, 10, seconds=1)
                    self.assertTrue(checked['ok'], checked)
                    self.assertEqual(checked['picture_quality']['almost_all_black'], expected)
                entry.verified = True
                entry.evidence = {'sample_offsets': [0]}
                artifact = f.audit_frames([[entry]], config, prober, root / 'frames', now)
                self.assertEqual(artifact['captured'], 1)
                self.assertFalse(artifact['semantic_match_checked'])
                self.assertTrue((root / 'frames/index.html').exists())
                self.assertNotIn('127.0.0.1', (root / 'frames/manifest.json').read_text())
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
