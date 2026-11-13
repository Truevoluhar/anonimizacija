import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
import app as service


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        service.DB = Path(self.temp.name) / 'test.sqlite3'
        self.calls = []
        self.output = ''
        self.missing_run = False
        self.bad_result = False
        self.lost_response = False
        self.connection_down = False
        self.run_state = 'completed'
        self.answer_mode = 'normal'
        self.artifact_status = 200
        self.artifact_bytes = b'%PDF-test'
        self.client = TestClient(service.app)
        self.client.__enter__()
        original = service.app.state.http
        self.client.portal.call(original.aclose)
        service.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(self.remote))

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def remote(self, request):
        self.calls.append(request)
        path = request.url.path
        if path == '/v1/auth/login':
            username = json.loads(request.content)['username']
            return httpx.Response(200, json={'guid': 'secret-'+username, 'expires_at': '2099-01-01T00:00:00+00:00'})
        user = request.url.params.get('guid', '').removeprefix('secret-')
        if path == '/v1/auth/me':
            return httpx.Response(200, json={'user_id':user, 'username':user})
        if path == '/v1/files':
            return httpx.Response(200, json={'path':'/uploads/test.pdf'})
        if path == '/v1/threads':
            return httpx.Response(200, json={'thread_id':'thread-1'})
        if path.endswith('/runs'):
            body = json.loads(request.content)
            self.assertEqual(body['attachments'], ['/uploads/test.pdf'])
            self.output = body['message'].split('natančno v ')[1].split('. ')[0]
            if self.lost_response:
                raise httpx.ReadTimeout('response lost', request=request)
            return httpx.Response(202, json={'run_id':'run-1'})
        if path == '/v1/runs/active':
            if self.connection_down:
                raise httpx.ConnectError('offline', request=request)
            return httpx.Response(200, json={'status': self.run_state, 'thread_id':'thread-1', 'run_id':'run-1'} if self.run_state == 'running' else {'status':'idle'})
        if path == '/v1/runs/run-1':
            if self.connection_down:
                raise httpx.ReadTimeout('slow', request=request)
            if self.missing_run:
                return httpx.Response(404)
            return httpx.Response(200, json={'status':self.run_state})
        if path.endswith('/messages'):
            if self.missing_run:
                return httpx.Response(200, json=[])
            payload = {'status':'review_required', 'output_path':'/exports/wrong.pdf' if self.bad_result else self.output, 'summary':'Preglejte vse strani.'}
            if self.answer_mode == 'alternate':
                payload['output_path'] = self.output.replace('.pdf', '_redacted_review_required.pdf')
            elif self.answer_mode == 'failed':
                payload.update(status='failed', output_path='', summary='Samodejna anonimizacija ni uspela.')
            content = json.dumps(payload)
            if self.answer_mode == 'fenced':content = '```json\n' + content + '\n```'
            if self.answer_mode == 'blocks':content = [{'type':'text', 'text':content}]
            if self.answer_mode == 'text':content = 'PDF je ustvarjen v workspaceu.'
            if self.answer_mode == 'none':return httpx.Response(200,json=[])
            return httpx.Response(200, json=[{'role':'assistant', 'content':content}])
        if path.startswith('/v1/files/exports/'):
            return httpx.Response(self.artifact_status, content=self.artifact_bytes)
        return httpx.Response(404)

    def test_authenticated_job_result_and_isolation(self):
        self.assertEqual(self.client.get('/api/jobs').status_code, 401)
        response = self.client.post('/api/login', json={'username':'ana','password':'pw'})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('secret-', response.text)
        self.assertIn('HttpOnly', response.headers['set-cookie'])
        response = self.client.post('/api/jobs', files={'file':('doc.pdf',b'%PDF-test','application/pdf')})
        self.assertEqual(response.status_code, 202)
        job_id = response.json()['id']
        for _ in range(50):
            status = self.client.get('/api/jobs/'+job_id).json()
            if status['status'] == 'completed':
                break
            time.sleep(.01)
        self.assertEqual(status['status'], 'completed', status)
        self.assertNotIn('guid', status)
        self.assertEqual(self.client.get('/api/jobs/'+job_id+'/download').content,b'%PDF-test')
        self.client.post('/api/login', json={'username':'boris','password':'pw'})
        self.assertEqual(self.client.get('/api/jobs/'+job_id).status_code,404)
        self.assertEqual(self.client.get('/api/jobs').json(),[])

    def test_unconfirmed_artifact_cannot_be_downloaded(self):
        self.bad_result = True
        self.client.post('/api/login',json={'username':'ana','password':'pw'})
        job_id = self.client.post('/api/jobs',files={'file':('doc.pdf',b'%PDF-test')}).json()['id']
        for _ in range(50):
            status = self.client.get('/api/jobs/'+job_id).json()
            if status['status'] == 'failed':
                break
            time.sleep(.01)
        self.assertEqual(status['status'],'failed')
        self.assertEqual(self.client.get('/api/jobs/'+job_id+'/download').status_code,409)

    def test_upstream_restart_is_reported_as_failed(self):
        self.missing_run = True
        self.client.post('/api/login',json={'username':'ana','password':'pw'})
        job_id = self.client.post('/api/jobs',files={'file':('doc.pdf',b'%PDF-test')}).json()['id']
        for _ in range(50):
            status = self.client.get('/api/jobs/'+job_id).json()
            if status['status'] == 'failed':
                break
            time.sleep(.01)
        self.assertEqual(status['status'],'failed')

    def test_lost_start_response_recovers_active_run_without_resubmission(self):
        self.lost_response = True
        self.run_state = 'running'
        self.client.post('/api/login',json={'username':'ana','password':'pw'})
        job_id = self.client.post('/api/jobs',files={'file':('doc.pdf',b'%PDF-test')}).json()['id']
        for _ in range(50):
            status = self.client.get('/api/jobs/'+job_id).json()
            if status['status'] == 'running':
                break
            time.sleep(.01)
        self.assertEqual(status['status'],'running',status)
        self.connection_down = True
        self.assertEqual(self.client.get('/api/jobs/'+job_id).json()['status'],'running')
        self.connection_down = False
        self.run_state = 'completed'
        self.answer_mode = 'normal'
        self.artifact_status = 200
        self.artifact_bytes = b'%PDF-test'
        self.assertEqual(self.client.get('/api/jobs/'+job_id).json()['status'],'completed')
        self.assertEqual(sum(r.method=='POST' and r.url.path.endswith('/runs') for r in self.calls),1)

    def test_lost_response_recovers_already_completed_transcript(self):
        self.lost_response = True
        self.client.post('/api/login',json={'username':'ana','password':'pw'})
        job_id = self.client.post('/api/jobs',files={'file':('doc.pdf',b'%PDF-test')}).json()['id']
        for _ in range(50):
            status = self.client.get('/api/jobs/'+job_id).json()
            if status['status'] == 'completed':
                break
            time.sleep(.01)
        self.assertEqual(status['status'],'completed',status)

    def submit_and_finish(self):
        self.client.post('/api/login',json={'username':'ana','password':'pw'})
        job_id = self.client.post('/api/jobs',files={'file':('doc.pdf',b'%PDF-test')}).json()['id']
        for _ in range(100):
            status = self.client.get('/api/jobs/'+job_id).json()
            if status['status'] in ('completed','review_required','failed'):
                return job_id, status
            time.sleep(.01)
        self.fail('Job did not finish')

    def test_provider_fences_and_content_blocks(self):
        for mode in ('fenced','blocks'):
            with self.subTest(mode=mode):
                self.answer_mode=mode
                _,status=self.submit_and_finish()
                self.assertEqual(status['status'],'completed',status)

    def test_differently_named_export_is_accepted_only_for_same_job(self):
        self.answer_mode='alternate'
        job_id,status=self.submit_and_finish()
        self.assertEqual(status['status'],'completed',status)
        self.assertEqual(self.client.get('/api/jobs/'+job_id+'/download').status_code,200)
        with service.db() as conn:
            path=conn.execute('SELECT output_path FROM jobs WHERE id=?',(job_id,)).fetchone()[0]
        self.assertTrue(path.endswith('_redacted_review_required.pdf'))
        self.assertIn(job_id,path)

    def test_unstructured_reply_preserves_export_with_explicit_warning(self):
        self.answer_mode='text'
        with self.assertLogs('uvicorn.error',level='INFO') as logs:
            job_id,status=self.submit_and_finish()
        self.assertEqual(status['status'],'review_required',status)
        self.assertIn('ne potrjuje',status['summary'])
        download=self.client.get('/api/jobs/'+job_id+'/download')
        self.assertEqual(download.content,b'%PDF-test')
        self.assertIn('_nepotrjeno_pregled.pdf',download.headers['content-disposition'])
        self.assertTrue(any('agent_confirmation_missing' in l for l in logs.output))
        self.assertFalse(any('secret-' in l for l in logs.output))

    def test_explicit_agent_failure_is_never_reported_as_confirmed(self):
        self.answer_mode='failed'
        _,status=self.submit_and_finish()
        self.assertEqual(status['status'],'review_required',status)
        self.assertIn('Samodejna anonimizacija ni uspela',status['summary'])

    def test_completed_run_without_transcript_can_offer_only_unconfirmed_export(self):
        self.answer_mode='none'
        _,status=self.submit_and_finish()
        self.assertEqual(status['status'],'review_required',status)

    def test_missing_or_non_pdf_artifact_is_not_offered(self):
        for code,content in ((404,b''),(200,b'<html>proxy error</html>')):
            with self.subTest(status=code):
                self.artifact_status=code;self.artifact_bytes=content
                job_id,status=self.submit_and_finish()
                self.assertEqual(status['status'],'failed',status)
                self.assertEqual(self.client.get('/api/jobs/'+job_id+'/download').status_code,409)

    def test_probe_network_failure_is_retried_without_restarting_agent(self):
        self.artifact_status=503
        self.client.post('/api/login',json={'username':'ana','password':'pw'})
        job_id=self.client.post('/api/jobs',files={'file':('doc.pdf',b'%PDF-test')}).json()['id']
        for _ in range(100):
            status=self.client.get('/api/jobs/'+job_id).json()
            if status['phase']:break
            time.sleep(.01)
        self.assertEqual(status['status'],'running',status)
        self.artifact_status=200
        self.assertEqual(self.client.get('/api/jobs/'+job_id).json()['status'],'completed')
        self.assertEqual(sum(r.method=='POST' and r.url.path.endswith('/runs') for r in self.calls),1)

    def test_old_false_failure_is_rechecked_on_restart_without_new_run(self):
        self.answer_mode='text'
        job_id,_=self.submit_and_finish()
        service.update(job_id,status='failed',summary='Agent ne potrjuje izdelave anonimiziranega dokumenta.')
        service.initialize()
        status=self.client.get('/api/jobs/'+job_id).json()
        self.assertEqual(status['status'],'review_required',status)
        self.assertEqual(sum(r.method=='POST' and r.url.path.endswith('/runs') for r in self.calls),1)

    def test_path_traversal_and_other_job_files_are_rejected(self):
        job={'id':'job-one','output_path':'/exports/anonymizer_job-one.pdf'}
        self.assertIsNone(service.result_path('/exports/../private/job-one.pdf',job))
        self.assertIsNone(service.result_path('/exports/job-two.pdf',job))
        self.assertIsNone(service.result_path('https://evil.invalid/job-one.pdf',job))

    def test_origin_and_upload_validation(self):
        self.assertEqual(self.client.post('/api/login',headers={'Origin':'https://evil.invalid'},json={}).status_code,403)
        self.client.post('/api/login',json={'username':'ana','password':'pw'})
        self.assertEqual(self.client.post('/api/jobs',files={'file':('bad.pdf',b'not pdf')}).status_code,415)
        self.assertEqual(self.client.post('/api/jobs',files={'file':('bad.txt',b'%PDF-test')}).status_code,415)


if __name__ == '__main__':
    unittest.main()
