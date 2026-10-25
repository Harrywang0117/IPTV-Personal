import copy
from datetime import datetime, timedelta
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import discover as d
import pipeline as p
import verify_publication as v


class DiscoveryAndPublicationTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026,10,25,9,tzinfo=ZoneInfo('Asia/Shanghai'))
        self.config = json.loads((p.ROOT/'config.json').read_text())

    def event(self, url='https://source.example/968123456', **kw):
        return p.Entry('CBA A VS B 全场回放 08:00', '', url, event_date=self.now.date(), **kw)

    def test_advertised_roots_only_no_credentials_or_private_hosts(self):
        text = ' '.join(['http://8.8.8.8:7788/968123456', 'http://192.168.1.1:7788/968123456',
                         'http://user:password@public.example:7788/968123456', 'http://public.example/live/user/password/968123456',
                         'http://public.example/968123456?token=private', 'http://public.example/968123456789'])
        self.assertEqual(d.extract_roots(text), ['http://8.8.8.8:7788'])

    def test_candidate_dns_cannot_resolve_to_lan(self):
        with patch('discover.socket.getaddrinfo', return_value=[(2,1,6,'',('192.168.1.2',80))]):
            self.assertFalse(d.safe_root('http://public.example', resolve=True))

    def test_two_observations_require_time_gap(self):
        item={'url':'https://public.example','id':'migu-new','status':'new'}
        good={'qualified':True}
        one=d.observe(item,good,self.now,{})
        self.assertEqual(one['status'],'probation')
        too_soon=d.observe(one,good,self.now+timedelta(minutes=5),{})
        self.assertEqual(too_soon['status'],'probation')
        two=d.observe(one,good,self.now+timedelta(hours=6),{})
        self.assertEqual(two['status'],'active')
        fail=d.observe(two,{'qualified':False},self.now+timedelta(hours=7),{})
        self.assertEqual(fail['status'],'active')
        fail=d.observe(fail,{'qualified':False},self.now+timedelta(hours=8),{})
        self.assertEqual(fail['status'],'quarantined')

    def test_active_registry_expiry_and_seed_deduplication(self):
        items=[{'url':self.config['sources'][0]['url'],'id':'seed','status':'active','last_qualified_at':self.now.isoformat()},
               {'url':'https://new.example','id':'migu-new','status':'active','last_qualified_at':self.now.isoformat()},
               {'url':'https://stale.example','id':'migu-old','status':'active','last_qualified_at':(self.now-timedelta(days=4)).isoformat()},
               {'url':'http://127.0.0.1','id':'private','status':'active','last_qualified_at':self.now.isoformat()}]
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'registry.json';path.write_text(json.dumps({'sources':items}))
            admitted=d.active_sources(path,self.now,self.config)
        self.assertEqual([s['id'] for s in admitted],['migu-new'])
        self.assertFalse(admitted[0]['channels'])

    def test_failed_feeds_preserve_registry_without_promoting(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'registry.json';report=Path(tmp)/'report.json'
            cfg=copy.deepcopy(self.config);cfg['sources']=[];cfg['discovery']['feeds']=['https://feed.example/list']
            item={'url':'https://old.example','id':'migu-old','status':'probation','first_seen_at':self.now.isoformat()}
            path.write_text(json.dumps({'sources':[item]}))
            with patch('builtins.print'):
                code=d.run(cfg,path,report,self.now,feeds={},checker=lambda *a:{'qualified':False,'error':'offline'})
            self.assertEqual(code,2)
            result=json.loads(path.read_text())['sources']
            self.assertEqual(len(result),1)
            self.assertNotEqual(result[0]['status'],'active')

    def test_previous_good_route_is_retained_before_new_priority(self):
        old=self.event(source='migu-primary');new=p.replace(old,url='https://better.example/968123456',source='migu-backup')
        kept,_=p.select_bucket([new,old],self.config,lambda e:{'ok':True,'reason':'decoded'},preferred=[old.fingerprint])
        self.assertEqual(kept[0].url,old.url)
        kept,_=p.select_bucket([new,old],self.config,lambda e:{'ok':e.url==new.url,'reason':'decoded' if e.url==new.url else 'timeout'},preferred=[old.fingerprint])
        self.assertEqual(kept[0].url,new.url)
        self.assertTrue(kept[0].verified)

    def test_implicit_user_agent_has_stable_route_fingerprint(self):
        e=self.event()
        self.assertEqual(e.fingerprint,p.replace(e,options=['#EXTVLCOPT:http-user-agent='+p.UA]).fingerprint)

    def test_live_first_observation_excluded_then_second_admitted(self):
        e=self.event(verified=True,check_status='decoded',evidence={'kind':'live'})
        accepted,proof=p.qualify_events([[e]],{},self.now,self.config)
        self.assertEqual(accepted,[])
        self.assertEqual(e.check_status,'observation_pending')
        fresh=self.event(verified=True,check_status='decoded',evidence={'kind':'live'})
        accepted,_=p.qualify_events([[fresh]],proof,self.now+timedelta(minutes=21),self.config)
        self.assertEqual(len(accepted),1)
        changed=p.replace(fresh,url='https://new.example/968123456')
        accepted,_=p.qualify_events([[changed]],proof,self.now+timedelta(minutes=21),self.config)
        self.assertEqual(accepted,[])

    def test_stale_live_observation_cannot_qualify(self):
        e=self.event(verified=True,evidence={'kind':'live'})
        _,proof=p.qualify_events([[e]],{},self.now,self.config)
        fresh=self.event(verified=True,evidence={'kind':'live'})
        accepted,_=p.qualify_events([[fresh]],proof,self.now+timedelta(hours=4),self.config)
        self.assertEqual(accepted,[])

    def test_vod_requires_distant_decode_samples(self):
        prober=object.__new__(p.Prober);prober.config=self.config
        result=subprocess.CompletedProcess([],0,'{"format":{"duration":"7200"}}','')
        with patch('pipeline.subprocess.run',return_value=result),patch.object(prober,'decode',return_value={'ok':True,'reason':'decoded'}) as decode:
            proof=prober.event_check(self.event(),45)
        self.assertTrue(proof['ok'])
        self.assertEqual([call.kwargs['offset'] for call in decode.call_args_list],[0,3600,5760])
        self.assertEqual(proof['kind'],'vod')

    def test_short_vod_or_midpoint_failure_never_qualifies(self):
        prober=object.__new__(p.Prober);prober.config=self.config
        short=subprocess.CompletedProcess([],0,'{"format":{"duration":"60"}}','')
        with patch('pipeline.subprocess.run',return_value=short),patch.object(prober,'decode') as decode:
            self.assertEqual(prober.event_check(self.event(),45)['reason'],'short_vod')
            decode.assert_not_called()
        long=subprocess.CompletedProcess([],0,'{"format":{"duration":"7200"}}','')
        with patch('pipeline.subprocess.run',return_value=long),patch.object(prober,'decode',side_effect=[{'ok':True},{'ok':False,'reason':'http_403'}]):
            self.assertFalse(prober.event_check(self.event(),45)['ok'])

    def test_hls_duration_retry_and_unknown_replay_cannot_downgrade_to_live(self):
        prober=object.__new__(p.Prober);prober.config=self.config;prober.hls_options=['-allowed_extensions','ALL']
        failed=subprocess.CompletedProcess([],1,'{}','Not detecting m3u8/hls with non standard extension')
        passed=subprocess.CompletedProcess([],0,'{"format":{"duration":"7200"}}','')
        with patch('pipeline.subprocess.run',side_effect=[failed,passed]) as run,patch.object(prober,'decode',return_value={'ok':True}):
            self.assertEqual(prober.event_check(self.event(),45)['kind'],'vod')
            self.assertIn('hls',run.call_args_list[1].args[0])
        with patch('pipeline.subprocess.run',return_value=failed),patch.object(prober,'decode') as decode:
            self.assertEqual(prober.event_check(self.event(),45)['reason'],'vod_duration_unknown')
            decode.assert_not_called()

    def test_known_wrong_match_is_blocked_before_any_network_or_decode(self):
        prober=object.__new__(p.Prober);prober.config=self.config
        bad=self.event(url=self.config['blocked_event_routes'][0]['url'])
        with patch('pipeline.access_hint') as hint,patch.object(prober,'decode') as decode:
            self.assertEqual(prober(bad)['reason'],'content_mismatch')
            hint.assert_not_called();decode.assert_not_called()

    def test_public_feed_with_more_than_two_megabytes_is_bounded_but_accepted(self):
        result=subprocess.CompletedProcess([],0,b'x'*(3*1024*1024),b'')
        with patch('discover.subprocess.run',return_value=result) as run:
            self.assertEqual(len(d.read_feed('https://public.example/feed')),3*1024*1024)
            self.assertIn(str(8*1024*1024),run.call_args.args[0])
        with patch('discover.subprocess.run',return_value=subprocess.CompletedProcess([],28,b'',b'')):
            with self.assertRaisesRegex(ValueError,'curl_28'):
                d.read_feed('https://public.example/feed')

    def test_trial_hidden_behind_second_redirect_is_rejected(self):
        opener=unittest.mock.MagicMock()
        opener.open.side_effect=[p.urllib.error.HTTPError('https://source.example',302,'',{'Location':'https://redirect.example/stream'},None),
                                p.urllib.error.HTTPError('https://redirect.example',302,'',{'Location':'https://gslbtrialmgsplive.miguvideo.com/a'},None)]
        with patch('pipeline.urllib.request.build_opener',return_value=opener):
            self.assertEqual(p.access_hint('https://source.example',2),'trial_stream')

    def test_independent_verifier_rejects_unqualified_stream_injection(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp)/'public'
            vod=self.event(verified=True,check_status='decoded',evidence={'kind':'vod','duration_seconds':7200,'sample_offsets':[0,3600,5760],'sample_seconds':5})
            _,proof=p.qualify_events([[vod]],{},self.now,self.config)
            p.atomic_write(out.parent/'state/event-evidence.json',json.dumps(proof))
            today=self.now.date()
            catalogue=[[vod]]+[ [p.replace(vod,event_date=today+timedelta(days=delta),verified=False,upcoming=True)] for delta in [-1,1]]
            report={'accepted':True,'channels':0,'generated_at':self.now.isoformat(),'event_policy':'qualified_only_with_separate_complete_schedule',
                    'events':{str(today+timedelta(days=d)):{} for d in [-1,0,1]},'published_events':1}
            p.publish(out,[[vod]],report,self.now,self.config,catalogue)
            self.assertEqual(v.verify(out)['published_events'],1)
            with (out/'events.m3u').open('a') as file:
                file.write('#EXTINF:-1 group-title="咪咕·今天",bad\nhttps://bad.example/968123456\n')
            with self.assertRaisesRegex(AssertionError,'unqualified_event'):
                v.verify(out)
