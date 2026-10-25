#!/usr/bin/env python3
"""Independent pre-publication assertions for the player-facing files."""
import argparse
from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import pipeline as p


def verify(out):
    status = json.loads((out/'status.json').read_text())
    schedule = json.loads((out/'schedule.json').read_text())
    evidence = json.loads((out.parent/'state/event-evidence.json').read_text())
    assert status['accepted'], 'generation_not_accepted'
    assert status['event_policy'] == 'qualified_only_with_separate_complete_schedule', 'wrong_policy'
    event_ids = {'event-'+e['id'][:16] for e in schedule['events'] if e['published']}
    expected_dates = set(status['events'])
    config = json.loads((p.ROOT/'config.json').read_text())
    blocked = {item['url'] for item in config.get('blocked_event_routes', [])}
    assert {e['date'] for e in schedule['events']} == expected_dates, 'schedule_date_gap'
    for name, limit in [('all.m3u',1), ('all-backup.m3u',2), ('events.m3u',1), ('channels.m3u',1)]:
        text = (out/name).read_text()
        assert all(not p.credential_style_url(line.strip()) for line in text.splitlines()
                   if line.startswith(('http://','https://'))), 'account_style_address_'+name
        entries = p.parse_playlist(text)
        keys = Counter((e.group,e.name) for e in entries)
        assert max(keys.values(), default=0) <= limit, 'too_many_lines_'+name
        assert all(not p.trial_url(e.url) for e in entries), 'trial_url_'+name
        assert all(not p.source_error_video(e.url) for e in entries), 'source_error_video_'+name
        assert all(e.url not in blocked for e in entries), 'blocked_content_mismatch_'+name
        events = [e for e in entries if e.group.startswith('咪咕·')]
        assert all(e.attrs.get('tvg-id') in event_ids for e in events), 'unqualified_event_'+name
        assert all('预告' not in e.group for e in events), 'upcoming_in_main_'+name
        for entry in events:
            prefix = entry.attrs['tvg-id'][len('event-'):]
            proofs = [value for key,value in evidence.items()
                      if key.startswith(prefix) and key.endswith(':'+entry.fingerprint)]
            assert proofs and proofs[0]['qualified'], 'missing_route_evidence'
            proof = proofs[0]
            if route := proof.get('route'):
                assert route['url'] == entry.url and p.replace(entry, options=route['options']).fingerprint == entry.fingerprint, 'route_record_mismatch'
            generated = datetime.fromisoformat(status['generated_at'])
            if proof.get('verification_reused'):
                assert proof.get('last_attempt_reason') in p.INCONCLUSIVE_EVENT_CHECKS, 'unsafe_evidence_reuse'
                assert datetime.fromisoformat(proof['last_attempt_at']) == generated, 'stale_reuse_attempt'
                assert p.reusable_vod_evidence(proof, generated, config), 'expired_or_weak_reused_evidence'
            else:
                assert datetime.fromisoformat(proof['last_success_at']) == generated, 'stale_route_evidence'
            if proof.get('kind') == 'vod':
                assert proof['duration_seconds'] >= 1200 and len(proof['sample_offsets']) == 3 and proof['sample_seconds'] >= 5, 'weak_vod_evidence'
            else:
                assert proof.get('sample_seconds',0) >= 15 and (datetime.fromisoformat(proof['last_success_at'])-datetime.fromisoformat(proof['first_success_at'])).total_seconds() >= 1200, 'weak_live_evidence'
        if name in ('all.m3u','events.m3u'):
            assert {e.attrs.get('tvg-id') for e in events} == event_ids, 'published_event_mismatch_'+name
        if name == 'channels.m3u':
            assert not events and len(entries) == status['channels'], 'channel_count_mismatch'
    assert len(event_ids) == status['published_events'], 'status_count_mismatch'
    assert datetime.fromisoformat(schedule['generated_at']) == datetime.fromisoformat(status['generated_at']), 'stale_schedule'
    catalogue_path = out/'events-catalogue.m3u'
    if status.get('catalogue_subscription') or catalogue_path.exists():
        raw = catalogue_path.read_text()
        assert all(not p.credential_style_url(line.strip()) and line.strip() not in blocked
                   for line in raw.splitlines() if line.startswith(('http://','https://'))), 'unsafe_catalogue_address'
        entries = p.parse_playlist(raw)
        expected = {'schedule-'+e['id'][:16] for e in schedule['events'] if e.get('player_entry_available',True)}
        assert {e.attrs.get('tvg-id') for e in entries} == expected and len(entries) == len(expected), 'incomplete_player_catalogue'
        assert all(e.group.startswith('赛事目录·') and '[' in e.name and ']' in e.name for e in entries), 'unlabelled_player_catalogue'
    return {'channels':status['channels'], 'published_events':len(event_ids), 'schedule_events':len(schedule['events'])}


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--out',type=Path,default=p.ROOT/'public')
    args=parser.parse_args()
    print(json.dumps(verify(args.out),ensure_ascii=False))
