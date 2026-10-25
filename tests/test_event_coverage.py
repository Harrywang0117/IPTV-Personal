from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
from zoneinfo import ZoneInfo
import unittest
from unittest.mock import patch
from unittest.mock import MagicMock

import frame_audit as f
import pipeline as p


class EventCoverageTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 25, 17, tzinfo=ZoneInfo('Asia/Shanghai'))
        self.config = json.loads((p.ROOT / 'config.json').read_text())
        self.config['sources'] += [{'id': 'migu-test', 'url': 'https://source.example', 'events': True}]

    def event(self, league, match, voice='', day=-1):
        return p.Entry(f'{league} {match} 全场回放（{voice}） 08:00', '',
                       'https://source.example/968123456', attrs={'tvg-id': match}, source='migu-test',
                       event_date=self.now.date()+timedelta(days=day))

    def proof(self, entry):
        checked = p.replace(entry, verified=True, evidence={'kind': 'vod', 'duration_seconds': 7200,
                         'sample_offsets': [0, 3600, 5760], 'sample_seconds': 5})
        return p.qualify_events([[checked]], {}, self.now-timedelta(hours=2), self.config)[1]

    def test_one_commentary_per_fixture_before_extras_and_every_league_gets_checked(self):
        entries = [self.event('英超', 'A VS B', str(i)) for i in range(20)]
        entries += [self.event('CBA', 'C VS D', voice) for voice in ['中文', '赛场原声']]
        entries += [self.event('NBA', 'E VS F', voice, 0) for voice in ['中文', '英文原声']]
        entries += [self.event('中超', 'G VS H', voice) for voice in ['中文', '赛场原声']]
        queue = p.order_probes([[e] for e in entries], {}, {}, self.now)
        first = [b[0] for b in queue[:4]]
        self.assertEqual({e.attrs['tvg-id'] for e in first}, {'A VS B', 'C VS D', 'E VS F', 'G VS H'})
        self.assertTrue(any('赛场原声' in e.name and e.name.startswith('CBA') for e in first))
        self.assertTrue(any('英文原声' in e.name and e.name.startswith('NBA') for e in first))
        self.assertEqual(len(queue), len(entries))

    def test_requested_target_first_and_dates_do_not_merge(self):
        entries = [self.event('英超', '维拉VS曼城', '中文'), self.event('CBA', 'A VS B', day=-1),
                   self.event('CBA', 'A VS B', day=0), self.event('NBA', '未来比赛', day=1)]
        entries[-1].upcoming = True
        queue = p.order_probes([[e] for e in entries], {}, {}, self.now, ['曼城'])
        self.assertEqual(queue[0][0], entries[0])
        self.assertEqual({b[0].event_date for b in queue[1:3]}, {self.now.date(), self.now.date()-timedelta(days=1)})
        self.assertEqual(queue[-1][0], entries[-1])

    def test_oldest_published_commentary_rotates_into_first_pass(self):
        old = self.event('英超', 'A VS B', '旧解说')
        fresh = self.event('英超', 'A VS B', '新解说')
        known = {'event-'+p.event_key(e)[:16]: [e.fingerprint] for e in [old, fresh]}
        proof = self.proof(old)
        proof.update(self.proof(fresh))
        proof[p.event_key(fresh)+':'+fresh.fingerprint]['last_success_at'] = self.now.isoformat()
        queue = p.order_probes([[fresh], [old]], known, proof, self.now)
        self.assertEqual(queue[0][0], old)

    def test_omitted_route_can_be_retested_from_proof_without_previous_playlist_row(self):
        old = self.event('CBA', 'A VS B')
        proof = self.proof(old)
        current = p.replace(old, url='https://other.example/968123456')
        groups = {current.identity: [current]}
        self.assertEqual(p.supplement_retained_routes(groups, [], proof, self.now, self.config), 1)
        self.assertEqual(groups[current.identity][1].url, old.url)
        self.assertFalse(groups[current.identity][1].verified)
        # The recovered candidate still fails closed on explicit removal.
        failed = p.replace(groups[current.identity][1], check_status='http_404')
        self.assertEqual(p.qualify_events([[failed]], proof, self.now, self.config), ([], {}))

    def test_legacy_proof_uses_matching_previous_player_entry(self):
        old = self.event('CBA', 'A VS B')
        proof = self.proof(old)
        for item in proof.values():
            item.pop('route')
        previous = p.parse_playlist(p.render_m3u([[old]], self.now, []))
        current = p.replace(old, url='https://other.example/968123456')
        groups = {current.identity: [current]}
        self.assertEqual(p.supplement_retained_routes(groups, previous, proof, self.now, self.config), 1)

    def test_expired_untrusted_blocked_signed_or_absent_match_cannot_be_resurrected(self):
        old = self.event('CBA', 'A VS B')
        current = p.replace(old, url='https://other.example/968123456')
        for variant in ['expired', 'untrusted', 'blocked', 'signed', 'missing_event']:
            proof = self.proof(old)
            config = json.loads(json.dumps(self.config))
            groups = {current.identity: [current]}
            now = self.now
            if variant == 'expired':
                now += timedelta(days=2)
            elif variant == 'untrusted':
                config['sources'] = [s for s in config['sources'] if s['id'] != 'migu-test']
            elif variant == 'blocked':
                config['blocked_event_routes'] = [{'url': old.url}]
            elif variant == 'signed':
                next(iter(proof.values()))['route']['url'] += '?token=not-for-storage'
            else:
                groups = {}
            self.assertEqual(p.supplement_retained_routes(groups, [], proof, now, config), 0, variant)

    def test_frame_samples_cover_basketball_and_csl_among_many_football_entries(self):
        entries = [self.event('英超', f'足球{i}', str(i), day=0) for i in range(20)]
        entries += [self.event(league, 'A VS B') for league in ['CBA', 'NBA', '中超']]
        entries = [p.replace(e, verified=True, evidence={'sample_offsets': [0, 3600, 5760]}) for e in entries]
        with tempfile.TemporaryDirectory() as tmp, patch('frame_audit.capture', return_value='timeout'):
            report = f.audit_frames([[e] for e in entries], self.config, object(), Path(tmp), self.now)
        leagues = {record['title'].split()[0] for record in report['records']}
        self.assertTrue({'CBA', 'NBA', '中超'} <= leagues)
        self.assertLessEqual(report['attempted'], 18)

    def test_known_upstream_message_clips_are_not_sports_and_need_no_media_read(self):
        for name in ['hyqx.mp4', 'dlsx.mp4', 'qtcw.mp4']:
            url = 'https://migu.lifit.uk/' + name
            with patch('pipeline.urllib.request.build_opener') as opener:
                self.assertEqual(p.access_hint(url, 2), 'source_error_video')
                opener.assert_not_called()
        self.assertFalse(p.source_error_video('https://migu.lifit.uk.example.com/hyqx.mp4'))
        self.assertFalse(p.source_error_video('https://other.example/hyqx.mp4'))

    def test_redirect_to_known_message_clip_is_rejected_before_decoding(self):
        opener = MagicMock()
        opener.open.side_effect = p.urllib.error.HTTPError('https://source.example', 302, '',
                                   {'Location': 'https://migu.lifit.uk/hyqx.mp4'}, None)
        with patch('pipeline.urllib.request.build_opener', return_value=opener):
            self.assertEqual(p.access_hint('https://source.example/968123456', 2), 'source_error_video')
