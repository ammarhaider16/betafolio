'use strict';
// Keep new screens aligned with docs/ui-guidance.md.
const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
let signupToken='';
let mfaSetupId='',mfaVersion=0,mfaBusy=false,mfaRegenerate=false;
let csrf=$('meta[name="csrf-token"]').content, session={}, workspace=null, adminConfig=null;
let editing=false, draft=[], importFile=null, registering=false, activeRun=null, selectedTrace=null, dirtyPreferences=false;
let detailSaving=false, detailError=false, preferencesSaving=false, savingPortfolio=false, runSubmitting=false, runError='', selectedReport=null, latestReportId=null, draftVersion=0, importVersion=0, workspaceRequest=0;
const lookupTimers=new Map(), lookupTasks=new Set(), rowElements=new WeakMap(), revisions=new WeakMap();
const money=v=>v===null||v===undefined?'Unavailable':new Intl.NumberFormat('en-US',{style:'currency',currency:'USD'}).format(Number(v));
function when(v){return v?new Date(v).toLocaleString('en-US',{timeZone:workspace?.preferences?.timezone||adminConfig?.schedule?.timezone||'America/New_York',year:'numeric',month:'short',day:'numeric',hour:'numeric',minute:'2-digit',timeZoneName:'short'}):'Unavailable';}
let noticeTimer=null, noticeDelay=6000;
function dismissNotice(){clearTimeout(noticeTimer);$('#notice').hidden=true;}
function scheduleNoticeDismiss(){clearTimeout(noticeTimer);const box=$('#notice');if(!box.hidden&&!box.matches(':hover')&&!box.contains(document.activeElement))noticeTimer=setTimeout(dismissNotice,noticeDelay);}
function notice(message,error=false){clearTimeout(noticeTimer);$('#notice').hidden=!message;$('#notice-text').textContent=message;$('#notice').classList.toggle('error',error);noticeDelay=error?10000:6000;if(message)scheduleNoticeDismiss();}
function node(tag,text,className){const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(className)e.className=className;return e;}
async function api(path,method='GET',data){const options={method,credentials:'same-origin',headers:{}};if(method!=='GET')options.headers['x-csrf-token']=csrf;if(data instanceof FormData)options.body=data;else if(data!==undefined){options.headers['content-type']='application/json';options.body=JSON.stringify(data);}const response=await fetch(path,options);const value=await response.json();if(!response.ok){const error=Error(value.detail||'Request could not be completed');error.status=response.status;throw error;}return value;}
function action(fn){return async event=>{event?.preventDefault();const button=event?.submitter||event?.currentTarget;const disable=button?.tagName==='BUTTON';if(disable)button.disabled=true;try{await fn(event);}catch(error){notice(error.message,true);}finally{if(disable)button.disabled=false;}};}
async function openMfa(){
  mfaVersion++;mfaBusy=false;mfaSetupId='';mfaRegenerate=false;
  $('#mfa-start').hidden=!session.mfa_enrolled;$('#mfa-code').hidden=true;$('#recovery').hidden=true;$('#mfa-regenerate').hidden=true;$('#mfa-retry').hidden=true;$('#mfa-existing-code').hidden=!session.mfa_enrolled;
  $('#mfa-start').reset();$('#mfa-confirm').reset();$('#mfa-key').textContent='';$('#mfa-qr').removeAttribute('src');showRoot('mfa');
  $('#mfa-state').textContent=session.mfa_enrolled?'Enter your password and current authenticator or unused recovery code to set up a replacement.':'Preparing your authenticator setup code...';
  if(!session.mfa_enrolled)await requestMfaSetup();
}
async function requestMfaSetup(values={}){
  if(mfaBusy)return;mfaBusy=true;const version=++mfaVersion,owner=session.email;
  const controls=$$('#mfa button');controls.forEach(button=>button.disabled=true);$('#mfa').setAttribute('aria-busy','true');$('#mfa-retry').hidden=true;
  try{
    const value=await api('/api/account/mfa/setup','POST',{...values,regenerate:mfaRegenerate,setup_id:mfaSetupId});
    if(owner!==session.email||version!==mfaVersion||document.body.dataset.screen!=='mfa')return;
    $('#mfa-start').reset();$('#mfa-start').hidden=true;$('#mfa-qr').src=value.qr;$('#mfa-key').textContent=value.manual_key;mfaSetupId=value.setup_id;$('#mfa-confirm').reset();$('#mfa-code').hidden=false;$('#mfa-regenerate').hidden=false;
    $('#mfa-state').textContent=mfaRegenerate?'A new setup code was generated. Remove the previous Betafolio entry from your authenticator and scan this QR code.':'Scan this QR code, then enter the six-digit code from your authenticator. Refreshing this page keeps the same setup code.';mfaRegenerate=false;
  }catch(error){
    if(owner!==session.email||version!==mfaVersion)return;
    mfaRegenerate=false;$('#mfa-state').textContent=error.message;$('#mfa-retry').hidden=!!session.mfa_enrolled;
    if(session.mfa_enrolled)$('#mfa-start').hidden=false;
  }finally{if(version===mfaVersion){mfaBusy=false;controls.forEach(button=>button.disabled=false);$('#mfa').removeAttribute('aria-busy');}}
}

function showRoot(id){for(const name of ['auth','create-account','reset','mfa','workspace','admin'])$('#'+name).hidden=name!==id;document.body.dataset.screen=id;}
function tab(name){if(name==='history')name='portfolio';$$('.tab').forEach(e=>e.hidden=e.id!==name);$$('[data-tab]').forEach(e=>{const active=e.dataset.tab===name;e.classList.toggle('active',active);if(active)e.setAttribute('aria-current','page');else e.removeAttribute('aria-current');});}
function adminTab(name){$$('.admin-tab').forEach(e=>e.hidden=e.id!==name);$$('[data-admin-tab]').forEach(e=>{const active=e.dataset.adminTab===name;e.classList.toggle('active',active);if(active)e.setAttribute('aria-current','page');else e.removeAttribute('aria-current');});}
function clearPrivate(){
  signupToken='';mfaSetupId='';mfaVersion++;mfaBusy=false;mfaRegenerate=false;cancelLookups();workspaceRequest++;workspace=null;adminConfig=null;importFile=null;secretStatus={};adminUsers=[];editing=false;dirtyPreferences=false;draft=[];activeRun=null;selectedTrace=null;selectedReport=null;latestReportId=null;runError='';detailError=false;detailSaving=false;preferencesSaving=false;savingPortfolio=false;runSubmitting=false;
  $('#report-frame').removeAttribute('src');delete $('#report-frame').dataset.reportSrc;$('#latest-report').open=false;$('#latest-report').hidden=true;
  for(const id of ['holdings','history-list','trace','users-list','signup-requests','research-secrets','email-secrets','recovery-codes','mfa-key','allocation-rows'])$('#'+id).replaceChildren();
  for(const id of ['holdings-value','cash-value','saved-cash','allocation-total','value-coverage','report-date','run-state','detail-state','verification-state','schedule-description'])$('#'+id).textContent='';
  $('#allocation-chart').style.background='';$('#run-progress').hidden=true;$('#detail-state').hidden=true;$('#detail').disabled=false;$$('#portfolio-editor input,#portfolio-editor button').forEach(el=>el.disabled=false);
  delete $('#history-list').dataset.signature;delete $('#trace').dataset.signature;$('#mfa-qr').removeAttribute('src');for(const id of ['question','research-date'])$('#'+id).value='';$$('input[type="password"]').forEach(input=>input.value='');notice('');tab('portfolio');adminTab('admin-connections');
}
async function loadSession(){const previous=session.email;session=await api('/api/session');if(previous&&previous!==session.email)clearPrivate();csrf=session.csrf;$('#optional-mfa').textContent=session.mfa_enrolled?'Replace authenticator':'Set up an authenticator';$('#logout').hidden=!session.signed_in;$('#identity').textContent=session.email||'';if(!session.signed_in){setAuthMode(false);showRoot('auth');$('#auth-switch').hidden=!session.signup_available;return;}if(session.admin_setup_required){await openMfa();return;}if(session.role==='admin'){showRoot('admin');await loadAdmin();}else{showRoot('workspace');await loadWorkspace(true);}}
function today(zone){const parts=new Intl.DateTimeFormat('en-CA',{timeZone:zone,year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(new Date());const get=k=>parts.find(p=>p.type===k).value;return `${get('year')}-${get('month')}-${get('day')}`;}
function cancelLookups(){draftVersion++;importVersion++;lookupTasks.clear();for(const timer of lookupTimers.values())clearTimeout(timer);lookupTimers.clear();}
function validSymbol(value){return /^[A-Z0-9.\-^=/]{1,40}$/.test(String(value||'').trim().toUpperCase());}
function instrumentInfo(p){const type={stock:'Stock',etf:'ETF',fund:'Fund',mutual_fund:'Mutual fund',bond:'Bond',cash:'Cash',other:'Other'}[p.asset_type];return [p.instrument_name,p.exchange,type,p.sector].filter(Boolean).join(' | ');}
function priceTitle(p){return p.price_as_of?`Price observed ${when(p.price_as_of)}`:p.price_session?`Closing price for ${p.price_session}`:'No quote available yet';}
function updateDraftRow(p){const row=rowElements.get(p);if(!row?.isConnected)return;row.querySelector('.instrument-info').textContent=instrumentInfo(p);const price=row.querySelector('.holding-price');price.textContent=money(p.price);price.title=priceTitle(p);row.querySelector('.holding-value').textContent=p.price==null?'Unavailable':money(Number(p.price)*Number(p.quantity));}
function scheduleLookup(p){clearTimeout(lookupTimers.get(p));lookupTimers.set(p,setTimeout(()=>{lookupTimers.delete(p);enrichDraft([p]);},650));}
function enrichDraft(positions){
  if(!workspace?.verified)return Promise.resolve();
  const version=draftVersion,owner=session.email;
  const candidates=positions.filter(p=>validSymbol(p.symbol)).map(p=>({p,symbol:p.symbol.trim().toUpperCase(),revision:revisions.get(p)||0}));
  const task=(async()=>{
    for(let offset=0;offset<candidates.length;offset+=30){
      if(version!==draftVersion||owner!==session.email)return;
      const batch=candidates.slice(offset,offset+30).filter(({p,revision})=>draft.includes(p)&&(revisions.get(p)||0)===revision);if(!batch.length)continue;
      try{
        const result=await api('/api/portfolio/enrich','POST',{positions:batch.map(({symbol})=>({symbol,quantity:null}))});
        if(version!==draftVersion||owner!==session.email)return;
        for(const [index,{p,symbol,revision}] of batch.entries()){
          if(!draft.includes(p)||(revisions.get(p)||0)!==revision||p.symbol.trim().toUpperCase()!==symbol)continue;
          const item=result.positions[index];if(item?.symbol!==symbol)continue;
          for(const key of ['instrument_name','exchange','asset_type','sector','price','previous_close','price_as_of','price_session','price_source'])p[key]=item[key]??null;
          updateDraftRow(p);
        }
      }catch(error){if(version!==draftVersion||owner!==session.email)return;$('#draft-state').textContent='Some instrument details are unavailable. You can still review quantities and cash, then save.';if(error.status===429||error.status===403)return;}
    }
  })();lookupTasks.add(task);task.finally(()=>lookupTasks.delete(task));return task;
}
function updateRunControls(){
  $('#run-button').disabled=editing||!workspace?.portfolio||!workspace?.service_available||!workspace?.verified||!!activeRun||detailSaving||detailError||runSubmitting||savingPortfolio||preferencesSaving;
  $('#run-button-label').textContent=runSubmitting?'Starting research...':activeRun?'Research in progress...':'Run research';
  const blocker=editing?'Save or discard holdings and cash before starting research.':detailError?'Save the briefing detail choice before starting research.':!workspace?.verified?'Verify your email before starting research.':!workspace?.portfolio?'Add and save your holdings and cash to begin.':'';
  $('#research-blocker').hidden=!blocker;$('#research-blocker').textContent=blocker;
  $('#run-progress').hidden=!activeRun&&!runSubmitting&&!runError;$('#run-state').textContent=runError||(runSubmitting?'Starting research...':activeRun?'Research in progress.':'');$('#run-trace').hidden=!activeRun;
}
function renderHoldings(){
  const root=$('#holdings');root.replaceChildren();const positions=editing?draft:(workspace?.portfolio?.positions||[]);$('#portfolio-editor').classList.toggle('is-editing',editing);
  positions.forEach((p,index)=>{
    const row=node('tr');rowElements.set(p,row);
    for(const [field,label] of [['symbol','Ticker'],['quantity','Quantity']]){
      const cell=node('td');cell.dataset.label=label;
      if(editing){const input=node('input');input.value=p[field]??'';input.setAttribute('aria-label',label);input.required=true;if(field==='quantity')input.inputMode='decimal';
        input.addEventListener('input',()=>{p[field]=input.value;if(field==='symbol'){revisions.set(p,(revisions.get(p)||0)+1);for(const key of ['price','previous_close','instrument_name','exchange','sector','price_as_of','price_session','price_source','asset_type'])p[key]=null;scheduleLookup(p);}updateDraftRow(p);});cell.append(input);
      }else cell.append(node('span',p[field]));
      if(field==='symbol')cell.append(node('small',instrumentInfo(p),'instrument-info'));row.append(cell);
    }
    const price=node('td',money(p.price),'holding-price');price.dataset.label='Price';price.title=priceTitle(p);const value=node('td',p.price==null?'Unavailable':money(Number(p.price)*Number(p.quantity)),'holding-value');value.dataset.label='Value';row.append(price,value);
    if(editing){const last=node('td',undefined,'holding-delete'),remove=node('button',undefined,'delete');remove.type='button';remove.setAttribute('aria-label',`Delete ${p.symbol||'holding'}`);const image=node('img');image.src='/static/icons/trash.svg';image.alt='';remove.append(image);remove.addEventListener('click',()=>{clearTimeout(lookupTimers.get(p));lookupTimers.delete(p);draft.splice(index,1);renderHoldings();});last.append(remove);row.append(last);}root.append(row);
  });
  if(!positions.length){const row=node('tr'),cell=node('td','No holdings yet. Add a holding or import a file.','helper');cell.colSpan=editing?5:4;row.append(cell);root.append(row);}
  $('.holdings-table th.holding-delete').hidden=!editing;$('#editor-actions').hidden=!editing;$('#edit-holdings').hidden=editing;$('#cash-edit').hidden=!editing;$('#saved-cash').hidden=editing;$('#saved-cash').textContent=money(workspace?.portfolio?.cash||0);updateRunControls();
}
function beginEditing(positions,cash){cancelLookups();editing=true;draft=structuredClone(positions||[]);$('#cash').value=cash??'0';$('#draft-state').textContent='Review holdings and cash, then save or discard.';renderHoldings();enrichDraft(draft.filter(p=>!p.instrument_name||p.price==null));}
function renderAllocation(){
  const portfolio=workspace.portfolio,positions=portfolio?.positions||[],cash=Number(portfolio?.cash||0);
  const priced=positions.filter(p=>p.price!=null&&Number.isFinite(Number(p.price)*Number(p.quantity))),missing=positions.filter(p=>!priced.includes(p));
  const parts=priced.map(p=>({symbol:p.symbol,value:Number(p.price)*Number(p.quantity)})).filter(p=>p.value>0);if(cash>0)parts.push({symbol:'Cash',value:cash});parts.sort((a,b)=>b.value-a.value);
  const total=parts.reduce((sum,p)=>sum+p.value,0);$('#holdings-value').textContent=money(total);$('#cash-value').textContent=money(cash);
  $('#value-coverage').textContent=portfolio?'':'Add holdings and cash below to begin';
  const rows=$('#allocation-rows');rows.replaceChildren();const colors=['#2563EB','#0E7490','#7C3AED','#475569','#4338CA','#BE185D','#64748B','#94A3B8'];let angle=0;const stops=[];
  const groups=parts.slice(0,7);if(parts.length>7)groups.push({symbol:`${parts.length-7} other positions`,value:parts.slice(7).reduce((sum,p)=>sum+p.value,0)});
  for(const [index,part] of groups.entries()){
    const share=part.value/total,next=angle+share*360,color=colors[index];stops.push(`${color} ${angle}deg ${next}deg`);angle=next;
    const row=node('tr'),name=node('th'),swatch=node('span',undefined,'allocation-swatch');name.scope='row';swatch.style.background=color;name.append(swatch,document.createTextNode(part.symbol));row.append(name,node('td',money(part.value)),node('td',`${(share*100).toFixed(1)}%`));rows.append(row);
  }
  if(!groups.length){const row=node('tr'),cell=node('td','No priced allocation yet.');cell.colSpan=3;row.append(cell);rows.append(row);}
  $('#allocation-chart').style.background=stops.length?`conic-gradient(${stops.join(',')})`:'';$('#allocation-chart').setAttribute('aria-label',total?groups.map(p=>`${p.symbol}: ${(p.value/total*100).toFixed(1)}%`).join(' | '):'No priced allocation yet');
  $('#allocation-total').textContent=total?new Intl.NumberFormat('en-US',{style:'currency',currency:'USD',notation:'compact',maximumFractionDigits:1}).format(total):money(0);$('#allocation-total').title=money(total);
  $('#allocation-note').textContent=missing.length?`${missing.map(p=>p.symbol).join(', ')} excluded until prices are available.`:'';$('#allocation-note').hidden=!missing.length;
}
function selectReport(run,expand=false){
  if(!run)return;const changed=selectedReport!==run.id;selectedReport=run.id;$('#latest-report').hidden=false;const frame=$('#report-frame');frame.dataset.reportSrc='/reports/'+encodeURIComponent(run.id);$('#report-open').href=frame.dataset.reportSrc;$('#report-date').textContent=when(run.research_as_of||run.created_at);$('#report-title').textContent=run.id===workspace.history.find(r=>['success','partial'].includes(r.status))?.id?'Latest briefing':'Briefing';
  if(changed){frame.removeAttribute('src');frame.style.height='';}if(expand)$('#latest-report').open=true;if($('#latest-report').open&&frame.getAttribute('src')!==frame.dataset.reportSrc)frame.src=frame.dataset.reportSrc;
}
function sizeReport(){const frame=$('#report-frame');if(!$('#latest-report').open)return;try{const doc=frame.contentDocument;if(doc?.body)frame.style.height=Math.max(450,doc.body.scrollHeight,doc.documentElement.scrollHeight)+'px';}catch{/* Keep minimum height. */}}
async function loadWorkspace(initial=false){
  const owner=session.email,request=++workspaceRequest,value=await api('/api/workspace');if(owner!==session.email||session.role!=='user'||request!==workspaceRequest)return;workspace=value;renderAllocation();
  $('#service-state').textContent=workspace.service_available?'':'Research is temporarily unavailable. Contact your administrator to check the service connections.';$('#service-state').hidden=workspace.service_available;$('#verification-state').textContent=workspace.verified?'Email verified.':'Verify your email before running research. Check your inbox or resend the verification email.';$('#resend-account').hidden=workspace.verified;
  const prefs=workspace.preferences;if(initial&&!dirtyPreferences){$('#timezone').value=prefs.timezone;$('#schedule-opt').checked=prefs.schedule_opt_in;$('#email-opt').checked=prefs.email_opt_in;}
  if(!detailSaving&&!detailError)$('#detail').value=prefs.report_length;
  const day=today(prefs.timezone);$('#research-date').max=day;if(!$('#research-date').value)$('#research-date').value=day;$('#run-email').hidden=!workspace.verified||!prefs.email_opt_in;if($('#run-email').hidden)$('#send-email').checked=false;
  const schedule=workspace.schedule;$('#schedule-description').textContent=schedule.enabled?`Daily briefing at ${schedule.at.slice(0,5)} (${schedule.timezone}) | Includes weekends`:'Scheduled research is not enabled by your administrator. You can opt in for when it becomes available.';
  const wasActive=activeRun;activeRun=workspace.history.find(r=>['queued','running'].includes(r.status))?.id||null;if(activeRun)runError='';
  if(!editing)renderHoldings();else updateRunControls();renderHistory();if(!editing&&initial&&!workspace.portfolio)beginEditing([],0);
  if(selectedTrace&&(selectedTrace===activeRun||selectedTrace===wasActive))await renderTrace(selectedTrace);
  if(wasActive&&!activeRun){const completed=workspace.history.find(r=>r.id===wasActive);if(completed&&['success','partial'].includes(completed.status)){runError='';$('#latest-report').open=false;selectReport(completed);}else if(completed?.status==='failed')runError=runProblem(completed.error_code)||'Research did not finish. Review the agent trace in Activity.';}updateRunControls();
}
function runProblem(code){return {service_unavailable:'Research could not start because the service needs administrator attention.',research_failed:'Research could not finish. Please try again or contact your administrator.',worker_interrupted:'Research stopped before it could finish. You can start a new run.',schedule_expired:'This scheduled run was skipped because its day had passed.',cancelled_before_start:'Research was cancelled before it started.'}[code]||'';}
function openReport(id){const run=workspace.history.find(r=>r.id===id);if(!run)return;selectReport(run,true);tab('portfolio');$('#latest-report').scrollIntoView({behavior:'smooth',block:'start'});}
function renderHistory(){
  const root=$('#history-list'),picker=$('#activity-run'),signature=JSON.stringify([workspace.preferences.timezone,workspace.history]);
  if(root.dataset.signature!==signature){
    const open=new Set([...root.querySelectorAll('details[open]')].map(e=>e.dataset.disclosureKey));root.dataset.signature=signature;root.replaceChildren();picker.replaceChildren();
    for(const run of workspace.history){
      const row=node('article',undefined,'history-row'),info=node('div',undefined,'history-info');const date=run.research_as_of||run.created_at;
      info.append(node('strong',when(date)),node('span',run.scheduled?'Scheduled briefing':'On-demand briefing','helper'));row.append(info);
      const actions=node('div',undefined,'history-controls'),labels={success:'Ready',partial:'Partial',failed:'Failed',cancelled:'Cancelled',queued:'Queued',running:'In progress'};
      actions.append(statusBadge(labels[run.status]||run.status,run.status==='success'?'ok':run.status==='failed'?'error':'idle'));
      if(['success','partial'].includes(run.status)){const report=node('button','Read briefing','text-button');report.addEventListener('click',()=>openReport(run.id));actions.append(report);}
      const trace=node('button','Agent trace','text-button');trace.addEventListener('click',action(async()=>{tab('activity');picker.value=run.id;await renderTrace(run.id);}));actions.append(trace);
      if(['queued','running'].includes(run.status)){const cancel=node('button','Cancel','secondary');cancel.addEventListener('click',action(async()=>{await api(`/api/runs/${run.id}/cancel`,'POST',{});await loadWorkspace();}));actions.append(cancel);}row.append(actions);
      const facts=[['Executed',when(run.created_at)]];if(run.research_as_of)facts.push(['Research date',when(run.research_as_of)]);if(run.delivery)facts.push(['Email delivery',run.delivery]);
      const more=uiDisclosure('Run details',factList(facts),run.id);more.open=open.has(run.id);row.append(more);if(runProblem(run.error_code))row.append(node('p',runProblem(run.error_code),'history-error'));root.append(row);
      const option=node('option',`${when(run.created_at)} | ${labels[run.status]||run.status}`);option.value=run.id;picker.append(option);
    }
    if(!workspace.history.length){root.append(node('p','Your briefings will appear here.'));picker.append(node('option','No research runs yet'));}
  }
  if(selectedTrace)picker.value=selectedTrace;
  const latest=workspace.history.find(r=>['success','partial'].includes(r.status));if(latest&&(!selectedReport||(latest.id!==latestReportId&&selectedReport===latestReportId))){if(latestReportId)$('#latest-report').open=false;selectReport(latest);}latestReportId=latest?.id||null;
}
async function renderTrace(id){
  selectedTrace=id;const owner=session.email,run=await api('/api/runs/'+encodeURIComponent(id));if(owner!==session.email||selectedTrace!==id)return;
  const root=$('#trace'),signature=JSON.stringify([id,run.status,run.error_code,run.activity]);$('#activity-run').value=id;$('#trace-report').hidden=!['success','partial'].includes(run.status);
  if(root.dataset.signature===signature)return;
  root.dataset.signature=signature;root.replaceChildren();
  const titles={run_setup:'Prepare research inputs',model_setup:'Connect research model',research:'Research portfolio',search_news:'Search news',read_article:'Read article',get_market_data:'Check market data',analyst:'Prepare briefing',briefing_cleanup:'Refine briefing'};
  const actions=[],pending=new Map();
  for(const [index,event] of run.activity.entries()){
    const key=JSON.stringify([event.tool,event.symbol||'',event.query||'',event.source||'']);
    if(event.status==='started'){const entry={start:event,end:null,key:String(index)};actions.push(entry);const queue=pending.get(key)||[];queue.push(entry);pending.set(key,queue);}
    else{const entry=pending.get(key)?.shift();if(entry)entry.end=event;else actions.push({start:null,end:event,key:String(index)});}
  }
  for(const item of actions){
    const event=item.end||item.start,row=node('article',undefined,'trace-action');
    row.append(node('span',titles[event.tool]||'Research action'));root.append(row);
  }
  if(!actions.length)root.append(node('p',run.status==='queued'?'Waiting for a research slot.':'No agent activity recorded.'));
}
function preferenceData(){return {...workspace.preferences,timezone:$('#timezone').value,schedule_opt_in:$('#schedule-opt').checked,email_opt_in:$('#email-opt').checked,profile:workspace.profile};}
async function saveDetail(){
  if(detailSaving||preferencesSaving)return;detailSaving=true;detailError=false;$('#detail').disabled=true;$('#detail-state').hidden=false;$('#detail-state').textContent='Saving briefing detail...';updateRunControls();
  const owner=session.email;
  try{const value=$('#detail').value;await api('/api/account/preferences','PUT',{...workspace.preferences,report_length:value,profile:workspace.profile});if(owner!==session.email||!workspace)return;workspaceRequest++;workspace.preferences.report_length=value;$('#detail-state').textContent='Briefing detail saved.';}
  catch(error){if(owner!==session.email)return;detailError=true;$('#detail-state').textContent=`Could not save briefing detail: ${error.message}`;const retry=node('button','Retry','text-button');retry.type='button';retry.addEventListener('click',action(saveDetail));$('#detail-state').append(' ',retry);}
  finally{if(owner===session.email){detailSaving=false;$('#detail').disabled=false;updateRunControls();}}
}
async function importDraft(mapping){if(!importFile||savingPortfolio)return;const owner=session.email,version=++importVersion;if(importFile.size>5*1024*1024||!/\.(csv|xlsx|xls)$/i.test(importFile.name))throw Error('Choose a CSV, XLSX or XLS file up to 5 MB.');const form=new FormData();form.append('file',importFile);if(mapping)form.append('mapping',JSON.stringify(mapping));const value=await api('/api/import/file','POST',form);if(owner!==session.email||version!==importVersion)return;if(value.needs_mapping){$('#mapping').hidden=false;for(const [id,key] of [['symbol-column','symbol'],['quantity-column','quantity']]){const select=$('#'+id);select.replaceChildren();value.columns.forEach((name,index)=>{const option=node('option',name||`Column ${index+1}`);option.value=index;select.append(option);});if(value.suggestions[key]!==undefined)select.value=value.suggestions[key];}notice('Choose which columns contain tickers and quantities.');return;}$('#mapping').hidden=true;beginEditing(value.draft.positions,value.draft.cash);notice('Import ready for review. Save holdings & cash to apply it.');}
async function refreshCredentialStatus(){const owner=session.email,value=await api('/api/admin/settings');if(owner!==session.email||session.role!=='admin')return;secretStatus=value.secrets;renderSecrets(secretStatus);renderChecks(value.readiness);}
async function loadAdmin(){const owner=session.email,value=await api('/api/admin/settings');if(owner!==session.email||session.role!=='admin')return;adminConfig=value.settings;const r=adminConfig.research,e=adminConfig.email,s=adminConfig.schedule;const values={'model-provider':r.model.provider,'model-name':r.model.model,'model-url':r.model.base_url,'search-provider':r.search_provider,'sec-contact':r.sec_contact,'temperature':r.model.temperature,'max-tokens':r.model.max_tokens,'model-timeout':r.model.request_timeout_seconds,'price-interval':r.price_refresh_seconds,'run-timeout':r.max_seconds,'benchmark':r.benchmark,'news-window':r.max_data_age_hours,'email-transport':e.transport,'email-sender':e.sender,'email-name':e.sender_name,'smtp-host':e.smtp_host,'smtp-port':e.smtp_port,'smtp-user':e.smtp_username,'smtp-tls':e.smtp_tls,'daily-time':s.at.slice(0,5),'daily-zone':s.timezone,'daily-allowance':adminConfig.daily_allowance,'concurrency':adminConfig.concurrency,'retention':r.retention_days,'input-cost':adminConfig.input_cost_per_million??'','output-cost':adminConfig.output_cost_per_million??''};for(const [id,v] of Object.entries(values))$('#'+id).value=v;for(const [id,v] of Object.entries({'fed-enabled':r.primary_feeds_enabled,'ticker-enabled':r.ticker_news_enabled,'sec-enabled':r.sec_company_filings_enabled,'daily-enabled':s.enabled,'signup-enabled':adminConfig.signup_enabled}))$('#'+id).checked=v;$$('[name="feed"]').forEach(el=>el.checked=r.news_feed_sources.includes(el.value));$('#smtp-fields').hidden=e.transport!=='smtp';secretStatus=value.secrets;renderSecrets(secretStatus);renderChecks(value.readiness);const mail=value.account_mail||{};$('#worker-state').replaceChildren(factList([['Worker last seen',value.worker.at?when(value.worker.at):'Not checked in'],['Queued account emails',mail.queued||0],['Failed account emails',mail.failed||0]]));await loadUsers();await loadSignupRequests();}
function renderSecrets(status){
  $('#research-secrets').replaceChildren();$('#email-secrets').replaceChildren();const model=$('#model-provider').value,search=$('#search-provider').value,transport=$('#email-transport').value;
  const names={[`model_api_key_${model}`]:`${{openai:'OpenAI',anthropic:'Anthropic',custom:'Custom model'}[model]} API key`};if(['tavily','exa','perplexity'].includes(search))names[search+'_api_key']={tavily:'Tavily API key',exa:'Exa API key',perplexity:'Perplexity API key'}[search];names[transport==='smtp'?'smtp_password':'email_api_key']=transport==='smtp'?'SMTP password':'Resend API key';
  for(const [name,label] of Object.entries(names)){
    const saved=status[name]?.present,row=node('div',undefined,'secret-row'),info=node('div',undefined,'secret-info');info.append(node('strong',label),node('p',saved?`Key saved${status[name].saved_at?' | '+when(status[name].saved_at):''}`:'No key saved',saved?'helper saved-status':'helper'));row.append(info);
    const field=node('label',saved?'Replacement key':'API key'),input=node('input');input.type='password';input.autocomplete='new-password';field.append(input);field.hidden=saved;row.append(field);const actions=node('div',undefined,'actions'),save=node('button',saved?'Save replacement':'Save key','secondary');save.hidden=saved;
    save.addEventListener('click',action(async()=>{if(!input.value.trim())throw Error('Enter a key to save.');await api('/api/admin/secrets/'+name,'PUT',{value:input.value.trim()});input.value='';await refreshCredentialStatus();notice('Key saved. Save any connection changes before checking the saved connections.');}));actions.append(save);
    if(saved){const change=node('button','Change key','text-button'),cancel=node('button','Cancel','text-button');cancel.hidden=true;change.addEventListener('click',()=>{field.hidden=false;save.hidden=false;cancel.hidden=false;change.hidden=true;input.focus();});cancel.addEventListener('click',()=>{input.value='';field.hidden=true;save.hidden=true;cancel.hidden=true;change.hidden=false;});const remove=node('button','Remove key','text-button');remove.addEventListener('click',action(async()=>{if(!confirm('Remove this saved key?'))return;await api('/api/admin/secrets/'+name,'DELETE');await refreshCredentialStatus();}));actions.append(change,cancel,remove);}
    row.append(actions);(name==='email_api_key'||name==='smtp_password'?$('#email-secrets'):$('#research-secrets')).append(row);
  }
}
function statusMark(state,label){
  const mark=node('span',undefined,`status-mark status-${state}`),image=node('img');image.src='/static/icons/status-'+({ok:'check',error:'error',idle:'clock'}[state]||'clock')+'.svg';image.alt='';image.setAttribute('aria-hidden','true');mark.title=label;mark.append(image,node('span',label,'sr-only'));return mark;
}
function statusBadge(label,state='idle'){const badge=node('span',undefined,`status-badge status-${state}`),mark=statusMark(state,label);mark.setAttribute('aria-hidden','true');badge.append(mark,node('span',label));return badge;}
function factList(items,className='fact-list'){const list=node('dl',undefined,className);for(const [label,value] of items){const pair=node('div');pair.append(node('dt',label),node('dd',String(value??'Unavailable')));list.append(pair);}return list;}
function uiDisclosure(title,content,key){const box=node('details',undefined,'ui-details');if(key)box.dataset.disclosureKey=key;box.append(node('summary',title),content);return box;}
function renderChecks(checks){
  const root=$('#checks'),open=new Set([...root.querySelectorAll('details[open]')].map(e=>e.dataset.disclosureKey));root.replaceChildren();
  const sourceLabels={fed:'Federal Reserve',search:{keyless:'DDGS web search',tavily:'Tavily',exa:'Exa',perplexity:'Perplexity'}[adminConfig?.research?.search_provider]||'Web search',google_news:'Google News',nasdaq:'Nasdaq',bing_news:'Bing News',sec_filings:'SEC filings'};
  for(const [kind,label] of [['model','Model'],['search','Research sources'],['email','Email']]){
    const check=checks[kind],state=check?(check.ok?'ok':'error'):'idle',card=node('section',undefined,'connection-check'),heading=node('div',undefined,'check-heading');heading.append(node('h3',label),statusMark(state,check?(check.ok?'Ready':'Needs attention'):'Not checked'));card.append(heading);
    if(!check){card.append(node('p','Not checked yet.','helper'));root.append(card);continue;}
    if(!check.ok&&check.message)card.append(node('p',check.message,'check-message'));
    const items=node('ul',undefined,'check-items');
    for(const step of check.steps||[]){const state=step.status==='passed'?'ok':step.status==='failed'?'error':'idle',item=node('li');item.append(statusMark(state,step.status==='passed'?'Ready':step.status==='failed'?'Needs attention':'Not checked'),node('span',step.label));if(step.message)item.title=step.message;items.append(item);}
    for(const [name,outcome] of Object.entries(check.sources||{})){const ready=['passed','empty'].includes(outcome.status),item=node('li');item.append(statusMark(ready?'ok':outcome.status==='failed'?'error':'idle',ready?'Connected':outcome.status==='failed'?'Needs attention':'Not checked'),node('span',sourceLabels[name]||name.replaceAll('_',' ')));if(outcome.reason)item.title=outcome.reason;items.append(item);}
    if(items.children.length)card.append(items);
    if(kind==='email'&&check.ok)card.append(node('p','Email connection ready.','helper'));
    const facts=[];if(check.tested_at)facts.push(['Last checked',when(check.tested_at)]);if(check.message)facts.push(['Summary',check.message]);
    for(const step of check.steps||[])if(step.message)facts.push([step.label,step.message]);
    for(const [name,outcome] of Object.entries(check.sources||{}))if(outcome.reason)facts.push([sourceLabels[name]||name.replaceAll('_',' '),outcome.reason]);
    if(facts.length){const more=uiDisclosure('Check details',factList(facts),kind);more.open=open.has(kind);card.append(more);}if(kind!=='model'){const retry=node('button','Retest','text-button');retry.type='button';retry.setAttribute('aria-label',`Retest ${label.toLowerCase()}`);retry.addEventListener('click',action(()=>checkConnections([kind])));card.append(retry);}root.append(card);
  }
}
let adminUsers=[], secretStatus={};
async function loadUsers(){const owner=session.email,value=await api('/api/admin/users');if(owner!==session.email||session.role!=='admin')return;adminUsers=value.users;renderUsers();}
async function loadSignupRequests(){
  const owner=session.email,value=await api('/api/admin/signup-requests');if(owner!==session.email||session.role!=='admin')return;
  const root=$('#signup-requests');root.replaceChildren();
  if(!value.requests.length){root.append(node('p','No access requests.','helper'));return;}
  const table=node('table',undefined,'access-requests-table'),caption=node('caption','Access requests and administrator decisions','sr-only'),head=node('thead'),headRow=node('tr'),body=node('tbody');
  for(const label of ['Email','Requested','Decision','Invitation email','Actions']){const cell=node('th',label);cell.scope='col';headRow.append(cell);}head.append(headRow);table.append(caption,head,body);
  for(const request of value.requests){
    const row=node('tr'),email=node('td',request.email,'request-email'),date=node('td',when(request.created_at),'request-date'),decision=node('td');
    decision.append(statusBadge({pending:'Pending',approved:'Approved',denied:'Denied'}[request.status]||request.status));
    const delivery=request.mail_status==='queued'?'Queued':request.mail_status==='running'?'Sending':request.delivery==='sent'?'Sent':request.delivery==='uncertain'?'Delivery unconfirmed':request.mail_status==='failed'?'Not sent':'Not sent yet';
    const mail=node('td',request.status==='denied'?'No email sent':delivery);if(request.expires)mail.title='Link expires '+when(request.expires);
    const actionCell=node('td'),actions=node('div',undefined,'request-actions');
    if(request.status==='pending'||request.status==='approved'){
      const approve=node('button',request.status==='approved'?'Send new link':'Approve','secondary');approve.type='button';approve.disabled=['queued','running'].includes(request.mail_status);approve.setAttribute('aria-label',`${request.status==='approved'?'Send a new account-creation link to':'Approve access for'} ${request.email}`);
      approve.addEventListener('click',action(async()=>{const result=await api('/api/admin/signup-requests/'+encodeURIComponent(request.id)+'/approve','POST',{});await loadSignupRequests();notice(result.message);}));actions.append(approve);
    }
    if(request.status==='pending'){
      const deny=node('button','Deny','text-button request-deny');deny.type='button';deny.disabled=['queued','running'].includes(request.mail_status);deny.setAttribute('aria-label',`Deny access for ${request.email}`);
      deny.addEventListener('click',action(async()=>{if(!confirm(`Deny access for ${request.email}? No email will be sent.`))return;const result=await api('/api/admin/signup-requests/'+encodeURIComponent(request.id)+'/deny','POST',{});await loadSignupRequests();notice(result.message);}));actions.append(deny);
    }
    if(actions.children.length)actionCell.append(actions);else actionCell.append(node('span','No actions','helper'));
    row.append(email,date,decision,mail,actionCell);body.append(row);
  }
  root.append(table);
}

function setAuthMode(requestAccess){
  registering=requestAccess;$('#auth-title').textContent=registering?'Request access':'Welcome back';$('#auth-form button').textContent=registering?'Request access':'Sign in';$('#auth-switch').textContent=registering?'Already have access? Sign in':'Request access';
  $('#login-password').hidden=registering;$('#auth-form [name="password"]').disabled=registering;$('#login-code').hidden=registering;$('#auth-form [name="code"]').disabled=registering;$('#forgot').hidden=registering;$('#resend').hidden=registering;
  $('#auth-note').hidden=!registering;$('#auth-note').textContent=registering?'Enter your email address for administrator approval. If approved, you will receive a link to verify your email and choose a password.':'';
}
async function showSignup(token){
  signupToken=token;showRoot('create-account');$('#signup-form').hidden=true;$('#signup-state').textContent='Checking your account-creation link...';
  try{const value=await api('/api/auth/signup-info','POST',{token});$('#signup-email').value=value.email;$('#signup-state').textContent='Your access request was approved. Choose a password to verify this email address and finish creating your account.';$('#signup-form').hidden=false;}
  catch(error){signupToken='';$('#signup-state').textContent=error.message;}
}
function renderUsers(){
  const query=$('#user-filter').value.toLowerCase(),root=$('#users-list'),open=new Set([...root.querySelectorAll('details[open]')].map(e=>e.dataset.disclosureKey));root.replaceChildren();$('#users-summary').replaceChildren();
  for(const [label,value] of [['Accounts',adminUsers.length],['Research runs',adminUsers.reduce((sum,u)=>sum+u.runs,0)]]){const card=node('article');card.append(node('span',label),node('strong',String(value)));$('#users-summary').append(card);}
  const users=adminUsers.filter(u=>u.email.toLowerCase().includes(query));
  if(!users.length)root.append(node('p','No matching accounts.','helper'));
  for(const u of users){
    const card=node('article',undefined,'user-card'),heading=node('div',undefined,'user-heading'),identity=node('div');identity.append(node('h3',u.email));const badges=node('div',undefined,'badge-list');badges.append(statusBadge(u.suspended?'Suspended':'Active',u.suspended?'error':'ok'));if(!u.verified)badges.append(statusBadge('Email unverified'));heading.append(identity,badges);card.append(heading);
    const summary=node('div',undefined,'user-summary');summary.append(node('span',`${u.runs} research ${u.runs===1?'run':'runs'}`),node('span',u.schedule_opt_in?'Scheduled research on':'Scheduled research off'),node('span',u.email_opt_in?'Briefing email on':'Briefing email off'));card.append(summary);
    const controls=node('div',undefined,'user-controls'),label=node('label','Daily allowance override'),input=node('input');input.type='number';input.min='0';input.max='100';input.placeholder='Default';input.value=u.allowance??'';input.setAttribute('aria-label',`Daily allowance override for ${u.email}`);label.append(input);
    const save=node('button','Save allowance','secondary');save.addEventListener('click',action(async()=>{await api('/api/admin/users/'+u.id,'PUT',{allowance:input.value===''?null:Number(input.value)});await loadUsers();}));
    const suspend=node('button',u.suspended?'Restore access':'Suspend account','text-button');suspend.addEventListener('click',action(async()=>{await api('/api/admin/users/'+u.id,'PUT',{suspended:!u.suspended});await loadUsers();}));controls.append(label,save,suspend);card.append(controls);
    const information=node('div',undefined,'user-information');information.append(factList([['Email verification',u.verified?'Verified':'Unverified'],['Joined',when(u.created_at)],['Last active',when(u.last_active)]]));
    for(const [title,facts] of [
      ['Research outcomes',[['Completed',u.outcomes.success],['Partial',u.outcomes.partial],['Failed',u.outcomes.failed],['Cancelled',u.outcomes.cancelled],['Queued',u.outcomes.queued],['Running',u.outcomes.running]]],
      ['Timing & usage',[['Average wait',u.mean_queue_seconds==null?'Unavailable':Math.round(u.mean_queue_seconds)+'s'],['Average duration',u.mean_run_seconds==null?'Unavailable':Math.round(u.mean_run_seconds)+'s'],['Model calls',u.model_calls],['Searches',u.searches],['Input tokens',u.input_tokens??'Unavailable'],['Output tokens',u.output_tokens??'Unavailable'],['Estimated cost',u.estimated_cost==null?'Unavailable':money(u.estimated_cost)]]],
      ['Email delivery',[['Sent',u.email_sent],['Uncertain outcome',u.email_uncertain]]]
    ]){information.append(node('h4',title),factList(facts,'metric-grid'));}
    const deletion=node('div',undefined,'account-deletion');deletion.append(node('h4','Account deletion'),node('p','Permanently removes this account, portfolio, reports, and access.','helper'));
    const remove=node('button','Delete account','danger');remove.type='button';remove.setAttribute('aria-label',`Delete account for ${u.email}`);
    remove.addEventListener('click',action(async()=>{if(!confirm(`Permanently delete ${u.email} and their portfolio, reports, and access? This cannot be undone.`))return;await api('/api/admin/users/'+encodeURIComponent(u.id),'DELETE');await loadUsers();notice('User account deleted.');}));deletion.append(remove);information.append(deletion);
    const more=uiDisclosure('Account & usage details',information,u.id);more.open=open.has(u.id);card.append(more);root.append(card);
  }
}
async function saveAdmin(){await api('/api/admin/settings','PUT',adminConfig);await loadAdmin();notice('Settings saved. Retest connections after changing a provider or sender.');}
$$('[data-tab]').forEach(button=>button.addEventListener('click',action(async()=>{tab(button.dataset.tab);if(button.dataset.tab==='activity'&&$('#activity-run').value)await renderTrace($('#activity-run').value);})));
$$('[data-admin-tab]').forEach(button=>button.addEventListener('click',action(async()=>{adminTab(button.dataset.adminTab);if(button.dataset.adminTab==='admin-access')await loadSignupRequests();})));
$('#logout').addEventListener('click',action(async()=>{await api('/api/auth/logout','POST',{});clearPrivate();await loadSession();}));
$('#auth-switch').addEventListener('click',()=>setAuthMode(!registering));
$('#auth-form').addEventListener('submit',action(async()=>{const value=Object.fromEntries(new FormData($('#auth-form')));const result=await api('/api/auth/'+(registering?'register':'login'),'POST',registering?{email:value.email}:value);$('#auth-form [name="password"]').value='';$('#auth-form [name="code"]').value='';if(registering){$('#auth-note').hidden=false;$('#auth-note').textContent=result.message;}else await loadSession();}));
$('#signup-form').addEventListener('submit',action(async()=>{const form=$('#signup-form'),value=Object.fromEntries(new FormData(form));if(value.password!==value.confirmation)throw Error('The passwords do not match.');const result=await api('/api/auth/complete-signup','POST',{token:signupToken,password:value.password});signupToken='';history.replaceState(null,'','/');form.reset();$('#signup-email').value='';clearPrivate();await loadSession();notice(result.message);}));
$('#signup-back').addEventListener('click',action(async()=>{signupToken='';history.replaceState(null,'','/');$('#signup-form').reset();await loadSession();}));
$('#refresh-requests').addEventListener('click',action(loadSignupRequests));
for(const [id,purpose] of [['forgot','reset'],['resend','verify']])$('#'+id).addEventListener('click',action(async()=>{const email=$('#auth-form [name="email"]').value;if(!email)throw Error('Enter your email address first.');notice((await api('/api/auth/request-link','POST',{email,purpose})).message);}));
$('#mfa-start').addEventListener('submit',action(()=>requestMfaSetup(Object.fromEntries(new FormData($('#mfa-start'))))));
$('#mfa-retry').addEventListener('click',action(()=>requestMfaSetup()));
$('#mfa-regenerate').addEventListener('click',action(async()=>{if(mfaBusy)return;mfaRegenerate=true;if(session.mfa_enrolled){$('#mfa-start').reset();$('#mfa-start').hidden=false;$('#mfa-code').hidden=true;$('#mfa-regenerate').hidden=true;$('#mfa-state').textContent='Confirm your password and current authenticator or recovery code to generate a new setup code.';}else await requestMfaSetup();}));
$('#mfa-confirm').addEventListener('submit',action(async()=>{
  if(mfaBusy||!mfaSetupId)return;mfaBusy=true;const owner=session.email,version=mfaVersion,controls=$$('#mfa button');controls.forEach(button=>button.disabled=true);$('#mfa').setAttribute('aria-busy','true');
  try{
    const value=await api('/api/account/mfa/confirm','POST',{...Object.fromEntries(new FormData($('#mfa-confirm'))),setup_id:mfaSetupId});
    if(owner!==session.email||version!==mfaVersion)return;
    mfaSetupId='';$('#mfa-confirm').reset();$('#recovery-codes').textContent=value.recovery_codes.join('\n');$('#recovery').hidden=false;$('#mfa-code').hidden=true;$('#mfa-start').hidden=true;$('#mfa-regenerate').hidden=true;$('#mfa-retry').hidden=true;$('#mfa-state').textContent='Your authenticator is ready. Save your recovery codes before continuing.';const authenticated=await api('/api/session');if(version!==mfaVersion)return;session=authenticated;csrf=session.csrf;
  }finally{if(version===mfaVersion){mfaBusy=false;controls.forEach(button=>button.disabled=false);$('#mfa').removeAttribute('aria-busy');}}
}));
$('#mfa-done').addEventListener('click',action(async()=>{$('#recovery-codes').textContent='';$('#mfa-key').textContent='';$('#mfa-qr').removeAttribute('src');$('#recovery').hidden=true;await loadSession();}));
$('#optional-mfa').addEventListener('click',action(openMfa));
$('#edit-holdings').addEventListener('click',()=>!savingPortfolio&&beginEditing(workspace.portfolio?.positions,workspace.portfolio?.cash));
$('#add-holding').addEventListener('click',()=>{if(savingPortfolio)return;draft.push({symbol:'',quantity:''});renderHoldings();$('#holdings tr:last-child input')?.focus();});
$('#discard').addEventListener('click',()=>{cancelLookups();editing=false;draft=[];$('#mapping').hidden=true;renderHoldings();});
$('#save-holdings').addEventListener('click',action(async()=>{
  if(savingPortfolio)return;savingPortfolio=true;const version=draftVersion,owner=session.email;const controls=$$('#portfolio-editor input,#portfolio-editor button');controls.forEach(el=>el.disabled=true);updateRunControls();
  try{const pending=[...lookupTimers.keys()];for(const timer of lookupTimers.values())clearTimeout(timer);lookupTimers.clear();if(pending.length)enrichDraft(pending);await Promise.allSettled([...lookupTasks]);if(version!==draftVersion||owner!==session.email)return;
    await api('/api/portfolio','PUT',{positions:draft.map(p=>({symbol:p.symbol.trim().toUpperCase(),quantity:String(p.quantity)})),cash:$('#cash').value});if(version!==draftVersion||owner!==session.email)return;
    cancelLookups();editing=false;draft=[];$('#mapping').hidden=true;await loadWorkspace();notice('Holdings and cash saved.');
  }finally{if(owner===session.email){savingPortfolio=false;controls.forEach(el=>el.disabled=false);updateRunControls();}}
}));
$('#upload').addEventListener('change',action(async event=>{importFile=event.target.files[0];await importDraft();}));
$('#upload-zone').addEventListener('dragover',event=>{event.preventDefault();$('#upload-zone').classList.add('dragover');});
$('#upload-zone').addEventListener('dragleave',()=>$('#upload-zone').classList.remove('dragover'));
$('#upload-zone').addEventListener('drop',action(async event=>{$('#upload-zone').classList.remove('dragover');importFile=event.dataTransfer.files[0];await importDraft();}));
$('#apply-mapping').addEventListener('click',action(()=>importDraft({symbol:Number($('#symbol-column').value),quantity:Number($('#quantity-column').value)})));
$('#preferences').addEventListener('input',()=>dirtyPreferences=true);
$('#preferences').addEventListener('submit',action(async()=>{if(detailSaving||preferencesSaving)throw Error('Wait for your briefing detail choice to finish saving.');preferencesSaving=true;$('#detail').disabled=true;updateRunControls();try{await api('/api/account/preferences','PUT',preferenceData());dirtyPreferences=false;await loadWorkspace(true);notice('Schedule and delivery choices saved.');}finally{preferencesSaving=false;$('#detail').disabled=false;updateRunControls();}}));
$('#detail').addEventListener('change',action(saveDetail));
$('#run-form').addEventListener('submit',async event=>{
  event.preventDefault();if(runSubmitting||activeRun)return;runError='';if(editing){runError='Save or discard holdings and cash first.';updateRunControls();return;}if(detailSaving||detailError||preferencesSaving){updateRunControls();return;}
  runSubmitting=true;notice('');updateRunControls();try{const run=await api('/api/runs','POST',{question:$('#question').value,research_date:$('#research-date').value||null,send_email:$('#send-email').checked});activeRun=run.id;selectedTrace=run.id;$('#latest-report').open=false;await loadWorkspace();}catch(error){runError=error.message;}finally{runSubmitting=false;updateRunControls();}
});
$('#trace-report').addEventListener('click',()=>{if(selectedTrace)openReport(selectedTrace);});
$('#resend-account').addEventListener('click',action(async()=>notice((await api('/api/auth/request-link','POST',{email:session.email,purpose:'verify'})).message)));
$('#activity-run').addEventListener('change',action(event=>renderTrace(event.target.value)));
$('#delete-form').addEventListener('submit',action(async()=>{if(!confirm('Permanently delete your account, portfolio, and research history?'))return;await api('/api/account','DELETE',{password:$('#delete-password').value});$('#delete-password').value='';clearPrivate();await loadSession();notice('Your account has been deleted.');}));
$('#model-provider').addEventListener('change',()=>{$('#model-url').value=$('#model-provider').value==='anthropic'?'https://api.anthropic.com':'https://api.openai.com/v1';renderSecrets(secretStatus);});
$('#connections-form').addEventListener('submit',action(async()=>{const r=adminConfig.research;r.model={...r.model,provider:$('#model-provider').value,model:$('#model-name').value,base_url:$('#model-url').value,temperature:Number($('#temperature').value),max_tokens:Number($('#max-tokens').value),request_timeout_seconds:Number($('#model-timeout').value)};Object.assign(r,{search_provider:$('#search-provider').value,primary_feeds_enabled:$('#fed-enabled').checked,ticker_news_enabled:$('#ticker-enabled').checked,news_feed_sources:$$('[name="feed"]:checked').map(el=>el.value),sec_company_filings_enabled:$('#sec-enabled').checked,sec_contact:$('#sec-contact').value,price_refresh_seconds:Number($('#price-interval').value),max_seconds:Number($('#run-timeout').value),benchmark:$('#benchmark').value,max_data_age_hours:Number($('#news-window').value)});await saveAdmin();}));
$('#email-transport').addEventListener('change',()=>{$('#smtp-fields').hidden=$('#email-transport').value!=='smtp';renderSecrets(secretStatus);});
$('#email-form').addEventListener('submit',action(async()=>{adminConfig.email={transport:$('#email-transport').value,sender:$('#email-sender').value,sender_name:$('#email-name').value,smtp_host:$('#smtp-host').value,smtp_port:Number($('#smtp-port').value),smtp_username:$('#smtp-user').value,smtp_tls:$('#smtp-tls').value};await saveAdmin();}));
$('#admin-policy').addEventListener('submit',action(async()=>{Object.assign(adminConfig,{schedule:{enabled:$('#daily-enabled').checked,at:$('#daily-time').value+':00',timezone:$('#daily-zone').value},daily_allowance:Number($('#daily-allowance').value),concurrency:Number($('#concurrency').value),signup_enabled:$('#signup-enabled').checked,input_cost_per_million:$('#input-cost').value===''?null:Number($('#input-cost').value),output_cost_per_million:$('#output-cost').value===''?null:Number($('#output-cost').value)});adminConfig.research.retention_days=Number($('#retention').value);await saveAdmin();}));
async function checkConnections(force=[]){const button=$('#check-service');if(button.disabled)return;button.disabled=true;try{renderChecks(await api('/api/admin/check','POST',{force}));}finally{button.disabled=false;}}
$('#check-service').addEventListener('click',async()=>{try{await checkConnections();}catch(error){notice(error.message,true);}});
$('#user-filter').addEventListener('input',renderUsers);
$('#reset-form').addEventListener('submit',action(async()=>{const token=new URLSearchParams(location.hash.slice(1)).get('reset');const value=await api('/api/auth/reset','POST',{token,password:$('#reset-form [name="password"]').value});history.replaceState(null,'','/');$('#reset-form').reset();await loadSession();notice(value.message);}));
$('#notice-close').addEventListener('click',dismissNotice);
$('#notice').addEventListener('pointerenter',()=>clearTimeout(noticeTimer));$('#notice').addEventListener('pointerleave',scheduleNoticeDismiss);$('#notice').addEventListener('focusin',()=>clearTimeout(noticeTimer));$('#notice').addEventListener('focusout',()=>setTimeout(scheduleNoticeDismiss,0));
$('#search-provider').addEventListener('change',()=>renderSecrets(secretStatus));
$('#run-trace').addEventListener('click',action(async()=>{const id=activeRun||selectedTrace;if(!id)return;tab('activity');$('#activity-run').value=id;await renderTrace(id);}));
$('#latest-report').addEventListener('toggle',()=>{if($('#latest-report').open){const frame=$('#report-frame');if(frame.dataset.reportSrc&&frame.getAttribute('src')!==frame.dataset.reportSrc)frame.src=frame.dataset.reportSrc;sizeReport();}});
$('#report-frame').addEventListener('load',sizeReport);window.addEventListener('resize',sizeReport);
window.addEventListener('beforeunload',event=>{if(editing){event.preventDefault();event.returnValue='';}});
async function start(){await loadSession();const link=new URLSearchParams(location.hash.slice(1));if(link.has('signup')){await showSignup(link.get('signup'));}else if(link.has('verify')){notice((await api('/api/auth/verify','POST',{token:link.get('verify')})).message);history.replaceState(null,'','/');}else if(link.has('reset'))showRoot('reset');}
start().catch(error=>notice(error.message,true));
setInterval(()=>{if(session.role==='user'&&!$('#workspace').hidden)loadWorkspace().catch(error=>{if(error.status===401){clearPrivate();loadSession().catch(()=>{});}});},10000);
