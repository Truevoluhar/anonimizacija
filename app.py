"""Independent document anonymization UI; talks exclusively to published agent APIs."""
from ArmUtils.environment import load_environment

load_environment("anonymizer_service")

from contextlib import asynccontextmanager
from datetime import datetime
from hashlib import sha256
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit, quote
import asyncio
import json
import logging
import os
import secrets
import sqlite3
import time
import uuid

import httpx
from fastapi import FastAPI, Request, UploadFile, File, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

AGENT = os.getenv('AGENT_API_URL', 'http://localhost:8081').rstrip('/')
WORKSPACE = os.getenv('WORKSPACE_API_URL', 'http://localhost:8090').rstrip('/')
DATA = Path(os.getenv('DATA_DIR', './data'))
DATA.mkdir(parents=True, exist_ok=True)
DB = DATA / 'anonymizer.sqlite3'
COOKIE = 'anonymizer_session'
LIMIT = int(os.getenv('MAX_UPLOAD_MB', '40')) * 1024 * 1024
TASKS = set()
LOGGER = logging.getLogger("uvicorn.error")
SCHEMA = {
    'type': 'object', 'properties': {
        'status': {'type': 'string', 'enum': ['review_required', 'failed']},
        'output_path': {'type': 'string'}, 'summary': {'type': 'string'},
    }, 'required': ['status', 'output_path', 'summary'], 'additionalProperties': False,
}


def db():
    conn = sqlite3.connect(DB, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def initialize():
    with db() as conn:
        conn.executescript('''
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY, guid TEXT NOT NULL, user_id TEXT NOT NULL,
            username TEXT NOT NULL, expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL, filename TEXT NOT NULL,
            status TEXT NOT NULL, created REAL NOT NULL, thread_id TEXT,
            run_id TEXT, output_path TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '');
        UPDATE jobs SET status='recovering', summary='Preverjam stanje že poslanega opravila pri agentu.'
            WHERE thread_id IS NOT NULL AND (status IN ('queued','starting','submitting')
            OR (status='failed' AND summary IN ('Povezava z agentom trenutno ni na voljo. Poskusite znova.',
            'Agent ne potrjuje izdelave anonimiziranega dokumenta.')));
        UPDATE jobs SET status='failed', summary='Nalaganje je bilo prekinjeno pred ustvarjanjem pogovora. Dokument naložite ponovno.'
            WHERE thread_id IS NULL AND status IN ('queued','starting');
        ''')
        conn.execute('DELETE FROM sessions WHERE expires < ?', (time.time(),))
    DB.chmod(0o600)


@asynccontextmanager
async def lifespan(app):
    initialize()
    app.state.http = httpx.AsyncClient(timeout=httpx.Timeout(30, connect=8), follow_redirects=False)
    yield
    for task in list(TASKS):
        task.cancel()
    await asyncio.gather(*list(TASKS), return_exceptions=True)
    await app.state.http.aclose()


app = FastAPI(title='Anonimizacija dokumentov', lifespan=lifespan)
app.mount('/static', StaticFiles(directory=Path(__file__).parent / 'static'), name='static')


@app.middleware('http')
async def security(request, call_next):
    if request.method in {'POST', 'DELETE', 'PUT', 'PATCH'}:
        origin = request.headers.get('origin')
        expected = os.getenv('PUBLIC_ORIGIN')
        actual = urlsplit(origin) if origin else None
        if origin and ((expected and origin.rstrip('/') != expected.rstrip('/')) or
                       (not expected and actual.netloc != request.headers.get('host'))):
            return JSONResponse({'detail': 'Nedovoljen izvor zahteve.'}, status_code=403)
    response = await call_next(request)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    return response


def session(request):
    token = request.cookies.get(COOKIE, '')
    key = sha256(token.encode()).hexdigest()
    with db() as conn:
        row = conn.execute('SELECT * FROM sessions WHERE id=? AND expires>?', (key, time.time())).fetchone()
    if row is None:
        raise HTTPException(401, 'Prijavite se za nadaljevanje.')
    return dict(row)


async def upstream(request, method, base, path, guid=None, **kwargs):
    params = {'guid': guid} if guid else {}
    try:
        response = await request.app.state.http.request(method, base + path, params=params, **kwargs)
    except httpx.RequestError as exc:
        LOGGER.warning('upstream_transport_error service=%s method=%s path=%s error_type=%s',
                       'agent' if base == AGENT else 'workspace', method, path, type(exc).__name__)
        raise HTTPException(503, 'Povezava z agentom trenutno ni na voljo. Poskusite znova.') from None
    if response.status_code >= 400:
        LOGGER.warning('upstream_http_error service=%s method=%s path=%s status=%s',
                       'agent' if base == AGENT else 'workspace', method, path, response.status_code)
        messages = {404: 'Opravilo ali datoteka pri agentu ne obstaja več.', 401: 'Prijava ni veljavna ali je potekla.', 403: 'Dostop ni dovoljen.',
                    409: 'Agent za ta račun že izvaja opravilo. Počakajte na zaključek.',
                    413: 'Dokument presega dovoljeno velikost.'}
        raise HTTPException(response.status_code if response.status_code in messages else 502,
                            messages.get(response.status_code, 'Agent zahteve ni mogel obdelati.'))
    return response


def update(job_id, **fields):
    with db() as conn:
        conn.execute('UPDATE jobs SET ' + ', '.join(k + '=?' for k in fields) + ' WHERE id=?',
                     (*fields.values(), job_id))


def owned_job(job_id, user):
    with db() as conn:
        row = conn.execute('SELECT * FROM jobs WHERE id=? AND user_id=?', (job_id, user['user_id'])).fetchone()
    if row is None:
        raise HTTPException(404, 'Opravilo ne obstaja.')
    return dict(row)


@app.get('/')
async def index():
    return FileResponse(Path(__file__).parent / 'static/index.html')


@app.get('/health')
async def health():
    return {'status': 'ok'}


@app.post('/api/login')
async def login(request: Request):
    body = await request.json()
    guid = body.get('guid', '')
    expires = time.time() + 12 * 3600
    if not guid:
        response = await upstream(request, 'POST', AGENT, '/v1/auth/login', json={
            'username': body.get('username', ''), 'password': body.get('password', '')})
        payload = response.json()
        guid = payload['guid']
        expires = min(expires, datetime.fromisoformat(payload['expires_at'].replace('Z', '+00:00')).timestamp())
    me = (await upstream(request, 'GET', AGENT, '/v1/auth/me', guid)).json()
    token = secrets.token_urlsafe(32)
    with db() as conn:
        conn.execute('INSERT INTO sessions VALUES(?,?,?,?,?)',
                     (sha256(token.encode()).hexdigest(), guid, me['user_id'], me['username'], expires))
    response = JSONResponse({'username': me['username']})
    response.set_cookie(COOKIE, token, httponly=True, secure=os.getenv('COOKIE_SECURE', 'false').lower() == 'true',
                        samesite='strict', max_age=max(0, int(expires-time.time())), path='/')
    return response


@app.get('/api/me')
async def me(request: Request):
    user = session(request)
    await upstream(request, 'GET', AGENT, '/v1/auth/me', user['guid'])
    return {'username': user['username']}


@app.post('/api/logout')
async def logout(request: Request):
    user = session(request)
    # Remove only this webservice session; other agent clients can keep working.
    with db() as conn:
        conn.execute('DELETE FROM sessions WHERE id=?', (user['id'],))
    response = JSONResponse({'ok': True})
    response.delete_cookie(COOKIE, path='/')
    return response


async def start_job(request, user, job_id, filename, content):
    try:
        update(job_id, status='starting')
        upload = (await upstream(request, 'POST', WORKSPACE, '/v1/files', user['guid'],
                  files={'file': (filename, content, 'application/pdf')},
                  data={'destination': 'uploads/anonymizer/' + job_id})).json()
        thread = (await upstream(request, 'POST', AGENT, '/v1/threads', user['guid'])).json()
        job = owned_job(job_id, user)
        update(job_id, thread_id=thread['thread_id'])
        prompt = (
            'Anonimiziraj priloženi PDF po skillu redact-pdf. Uporabi redact_pdf_personal_data '
            'za celoten dokument in shrani rezultat natančno v ' + job['output_path'] + '. '
            'Upoštevaj izjeme za javne uradne in poslovne podatke ter zaščiti zasebne in vozilne identifikatorje. '
            'Ne ponavljaj celotnih pregledov z inspect_pdf. Če orodje odpove ali ne ustvari dokumenta, '
            'vrni status failed in prazen output_path. Sicer vrni review_required, dejansko output_path '
            'in kratko slovensko navodilo za človeški pregled. Ne izpisuj zaznanih osebnih podatkov.'
        )
        update(job_id, status='submitting')
        run = (await upstream(request, 'POST', AGENT, '/v1/threads/' + thread['thread_id'] + '/runs', user['guid'],
               json={'message': prompt, 'attachments': [upload['path']], 'response_schema': SCHEMA})).json()
        update(job_id, status='running', run_id=run['run_id'])
    except HTTPException as exc:
        job = owned_job(job_id, user)
        if job['thread_id'] and exc.status_code in {409, 502, 503, 504}:
            update(job_id, status='recovering', summary='Potrditev agenta ni prispela. Preverjam stanje opravila.')
        else:
            update(job_id, status='failed', summary=str(exc.detail))
    except Exception:
        job = owned_job(job_id, user)
        if job['thread_id']:
            update(job_id, status='recovering', summary='Preverjam, ali je agent že sprejel opravilo.')
        else:
            update(job_id, status='failed', summary='Opravila ni bilo mogoče začeti. Dokument naložite ponovno.')


@app.post('/api/jobs', status_code=202)
async def create_job(request: Request, file: UploadFile = File(...)):
    user = session(request)
    filename = Path((file.filename or 'dokument.pdf').replace('\\', '/')).name
    if not filename.lower().endswith('.pdf'):
        raise HTTPException(415, 'Trenutno so podprti dokumenti PDF.')
    content = await file.read(LIMIT + 1)
    await file.close()
    if len(content) > LIMIT:
        raise HTTPException(413, 'Dokument je prevelik.')
    if not content.startswith(b'%PDF-'):
        raise HTTPException(415, 'Datoteka ni veljaven dokument PDF.')
    with db() as conn:
        active = conn.execute("SELECT id FROM jobs WHERE user_id=? AND status IN ('queued','starting','submitting','recovering','running')", (user['user_id'],)).fetchone()
        if active:
            raise HTTPException(409, 'Za ta račun že poteka anonimizacija.')
        job_id = str(uuid.uuid4())
        conn.execute('INSERT INTO jobs(id,user_id,filename,status,created,output_path) VALUES(?,?,?,?,?,?)',
                     (job_id, user['user_id'], filename, 'queued', time.time(), '/exports/anonymizer_' + job_id + '.pdf'))
    task = asyncio.create_task(start_job(request, user, job_id, filename, content))
    TASKS.add(task)
    task.add_done_callback(TASKS.discard)
    return {'id': job_id, 'status': 'queued'}


@app.get('/api/jobs')
async def list_jobs(request: Request):
    user = session(request)
    with db() as conn:
        rows = conn.execute('SELECT id,filename,status,created,summary FROM jobs WHERE user_id=? ORDER BY created DESC LIMIT 50', (user['user_id'],)).fetchall()
    return [dict(row) for row in rows]


def parse_agent_result(answer):
    """Accept text blocks/fences produced by model providers, without guessing fields."""
    if isinstance(answer, list):
        answer = ''.join(block.get('text', '') for block in answer
                         if isinstance(block, dict) and isinstance(block.get('text'), str))
    for _ in range(2):
        if isinstance(answer, dict):
            return answer
        if not isinstance(answer, str):
            return {}
        text = answer.strip()
        if text.startswith('```') and text.endswith('```'):
            lines = text.splitlines()
            text = '\n'.join(lines[1:-1])
        try:
            answer = json.loads(text)
        except (ValueError, TypeError):
            return {}
    return answer if isinstance(answer, dict) else {}


def result_path(raw, job):
    """Only accept exports belonging to this job, never arbitrary agent file paths."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    value = raw.strip()
    if '\\' in value or '..' in PurePosixPath(value).parts:
        return None
    path = PurePosixPath('/' + value.lstrip('/'))
    normalized = str(path)
    if normalized == job['output_path']:
        return normalized
    if (path.parts[:2] == ('/', 'exports') and path.suffix.lower() == '.pdf'
            and job['id'] in path.name):
        return normalized
    return None


async def artifact_available(request, user, job, path):
    """Probe the scoped export, not a list of possibly unrelated workspace files."""
    url = WORKSPACE + '/v1/files/' + quote(path.lstrip('/'), safe='/')
    req = request.app.state.http.build_request('GET', url, params={'guid': user['guid']},
                                              headers={'Range': 'bytes=0-4'})
    try:
        response = await request.app.state.http.send(req, stream=True)
    except httpx.RequestError as exc:
        LOGGER.warning('artifact_probe_transport_error job=%s error_type=%s', job['id'], type(exc).__name__)
        raise HTTPException(503, 'Preverjanje ustvarjene datoteke trenutno ni mogoče.') from None
    try:
        if response.status_code == 404:
            return False
        if response.status_code in (401, 403):
            raise HTTPException(response.status_code, 'Dostop do rezultata ni dovoljen ali je prijava potekla.')
        if response.status_code not in (200, 206):
            LOGGER.warning('artifact_probe_http_error job=%s status=%s', job['id'], response.status_code)
            raise HTTPException(502, 'Preverjanje ustvarjene datoteke trenutno ni mogoče.')
        header = b''
        async for chunk in response.aiter_bytes():
            header += chunk[:5-len(header)]
            if len(header) >= 5:
                break
        available = header == b'%PDF-'
        LOGGER.info('artifact_probe job=%s pdf_header=%s', job['id'], available)
        return available
    except httpx.RequestError as exc:
        LOGGER.warning('artifact_probe_transport_error job=%s error_type=%s', job['id'], type(exc).__name__)
        raise HTTPException(503, 'Preverjanje ustvarjene datoteke trenutno ni mogoče.') from None
    finally:
        await response.aclose()


async def synchronize_result(request, user, job, completed=False):
    messages = (await upstream(request, 'GET', AGENT,
                '/v1/threads/' + job['thread_id'] + '/messages', user['guid'])).json()
    answer = next((m['content'] for m in reversed(messages) if m['role'] == 'assistant'), None)
    if answer is None and not completed:
        return False
    result = parse_agent_result(answer)
    declared_path = result.get('output_path')
    candidate = result_path(declared_path, job)
    if declared_path and candidate is None:
        LOGGER.warning('result_rejected job=%s reason=unscoped_output_path', job['id'])
        update(job['id'], status='failed', summary='Agent je vrnil pot, ki ne pripada temu opravilu; prenos ni dovoljen.')
        return True
    candidate = candidate or job['output_path']
    confirmed = result.get('status') == 'review_required' and result_path(declared_path, job) is not None
    LOGGER.info('result_received job=%s json_object=%s confirmed=%s path_matches=%s',
                job['id'], bool(result), confirmed, declared_path == job['output_path'])
    if not await artifact_available(request, user, job, candidate):
        LOGGER.warning('result_rejected job=%s reason=missing_or_non_pdf_export', job['id'])
        update(job['id'], status='failed', summary='Agentovo opravilo je zaključeno, vendar izhodni PDF ni dosegljiv na poti tega opravila.')
        return True
    summary = result.get('summary')
    summary = summary if isinstance(summary, str) else ''
    if confirmed:
        update(job['id'], status='completed', output_path=candidate,
               summary=summary or 'Dokument je ustvarjen. Pred uporabo ročno preverite vse strani.')
    else:
        warning = ('PDF je ustvarjen, vendar agentov odgovor ne potrjuje uspešne anonimizacije. '
                   'Prenos je namenjen samo ročnemu pregledu; dokumenta ne obravnavajte kot preverjeno anonimiziranega.')
        update(job['id'], status='review_required', output_path=candidate,
               summary=warning + (' Agentovo pojasnilo: ' + summary if summary else ''))
        LOGGER.warning('result_needs_review job=%s reason=agent_confirmation_missing', job['id'])
    return True


@app.get('/api/jobs/{job_id}')
async def job_status(job_id: str, request: Request):
    user = session(request)
    job = owned_job(job_id, user)
    phase = ''
    try:
        if job['status'] == 'recovering':
            phase = 'Potrditev ni prispela. Preverjam že poslano opravilo; ne začenjam novega.'
            # The dedicated thread identifies this job even when POST /runs lost its response.
            active = (await upstream(request, 'GET', AGENT, '/v1/runs/active', user['guid'])).json()
            if active.get('thread_id') == job['thread_id'] and active.get('run_id'):
                update(job_id, status='running', run_id=active['run_id'], summary='')
                job = owned_job(job_id, user)
            else:
                await synchronize_result(request, user, job)
        if job['status'] == 'running':
            state = (await upstream(request, 'GET', AGENT, '/v1/runs/' + job['run_id'], user['guid'])).json()
            phase = state.get('phase', '')
            if state['status'] == 'completed':
                await synchronize_result(request, user, job, completed=True)
            elif state['status'] in {'failed', 'cancelled'}:
                update(job_id, status='failed', summary=state.get('error') or 'Agent je opravilo prekinil.')
    except HTTPException as exc:
        if exc.status_code in {401, 403}:
            raise
        if exc.status_code == 404:
            # The run may have disappeared after completion/restart; check the durable transcript first.
            try:
                recovered = await synchronize_result(request, user, job)
            except HTTPException:
                recovered = False
            if not recovered:
                update(job_id, status='failed', summary='Agent ne pozna več opravila in nima shranjenega rezultata; opravilo je bilo prekinjeno.')
        else:
            phase = 'Povezava z agentom je počasna ali prekinjena. Stanje preverjam ponovno.'
    job = owned_job(job_id, user)
    return {k: job[k] for k in ('id','filename','status','created','summary')} | {'phase': phase}


@app.get('/api/jobs/{job_id}/download')
async def download(job_id: str, request: Request):
    user = session(request)
    job = owned_job(job_id, user)
    if job['status'] not in ('completed', 'review_required'):
        raise HTTPException(409, 'Rezultat še ni pripravljen.')
    path = '/v1/files/' + quote(job['output_path'].lstrip('/'), safe='/')
    req = request.app.state.http.build_request('GET', WORKSPACE + path, params={'guid': user['guid']})
    try:
        response = await request.app.state.http.send(req, stream=True)
    except httpx.RequestError:
        raise HTTPException(503, 'Prenos trenutno ni mogoč.') from None
    if response.status_code != 200:
        await response.aclose()
        raise HTTPException(502, 'Rezultat pri agentu ni dosegljiv.')
    async def chunks():
        try:
            async for chunk in response.aiter_bytes():
                yield chunk
        finally:
            await response.aclose()
    suffix = '_nepotrjeno_pregled.pdf' if job['status'] == 'review_required' else '_anonimizirano_pregled.pdf'
    name = Path(job['filename']).stem + suffix
    return StreamingResponse(chunks(), media_type='application/pdf', headers={
        'Content-Disposition': "attachment; filename*=UTF-8''" + quote(name)})
