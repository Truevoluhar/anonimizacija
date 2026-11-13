const $ = s => document.querySelector(s);
let jobs = [], busy = false, authenticated = false;
const active = s => ['queued', 'starting', 'submitting', 'recovering', 'running'].includes(s);
function notice(message='') { $('#notice').textContent=message; $('#notice').hidden=!message; }
async function api(path, options={}) {
  const response = await fetch(path, {...options, signal: AbortSignal.timeout(35000)});
  if(!response.ok) {
    let message='Zahteve ni bilo mogoče izvesti.';
    try {const data=await response.json(); message=typeof data.detail==='string'?data.detail:message;}catch{}
    if(response.status===401) showSession(null);
    throw new Error(message);
  }
  return response.json();
}
function showSession(user) {authenticated=!!user; $('#login-panel').hidden=authenticated; $('#workspace').hidden=!authenticated; $('#account').hidden=!authenticated; $('#username').textContent=user?.username||''; if(!user){jobs=[];$('#jobs').replaceChildren();}}
function render() {
  $('#empty').hidden=jobs.length>0;
  $('#jobs').replaceChildren(...jobs.map(job=>{
    const el=document.createElement('article'); el.className='job '+(active(job.status)?'active':job.status);
    const title=document.createElement('h3'); title.textContent=job.filename;
    const status=document.createElement('div');status.className='status';
    const dot=document.createElement('span');dot.className='dot';status.append(dot);
    const labels={queued:'Pripravljam opravilo …',starting:'Nalaganje dokumenta in priprava agenta …',submitting:'Pošiljam opravilo agentu …',recovering:'Preverjam povezavo in stanje pri agentu …',running:'Agent izvaja anonimizacijo …',completed:'Kopija je pripravljena za pregled',failed:'Opravila ni bilo mogoče dokončati'};
    status.append(document.createTextNode(job.phase||labels[job.status]||'Preverjam stanje …'));
    const meta=document.createElement('div');meta.className='meta';
    const elapsed=Math.max(0,Math.floor(Date.now()/1000-job.created));
    meta.textContent=job.status==='recovering'?`Od oddaje ${Math.floor(elapsed/60)} min ${elapsed%60} s · preverjam dejansko stanje`:active(job.status)?`V teku ${Math.floor(elapsed/60)} min ${elapsed%60} s · stanje se osvežuje`:job.summary;
    el.append(title,status,meta);
    if(job.status==='completed'){const link=document.createElement('a');link.className='download';link.href=`/api/jobs/${encodeURIComponent(job.id)}/download`;link.textContent='Prenesi PDF za pregled ↓';el.append(link);}
    return el;
  }));
  $('#start').disabled=busy||jobs.some(j=>active(j.status));
}
async function refresh() {
  if(!authenticated)return;
  try {
    jobs=await api('/api/jobs');
    render();
    for(const job of jobs.filter(j=>active(j.status))) {
      try {Object.assign(job,await api(`/api/jobs/${encodeURIComponent(job.id)}`));}
      catch(error){if(authenticated)job.phase='Povezava je počasna. Stanje preverjam ponovno.';else return;}
    }
    render();
  } catch(error){if(authenticated)notice(error.message);}
}
for(const formId of ['#login','#token-login']) $(formId).addEventListener('submit',async event=>{
  event.preventDefault(); const button=event.target.querySelector('button');button.disabled=true;notice();
  try {const body=Object.fromEntries(new FormData(event.target)); const user=await api('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});event.target.reset();showSession(user);await refresh();}
  catch(error){notice(error.message);}finally{button.disabled=false;}
});
$('#logout').addEventListener('click',async()=>{try{await api('/api/logout',{method:'POST'});showSession(null);notice();}catch(error){notice(error.message);}});
$('#upload').addEventListener('submit',async event=>{
  event.preventDefault();const file=$('#file').files[0];if(!file)return;
  if(file.size>40*1024*1024){notice('Največja dovoljena velikost je 40 MB.');return;}
  busy=true;render();notice();
  try {await api('/api/jobs',{method:'POST',body:new FormData(event.target)});event.target.reset();await refresh();}
  catch(error){notice(error.message);}finally{busy=false;render();}
});
$('#refresh').addEventListener('click',refresh);
let polling=false;
setInterval(async()=>{if(polling||!authenticated)return;polling=true;try{await refresh();}finally{polling=false;}},2500);
api('/api/me').then(async user=>{showSession(user);await refresh();}).catch(()=>showSession(null));
