"""Step 7: bounded mock workloads, clean offline install, evidence handover."""
from contextlib import redirect_stdout
import copy
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from test_mock_provider import ROOT, MOCKS


class SubmissionContract(unittest.TestCase):
    def make_workload(self, root, count):
        inputs=root/'inputs';fixtures=root/'fixtures';inputs.mkdir(parents=True);fixtures.mkdir()
        base=json.loads((ROOT/'materials/requests/IT-001.json').read_text())
        reply=json.loads((MOCKS/'IT-001.json').read_text())
        entries=[]
        for index in range(count):
            identifier=f'IT-{10000+index}'
            source=copy.deepcopy(base);source['message']=source['message'].replace('IT-001',identifier)
            raw=(json.dumps(source,ensure_ascii=False)+'\n').encode()
            name=identifier+'.json';(inputs/name).write_bytes(raw)
            payload=copy.deepcopy(reply)
            payload['output'][0]['content'][0]['text']=payload['output'][0]['content'][0]['text'].replace('IT-001',identifier)
            (fixtures/name).write_text(json.dumps(payload))
            entries.append(dict(document_id=name,source_sha256=hashlib.sha256(raw).hexdigest(),message_sha256=hashlib.sha256(source['message'].encode()).hexdigest(),response_file=name))
        (fixtures/'manifest.json').write_text(json.dumps(dict(format_version=1,response_source='mock',fixtures=entries)))
        return inputs,fixtures

    def test_T_41_mock_workload(self):
        from it_access.runner import capture_settings,run_jobs
        from it_access.providers import MockProvider,ProviderConfig
        from it_access.state import StateStore
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);datasets={}
            for count in (400,2000):
                inputs,fixtures=self.make_workload(root/str(count),count)
                settings,provider=capture_settings(ProviderConfig(),fixtures,2,0)
                datasets[count]=(inputs,settings,provider)
            # Provenance per record must remain bounded when the batch grows,
            # rather than duplicating every fixture hash into every DB row.
            self.assertLess(len(json.dumps(datasets[2000][1])),2*len(json.dumps(datasets[400][1])))
            for count,(inputs,settings,provider) in datasets.items():
                active=maximum=calls=0;lock=threading.Lock();real=MockProvider.extract
                def observe(instance,**kwargs):
                    nonlocal active,maximum,calls
                    with lock:active+=1;maximum=max(maximum,active);calls+=1
                    try:return real(instance,**kwargs)
                    finally:
                        with lock:active-=1
                started=time.monotonic()
                with StateStore(root/str(count)/'state.db') as store, mock.patch.object(MockProvider,'extract',observe), redirect_stdout(io.StringIO()):
                    jobs=[store.enqueue(path.name,path.read_bytes(),settings) for path in sorted(inputs.glob('*.json'))]
                    key=('mock',settings['mock_dir'],settings['endpoint'])
                    run_jobs(store,jobs,providers={key:provider})
                    rows=store.queue();self.assertEqual(len(rows),count)
                    self.assertEqual(len({r['document_id'] for r in rows}),count)
                    for row in rows:
                        self.assertEqual(row['source_sha256'],hashlib.sha256((inputs/row['document_id']).read_bytes()).hexdigest())
                        self.assertEqual(row['extraction']['request_id'],Path(row['document_id']).stem)
                        self.assertEqual((row['status'],row['owner'],row['next_action']),('review','System Owner','review-request'))
                    repeated=[store.enqueue(path.name,path.read_bytes(),settings) for path in sorted(inputs.glob('*.json'))]
                    self.assertTrue(all(not job.needs_processing for job in repeated))
                    run_jobs(store,repeated,providers={key:provider})
                    self.assertEqual(calls,count);self.assertLessEqual(maximum,2);self.assertEqual(active,0)
                print(f'WORKLOAD mock count={count} seconds={time.monotonic()-started:.3f} calls={calls} max_in_flight={maximum}',flush=True)

    def test_T_42_readme_lockfile_handover(self):
        readme=(ROOT/'README.md').read_text()
        for term in ('--require-hashes','--no-build-isolation','intake','queue','retry','resume','refresh','export','accuracy','API_KEY','one host','mock'):
            self.assertIn(term,readme)
        lockfile=ROOT/'requirements.lock';self.assertTrue(lockfile.is_file())
        lines=[line for line in lockfile.read_text().splitlines() if line and not line.startswith(('#','--'))]
        self.assertGreaterEqual(len(lines),7)
        wheels=Path('/tmp/it-access-wheels')
        if not wheels.is_dir():self.skipTest('Offline wheel cache unavailable; fresh installation NOT VERIFIED')
        for line in lines:self.assertRegex(line,r'^[A-Za-z0-9_-]+==[^ ]+ --hash=sha256:[0-9a-f]{64}$')
        with tempfile.TemporaryDirectory() as tmp:
            env=Path(tmp)/'env'
            subprocess.run([sys.executable,'-m','venv','--without-pip',str(env)],check=True,capture_output=True)
            python=env/'bin/python'
            clean={'PATH':'/usr/bin:/bin','PIP_NO_CACHE_DIR':'1','PYTHONDONTWRITEBYTECODE':'1'}
            def execute(command):
                result=subprocess.run(list(map(str,command)),cwd=ROOT,env=clean,capture_output=True,text=True,timeout=60)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                return result.stdout
            execute([sys.executable,'-m','pip','--python',python,'install','--no-index','--find-links',wheels,'--require-hashes','-r',lockfile])
            execute([sys.executable,'-m','pip','--python',python,'install','--no-index','--no-build-isolation','--no-deps','-e',ROOT])
            executable=env/'bin/it-access'
            self.assertIn('intake',execute([executable,'--help']))
            db=Path(tmp)/'state.db'
            execute([executable,'--db',db,'intake','materials/requests','--min-interval','0'])
            rows=json.loads(execute([executable,'--db',db,'queue','--json']))
            self.assertEqual(len(rows),20)
            execute([executable,'--db',db,'export','--output',Path(tmp)/'out'])
            execute([executable,'verify-export','--output',Path(tmp)/'out'])

    def test_T_43_verification_evidence_index(self):
        spec=(ROOT/'planning/spec.md').read_text()
        validation=(ROOT/'planning/validation.md').read_text()
        ids=re.findall(r'^\| (R-\d+) \|',spec,re.M)
        self.assertEqual(re.findall(r'^\| (R-\d+) \|',validation,re.M),ids)
        # Verdicts belong to the human reviewer; editing them must not break the suite.
        verdicts=re.findall(r'^\| R-\d+ \| .*? \| (met|not met) \|',validation,re.M)
        self.assertEqual(len(verdicts),len(ids))
        self.assertIn(f'Counts: {verdicts.count("met")} met; {verdicts.count("not met")} not met.',validation)
        verification=(ROOT/'deliverables/verification.md').read_text()
        for word in ('mock','400','2,000','interruption','limits','planning/test-run.txt'):
            self.assertIn(word,verification)
        for name in ('extractions.jsonl','queue.json','accuracy.md','request-evidence.json'):
            self.assertTrue((ROOT/'deliverables'/name).is_file())
