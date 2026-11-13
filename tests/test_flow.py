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
            return httpx.Response(200, json=[{'role':'assistant', 'content':json.dumps({
                'status':'review_required', 'output_path':'/exports/wrong.pdf' if self.bad_result else self.output, 'summary':'Preglejte vse strani.'})}])
        if path.startswith('/v1/files/exports/'):
            return httpx.Response(200, content=b'%PDF-test')
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

    def test_origin_and_upload_validation(self):
        self.assertEqual(self.client.post('/api/login',headers={'Origin':'https://evil.invalid'},json={}).status_code,403)
        self.client.post('/api/login',json={'username':'ana','password':'pw'})
        self.assertEqual(self.client.post('/api/jobs',files={'file':('bad.pdf',b'not pdf')}).status_code,415)
        self.assertEqual(self.client.post('/api/jobs',files={'file':('bad.txt',b'%PDF-test')}).status_code,415)


if __name__ == '__main__':
    unittest.main()
