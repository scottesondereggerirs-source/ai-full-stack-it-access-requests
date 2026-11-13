"""Local request lab: controller tests are offline and use the real pipeline."""
from contextlib import ExitStack
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import shutil
import socket
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from urllib.error import HTTPError
from unittest import mock

from test_mock_provider import CredentialsForbidden, ROOT, MOCKS


class WebDemoContract(unittest.TestCase):
    def test_W_08_rejected_http_connection_is_closed(self):
        # Feed actual HTTP bytes through the handler without binding a socket.
        # An unread rejected body must never become the next request line.
        class Connection:
            def __init__(self, raw):self.input=io.BytesIO(raw);self.output=bytearray()
            def makefile(self, *args):return self.input
            def sendall(self, raw):self.output.extend(raw)
        connection=Connection(b'POST /api/process HTTP/1.1\r\nHost: 127.0.0.1:8765\r\nOrigin: https://example.com\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}GET /api/files HTTP/1.1\r\nHost: 127.0.0.1:8765\r\n\r\n')
        self.web.RequestHandler(connection,('127.0.0.1',1234),SimpleNamespace(app=self.app,server_port=8765))
        response=connection.output.decode()
        self.assertIn('HTTP/1.1 403',response)
        self.assertIn('Connection: close\r\n',response)
        self.assertEqual(response.count('HTTP/1.1'),1)
        self.assertFalse(self.db.exists())

    def setUp(self):
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(os,'environ',CredentialsForbidden()))
        for owner,name in ((socket.socket,'connect'),(socket,'create_connection')):
            guard=self.stack.enter_context(mock.patch.object(owner,name,side_effect=AssertionError('network forbidden')))
            self.addCleanup(guard.assert_not_called)
        self.directory=Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.inputs=self.directory/'inputs';shutil.copytree(ROOT/'materials/requests',self.inputs)
        self.db=self.directory/'web.sqlite3'
        self.web=importlib.import_module('it_access.web')
        self.app=self.web.DemoApplication(self.inputs,self.db,MOCKS)
        self.addCleanup(self.app.close)

    def wait(self,job_id):
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            job=self.app.job(job_id)
            if job['state']!='processing':return job
            time.sleep(0.01)
        self.fail('web job did not complete')

    def start(self,document='IT-001.json',provider='mock',retry=False):
        source=self.app.source(document)
        return self.app.start(document,provider,source['source_sha256'],retry=retry)

    def test_W_01_sources_and_confinement(self):
        files=self.app.files();self.assertEqual(len(files),20)
        source=self.app.source('IT-006.json')
        raw=(self.inputs/'IT-006.json').read_bytes()
        self.assertEqual(source['source_sha256'],hashlib.sha256(raw).hexdigest())
        self.assertEqual(source['message'],json.loads(raw)['message'])
        self.assertIn('SYSTEM OVERRIDE',source['message'])
        for name in ('../CHALLENGE.md','/etc/passwd','missing.json'):
            with self.subTest(name=name):
                with self.assertRaises(self.web.WebError):self.app.source(name)
        external=self.directory/'outside.json';external.write_text('{"message":"outside"}')
        (self.inputs/'linked.json').symlink_to(external)
        self.assertNotIn('linked.json',{item['document_id'] for item in self.app.files()})

    def test_W_02_pipeline_and_cache(self):
        from it_access.providers import MockProvider
        real=MockProvider.extract;calls=[]
        def observed(instance,**kwargs):calls.append(kwargs['document_id']);return real(instance,**kwargs)
        with mock.patch.object(MockProvider,'extract',observed):
            first=self.wait(self.start('IT-004.json'))
            self.assertEqual(first['state'],'complete')
            result=first['result']
            self.assertEqual((result['status'],result['owner'],result['next_action']),('needs-information','IT Service Desk','clarify'))
            self.assertIsNone(result['extraction']['environment'])
            self.assertEqual(result['response_source'],'mock')
            self.assertNotIn('settings',result)
            second=self.wait(self.start('IT-004.json'))
            self.assertTrue(second['reused'])
            self.assertEqual(second['result']['attempt_id'],result['attempt_id'])
            self.assertEqual(calls,['IT-004.json'])

    def test_W_03_intercepted_live_error_retry(self):
        from it_access.live_provider import OpenAIProvider
        secret='WEB_SYNTHETIC_SECRET'
        transport=mock.Mock(side_effect=[HTTPError('https://example.test',401,secret,{},None),io.BytesIO((MOCKS/'openai-completed.json').read_bytes())])
        provider=OpenAIProvider(transport=transport,environ={'API_KEY':secret})
        with mock.patch('it_access.runner.provider_for',return_value=provider):
            failed=self.wait(self.start(provider='openai'))
            self.assertEqual(failed['result']['status'],'error')
            self.assertIsNone(failed['result']['extraction'])
            self.assertEqual(failed['result']['error']['category'],'provider_auth')
            self.assertNotIn(secret,json.dumps(failed))
            recovered=self.wait(self.start(provider='openai',retry=True))
            self.assertEqual(recovered['result']['status'],'review')
            self.assertNotEqual(recovered['result']['attempt_id'],failed['result']['attempt_id'])
            self.assertTrue(recovered['result']['response_source'].startswith('openai:'))
            self.assertEqual(transport.call_count,2)
            self.assertNotIn(secret,json.dumps(recovered))

    def test_W_04_rejected_requests(self):
        source=self.app.source('IT-001.json')
        body={'document_id':'IT-001.json','provider':'mock','source_sha256':source['source_sha256']}
        status,_=self.app.dispatch('POST','/api/process',body,token='invalid')
        self.assertEqual(status,403);self.assertFalse(self.db.exists())
        body['source_sha256']='0'*64
        status,_=self.app.dispatch('POST','/api/process',body,token=self.app.csrf_token)
        self.assertEqual(status,409);self.assertFalse(self.db.exists())
        body.update(source_sha256=source['source_sha256'],provider='unknown')
        status,_=self.app.dispatch('POST','/api/process',body,token=self.app.csrf_token)
        self.assertEqual(status,400)
        self.assertTrue(self.web.is_local_request('127.0.0.1:8765','http://127.0.0.1:8765',8765))
        self.assertFalse(self.web.is_local_request('evil.example:8765','http://evil.example:8765',8765))
        self.assertFalse(self.web.is_local_request('127.0.0.1:8765','https://evil.example',8765))

    def test_W_05_busy_but_readable(self):
        from it_access.providers import MockProvider
        from it_access.runner import BusyError
        entered=threading.Event();release=threading.Event();self.addCleanup(release.set)
        real=MockProvider.extract
        def blocked(instance,**kwargs):
            entered.set()
            if not release.wait(4):raise AssertionError('test did not release provider')
            return real(instance,**kwargs)
        with mock.patch.object(MockProvider,'extract',blocked):
            identifier=self.start();self.assertTrue(entered.wait(3))
            self.assertEqual(self.app.job(identifier)['state'],'processing')
            self.assertEqual(len(self.app.files()),20)
            with self.assertRaises(BusyError):self.start('IT-002.json')
            release.set();self.assertEqual(self.wait(identifier)['state'],'complete')

    def test_W_06_assets_and_text_rendering(self):
        html=self.app.asset('/')[1].decode()
        script=self.app.asset('/assets/app.js')[1].decode()
        css=self.app.asset('/assets/app.css')[1].decode()
        self.assertIn(self.app.csrf_token,html)
        for value in ('file-select','provider-select','run-button','input-message','result-panel'):
            self.assertIn(value,html)
        self.assertIn('textContent',script);self.assertNotIn('innerHTML',script)
        self.assertIn('supporting_quotes',script);self.assertIn('source_sha256',script)
        self.assertIn('@media',css)
        with self.assertRaises(self.web.WebError):self.app.asset('/assets/../../CHALLENGE.md')

    def test_W_07_cli_launch_options(self):
        from it_access.cli import _parser
        args=_parser().parse_args(['--db',str(self.db),'serve','--port','8765','--input-dir',str(self.inputs)])
        self.assertEqual(args.command,'serve');self.assertEqual(args.port,8765)
        self.assertEqual(args.input_dir,self.inputs)
        status,payload=self.app.dispatch('GET','/api/config')
        self.assertEqual(status,200);self.assertEqual(payload['default_provider'],'mock')
        self.assertNotIn('API_KEY',json.dumps(payload))

    def test_W_09_explicit_gateway_origin(self):
        gateway='https://8765-demo.gw.example.test'
        app=self.web.DemoApplication(self.inputs,self.db,MOCKS,browser_origin=gateway+'/')
        self.addCleanup(app.close)
        from it_access.cli import _parser
        self.assertEqual(_parser().parse_args(['serve','--browser-origin',gateway]).browser_origin,gateway)
        for invalid in ('null','*','https://example.test/path','https://user:pass@example.test','https://example.test?key=value','https://example.test#fragment','https://example.test:bad','https://example.test\n'):
            with self.subTest(invalid=invalid),self.assertRaises(self.web.StateError):
                self.web.DemoApplication(self.inputs,self.db,MOCKS,browser_origin=invalid)
        class Connection:
            def __init__(self, raw):self.input=io.BytesIO(raw);self.output=bytearray()
            def makefile(self,*args):return self.input
            def sendall(self,raw):self.output.extend(raw)
        source=app.source('IT-001.json')
        body=json.dumps(dict(document_id='IT-001.json',provider='mock',source_sha256=source['source_sha256'])).encode()
        def post(origin,host='127.0.0.1:8765',token=None,application=app):
            headers=f'POST /api/process HTTP/1.1\r\nHost: {host}\r\nOrigin: {origin}\r\nContent-Type: application/json\r\nX-CSRF-Token: {token or application.csrf_token}\r\nContent-Length: {len(body)}\r\n\r\n'
            connection=Connection(headers.encode()+body)
            self.web.RequestHandler(connection,('127.0.0.1',1234),SimpleNamespace(app=application,server_port=8765))
            head,payload=bytes(connection.output).split(b'\r\n\r\n',1)
            return int(head.split()[1]),json.loads(payload)
        for origin,host,token,application in (
            ('https://evil.example','127.0.0.1:8765',None,app),
            ('null','127.0.0.1:8765',None,app),
            (gateway+':443','127.0.0.1:8765',None,app),
            (gateway,'evil.example',None,app),
            (gateway,'127.0.0.1:8765','wrong-token',app),
            (gateway,'127.0.0.1:8765',None,self.app),
        ):
            self.assertEqual(post(origin,host,token,application)[0],403)
        self.assertFalse(self.db.exists())
        status,job=post(gateway)
        self.assertEqual(status,202)
        deadline=time.monotonic()+5
        while app.job(job['job_id'])['state']=='processing' and time.monotonic()<deadline:time.sleep(0.01)
        self.assertEqual(app.job(job['job_id'])['result']['status'],'review')
        self.assertTrue(self.web.is_local_request('8765-demo.gw.example.test',gateway,8765,gateway))
        self.assertTrue(self.web.is_local_request('localhost:8765',None,8765,gateway))
