import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import diagnose_vod as d
import pipeline as p


class VodDiagnosticTests(unittest.TestCase):
    def test_signed_urls_are_redacted(self):
        text=d.redact('Failed https://cdn.example/a.m3u8?token=private-token\nConnection timed out')
        self.assertNotIn('private-token',text)
        self.assertIn('Connection timed out',text)

    def test_only_same_public_unsigned_entrypoints_are_allowed(self):
        data={'targets':[{'name':'Replay','source':'test','url':'https://public.example/968123456'}]}
        self.assertEqual(len(d.validate_targets(data)),1)
        for url in ['http://127.0.0.1/968123456','https://public.example/968123456?token=secret',
                    'https://public.example/live/user/password/123.ts']:
            data['targets'][0]['url']=url
            with self.assertRaises(ValueError):d.validate_targets(data)

    def test_metadata_profiles_keep_the_actual_ffprobe_error(self):
        result=subprocess.CompletedProcess([],1,'{}','[hls] URL https://cdn.example/a?token=secret is not in allowed_extensions')
        entry=p.Entry('Replay','','https://public.example/968123456')
        with patch('diagnose_vod.subprocess.run',return_value=result) as run:
            baseline=d.metadata(entry,'baseline',['-allowed_extensions','ALL'])
            self.assertNotIn('-allowed_extensions',run.call_args.args[0])
            self.assertIn('allowed_extensions',baseline['error'])
            self.assertNotIn('secret',baseline['error'])
            d.metadata(entry,'forced_hls',['-allowed_extensions','ALL'])
            self.assertIn('hls',run.call_args.args[0])
            d.metadata(entry,'proxy_protocol',[])
            self.assertIn('http,https,tcp,tls,crypto,httpproxy',run.call_args.args[0])

    def test_timeout_is_distinct_from_missing_duration(self):
        entry=p.Entry('Replay','','https://public.example/968123456')
        with patch('diagnose_vod.subprocess.run',side_effect=subprocess.TimeoutExpired('ffprobe',10,stderr=b'Connection timed out')):
            result=d.metadata(entry,'baseline',[])
        self.assertTrue(result['total_timeout'])
        self.assertIn('Connection timed out',result['error'])
        good=subprocess.CompletedProcess([],0,'{"format":{"duration":"9520.48"}}','')
        self.assertEqual(d.duration_result(good)['duration_seconds'],9520.48)

    def test_transport_report_does_not_keep_redirect_tokens(self):
        def fake(cmd,**kwargs):
            Path(cmd[cmd.index('--output')+1]).write_bytes(b'#EXTM3U\n')
            return subprocess.CompletedProcess(cmd,0,'206\napplication/vnd.apple.mpegurl\nhttps://cdn.example/a.m3u8?token=private-token','')
        with patch('diagnose_vod.subprocess.run',side_effect=fake):
            report=d.transport('https://public.example/968123456')
        self.assertEqual(report['final_host'],'cdn.example')
        self.assertEqual(report['media_prefix'],'hls')
        self.assertNotIn('private-token',json.dumps(report))

    def test_tcp_timeout_and_recovery_are_separate(self):
        with patch('diagnose_vod.socket.create_connection',side_effect=[TimeoutError(),unittest.mock.MagicMock()]):
            result=d.tcp_check('gslbmgspvod.miguvideo.com')
        self.assertFalse(result[0]['ok']);self.assertTrue(result[1]['ok'])
        self.assertEqual([x['timeout'] for x in result],[5,15])

    def test_nba_diagnostic_targets_are_unsigned_and_cover_both_voices(self):
        entries=d.validate_targets(json.loads((p.ROOT/'diagnostic-nba-targets.json').read_text()))
        self.assertEqual(len(entries),6)
        self.assertTrue(any('中文' in e.name for e in entries))
        self.assertTrue(any('英文' in e.name for e in entries))

    def test_encryption_diagnostic_keeps_flags_without_key_addresses(self):
        def fake(cmd,**kwargs):
            Path(cmd[cmd.index('--output')+1]).write_bytes(b'#EXTM3U\n#EXT-X-KEY:METHOD=SAMPLE-AES,KEYFORMAT="com.widevine",URI="https://key.example/?token=private"\n')
            return subprocess.CompletedProcess(cmd,0,'200\napplication/vnd.apple.mpegurl\nhttps://cdn.example/video?token=private','')
        with patch('diagnose_vod.subprocess.run',side_effect=fake):
            report=d.transport('https://public.example/968123456')
        self.assertTrue(report['hls_has_encryption_tag'])
        self.assertTrue(report['hls_has_drm_keyformat'])
        self.assertNotIn('private',json.dumps(report))
        self.assertNotIn('key.example',json.dumps(report))
