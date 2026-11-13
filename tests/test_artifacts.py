"""Step 6: export integrity and source-independent accuracy comparisons."""
from contextlib import ExitStack, redirect_stdout
import copy
import hashlib
import importlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from test_mock_provider import ROOT, MOCKS


class ArtifactContract(unittest.TestCase):
    def setUp(self):
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        self.directory=Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.db=self.directory/'intake.sqlite3';self.output=self.directory/'deliverables'
        self.expected_path=ROOT/'tests/expected/facts.json'
        self.expected=json.loads(self.expected_path.read_text())
        self.a=importlib.import_module('it_access.artifacts')

    def seed(self, documents=('IT-001.json','IT-002.json'), failed=False, pending=False):
        from it_access.providers import ProviderConfig,ProviderResponse
        from it_access.runner import capture_settings
        from it_access.state import StateStore
        from it_access.extraction import process_response
        settings,_=capture_settings(ProviderConfig(),MOCKS,2,0)
        with StateStore(self.db) as store:
            for doc in self.expected['documents']:
                if doc['document_id'] not in documents:continue
                raw=(ROOT/doc['source_path']).read_bytes()
                job=store.enqueue(doc['document_id'],raw,settings)
                if not job.needs_processing:continue
                store.mark_running(job.attempt_id)
                name='paraphrase.json' if doc['document_id']=='IT-101.json' else doc['document_id']
                payload=json.loads((MOCKS/name).read_text())
                record=process_response(document_id=doc['document_id'],source_sha256=hashlib.sha256(raw).hexdigest(),message=json.loads(raw)['message'],response=ProviderResponse(payload,'mock'))
                store.finish(job.attempt_id,record)
            if failed:
                raw=(ROOT/'materials/requests/IT-003.json').read_bytes()
                job=store.enqueue('IT-003.json',raw,settings);store.mark_running(job.attempt_id)
                failure=json.loads((MOCKS/'provider-unavailable.json').read_text())['mock_error']
                store.finish(job.attempt_id,dict(document_id='IT-003.json',source_sha256=hashlib.sha256(raw).hexdigest(),response_source='mock',extraction=None,status='error',owner='IT Service Desk',next_action='retry-extraction',error={**failure,'retry_command':'it-access retry IT-003.json'}))
            if pending:store.enqueue('IT-012.json',(ROOT/'materials/requests/IT-012.json').read_bytes(),settings)

    def rewrite_hash(self, filename):
        manifest=json.loads((self.output/'manifest.json').read_text())
        manifest['files'][filename]=hashlib.sha256((self.output/filename).read_bytes()).hexdigest()
        (self.output/'manifest.json').write_text(json.dumps(manifest))

    def test_T_37_export_consistency(self):
        self.seed(failed=True,pending=True)
        self.a.export_snapshot(self.db,self.output)
        manifest=self.a.verify_export(self.output)
        self.assertEqual(manifest['pending_documents'],['IT-012.json'])
        records=[json.loads(line) for line in (self.output/'extractions.jsonl').read_text().splitlines()]
        queue=json.loads((self.output/'queue.json').read_text())
        self.assertEqual({r['document_id'] for r in records},{'IT-001.json','IT-002.json','IT-003.json'})
        self.assertEqual(len(queue),3)
        self.assertIsNone(next(r for r in records if r['document_id']=='IT-003.json')['extraction'])
        for record,row in zip(records,queue):
            for key in ('document_id','source_sha256','attempt_id','generation_id','status','owner','next_action'):
                self.assertEqual(record[key],row[key])
        for field,value in (('owner','Wrong Owner'),('attempt_id',-1),('source_sha256','0'*64)):
            with self.subTest(tamper=field):
                self.a.export_snapshot(self.db,self.output)
                queue=json.loads((self.output/'queue.json').read_text());queue[0][field]=value
                (self.output/'queue.json').write_text(json.dumps(queue));self.rewrite_hash('queue.json')
                with self.assertRaises(self.a.ArtifactError):self.a.verify_export(self.output)
        self.a.export_snapshot(self.db,self.output)
        with (self.output/'extractions.jsonl').open('a') as f:f.write((self.output/'extractions.jsonl').read_text().splitlines()[0]+'\n')
        self.rewrite_hash('extractions.jsonl')
        with self.assertRaises(self.a.ArtifactError):self.a.verify_export(self.output)

    def test_T_38_interrupted_export(self):
        from it_access.runner import WriterLock,BusyError
        self.seed()
        for stop in (2,3):
            self.a.export_snapshot(self.db,self.output)
            real_replace=self.a.os.replace;calls=[]
            def interrupted(source,destination):
                calls.append(destination)
                if len(calls)==stop:raise OSError('simulated publication interruption')
                return real_replace(source,destination)
            with mock.patch.object(self.a.os,'replace',interrupted):
                with self.assertRaises(OSError):self.a.export_snapshot(self.db,self.output)
            with self.assertRaises(self.a.ArtifactError):self.a.verify_export(self.output)
            self.a.export_snapshot(self.db,self.output);self.a.verify_export(self.output)
        with WriterLock(self.db):
            with self.assertRaises(BusyError):self.a.export_snapshot(self.db,self.output)

    def perfect_records(self):
        records=[]
        for doc in self.expected['documents']:
            filename='paraphrase.json' if doc['document_id']=='IT-101.json' else doc['document_id']
            extraction=json.loads(json.loads((MOCKS/filename).read_text())['output'][0]['content'][0]['text'])
            records.append({key:doc[key] for key in ('document_id','source_sha256','status','owner','next_action') } | {'extraction':extraction,'error':None,'response_source':'mock'})
        return records

    def score(self,records,extra=''):
        path=self.directory/'actual.jsonl'
        path.write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in records)+extra)
        return self.a.score(self.expected_path,path)

    def test_T_39_accuracy_scoring(self):
        baseline=self.perfect_records()
        result=self.score(baseline)
        self.assertEqual((result['correct'],result['total'],result['routing_correct']),(252,252,21))
        changed=copy.deepcopy(baseline);changed[0]['extraction']['requester']='Invented Person'
        result=self.score(changed);self.assertEqual(result['correct'],251)
        self.assertEqual(result['mismatches'][0]['classification'],'unreviewed')
        for name in ('failed','missing','duplicate','invalid'):
            with self.subTest(case=name):
                changed=copy.deepcopy(baseline)
                if name=='failed':changed[2].update(extraction=None,status='error',error={'category':'timeout'})
                elif name=='missing':changed.pop(0)
                elif name=='duplicate':changed.append(copy.deepcopy(changed[0]))
                else:del changed[2]['extraction']['environment']
                result=self.score(changed)
                self.assertEqual(result['correct'],240)
                self.assertEqual(result['total'],252)
        changed=copy.deepcopy(baseline);changed[0]['owner']='Security Review'
        result=self.score(changed);self.assertEqual(result['correct'],252);self.assertEqual(result['routing_correct'],20)
        result=self.score(baseline,extra='not JSON\n');self.assertTrue(result['errors'])
        changed=copy.deepcopy(baseline);extra=copy.deepcopy(changed[0]);extra['document_id']='unexpected.json';changed.append(extra)
        result=self.score(changed);self.assertTrue(result['errors']);self.assertEqual(result['total'],252)

    def test_T_40_mock_deliverable_generation(self):
        self.seed(documents=tuple(doc['document_id'] for doc in self.expected['documents']))
        self.a.export_snapshot(self.db,self.output)
        self.a.verify_export(self.output)
        report=self.a.write_accuracy(self.expected_path,self.output/'extractions.jsonl',self.output/'accuracy.md')
        self.assertEqual(report['correct'],252)
        self.assertEqual(report['routing_correct'],21)
        text=(self.output/'accuracy.md').read_text()
        self.assertIn('mock',text.lower());self.assertIn('252',text)
        for doc in self.expected['documents']:self.assertIn(doc['document_id'],text)
