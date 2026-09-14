'use strict';
  const failureReasons = {
    store_busy: "The service is busy. Try again in a moment.",
    authorization_unavailable: 'Access could not be checked; please try again.',
    receipt_persist_failed: 'Not received. Try again.',
    media_storage_unavailable: 'The file is unavailable; please try again.',
    ingress_conflict: 'The request could not be saved; please try again.',
    ingress_exception: 'The request could not be received; please try again.',
    configuration: 'The service is starting; please try again.',
    unhandled: 'The request could not be completed; please try again.'
  };

(() => {
  function displayZone(){try{const zone=Intl.DateTimeFormat().resolvedOptions().timeZone;if(zone){new Intl.DateTimeFormat('en',{timeZone:zone}).format();return zone;}}catch(_){}return 'UTC';}
  const browserZone=displayZone();
  function calendarDate(value){if(/^\d{4}-\d{2}-\d{2}$/.test(value||''))return new Intl.DateTimeFormat('en',{dateStyle:'medium',timeZone:'UTC'}).format(new Date(value));const date=new Date(value);return value&&Number.isFinite(+date)?new Intl.DateTimeFormat('en',{dateStyle:'medium',timeZone:browserZone}).format(date):'Not recorded';}
  const demo = document.body.dataset.demo === 'true';
  const fixtures = new Map();
  function demoFixture(name) {
    if (!fixtures.has(name)) fixtures.set(name, fetch(`/assets/demo-${name}.json`, {credentials:'omit', cache:'no-store'})
      .then(response => {if (!response.ok) throw new Error('Demonstration unavailable.'); return response.json();})
      .catch(error => {fixtures.delete(name); throw error;}));
    return fixtures.get(name);
  }
  // Demo requests never enter a transport or inspect a browser credential.
  async function patientDemo(path, options={}) {
    const data = await demoFixture('patient');
    if (!options.method || options.method === 'GET') {
      if (path === '/api/patient/me') return {display_name:data.display_name};
      if (path === '/api/patient/agreement') return data.agreement;
      if (path === '/api/patient/plan') return data.plan;
      if (path === '/api/patient/preferences') return data.preferences;
      if (path === '/api/patient/uploads') return data.uploads;
      if (path === '/api/patient/conversation') return {items:data.conversation, cursor:null};
    } else if (options.method === 'POST') {
      const payload = JSON.parse(options.body);
      if (path === '/api/patient/messages') {
        data.conversation.push({id:crypto.randomUUID(), at:new Date().toISOString(), direction:'inbound', text:payload.text});
        setTimeout(() => {
          data.conversation.push({id:crypto.randomUUID(), at:new Date().toISOString(), direction:'outbound', text:data.reply});
          window.dispatchEvent(new Event('patient-demo-updated'));
        }, 2000);
        return {status:'accepted'};
      }
      if (path === '/api/patient/preferences') {
        if (payload.reminders === 'resume') {
          data.confirmation = crypto.randomUUID();
          return {status:'accepted', confirmation_token:data.confirmation};
        }
        if (payload.reminders === 'stop') data.preferences.reminders = 'paused';
        if (payload.quiet_hours) data.preferences.quiet_hours = payload.quiet_hours;
        return {status:'accepted', confirmation_token:null};
      }
      if (path === '/api/patient/preferences/confirm' && data.confirmation && payload.token === data.confirmation) {
        data.preferences.reminders = 'enabled';
        data.confirmation = null;
        return {status:'accepted'};
      }
    }
    throw new Error('Unknown demonstration action.');
  }
  document.addEventListener('invalid',event=>{
    event.target.setAttribute('aria-invalid','true');
    if(event.target.id==='digest-time')document.getElementById('digest-result').textContent='Choose a valid daily time.';
  },true);
  document.addEventListener('input',event=>{if(event.target.validity?.valid)event.target.removeAttribute('aria-invalid');});
  // Submission labels follow the existing disabled state; command handlers own outcomes.
  document.addEventListener('submit',event=>{
    const button=event.submitter;if(!button)return;
    queueMicrotask(()=>{
      if(!button.disabled)return;
      const label=button.textContent, pending=event.target.id==='patient-message-form'?'Sending…':event.target.id==='patient-upload-form'?'Uploading…':'Saving…';
      button.textContent=pending;
      const watch=new MutationObserver(()=>{if(!button.disabled){if(button.textContent===pending)button.textContent=label;watch.disconnect();}});
      watch.observe(button,{attributes:true,attributeFilter:['disabled']});
    });
  });
  const revealSeen=new WeakSet(), counted=new WeakSet();
  const reduceMotion=matchMedia('(prefers-reduced-motion: reduce)');
  function countUp(el){
    if(counted.has(el))return;counted.add(el);
    const target=Number(el.dataset.count), start=performance.now();
    if(!Number.isFinite(target))return;
    const step=now=>{if(!el.isConnected)return;const p=reduceMotion.matches?1:Math.min(1,(now-start)/1000);
      el.textContent=String(Math.round(target*(1-Math.pow(1-p,3))));if(p<1)requestAnimationFrame(step);};
    if(reduceMotion.matches)el.textContent=String(target);else requestAnimationFrame(step);
  }
  const revealObserver=new IntersectionObserver(entries=>entries.forEach(en=>{
    if(!en.isIntersecting||(!(en.intersectionRatio>=.15)&&en.boundingClientRect.height<=innerHeight))return;
    en.target.classList.add('in');revealObserver.unobserve(en.target);
    en.target.querySelectorAll('[data-count]').forEach(countUp);
  }),{threshold:[0,.15],rootMargin:'0px 0px -8% 0px'});
  const trendObserver=new IntersectionObserver(entries=>entries.forEach(en=>{
    if(en.isIntersecting){en.target.classList.add('in');trendObserver.unobserve(en.target);}
  }),{threshold:.3});
  function reveal(root=document){
    root.querySelectorAll('.rv,.trend').forEach(el=>{
      if(revealSeen.has(el))return;revealSeen.add(el);
      if(reduceMotion.matches){el.classList.add('in');el.querySelectorAll('[data-count]').forEach(countUp);}
      else (el.matches('.trend')?trendObserver:revealObserver).observe(el);
    });
  }
  reduceMotion.addEventListener('change',()=>{if(reduceMotion.matches){document.querySelectorAll('.rv,.trend').forEach(el=>el.classList.add('in'));document.querySelectorAll('[data-count]').forEach(el=>el.textContent=el.dataset.count);}});
  const adminWords={"suspended":["Suspended. Their patients need another doctor to take over.","حسابه موقوف. مرضاه محتاجين دكتور تاني يتابعهم."],"restore":["Restore access","رجّع الدخول"],"restored":["Access restored.","الدخول رجع."],"suspendConfirm":["Suspending closes this doctor's access and opens an item for you to find another doctor to take over their patients (it does not do this by itself). Restore access gives the account back.","وقف الحساب بيقفل دخول الدكتور وبيفتحلك بند عشان تلاقي دكتور تاني يتابع مرضاه (مش بيعمل ده لوحده). رجّع الدخول بيرجع الحساب."],"restoreConfirm":["Restoring access lets this doctor sign in again. Their patients' records were kept.","رجوع الدخول بيسمح للدكتور يدخل تاني. ملفات مرضاه محفوظة."]};
  if (document.getElementById('admin-applications')) {
    const target = document.getElementById('admin-applications');
    const status = document.getElementById('admin-result');
    const csrf = () => decodeURIComponent(document.cookie.split('; ').find(x => x.startsWith('sanad_csrf='))?.slice(11) || '');
    const send = async (path, body) => {
      if (demo) {
        const rows = await demoFixture('admin'), parts = path.split('/'), verb = parts.pop(), id = decodeURIComponent(parts.pop());
        const row = rows.find(row => row.id === id || row.doctor_id === id);
        const next = {approve:'approved', reject:'rejected', suspend:'suspended', reinstate:'approved'}[verb];
        if (!row || !next) throw new Error('Unknown demonstration action.');
        row.status = next;
        if (verb === 'approve') {row.doctor_id = row.id; row.doctor_version = 1;}
        if (verb === 'reject') {row.decision_reason=body.reason_code;row.decided_at=new Date().toISOString();}
        status.textContent = results[verb];
        return {ok:true};
      }
      const response = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json', 'X-CSRF-Token':csrf()}, body:JSON.stringify(body)});
      const result = await response.json();
      status.textContent = response.ok ? results[path.split('/').pop()] || 'Done.' : failureReasons[result.reason] || 'Action refused. Refresh and check the account before retrying.';
      return response;
    };
    const results={approve:'Approved.',reject:'Rejected.',suspend:'Suspended.',reinstate:adminWords.restored[0]};
    const load = async () => {
      const response = demo ? {ok:true, json:() => demoFixture('admin')} : await fetch('/api/admin/applications');
      if (!response.ok) { const result=await response.json().catch(()=>({}));status.textContent = failureReasons[result.reason] || 'Sign in again.'; return; }
      const rows = await response.json();
      target.replaceChildren();
      const summary=document.createElement('div');summary.className='summary-strip rv';summary.style.setProperty('--d','180ms');
      const labels={pending:'Waiting for your decision',approved:'Approved',suspended:'Suspended',rejected:'Rejected',revoked:'Access revoked'};
      for(const [key,label] of Object.entries(labels)){
        const count=rows.filter(r=>r.status===key).length;if(!count&&!['pending','approved','suspended'].includes(key))continue;
        const tile=document.createElement('article');tile.className='summary-tile';const caption=document.createElement('span');caption.textContent=label;const value=document.createElement('strong');value.textContent='0';value.dataset.count=String(count);tile.append(caption,value);summary.append(tile);
      }target.append(summary);
      if(!rows.length){const empty=document.createElement('div');empty.className='empty';empty.innerHTML='<svg class="icon" viewBox="0 0 16 16" aria-hidden="true"><path d="M4 2h8v12H4zM6 5h4M6 8h4M6 11h2"/></svg><p class="empty-title">No applications are waiting.</p><p class="empty-next">New applications will appear here.</p>';target.append(empty);}
      for (const row of rows) {
        const item=document.createElement('section');item.className='section application-card rv';item.style.setProperty('--d',`${240+rows.indexOf(row)*60}ms`);
        item.tabIndex=0;item.setAttribute('aria-expanded','false');
        const av=document.createElement('span');av.className='av';av.setAttribute('aria-hidden','true');av.textContent=String(row.name).replace(/^Dr\.?\s+/i,'').trim().split(/\s+/).slice(0,2).map(word=>Array.from(word)[0]).join('').toUpperCase();item.append(av);
        const name=document.createElement('h2');name.textContent=row.name;
        const info=document.createElement('p');
        info.textContent=`${row.specialty} · ${row.city} · applied on ${calendarDate(row.applied_at)}`;
        const line=document.createElement('p');line.className='application-state status '+({pending:'warning',approved:'success',suspended:'warning',rejected:'danger',revoked:'danger'}[row.status]||'quiet');line.textContent={pending:'Waiting for your decision',approved:'Approved and working',suspended:adminWords.suspended[0],rejected:'Rejected',revoked:'Access revoked'}[row.status];item.append(name,info,line);const acts=document.createElement('div');acts.className='acts';item.append(acts);
        for(const verb of {pending:['approve','reject'],approved:['suspend'],suspended:['reinstate'],rejected:[],revoked:[]}[row.status]||[]){
          const button=document.createElement('button');button.textContent=verb==='reinstate'?adminWords.restore[0]:verb[0].toUpperCase()+verb.slice(1);
          button.onclick=async()=>{
            if(verb==='reject'){
              if(item.querySelector('form'))return;
              const form=document.createElement('form');form.innerHTML='<fieldset><legend>Why are you rejecting this application?</legend><label><input type="radio" name="reason" value="unverified" required>Identity could not be verified</label><label><input type="radio" name="reason" value="admin_rejected" required>Declined by the administrator</label></fieldset><button type="submit">Confirm rejection</button><button type="button">Cancel</button>';
              form.querySelector('[type=button]').onclick=()=>form.remove();form.onsubmit=event=>{event.preventDefault();perform(verb,form.elements.reason.value,form.querySelector('[type=submit]'));};item.append(form);form.querySelector('input').focus();return;
            }
            const confirmation={suspend:adminWords.suspendConfirm[0],reinstate:adminWords.restoreConfirm[0]}[verb];
            if(confirmation&&!window.confirm(confirmation))return;
            await perform(verb,verb==='suspend'?'coverage':null,button);
          };
          acts.append(button);
        }
        async function perform(verb,reason,button){
          button.disabled=true;const clinical=verb==='suspend'||verb==='reinstate';
          try{const reply=await send(`/api/admin/${clinical?'doctors':'applications'}/${encodeURIComponent(clinical?row.doctor_id:row.id)}/${verb}`,{command_id:crypto.randomUUID(),expected_version:clinical?row.doctor_version:row.version,...(reason?{reason_code:reason}:{})});if(reply.ok)await load();else button.disabled=false;}
          catch{status.textContent='Request failed. Refresh before retrying.';button.disabled=false;}
        }
        const panel=document.createElement('div');panel.className='detail';
        const inner=document.createElement('div');inner.className='inner';panel.append(inner);
        const facts=document.createElement('div');inner.append(facts);
        const dateText=calendarDate;
        const details=[['Specialty',row.specialty],['City',row.city],['Applied on',dateText(row.applied_at)],['Status',labels[row.status]]];
        if(['approved','rejected'].includes(row.status))details.push(['Decided on',dateText(row.decided_at)]);
        if(row.status==='rejected')details.push(['Reason',{unverified:'Identity could not be verified',admin_rejected:'Declined by the administrator'}[row.decision_reason]||'Not recorded']);
        if(row.status==='suspended')details.push(['Suspended on',dateText(row.suspended_at)],['Reason',row.suspension_reason?'Paused by the administrator':'Not recorded']);
        for(const [label,value] of details){const line=document.createElement('div');line.className='kv';const key=document.createElement('span'),fact=document.createElement('b');key.textContent=label;fact.textContent=value||'Not recorded';line.append(key,fact);facts.append(line);}
        item.append(panel);
        const toggle=()=>{const open=item.classList.contains('open');target.querySelectorAll('.application-card.open').forEach(card=>{card.classList.remove('open');card.setAttribute('aria-expanded','false');});if(!open){item.classList.add('open');item.setAttribute('aria-expanded','true');}};
        item.addEventListener('click',event=>{if(!event.target.closest('button,form,a,input,label'))toggle();});
        item.addEventListener('keydown',event=>{if(event.target===item&&['Enter',' '].includes(event.key)){event.preventDefault();toggle();}});
        target.append(item);
      }
      const firstPending=[...target.querySelectorAll('.application-card')].find((el,i)=>rows[i].status==='pending');firstPending?.querySelector('button')?.classList.add('primary');
      document.querySelectorAll('.page-heading,main>h1,main>h1+p').forEach((el,i)=>{el.classList.add('rv');el.style.setProperty('--d',`${i*60}ms`);});reveal();
    };
    if (!demo) document.getElementById('admin-logout').onclick = async () => {
      try { const response = await send('/api/admin/logout', {}); if (response.ok) { target.replaceChildren(); status.textContent = 'Signed out everywhere.'; } }
      catch { status.textContent = 'Sign out failed. Try again.'; }
    };
    load().catch(() => { status.textContent = 'Could not load applications.'; });
    return;
  }
  const body = document.body, lang = document.documentElement.lang;
  const patient = body.dataset.audience === 'patient', en = !patient || lang === 'en';
  let view = body.dataset.view;
  const words = {
    patients:['Patients','المرضى'], inbox:['Inbox','المراجعات'], history:['Review history','سجل المراجعات'], preferences:['Preferences','التفضيلات'],
    detail:['Patient record','ملف المريض'], yourcare:['Your care','خطتك'], clinic:['Your clinical workspace','مساحة المتابعة'],
    subtitle:['A clear view of what needs your attention.','المطلوب متابعته في مكان واحد.'],
    search:['Search patients','ابحث عن مريض'], filter:['Show','اعرض'], all:['All patients','كل المرضى'], active:['Active','نشط'], overdue:['Overdue','متأخر'], blocked:['Patient cannot do it yet','المريض لسه مش قادر يعملها'], awaiting_link:['Awaiting link','بانتظار الربط'],
    clear:['Clear filters','إلغاء الفلاتر'], results:['results','نتيجة'], patient:['Patient','المريض'], age:['Age','العمر'], outstanding:['Outstanding','المطلوب'], due:['Due','الموعد'], status:['Status','الحالة'],
    missing:['Missing','غير موجود'], not_yet_due:['Not yet due','لم يحن الموعد'], processing:['Received but processing','وصل وجار تجهيزه'], unverifiable:['Unverifiable','غير قابل للتحقق'], fulfilled:['Fulfilled','تم استيفاء المطلوب'], pending_review:['Pending review','بانتظار المراجعة'], resolved:['Resolved','تمت المراجعة'], open:['Open','مفتوح'], acknowledged:['Acknowledged','تم الاطلاع'],
    unmatched:['Unmatched document','مستند غير مرتبط'], accepted:['Accepted document','مستند مقبول'], rejected:['Rejected document','مستند مرفوض'], not_required:['No review required','لا تتطلب مراجعة'], correction_requested:['Correction requested','مطلوب تصحيح'],
    unassigned:['No patient associated','لم يرتبط بمريض بعد'], no_work:['No outstanding obligation recorded','لا توجد متابعة معلّقة مسجلة'], more:['other outstanding items','متابعات معلّقة أخرى'],
    refresh:['Refresh','تحديث'], updated:['Updated','تم التحديث'], timezone:['Times shown in','التوقيت المعروض'], previous:['Previous','السابق'], next:['Next','التالي'], page:['Page','صفحة'], of:['of','من'],
    empty:['No patient needs anything in this view. Clear filters to see all patients.','لا توجد نتائج في هذه الصفحة.'], no_patients:['No patients recorded yet.','لم يُسجل مرضى بعد.'],
    failed:['Could not load this view. Refresh to try again or return to Patients.','تعذر تحميل الصفحة. حدّثها للمحاولة أو ارجع للمرضى.'],
    expired:['Your session has expired. Open Telegram and use /login to sign in again.','انتهت جلسة الدخول. افتح تيليجرام واستخدم /login للدخول من جديد.'],
    denied:['This record is unavailable to your account. Return to Patients.','هذا الملف غير متاح لحسابك. ارجع للمرضى.'],
    stale:['The information changed in another tab. Refresh, check it, and try again.','تغيرت البيانات في صفحة أخرى. حدّثها وراجعها ثم حاول مجدداً.'],
    loading:['Loading your page…','جار تحميل الصفحة…'], plan:['Active orders','التعليمات الحالية'], missions:['Care requests','طلبات المتابعة'], facts:['Record and history','البيانات والتاريخ المرضي'], evidence:['Evidence and provenance','المستندات ومصادرها'],
    no_plan:['No active medication is recorded.','لا يوجد دواء حالي مسجل.'], no_requests:['No upcoming request is recorded.','لا يوجد طلب قادم مسجل.'],
    received:['Received','وصل'], source:['Source','المصدر'], version:['Version','النسخة'], printed:['Printed date','التاريخ على المستند'], not_recorded:['Not recorded','غير مسجل'],
    monitoring:['Monitoring readings','قراءات المتابعة'], slot:['Scheduled time','الموعد المحدد'], reading:['Reading','القراءة'], extras:['Extra readings (outside scheduled slots)','قراءات إضافية خارج المواعيد'],
    changed:['Material change since the original review','تغير مهم منذ المراجعة الأصلية'], notice:['First notice','أول إشعار'], reason:['Resolution reason','سبب انتهاء المراجعة'], review_note:['You have seen this. It stays open until you reply.','إنت شفت ده. هيفضل مفتوح لحد ما ترد.'],
    language:['Language','اللغة'], save:['Save language','حفظ اللغة'], saved:['Language preference saved.','تم حفظ اللغة المفضلة.'], contest:['The contest build displays English. Your stored choice is preserved.','نسخة المسابقة تعرض الإنجليزية مع حفظ اختيارك.'],
    back:['Back to patients','العودة للمرضى'], demo:['Synthetic demonstration · Fictional records only','عرض تجريبي · بيانات خيالية فقط'], demo_note:['This view uses a separate static data source. It cannot open a clinical account.','هذا العرض يستخدم بيانات تجريبية مستقلة ولا يفتح حساباً طبياً.'],
    patient_note:['Your doctor’s recorded plan. Send a message or photo below.','الخطة المسجلة من دكتورك. أرسل رسالة أو صورة أدناه.'],
    doctor:['Your doctor','دكتورك'], your_plan:['Your medication plan','خطة أدويتك'], your_requests:['What your doctor asked for','المطلوب منك'], reports:['What you reported about your medication','ما أبلغت به عن أدويتك'], last_reading:['Your last reported reading','آخر قراءة أبلغت بها'], reminders:['Reminders','التذكيرات'], questions:['Your open questions','أسئلتك المعلّقة'], waiting:['Waiting for your doctor','بانتظار دكتورك'], quiet:['Quiet hours','ساعات الهدوء'], resume:['Paused until','مؤجلة حتى'], paused:['Paused','مؤجلة'], opted_out:['Stopped','متوقفة'], frozen:['Access paused','التواصل مجمد'], unreachable:['Unreachable','تعذر التواصل'], no_reports:['No medication report recorded.','لا يوجد بلاغ عن الأدوية مسجل.'], no_questions:['No open questions recorded.','لا توجد أسئلة معلّقة مسجلة.'], self_report:['Self-reported; this does not prove the medicine was taken.','حسب البلاغ؛ لا يثبت تناول الدواء.'], retained:['Retained observation','ملاحظة محفوظة'], historical:['History: not an active order','تاريخ سابق وليس أمراً حالياً'], details:['Details and provenance','التفاصيل والمصدر'], overdue_by:['Overdue by','متأخر بمقدار'], no_due:['No due time recorded','لا يوجد موعد مسجل'], review:['Review','مراجعة'],
    result_review:['Result review','مراجعة نتيجة'], incident_response:['Danger response','متابعة خطر'], deadline_disposition:['Deadline follow-up','متابعة الموعد المتأخر'], evidence_association:['Document association','ربط مستند'], intake_clarification:['Intake clarification','استيضاح ملف'], question_answer:['Answer question','إجابة سؤال'], correction_disposition:['Correction review','مراجعة تصحيح'], media_failure:['File processing failure','تعذر تجهيز ملف'], followup_disposition:['Follow-up review','مراجعة المتابعة'],
    completed:['Request fulfilled','تم استيفاء الطلب'], invalidated_pending_review:['Fulfillment under review','استيفاء الطلب قيد المراجعة'], closed_unfulfilled:['Closed without fulfillment','أغلق دون استيفاء'], cancelled:['Cancelled','ملغى'], superseded:['Superseded','استُبدل'], waiting_patient:['Waiting for patient','بانتظار المريض'], proposed:['Proposed','مقترح'],
    enabled:['Enabled','مفعلة'], disabled:['Disabled','متوقفة'], until:['Until','حتى'], original:['Open original document','افتح المستند الأصلي'], source_files:['Source files','الملفات الأصلية'], sort_help:['Use the column buttons to sort.','استخدم أزرار الأعمدة للترتيب.']
  };
  Object.assign(words,Object.fromEntries(Object.entries({
    detached:'Not used', lab_result:'Lab result', discharge_summary:'Discharge summary', lab_report:'Lab report', prescription:'Prescription', medication_list:'Medication list',
    imaging_report:'Imaging report', medical_report:'Medical report', other:'Document', monitor_screen:'Reading photograph',
    'Blood pressure':'Blood pressure', 'blood pressure':'Blood pressure', blood_glucose:'Blood glucose', 'blood glucose':'Blood glucose', photo:'Photograph', image:'Image', document:'Document', blood_pressure:'Blood pressure', glucose:'Glucose', weight:'Weight', pulse:'Pulse',
    doctor_reviewed:'Reviewed by the doctor', answered:'Answered', closed:'Closed', corrected:'Corrected',
    patient_stopped:'Reminders stopped', order_superseded:'Instruction replaced', valid:'Valid', satisfied:'Fulfilled',
    none:'Not required', pending:'Pending review', reviewed:'Reviewed', question_digest:'Questions',
    unmet_objective:'Deadline follow-up', binding_review:'Patient link review', coverage_review:'Coverage review', voice:'Voice recording', delivery_failure:'Delivery needs attention', coverage:'Coverage review', intake_concern:'Intake concern'
  }).map(([k,v])=>[k,[v,v]])));
  Object.assign(words, {
    unassigned_intake:['Unassigned intake','ملف جديد لسه مش مربوط بمريض'], your_account:['Your account','حسابك'],
    inbox_result:['Result to review','نتيجة محتاجة مراجعتك'], inbox_association:['Document to link to a request','مستند محتاج تربطه بطلب'],
    inbox_question:['Patient question waiting for an answer','سؤال من المريض مستني ردك'], inbox_correction:['Correction to review','تصحيح محتاج مراجعتك'],
    inbox_danger:['Danger report needs a response','بلاغ خطر محتاج ردك'], inbox_intake_danger:['Danger found in a new-patient intake','علامة خطر في ملف مريض جديد'],
    inbox_deadline:['Deadline missed, decide what happens next','الموعد فات، حدد الخطوة الجاية'], inbox_followup:['Follow-up outcome to review','نتيجة متابعة محتاجة مراجعتك'],
    inbox_binding:['Patient link needs your review','ربط المريض محتاج مراجعتك'], inbox_media:['A file could not be processed','ملف معرفناش نجهزه'],
    inbox_intake:['New-patient intake needs clarification','ملف مريض جديد محتاج توضيح'], inbox_patient_delivery:['A message about this patient was not delivered','رسالة تخص المريض ده موصلتش'],
    inbox_intake_delivery:['An intake message was not delivered','رسالة تخص ملف جديد موصلتش'], inbox_doctor_delivery:['A message to you was not delivered','رسالة ليك موصلتش'],
    inbox_coverage:['Patient coverage needs your review','تغطية متابعة المرضى محتاجة مراجعتك'],
    open_evidence:['Open evidence','افتح المستندات'], answer_question:['Answer question','رد على السؤال'], open_history:['Open history','افتح السجل'],
    open_record:['Open record','افتح الملف'], open_requests:['Open requests','افتح الطلبات'],
    intake_telegram:["This new patient's file needs your reply.",'ملف المريض الجديد ده محتاج ردك.'],
    review_telegram:['This item needs your reply.','البند ده محتاج ردك.'],
    due_now:['due now','ميعاده دلوقتي'], due_today:['due today','ميعاده النهارده'], due_in:['due in','فاضل عليه'], late_by:['overdue by','متأخر بقاله'],
    day:['day','يوم'], days:['days','أيام'], hour:['hour','ساعة'], hours:['hours','ساعات'],
    current_plan:['Current plan','الخطة الحالية'], outstanding_work:['Outstanding work','المتابعة المطلوبة'], drawer_evidence:['Evidence','المستندات'], drawer_history:['History','السجل'],
    no_evidence:['No evidence is recorded for this patient.','مفيش مستندات مسجلة للمريض ده.'], no_corrections:['No corrections are recorded for this patient.','مفيش تصحيحات مسجلة للمريض ده.'],
    followups:['Follow-ups','المتابعات'], MEDICATION_DAY3:['Day-three medication follow-up','متابعة الدواء في اليوم التالت'], CLINICAL_CHECKIN:['Clinical check-in','متابعة الحالة'], awaiting_anchor:['Waiting for the start date','مستني تاريخ البداية'], scheduled:['Scheduled','متحدد ميعاده'], waiting_response:['Waiting for a response','مستني الرد'], contact_suppressed:['Contact paused','التواصل متوقف'],
    full_record:['Open full patient record','افتح ملف المريض بالكامل']
  });
  Object.assign(words, {
    order_history:['Earlier versions of this instruction','النسخ الأقدم من التعليمات دي'],
    history:['Past reviews','مراجعات خلصت'], plan:['Current medicines','الأدوية الحالية'],
    missions:['What you asked the patient for','اللي طلبته من المريض'], facts:['Facts on file','المعلومات المسجلة'],
    evidence:['Documents received','المستندات اللي وصلت'], source_files:['Original photos','الصور الأصلية'],
    outstanding:['What to do and why','المطلوب وليه'], missing:['No reading received','مفيش قراءة وصلت'],
    not_yet_due:['Not due yet','لسه ميعادها مجاش'], processing:['Being checked','بنتأكد منه'],
    invalidated_pending_review:['Confirm still done','أكد إنه لسه تم'], fulfilled:['Done','تم'],
    closed_unfulfilled:['Closed','اتقفل'], superseded:['Replaced','اتبدل'],
    awaiting_link:['Starts when the patient joins','يبدأ لما المريض ينضم'],
    waiting_patient:['Waiting for the patient','مستني المريض'], overdue:['Late','متأخر'],
    pending_review:['Waiting for your decision','مستني قرارك'], pending:['Waiting for your decision','مستني قرارك'],
    acknowledged:['Seen by you','إنت شفته'], changed:['Something changed since you last looked.','في حاجة اتغيرت من آخر مرة بصيت.'],
    candidate:['Waiting for you to say whose it is.','مستنيك تقول بتاع مين.'],
    unmatched:['Not matched to any request yet.','لسه مش مربوط بأي طلب.'],
    accepted:['On file.','اتحفظ في الملف.'], accepted_pending_identity:["On file, but the patient's identity is not confirmed.",'اتحفظ في الملف، بس لسه هوية المريض متأكدناش منها.'],
    rejected:['Not used.','متستخدمش.'], detached:['Not used.','متستخدمش.'],
    unverifiable:["The patient's identity is not confirmed.",'لسه هوية المريض متأكدناش منها.'],
    waiting_response:['Waiting for an answer','مستني إجابة'], contact_suppressed:['Cannot be sent','مينفعش يتبعت'],
    no_due:['date not recorded','التاريخ مش مسجل'], no_due_state:['No due date','مفيش ميعاد'],
    correction_requested:['Waiting for your correction decision','مستني قرارك في التصحيح'],
    patient_note:['Your doctor’s recorded plan. Send a message or photo below.','دي خطة دكتورك المسجلة. ابعت رسالة أو صورة تحت.'],
    no_plan:['No active medication is recorded.','لسه مفيش دوا حالي مسجل.'],
    no_requests:['No upcoming request is recorded.','لسه مفيش طلب جاي مسجل.'],
    no_reports:['No medication report recorded.','لسه مفيش كلام مسجل منك عن الدوا.'],
    no_questions:['No open questions recorded.','لسه مفيش أسئلة مستنية الرد.'],
    reports:['What you reported about your medication','اللي قلت لنا عليه بخصوص دواك'],
    last_reading:['Your last reported reading','آخر قراءة بعتها'], questions:['Your open questions','أسئلتك اللي مستنية رد'],
    waiting:['Waiting for your doctor','مستني رد دكتورك'], self_report:['Self-reported; this does not prove the medicine was taken.','ده حسب كلامك؛ مش دليل إن الدوا اتاخد.']
  });
  Object.assign(words, {
    patients_headline:['{N} patients, {M} need you now','{N} مريض، {M} محتاجينك دلوقتي'],
    patients_single:['One patient','مريض واحد'], patients_plural:['{N} patients','{N} مريض'],
    needs_none:['nobody needs you now','محدش محتاجك دلوقتي'], needs_one:['one needs you now','واحد محتاجك دلوقتي'],
    patients_subline:['Everything below is sorted by <b>what is urgent, then how long someone has been waiting for you</b>, not by name.','اللي تحت مترتب حسب <b>إيه المستعجل، وبعده مين مستنيك بقاله أطول</b>، مش حسب الاسم.'],
    find_patient:['Find a patient by name','دور على مريض بالاسم'], needs_me:['Needs me','محتاجني'], all_count:['All {N}','الكل {N}'], settled:['Settled','مفيش حاجة مستعجلة'],
    years_old:['{age} years old','عمره {age} سنة'], latest_patient:['Latest from the patient','آخر اللي وصل من المريض'],
    metric_when:['{metric}, {when}','{metric}، {when}'], medicines_count:['Medicines on the plan','الأدوية في الخطة'], requests_count:['Open requests','الطلبات المفتوحة'], documents_count:['Documents sent','المستندات اللي اتبعتت'],
    card_waiting:['One thing is waiting:','في حاجة مستنياك:'], card_late:['The patient is late:','المريض متأخر:'], card_settled:['Nothing is waiting.','مفيش حاجة مستنية.'],
    card_more:['{K} more on the full record','وفيه {K} كمان في الملف الكامل'], card_record:['Open the full record','افتح الملف الكامل'], card_question:['Answer the question','رد على السؤال'], card_document:['Open the document','افتح المستند'], row_open:['Open {name}','افتح {name}'],
    chip_still:['Still open','لسه مفتوح'], chip_respond:['Respond now','رد دلوقتي'], chip_result:['Read the result','اقرا النتيجة'], chip_decide:['Decide what next','حدد الخطوة الجاية'], chip_photo:['Ask for a new photo','اطلب صورة جديدة'], chip_contact:['Check contact','راجع التواصل'], chip_change:['Confirm the change','أكد التغيير'], chip_document:['Link the document','اربط المستند'], chip_link:['Check the link','راجع الربط'], chip_clarify:['Clarify the file','وضح الملف'], chip_cover:['Check cover','راجع مين بيتابع'], chip_nudge:['Nudge the patient','فكر المريض'], chip_confirm:['Confirm the request','أكد الطلب'], chip_today:['Due today','ميعاده النهارده'], chip_nothing:['Nothing needed','مفيش حاجة مطلوبة']
  });
  const t = key => words[key]?.[en ? 0 : 1] || words.not_recorded[en ? 0 : 1];
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const bdi = value => `<bdi>${esc(value)}</bdi>`;
  const $ = id => document.getElementById(id);
  const state = {records:[], reviews:[], evidence:[], pref:null, data:null, sort:'urgency', descending:false, page:1, query:'', filter:view==='patients'?'needs':'all', zone:browserZone};
  const params = new URLSearchParams(location.search);
  {state.query=params.get('q')||'';state.filter=params.get('filter')||state.filter;state.sort=params.get('sort')||'urgency';state.descending=params.get('desc')==='1';state.page=Math.max(1,Number(params.get('page'))||1);}
  if(!['patient','age','last_activity','urgency'].includes(state.sort))state.sort='urgency';
  if(view==='patients'){if(!['needs','all','settled','danger','pending_review','overdue','due_today'].includes(state.filter))state.filter='needs';state.sort='urgency';state.descending=false;}
  else if(!['all','danger','pending_review','overdue','due_today'].includes(state.filter))state.filter='all';
  let generation=0, currentAbort=null;
  const errorText = status => status === 401 || status === 403 ? t('expired') : status === 404 ? t('denied') : status === 409 ? t('stale') : t('failed');
  async function api(path, options={}) {
    if (demo && patient) return patientDemo(path, options);
    const response = await fetch(path,{credentials:demo?'omit':'same-origin',cache:'no-store',...options});
    if(!response.ok){const body=await response.json().catch(()=>({}));throw Object.assign(new Error(failureReasons[body.reason]||(path.startsWith('/api/evidence/')&&body.detail)||errorText(response.status)),{status:response.status});}
    return response.json();
  }
  function icon(name='status-icon') {return `<svg class="icon" viewBox="0 0 16 16" aria-hidden="true"><use href="#${name}"/></svg>`;}
  function badge(key, weight=tone(key)) {return `<span class="status ${weight}"><span>${esc(t(key))}</span></span>`;}
  function time(value, zone=state.zone, compact=false) {
    if(!value) return `<span class="muted">${t('no_due')}</span>`;
    if(/^\d{4}-\d{2}-\d{2}$/.test(value))return bdi(value);
    const date=new Date(value); if(Number.isNaN(+date)) return `<span class="muted">${t('no_due')}</span>`;
    const minutes=Math.round((+date-Date.now())/60000), abs=Math.abs(minutes);
    const unit=abs>=1440?'day':abs>=60?'hour':'minute', amount=unit==='day'?Math.trunc(minutes/1440):unit==='hour'?Math.trunc(minutes/60):minutes;
    const absolute=new Intl.DateTimeFormat(lang,{...(compact?{}:{year:'numeric',month:'short',day:'numeric'}),hour:'2-digit',minute:'2-digit',timeZone:zone}).format(date);
    const relative=new Intl.RelativeTimeFormat(lang,{numeric:'always'}).format(amount,unit);
    return `<time datetime="${esc(value)}">${bdi(absolute)}</time><small>${bdi(relative)}</small>`;
  }
  function reviewTime(value, now=Date.now(), zone=state.zone) {
    const date=new Date(value);if(!value||!Number.isFinite(+date))return t('no_due');
    const minutes=Math.trunc((+date-now)/60000),abs=Math.abs(minutes);
    if(abs<2880){const unit=abs>=1440?'day':abs>=60?'hour':'minute';const n=unit==='day'?Math.trunc(minutes/1440):unit==='hour'?Math.trunc(minutes/60):minutes;return new Intl.RelativeTimeFormat(en?'en':'ar',{numeric:'auto'}).format(n,unit);}
    return new Intl.DateTimeFormat(en?'en':'ar',{timeZone:zone,month:'short',day:'numeric'}).format(date);
  }
  const sentences = {
    incident_response:["Respond to the danger report{when}. Nobody has answered it yet.","رد على بلاغ الخطر{when}. لسه محدش رد عليه."],
    result_review:["Read the result{when} and tell the patient what it means.","اقرا النتيجة{when} وقول للمريض معناها إيه."],
    evidence_association:["A document arrived{when}. Say which request it belongs to, or that it is not this patient's.","وصل مستند{when}. حدد تابع لأنهي طلب، أو إنه مش بتاع المريض ده."],
    question_answer:["The patient asked a question{when}. Answer it.","المريض سأل سؤال{when}. رد عليه."],
    correction_disposition:["A correction to the record is waiting for your yes or no.","في تصحيح في الملف مستني موافقتك أو رفضك."],
    unmet_objective:['The patient missed "{title}"{when}. Decide: chase again, extend, or close it.','المريض متأخر في "{title}"{when}. قرر: نتابعه تاني، نمد الميعاد، أو نقفل الطلب.'],
    followup_disposition:["The follow-up on the new medicine needs your decision (it came back, ran late, or could not be sent).","متابعة الدوا الجديد محتاجة قرارك (ردها وصل، اتأخرت، أو معرفناش نبعتها)."],
    media_failure:["A photo the patient sent could not be read. Ask them to send it again.","معرفناش نقرا صورة المريض بعتها. اطلب منه يبعتها تاني."],
    intake_clarification:["A new patient's file needs one clarification before it is complete.","ملف مريض جديد محتاج توضيح واحد عشان يكمل."],
    delivery_patient:["A message to this patient did not arrive. Check how to reach them.","رسالة للمريض ده موصلتش. راجع طريقة التواصل معاه."],
    delivery_intake:["A message about a new patient's file did not arrive.","رسالة بخصوص ملف مريض جديد موصلتش."],
    delivery_doctor:["A message to you did not arrive.","رسالة ليك موصلتش."],
    binding_review:["Check this patient's link: who joined, or a conflict in what they asked for (stop, quiet hours).","راجع ربط المريض ده: مين انضم، أو تعارض في طلباته (وقف الرسايل، ساعات الهدوء)."],
    coverage_review:["Check who is covering these patients.","راجع مين بيتابع المرضى دول."],
    acknowledged:["You have seen this. It stays open until you reply.","إنت شفت ده. هيفضل مفتوح لحد ما ترد."],
    changed:["Something changed since you last looked.","في حاجة اتغيرت من آخر مرة بصيت."],
    resolved:["Reviewed on {date}: {kind} {resolution}.","اتراجع يوم {date}: {kind} {resolution}."],
    proposed:['"{title}" is proposed and waits for your confirmation.','"{title}" مقترح ومستني تأكيدك.'],
    awaiting_link:['"{title}" starts once the patient joins.','"{title}" يبدأ لما المريض ينضم.'],
    future:['Nothing to do yet: "{title}" is due {date}.','لسه مفيش حاجة تعملها: ميعاد "{title}" هو {date}.'],
    late:['"{title}" was due {date} and has not arrived.','ميعاد "{title}" كان {date} ولسه موصلش.'],
    paused_late:["Reminders are paused, so nobody is chasing it.","التذكيرات واقفة، فمحدش بيتابع الطلب."],
    no_due:['"{title}" is in progress; no due date was set.','"{title}" شغال؛ متحددلوش ميعاد.'],
    waiting_patient:['Sanad asked the patient about "{title}" and is waiting for their answer.','سند سأل المريض عن "{title}" ومستني رده.'],
    blocked:['The patient cannot do "{title}" yet.','المريض لسه مش قادر يعمل "{title}".'],
    unreachable:['"{title}" cannot reach the patient. Check how to contact them.','"{title}" مش بيوصل للمريض. راجع طريقة التواصل معاه.'],
    invalidated_pending_review:['"{title}" was marked done, then something changed. Confirm it is still done.','"{title}" اتسجل إنه تم، وبعدها حاجة اتغيرت. أكد إنه لسه تم.'],
    fulfilled:['"{title}": done on {date}.','"{title}": تم يوم {date}.'],
    closed_unfulfilled:['"{title}": closed without being done on {date}.','"{title}": اتقفل من غير ما يتم يوم {date}.'],
    cancelled:['"{title}": cancelled on {date}.','"{title}": اتلغى يوم {date}.'],
    superseded:['"{title}": replaced on {date}.','"{title}": اتبدل يوم {date}.'],
    processing:['"{title}": something arrived and is being checked.','"{title}": حاجة وصلت وبنتأكد منها.'],
    awaiting_anchor:["Day-three check on the new medicine will be scheduled once the start date is known.","متابعة اليوم التالت للدوا الجديد هتتحدد لما نعرف تاريخ البداية."],
    medication_scheduled:["Day-three check on the new medicine is on {date}. Nothing to do until then.","متابعة اليوم التالت للدوا الجديد يوم {date}. مفيش حاجة تعملها لحد ساعتها."],
    clinical_scheduled:["Check-in with the patient is on {date}.","متابعة المريض يوم {date}."],
    medication_waiting:["Sanad asked how the new medicine is going and is waiting for the patient's answer.","سند سأل عن الدوا الجديد ومستني رد المريض."],
    clinical_waiting:["Sanad asked the patient how they are doing and is waiting for their answer.","سند سأل المريض عامل إيه ومستني رده."],
    followup_overdue:["The follow-up was due {date} and has no answer yet.","ميعاد المتابعة كان {date} ولسه مفيش رد."],
    contact_suppressed:["The follow-up cannot be sent: contact with this patient is paused.","المتابعة مينفعش تتبعت: التواصل مع المريض ده واقف."],
    followup_fulfilled:["Follow-up done on {date}.","المتابعة تمت يوم {date}."],
    followup_cancelled:["Follow-up cancelled on {date}.","المتابعة اتلغت يوم {date}."],
    quiet_awaiting_link:["Has not joined Sanad yet. Nothing reaches them until they open the invitation.","لسه منضمش لسند. مفيش حاجة بتوصله لحد ما يفتح الدعوة."],
    quiet_paused:["All quiet. Reminders are paused by the patient.","كله هادي. المريض موقف التذكيرات مؤقتًا."],
    quiet_opted_out:["All quiet. The patient turned routine messages off.","كله هادي. المريض قفل الرسايل العادية."],
    quiet_unreachable:["Sanad cannot reach this patient. Check how to contact them.","سند مش قادر يوصل للمريض ده. راجع طريقة التواصل معاه."],
    quiet_frozen:["Contact with this patient is frozen.","التواصل مع المريض ده متجمد."],
    quiet:["All quiet. Nothing is waiting on you or on the patient.","كله هادي. مفيش حاجة مستنياك أو مستنية المريض."],
    hold:['{drug} is on hold since {date}: {reason}.','{drug} متوقف مؤقتًا من {date}: {reason}.']
  };
  function prose(key, values={}) {return sentences[key][en?0:1].replace(/\{(\w+)\}/g,(_,key)=>String(values[key]??''));}
  function sourceObject(r,record){
    const collections={mission:record.missions,followup:record.followups,correction:record.corrections,evidence:record.evidence||state.evidence,document:record.evidence||state.evidence,question:record.missions};
    return (collections[r.source_type]||[]).find(x=>(x.id===r.source_id||x.evidence_id===r.source_id)&&(!x.scope?.patient_id||x.scope.patient_id===record.patient_id));
  }
  function sourceDate(r,record){
    const source=['evidence','document'].includes(r.source_type)?(record.evidence||[]).find(e=>e.id===r.source_id||e.evidence_id===r.source_id):sourceObject(r,record);if(!source)return null;
    // An execution deadline and provider acceptance are never an arrival time.
    if(r.review_kind==='unmet_objective')return source.due_at;
    if(['result_review','evidence_association'].includes(r.review_kind))return ['evidence','document'].includes(r.source_type)?source.received_at||source.provenance?.received_at:null;
    if(r.review_kind==='question_answer')return source.asked_at||source.created_at;
    if(r.review_kind==='correction_disposition')return source.created_at;
    return null; // The record projection does not contain danger-report message times.
  }
  function sentence(item,record){
    if(!item)return prose('quiet_'+record.contact_status in sentences?'quiet_'+record.contact_status:'quiet');
    if(item.review){
      const r=item.review, source=sourceObject(r,record), date=sourceDate(r,record), when=date?reviewTime(date):'';
      if(r.state==='resolved'){
        const kinds={incident_response:'danger report',result_review:'result',evidence_association:'document',question_answer:'question',correction_disposition:'correction',unmet_objective:'missed deadline',followup_disposition:'follow-up',media_failure:'unreadable photo',intake_clarification:"new patient's file",delivery_failure:'undelivered message',binding_review:'patient link',coverage_review:'cover'};
        const reasons={doctor_reviewed:'reviewed by you',answered:'answered',closed:'closed',corrected:'corrected',patient_stopped:'closed because the patient stopped reminders',order_superseded:'closed because the instruction was replaced'};
        return prose('resolved',{date:reviewTime(r.resolved_at),kind:en?kinds[r.review_kind]:({incident_response:'بلاغ الخطر',result_review:'النتيجة',evidence_association:'المستند',question_answer:'السؤال',correction_disposition:'التصحيح',unmet_objective:'الميعاد اللي فات',followup_disposition:'المتابعة',media_failure:'الصورة اللي متقرتش',intake_clarification:'ملف المريض الجديد',delivery_failure:'الرسالة اللي موصلتش',binding_review:'ربط المريض',coverage_review:'متابعة المرضى'}[r.review_kind]),resolution:en?reasons[r.resolved_reason]||'closed':({doctor_reviewed:'إنت راجعته',answered:'اترد عليه',closed:'اتقفل',corrected:'اتصحح',patient_stopped:'اتقفل عشان المريض وقف التذكيرات',order_superseded:'اتقفل عشان التعليمات اتبدلت'}[r.resolved_reason]||'اتقفل')});
      }
      const key=r.state==='acknowledged'?'acknowledged':r.review_kind==='delivery_failure'?(intakeReview(r)?'delivery_intake':r.patient_id||r.scope?.patient_id||record.patient_id?'delivery_patient':'delivery_doctor'):r.review_kind;
      const prefix=r.review_kind==='incident_response'?(en?' from ':' من '):r.review_kind==='result_review'?(en?' that arrived ':' اللي وصلت '):r.review_kind==='unmet_objective'?(en?' (due ':' (ميعاده '):' ';
      const text=prose(key,{title:source?.title||'the request',when:date?prefix+when+(r.review_kind==='unmet_objective'?')':''):''});
      return text+(r.last_material_change_version>r.source_version?' '+prose('changed'):'');
    }
    if(item.followup){
      const f=item.followup, medication=f.kind==='MEDICATION_DAY3';
      const key=f.state==='scheduled'?(medication?'medication_scheduled':'clinical_scheduled'):f.state==='waiting_response'?(medication?'medication_waiting':'clinical_waiting'):['overdue','fulfilled','cancelled'].includes(f.state)?'followup_'+f.state:f.state;
      return prose(key,{date:reviewTime(f.state==='fulfilled'?f.fulfilled_at:f.state==='cancelled'?f.cancelled_at:f.due_at)});
    }
    const m=item.mission,key=stateOf(m), mapped={not_yet_due:'future',missing:'late',no_due_state:'no_due',overdue:'late'}[key]||key;
    const date=['fulfilled','cancelled','closed_unfulfilled','superseded'].includes(key)?m[key+'_at']:m.due_at;
    return prose(mapped,{title:m.title,date:reviewTime(date)})+(mapped==='late'&&['paused','opted_out'].includes(record.contact_status)?' '+prose('paused_late'):'');
  }
  function itemTarget(item,record){
    const r=item.review, type=r?.source_type;
    const tab=!r?'requests':['evidence','document'].includes(type)?'documents':type==='correction'?'history':'requests';
    const source=r?sourceObject(r,record):item.mission||item.followup;
    const rendered=source&&(!r||['mission','followup','correction','evidence','document'].includes(type));
    return `tab=${tab}`+(rendered?'&item='+encodeURIComponent(source.id):'');
  }
  function intakeReview(r) {
    return !r.patient_id&&!r.scope?.patient_id&&(r.source_type==='intake'||['media_failure','incident_response','intake_clarification'].includes(r.review_kind));
  }
  function reviewPresentation(r) {
    const intake=intakeReview(r), patientScope=Boolean(r.patient_id||r.scope?.patient_id);
    const table={
      result_review:['inbox_result','open_evidence','evidence'], evidence_association:['inbox_association','open_evidence','evidence'],
      question_answer:['inbox_question','answer_question','questions'], correction_disposition:['inbox_correction','open_history','history'],
      incident_response:intake?['inbox_intake_danger','intake_telegram']:['inbox_danger','open_record','plan'],
      unmet_objective:['inbox_deadline','open_requests','requests'], followup_disposition:['inbox_followup','open_requests','requests'],
      binding_review:['inbox_binding','open_requests','requests'], media_failure:intake?['inbox_media','intake_telegram']:['inbox_media','open_requests','requests'],
      intake_clarification:['inbox_intake','intake_telegram'],
      delivery_failure:intake?['inbox_intake_delivery','intake_telegram']:patientScope?['inbox_patient_delivery','open_requests','requests']:['inbox_doctor_delivery','review_telegram'],
      coverage_review:['inbox_coverage','review_telegram']
    };
    return table[r.review_kind]||[r.review_kind,'review_telegram'];
  }
  function inboxCard(record,item) {
    const r=item.review, [,action,tab]=reviewPresentation(r), scope=intakeReview(r)?'unassigned_intake':'your_account';
    const name=r.patient_id?record.display_name:t(scope);
    const evidence=state.evidence.find(e=>e.evidence_id===r.source_id&&e.scope.patient_id===record.patient_id);
    const href=tab==='questions'?'#questions':`${link(record)}#${itemTarget(item,record)}`;
    return `<details class="inbox-item" id="inbox-${esc(r.id)}" ${item.urgent||+new Date(r.review_at)<Date.now()?'open':''}><summary><span class="review-line">${esc(sentence(item,record))}</span><small>${bdi(name)}</small></summary>${tab?`<a class="button" href="${esc(href)}">${t(action)}</a>`:`<p class="review-action">${t(action)}</p>`}${evidence?evidenceActions(evidence):''}</details>`;
  }
  function stateOf(m) {
    if(m.fulfillment_validity==='invalidated_pending_review')return 'invalidated_pending_review';
    if(m.state!=='open')return m.state;
    if(m.processing)return 'processing';
    if(!m.due_at||!Number.isFinite(+new Date(m.due_at)))return 'no_due_state';
    return +new Date(m.due_at)>Date.now()?'not_yet_due':'overdue';
  }
  function tone(key) {return ['danger','incident_response','unverifiable'].includes(key)?'danger':['missing','overdue'].includes(key)?'warning':['processing','pending_review','pending','open','invalidated_pending_review'].includes(key)?'info':['fulfilled','resolved','satisfied','completed'].includes(key)?'success':'quiet';}
  const summaryLabels=Object.assign(Object.create(null),{danger:'Emergency',pending_review:'Waiting on you',overdue:'Patient is late',due_today:'Patient tasks due today'});
  function matchesSummary(item,filter){
    if(filter==='danger')return Boolean(item.urgent);
    if(filter==='pending_review')return Boolean(item.review)&&!item.urgent;
    if(!item.due||!Number.isFinite(+new Date(item.due)))return false;
    if(filter==='overdue')return !item.review&&+new Date(item.due)<Date.now();
    const calendar=new Intl.DateTimeFormat('en-CA',{timeZone:state.zone});
    return filter==='due_today'&&!item.review&&Boolean(item.mission||item.followup)&&+new Date(item.due)>=Date.now()&&calendar.format(new Date(item.due))===calendar.format(new Date());
  }

  function reviewRows(record, history=false) {return (history?record.review_history:record.reviews)||[];}
  function waitTime(item,record){return +(new Date(item?.review?sourceDate(item.review,record):item?.due))||Infinity;}
  function obligations(record) {
    const reviews=reviewRows(record).filter(r=>r.state!=='resolved').map(r=>({id:r.id,title:t(r.review_kind),due:r.review_at,status:r.state==='open'?'pending_review':r.state,urgent:r.review_kind==='incident_response',review:r}));
    const missions=(record.missions||[]).filter(m=>m.fulfillment_validity==='invalidated_pending_review'||!['fulfilled','cancelled','closed_unfulfilled','superseded'].includes(m.state)).map(m=>({id:m.id,title:m.title,due:m.due_at,status:stateOf(m),urgent:false,mission:m}));
    const followups=(record.followups||[]).filter(f=>!['fulfilled','cancelled'].includes(f.state)).map(f=>({id:f.id,title:t(f.kind),due:f.due_at,status:f.state,urgent:false,followup:f}));
    return [...reviews,...missions,...followups].sort((a,b)=>Number(b.urgent)-Number(a.urgent)||(waitTime(a,record)-waitTime(b,record))||a.id.localeCompare(b.id));
  }
  function link(record) {return demo?`/demo${location.search}#${encodeURIComponent(record.patient_id)}`:`/a/patients/${encodeURIComponent(record.patient_id)}`;}
  const fillWords=(key,values={})=>t(key).replace(/\{(\w+)\}/g,(_,name)=>String(values[name]??''));
  function needsMe(record){return obligations(record).some(item=>item.urgent||item.review||item.status==='overdue');}
  function patientRoster(){return state.records.filter(record=>!record.removed_at);}
  function patientHeadline(){
    const records=patientRoster(),n=records.length,m=records.filter(needsMe).length;
    const number=value=>en&&value<=20?['zero','one','two','three','four','five','six','seven','eight','nine','ten','eleven','twelve','thirteen','fourteen','fifteen','sixteen','seventeen','eighteen','nineteen','twenty'][value]:String(value);
    let headline=fillWords('patients_headline',{N:number(n),M:number(m)});
    if(n===1)headline=headline.replace(fillWords('patients_plural',{N:number(n)}),t('patients_single'));
    if(m<=1)headline=headline.slice(0,headline.indexOf(en?', ':'، ')+2)+t(m?'needs_one':'needs_none');
    return headline[0].toUpperCase()+headline.slice(1);
  }
  function patientChip(item,record){
    const r=item?.review;
    if(r){
      if(r.state==='acknowledged')return ['cool','chip_still'];
      return {incident_response:['red','chip_respond'],question_answer:['red','card_question'],result_review:['amber','chip_result'],unmet_objective:['amber','chip_decide'],followup_disposition:['amber','chip_decide'],media_failure:['amber','chip_photo'],delivery_failure:['amber','chip_contact'],correction_disposition:['cool','chip_change'],evidence_association:['cool','chip_document'],binding_review:['cool','chip_link'],intake_clarification:['cool','chip_clarify'],coverage_review:['cool','chip_cover']}[r.review_kind];
    }
    if(item){
      if(['overdue','missing'].includes(item.status))return ['amber','chip_nudge'];
      if(['blocked','unreachable','contact_suppressed'].includes(item.status))return ['amber','chip_contact'];
      if(['proposed','invalidated_pending_review'].includes(item.status))return ['cool','chip_confirm'];
      if(item.due&&+new Date(item.due)>Date.now()&&matchesSummary(item,'due_today'))return ['cool','chip_today'];
    }else if(['unreachable','frozen'].includes(record.contact_status))return ['amber','chip_contact'];
    return ['green','chip_nothing'];
  }
  function patientFacts(record){
    const readings=(record.missions||[]).flatMap(m=>{
      const d=m.details;if(d?.kind!=='MONITOR')return [];
      const all=d.readings||[];
      return all.filter((r,i)=>r.slot===null||!all.slice(i+1).some(later=>later.slot===r.slot)).map(r=>({label:fillWords('metric_when',{metric:clinicalLabel(d.metric),when:reviewTime(r.observed_at)}),value:r.value+' '+d.unit,at:r.observed_at}));
    }).sort((a,b)=>+new Date(b.at)-new Date(a.at)).slice(0,2);
    const facts=[...readings,...[[t('medicines_count'),(record.orders||[]).filter(o=>o.status==='active').length],[t('requests_count'),obligations(record).filter(x=>x.mission).length],[t('documents_count'),(record.evidence||[]).length]].map(([label,value])=>({label,value}))];
    return facts.map(({label,value})=>`<div class="kv"><span>${esc(label)}</span><b>${esc(value)}</b></div>`).join('');
  }
  function patientCard(record,item,count){
    const [colour,chip]=patientChip(item,record),name=record.display_name,description=sentence(item,record);
    const kind=item?.review?.review_kind,source=item?.review?.source_id;
    const extra=kind==='question_answer'?['card_question',demo?`/demo#patient=${encodeURIComponent(record.patient_id)}&tab=requests&item=${encodeURIComponent(source)}`:'/a/inbox#questions']:['result_review','evidence_association'].includes(kind)?['card_document',`${demo?'/demo#patient='+encodeURIComponent(record.patient_id)+'&':link(record)+'#'}tab=documents&item=${encodeURIComponent(source)}`]:null;
    return `<div class="row patient-row ${item?.urgent?'danger urgent':item?.due&&+new Date(item.due)<Date.now()?'warning':'calm'}" data-row role="button" tabindex="-1" aria-expanded="false"><div class="who2">${avatar(name)}<div><b>${esc(name)}</b>${record.age==null?'':`<small>${esc(fillWords('years_old',{age:record.age}))}</small>`}</div></div><div class="need patient-sentence">${esc(description)}</div><div class="when">${activity(record.last_activity_at)}</div><div><span class="chip ${colour}"><em></em>${esc(t(chip))}</span></div><button class="go" type="button" aria-label="${esc(fillWords('row_open',{name}))}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><path d="M8 5l7 7-7 7"/></svg></button></div><div class="detail"><div class="inner"><div><h4>${t('latest_patient')}</h4>${patientFacts(record)}</div><div class="ask"><p><b>${t(!needsMe(record)?'card_settled':item?.status==='overdue'?'card_late':'card_waiting')}</b> ${esc(description)}</p>${count>1?`<p>${esc(fillWords('card_more',{K:count-1}))}</p>`:''}<div class="acts"><a class="button primary" href="${esc(link(record))}">${t('card_record')}</a>${extra?`<a class="button" href="${esc(extra[1])}">${t(extra[0])}</a>`:''}</div></div></div></div>`;
  }
  function patientPager(total){
    const pages=Math.ceil(total/20);if(pages<=1)return '';
    const visible=[...new Set([1,pages,state.page-1,state.page,state.page+1])].filter(n=>n>=1&&n<=pages).sort((a,b)=>a-b);
    return `<div class="pager"><button id="previous" ${state.page===1?'disabled':''}>${t('previous')}</button>${visible.map((n,i)=>`${i&&n-visible[i-1]>1?'<span>…</span>':''}<button data-page="${n}" ${n===state.page?'aria-current="page" class="primary"':''}>${n}</button>`).join('')}<button id="next" ${state.page===pages?'disabled':''}>${t('next')}</button></div>`;
  }
  function patientsList(refresh=false){
    const records=patientRoster(),rows=tableRows();state.page=Math.min(state.page,Math.max(1,Math.ceil(rows.length/20)));
    const initial=!$('content').querySelector('.bar');
    remember();
    if(initial||refresh){
      const heading=document.querySelector('.page-heading h1');heading.textContent=patientHeadline();heading.classList.add('htitle');
      const header=heading.parentElement;header.classList.add('patients-heading');
      if(!header.querySelector('.hsub'))heading.insertAdjacentHTML('afterend',`<p class="hsub">${t('patients_subline')}</p>`);
    }
    if(initial){
      $('content').innerHTML=`<div class="bar toolbar"><label class="search"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9"><circle cx="11" cy="11" r="6.5"/><path d="M16 16l4 4"/></svg><input id="search" type="search" value="${esc(state.query)}" autocomplete="off" placeholder="${t('find_patient')}" aria-label="${t('find_patient')}"></label><div id="filter" class="seg" role="group" aria-label="${t('filter')}">${[['needs',t('needs_me')],['all',fillWords('all_count',{N:records.length})],['settled',t('settled')]].map(([key,label])=>`<button type="button" data-filter="${key}" aria-pressed="${state.filter===key}">${key==='all'?fillWords('all_count',{N:`<span data-count="${records.length}">${records.length}</span>`}):esc(label)}</button>`).join('')}</div></div><section class="card list work-surface"></section>`;
      $('search').addEventListener('input',event=>{state.query=event.target.value;state.page=1;patientsList();});
      $('filter').onclick=event=>{const button=event.target.closest('[data-filter]');if(!button)return;state.filter=button.dataset.filter;state.page=1;patientsList();button.focus();};
    }
    if(refresh&&!initial){
      $('content').querySelector('.summary-strip')?.remove();
      const count=$('filter').querySelector('[data-count]');count.dataset.count=records.length;counted.delete(count);countUp(count);
    }
    $('content').querySelector('.work-surface').innerHTML=rows.slice((state.page-1)*20,state.page*20).map(({record,item,count})=>patientCard(record,item,count)).join('')+(rows.length?'':emptyState(t(records.length?'empty':'no_patients'),t('refresh'),Boolean(state.query||state.filter!=='all')));
    $('content').querySelector('.pager')?.remove();
    $('content').insertAdjacentHTML('beforeend',patientPager(rows.length));
    document.querySelector('[data-clear]')?.addEventListener('click',()=>{state.query='';state.filter='all';state.page=1;$('search').value='';patientsList();$('search').focus();});
    const changePage=page=>{state.page=page;patientsList();document.querySelector('.pager [aria-current="page"]')?.focus();};
    if($('previous')){$('previous').onclick=()=>changePage(state.page-1);$('next').onclick=()=>changePage(state.page+1);}
    document.querySelectorAll('[data-page]').forEach(button=>button.onclick=()=>changePage(Number(button.dataset.page)));
    listPresentation(rows,initial||refresh);
    document.querySelectorAll('#filter [data-filter],[data-summary]').forEach(button=>{const pressed=String((button.dataset.filter||button.dataset.summary)===state.filter);if(button.getAttribute('aria-pressed')!==pressed)button.setAttribute('aria-pressed',pressed);});
    const elements=[...document.querySelectorAll('.patient-row')];
    let listPosition=scrollY;
    const toggle=row=>{const open=row.classList.contains('open');if(!open)listPosition=scrollY;elements.forEach(other=>{other.classList.remove('open');other.setAttribute('aria-expanded','false');});if(!open){row.classList.add('open');row.setAttribute('aria-expanded','true');}};
    elements.forEach(row=>{row.onclick=()=>toggle(row);row.addEventListener('keydown',event=>{if(['Enter',' '].includes(event.key)){event.preventDefault();toggle(row);}});const anchor=row.nextElementSibling.querySelector('a.primary');const rememberPosition=()=>{try{sessionStorage.setItem('sanad-list-return',location.pathname+location.search);sessionStorage.setItem('sanad-list-scroll',String(listPosition));}catch(_){}history.replaceState({...history.state,scroll:listPosition},'');};anchor.addEventListener('click',rememberPosition);anchor.addEventListener('auxclick',rememberPosition);});
    if(initial||refresh){auroraPresentation();$('content').querySelectorAll('[data-count]').forEach(countUp);}
  }
  function tableRows() {
    let rows=[];
    for(const record of view==='patients'?patientRoster():state.records){
      const work=obligations(record);
      if(view==='inbox'||view==='history') for(const r of reviewRows(record,view==='history')) rows.push({record,item:{id:r.id,title:t(r.review_kind),due:r.review_at,status:r.state,urgent:r.review_kind==='incident_response'&&r.state!=='resolved',review:r},count:1});
      else rows.push({record,item:(state.filter in summaryLabels?work.find(x=>matchesSummary(x,state.filter)):work[0])||null,count:work.length});
    }
    if(view==='inbox'||view==='history')for(const r of state.reviews.filter(r=>!r.patient_id)){rows.push({record:{patient_id:'',display_name:t('unassigned'),reviews:[],missions:[]},item:{id:r.id,title:t(r.review_kind),due:r.review_at,status:r.state,urgent:r.review_kind==='incident_response'&&r.state!=='resolved',review:r},count:1});}
    if(view==='patients')return rows.filter(({record})=>record.display_name.toLocaleLowerCase().includes(state.query.toLocaleLowerCase())&&(state.filter==='all'||(state.filter in summaryLabels?obligations(record).some(item=>matchesSummary(item,state.filter)):needsMe(record)===(state.filter==='needs')))).sort((a,b)=>Number(Boolean(b.item?.urgent))-Number(Boolean(a.item?.urgent))||(waitTime(a.item,a.record)-waitTime(b.item,b.record))||a.record.patient_id.localeCompare(b.record.patient_id));
    rows=rows.filter(({record,item})=>(`${record.display_name} ${sentence(item,record)}`).toLocaleLowerCase().includes(state.query.toLocaleLowerCase())&&(
      state.filter==='all'||state.filter===record.contact_status||(state.filter in summaryLabels&&item&&matchesSummary(item,state.filter))));
    const val=row=>state.sort==='patient'?row.record.display_name:state.sort==='age'?(Number(row.record.age)||0):state.sort==='last_activity'?(+new Date(row.record.last_activity_at)||0):waitTime(row.item,row.record);
    rows.sort((a,b)=>{if((view==='inbox'||state.sort==='urgency')&&Boolean(a.item?.urgent)!==Boolean(b.item?.urgent))return Number(Boolean(b.item?.urgent))-Number(Boolean(a.item?.urgent));const av=val(a),bv=val(b); const cmp=typeof av==='string'?av.localeCompare(String(bv),lang):(av===bv?0:av<bv?-1:1);return (state.descending?-cmp:cmp)||a.record.patient_id.localeCompare(b.record.patient_id)||(a.item?.id||'').localeCompare(b.item?.id||'');});
    return rows;
  }
  function remember() {
    const q=new URLSearchParams();if(state.query)q.set('q',state.query);if(state.filter!==(view==='patients'?'needs':'all'))q.set('filter',state.filter);if(view!=='patients'){q.set('sort',state.sort);if(state.descending)q.set('desc','1');}q.set('page',state.page);
    history.replaceState({...history.state,scroll:scrollY},'',`${location.pathname}?${q}${location.hash}`);
  }
  function list() {
    if(view==='patients'){patientsList();return;}
    const rows=tableRows();state.page=Math.min(state.page,Math.max(1,Math.ceil(rows.length/50)));
    const columns=['patient','age','urgency','last_activity'];
    $('content').innerHTML=`<div class="toolbar"><label class="search">${t('search')}<input id="search" type="search" value="${esc(state.query)}" autocomplete="off"></label><div class="filter-control"><span id="filter-label" class="visually-hidden">${t('filter')}</span><div id="filter" class="seg" role="group" aria-labelledby="filter-label">${['all','danger','pending_review','overdue','due_today'].map(k=>`<button type="button" data-filter="${k}" aria-pressed="${state.filter===k}">${summaryLabels[k]||t(k)}</button>`).join('')}</div></div></div><div class="filter-summary"><span id="result-count">${rows.length} ${t('results')} · ${summaryLabels[state.filter]||t(state.filter)}${state.query?' · '+bdi(state.query):''}</span><button id="clear">${t('clear')}</button></div><div class="work-surface"><table class="clinical" role="table"><caption>${t(view==='patients'?'outstanding':view)}. ${t('sort_help')}</caption><colgroup>${columns.map(()=>'<col>').join('')}</colgroup><thead><tr role="row">${columns.map(k=>`<th role="columnheader" scope="col" ${state.sort===k?`aria-sort="${state.descending?'descending':'ascending'}"`:''}><button data-sort="${k}">${k==='last_activity'?'Last activity':k==='urgency'?'What to do and why (urgency)':t(k)} <svg class="icon sort-chevron" viewBox="0 0 16 16" aria-hidden="true"><use href="#chevron-icon"/></svg></button></th>`).join('')}</tr></thead><tbody>${rows.slice((state.page-1)*50,state.page*50).map(({record,item,count})=>`<tr role="row" class="patient-row ${item?.urgent?'danger urgent':item?.due&&+new Date(item.due)<Date.now()?'warning':'calm'}"><td role="cell"><span class="stack-label">${t('patient')}</span>${record.patient_id?`<a data-record href="${esc(link(record))}">${bdi(record.display_name)}</a>`:`<span>${t('unassigned')}</span>`}</td><td role="cell" class="age"><span class="stack-label">${t('age')}</span><span class="age-value ${record.age==null?'muted':''}">${bdi(record.age??t('not_recorded'))}</span></td><td role="cell"><span class="stack-label">${t('outstanding')}</span><span class="patient-sentence">${esc(sentence(item,record))}</span>${count>1?`<small>and ${count-1} more</small>`:''}</td><td role="cell" class="muted"><span class="stack-label">Last activity</span>${activity(record.last_activity_at)}</td></tr>`).join('')}</tbody></table>${!rows.length?`${emptyState(t(state.records.length?'empty':'no_patients'),t('refresh'),Boolean(state.query||state.filter!=='all'))}`:''}</div><div class="pager"><button id="previous" ${state.page<=1?'disabled':''}><span class="turn-arrow" aria-hidden="true">←</span> ${t('previous')}</button><span>${t('page')} ${state.page} ${t('of')} ${Math.max(1,Math.ceil(rows.length/50))}</span><button id="next" ${state.page*50>=rows.length?'disabled':''}>${t('next')} <span class="turn-arrow" aria-hidden="true">→</span></button></div>`;
    $('search').addEventListener('input',e=>{const start=e.target.selectionStart;state.query=e.target.value;state.page=1;list();$('search').focus();$('search').setSelectionRange(start,start);});
    document.querySelector('[data-clear]')?.addEventListener('click',()=>$('clear').click());
    $('filter').onclick=e=>{const button=e.target.closest('[data-filter]');if(!button)return;state.filter=button.dataset.filter;state.page=1;list();document.querySelector('#filter [aria-pressed=true]').focus();};
    $('clear').onclick=()=>{state.query='';state.filter='all';state.page=1;list();$('search').focus();};
    for(const button of document.querySelectorAll('[data-sort]'))button.onclick=()=>{state.descending=state.sort===button.dataset.sort?!state.descending:false;state.sort=button.dataset.sort;state.page=1;list();document.querySelector(`[data-sort="${state.sort}"]`).focus();};
    $('previous').onclick=()=>{state.page--;list();$('next').focus();};$('next').onclick=()=>{state.page++;list();$('previous').focus();};remember();
    listPresentation(rows);
    for(const row of document.querySelectorAll('.patient-row')){
      const anchor=row.querySelector('[data-record]');if(!anchor)continue;
      const rememberPosition=()=>{try{sessionStorage.setItem('sanad-list-return',location.pathname+location.search);sessionStorage.setItem('sanad-list-scroll',String(scrollY));}catch(_){}history.replaceState({...history.state,scroll:scrollY},'');};
      anchor.addEventListener('click',rememberPosition);anchor.addEventListener('auxclick',rememberPosition);
      const follow=e=>{if(e.target.closest('a'))return;if(e.button!==0&&e.button!==1)return;rememberPosition();anchor.dispatchEvent(new MouseEvent(e.type,{bubbles:true,cancelable:true,view:window,button:e.button,ctrlKey:e.ctrlKey,metaKey:e.metaKey,shiftKey:e.shiftKey,altKey:e.altKey}));};
      row.onclick=follow;row.onauxclick=follow;
    }
    if(view==='inbox'){questions();bindEvidenceActions();}
    auroraPresentation();
  }
  function activity(value){
    if(!value)return t('not_recorded');
    const minutes=Math.floor((Date.now()-new Date(value))/60000), amount=Math.abs(minutes);
    const [number,unit]=amount<60?[minutes,'minute']:amount<1440?[Math.floor(minutes/60),'hour']:[Math.floor(minutes/1440),'day'];
    return `<time datetime="${esc(value)}">${esc(new Intl.RelativeTimeFormat(lang,{numeric:'auto'}).format(-number,unit))}</time>`;
  }
  function section(title, content){return `<section class="section${patient&&title==='reminders'?' remind':''}"><h2>${t(title)}</h2>${content}</section>`;}
  function empty(key='empty'){return emptyState(key==='empty'?t('not_recorded')+'.':t(key), patient?t('patient_note'):t('refresh'));}
  function emptyState(sentence, next, clear=false){
    if(next===t('refresh'))next='Refresh to see the latest information.';
    return `<div class="empty">${icon('review-icon')}<p class="empty-title">${esc(sentence)}</p><p class="empty-next">${esc(next)}</p>${clear?`<button class="secondary" data-clear>${t('clear')}</button>`:''}</div>`;
  }
  function provenance(value){if(!value)return ''; const rows=Array.isArray(value)?value:[value];return rows.filter(p=>p.received_at).map(p=>`<p class="provenance">${t('received')}: ${time(p.received_at)}</p>`).join('');}
  function clinicalLabel(value){const text=String(value??'');return words[text]?t(text):text.includes('_')?t('not_recorded'):text;}
  function instruction(order){const i=order.structured_instruction||{};const comparisons={gt:'above',ge:'at least',lt:'below',le:'at most'};return ['drug','dose','frequency','timing','route','duration','text','metric','comparator','threshold','unit'].filter(k=>i[k]!==undefined&&i[k]!==null&&i[k]!=='').map(k=>bdi(k==='comparator'?comparisons[i[k]]||t('not_recorded'):k==='metric'?clinicalLabel(i[k]):i[k])).join(' · ');}
  function monitor(m){const d=m.details||{};if(d.kind!=='MONITOR')return '';const clock=value=>new Intl.DateTimeFormat('en-GB',{timeZone:state.zone,hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).format(new Date(value));const history=(m.schedule_history||[]).map(h=>`<p class="schedule-history">${esc(`Patient changed reading times from ${h.old.map(clock).join(', ')} to ${h.new.map(clock).join(', ')}, starting ${new Intl.DateTimeFormat('en-CA',{timeZone:state.zone}).format(new Date(h.new[0]))}.`)}</p>`).join('');return `${history}<table class="reading-table" role="table"><caption>${t('monitoring')} · ${bdi(clinicalLabel(d.metric))} · ${bdi(d.unit)}</caption><thead><tr><th scope="col">${t('slot')}</th><th scope="col">${t('reading')}</th><th scope="col">${t('source')}</th></tr></thead><tbody>${(d.slots||[]).map((slot,index)=>{const r=(d.readings||[]).findLast(r=>r.slot===index);return `<tr role="row"><th role="rowheader" scope="row">${time(slot)}</th><td role="cell"><span class="stack-label">${t('reading')}</span>${r?bdi(r.value)+' '+bdi(d.unit):badge(+new Date(slot)>Date.now()?'not_yet_due':'missing')}</td><td role="cell"><span class="stack-label">${t('source')}</span>${r?`${t('received')}<small>${t('received')}: ${time(r.received_at)}</small>`:t('missing')}</td></tr>`;}).join('')}</tbody></table>${(d.readings||[]).some(r=>r.slot===null)?`<h3>${t('extras')}</h3>${d.readings.filter(r=>r.slot===null).map(r=>`<p>${bdi(r.value)} ${bdi(d.unit)} ${time(r.observed_at)}</p>`).join('')}`:''}`;}
  function holds(record){return (record.held_medications||[]).map(h=>`<article class="record-item" data-held-order="${esc(h.order_id)}"><h3>On hold</h3><p>${esc(prose('hold',{drug:h.drug,date:reviewTime(h.since),reason:h.reason||'no reason given'}))}</p></article>`).join('');}
  function joining(record){
    const lines={paused:'Reminders paused by the patient',opted_out:'The patient turned routine messages off',unreachable:'Sanad cannot reach this patient',frozen:'Contact frozen'};
    if(lines[record.contact_status])return lines[record.contact_status];
    const confirmed=(record.bindings||[]).find(b=>b.confirmed_at);
    return record.contact_status==='active'&&confirmed?'Joined on '+reviewTime(confirmed.confirmed_at):'Has not joined yet: nothing reaches them until they open the invitation';
  }
  function evidenceActions(e){return demo?'':`<div data-evidence-actions="${esc(e.evidence_id)}">${(e.actions||[]).map((a,i)=>`<button data-evidence-action="${i}">${esc(a.label)}</button>`).join('')}<div data-evidence-confirm></div></div>`;}
  function bindEvidenceActions(root=document){
    root.querySelectorAll('[data-evidence-actions]').forEach(container=>{
      const e=state.evidence.find(e=>e.evidence_id===container.dataset.evidenceActions);if(!e)return;
      container.querySelectorAll('[data-evidence-action]').forEach(button=>button.onclick=()=>{
        const action=e.actions[Number(button.dataset.evidenceAction)], target=container.querySelector('[data-evidence-confirm]');
        const command={command_id:crypto.randomUUID(),evidence_version:e.version,...(action.action==='associate'?{mission_id:action.mission_id}:{})};
        target.innerHTML=`<form><p>${esc(action.label)}</p>${action.action==='reject'?'<label>Reason<textarea name="reason" required maxlength="1000"></textarea></label>':''}<button class="primary">Confirm</button><button type="button" data-cancel>Cancel</button><p role="alert"></p></form>`;
        target.querySelector('[data-cancel]').onclick=()=>target.replaceChildren();
        target.querySelector('form').onsubmit=async event=>{event.preventDefault();const submit=target.querySelector('.primary');submit.disabled=true;
          if(action.action==='reject')command.reason=target.querySelector('textarea').value;
          try{const result=await api(`/api/evidence/${encodeURIComponent(e.evidence_id)}/${action.action.replace('_','-')}`,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':decodeURIComponent(document.cookie.split('; ').find(x=>x.startsWith('sanad_csrf='))?.slice(11)||'')},body:JSON.stringify(command)});await load();toast(result.detail);}
          catch(error){target.querySelector('[role=alert]').textContent=error.message;submit.disabled=false;}
        };
      });
    });
  }
  function detail(record){
    const orders=(record.orders||[]).filter(o=>o.status==='active');
    const consentHTML=`<div class="consent-binding"><h3>Consent and joining</h3>${record.contact_preferences?`<p>${t('reminders')}: ${esc(t(record.contact_preferences.reminders))} · ${t('quiet')}: ${bdi(record.contact_preferences.quiet_hours.join(' to ')||t('not_recorded'))} · ${bdi(record.contact_preferences.timezone)}</p>`:''}${(record.consents||[]).map(c=>`<p>Agreed to Sanad messages on ${esc(reviewTime(c.accepted_at))} (consent version ${bdi(c.version)})</p>${c.withdrawn_at?`<p>Withdrew consent on ${esc(reviewTime(c.withdrawn_at))}</p>`:''}`).join('')||'<p>No consent recorded.</p>'}</div>`;
    const ordersHTML=orders.map(o=>`<article class="record-item" data-order="${esc(o.id)}"><p>${instruction(o.current_version||{})}</p><small>${t(o.status)}</small>${provenance(o.current_version?.provenance)}${!demo&&!record.removed_at?`<button data-amend="${esc(o.id)}">Amend instruction</button>`:''}<details><summary>${t('order_history')}</summary>${(o.history||[]).filter(h=>h.id!==o.current_version?.id).map(h=>`<p>${t('historical')} · ${instruction(h)}</p>${provenance(h.provenance)}`).join('')||empty()}</details></article>`).join('')+holds(record)||empty('no_plan');
    const missions=(record.missions||[]).map(m=>`<article class="record-item" data-mission="${esc(m.id)}"><h3>${bdi(m.title)}</h3>${badge(stateOf(m),tone(stateOf(m)))}${['fulfilled','cancelled','closed_unfulfilled','superseded'].includes(stateOf(m))?`<p>${esc(sentence({mission:m},record))}</p>`:''} <p>${t('due')}: ${time(m.due_at)}</p>${monitor(m)}${!demo&&!record.removed_at&&['fulfilled','cancelled','closed_unfulfilled'].includes(m.state)?`<button data-reopen="${esc(m.id)}">Preview reopening</button>`:''}</article>`).join('')||empty('no_requests');
    const followups=(record.followups||[]).map(f=>`<article class="record-item" data-followup="${esc(f.id)}"><h3>${esc(t(f.kind))}</h3>${badge(f.state)}${['fulfilled','cancelled'].includes(f.state)?`<p>${esc(sentence({followup:f},record))}</p>`:''}<p>${t('due')}: ${time(f.due_at)}</p></article>`).join('');
    const facts=(record.facts||[]).map(f=>`<article class="record-item" data-fact="${esc(f.id)}"><p>${bdi(f.payload?.text||f.payload?.clinical_en||f.text||t('retained'))}</p>${provenance(f.provenance)}${!demo?`<button data-correct-fact="${esc(f.id)}">Correct or detach</button>`:''}</article>`).join('')||empty();
    const evidence=state.evidence.map(e=>`<article class="record-item" data-evidence="${esc(e.id)}"><h3>${esc(t(e.category))}</h3>${badge(e.association_state)}<p>${t('printed')}: ${bdi(e.printed_date||t('not_recorded'))}</p><p>${t('received')}: ${time(e.provenance?.received_at)}</p>${(e.extracted_values||[]).map(v=>`<p>${bdi(clinicalLabel(v.name||v.analyte||''))} · ${v.dose?bdi(v.dose):bdi(v.value??t('not_recorded'))} ${v.dose?'':bdi(v.unit||t('not_recorded'))}${v.frequency?' · '+bdi(v.frequency):''}</p>`).join('')}${provenance(e.provenance)}${evidenceActions(e)}${!demo&&['accepted','detached'].includes(e.association_state)?`<button data-correct-evidence="${esc(e.id)}">Correct or detach</button>`:''}</article>`).join('')||empty('no_evidence');
    const reviews=reviewRows(record,true).map(r=>`<article class="record-item" data-review="${esc(r.id)}"><p>${esc(sentence({review:r},record))}</p></article>`).join('')||empty();
    $('content').innerHTML=`<a class="back" id="back" href="${demo?'/demo':'/a'}"><span class="turn-arrow" aria-hidden="true">←</span> ${t('back')}</a><div class="detail-grid"><div>${section('plan',ordersHTML+consentHTML)}${section('missions',missions+(followups?`<h3>${t('followups')}</h3>${followups}`:''))}${section('facts',facts)}</div><div>${section('history',reviews)}${section('evidence',evidence)}${section('source_files',(!demo?record.media||[]:[]).map(m=>`<article class="record-item"><a data-media="${esc(m.mime||'')}" data-captured="${esc(m.date||'')}" href="/api/patients/${encodeURIComponent(record.patient_id)}/media/${encodeURIComponent(m.media_id)}">${t('original')} · ${m.uploaded_by_you?'Uploaded by you on '+esc(new Intl.DateTimeFormat(lang,{dateStyle:'medium',timeZone:state.zone}).format(new Date(m.date))):esc(t(m.kind))}</a><p>${time(m.date)}</p></article>`).join('')||empty())}</div></div>`;
    if(!demo) correctionControls(record);
    else $('content').insertAdjacentHTML('beforeend',`<section class="section correction-timeline"><h2>Corrections and doctor decisions</h2>${(record.corrections||[]).map(c=>`<article class="record-item" data-correction="${esc(c.id)}"><p>${correctionProse(c)}</p></article>`).join('')||empty('no_corrections')}</section>`);
    document.querySelectorAll('[data-mission],[data-followup],[data-evidence],[data-correction]').forEach(element=>{element.id=element.dataset.mission||element.dataset.followup||element.dataset.evidence||element.dataset.correction;element.tabIndex=-1;});
    recordAnatomy($('content'));
    detailPresentation(record);
    if(!demo) removalControls(record);
    bindEvidenceActions();
    if(!demo){try{const saved=sessionStorage.getItem('sanad-list-return');if(saved&&/^\/a(?:\/(?:inbox|history))?(?:\?|$)/.test(saved))$('back').href=saved;}catch(_){}}
    $('back').onclick=e=>{if(!demo){try{const saved=sessionStorage.getItem('sanad-list-return');if(saved&&/^\/a(?:\/(?:inbox|history))?(?:\?|$)/.test(saved)){e.currentTarget.href=saved;}}catch(_){}}};
  }
  function removalControls(record){
    const grid=document.querySelector('.detail-grid'), words=record.removal_words;
    if(!words)return;
    if(record.removed_at){
      const banner=document.createElement('p');banner.className='removed-banner';
      banner.textContent=words.banner.replace('{date}',new Intl.DateTimeFormat(lang,{dateStyle:'medium',timeStyle:'short',timeZone:state.zone}).format(new Date(record.removed_at)));
      grid.before(banner);return;
    }
    const control=document.createElement('div');control.className='removal';
    control.innerHTML=`<button type="button">${esc(words.open)}</button>`;grid.after(control);
    const open=()=>{
      if(control.querySelector('form'))return;
      control.innerHTML=`<form><p>${esc(words.confirm)}</p><label>${esc(words.name)}<input name="name" autocomplete="off" required></label><p role="status"></p><button type="submit">${esc(words.remove)}</button><button type="button" data-keep>${esc(words.keep)}</button></form>`;
      const form=control.querySelector('form'), input=form.elements.name, commandId=crypto.randomUUID();
      control.querySelector('[data-keep]').onclick=()=>{control.innerHTML=`<button type="button">${esc(words.open)}</button>`;control.firstChild.onclick=open;control.firstChild.focus();};
      form.onsubmit=async event=>{
        event.preventDefault();form.querySelector('[type="submit"]').disabled=true;
        try{
          const result=await api(`/api/patients/${encodeURIComponent(record.patient_id)}/remove`,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':decodeURIComponent((document.cookie.split('; ').find(v=>v.startsWith('sanad_csrf='))||'').split('=')[1]||'')},body:JSON.stringify({expected_version:record.profile_version,command_id:commandId,name:input.value})});
          await load();
          const message=document.createElement('p');message.setAttribute('role','status');message.textContent=result.message||words.result;document.querySelector('.detail-grid').after(message);
        }catch(error){form.querySelector('[role="status"]').textContent=error.message;form.querySelector('[type="submit"]').disabled=false;}
      };input.focus();
    };
    control.firstChild.onclick=open;if(location.hash==='#remove')open();
  }
  function recordAnatomy(root){
    root.querySelectorAll('.record-item').forEach(item=>{
      item.querySelectorAll(':scope > p').forEach(line=>{if(line.querySelector('time'))line.classList.add('record-meta');});
      const chips=[...item.querySelectorAll(':scope > .status')];
      if(chips.length){const row=document.createElement('div');row.className='chip-row';row.append(...chips);item.prepend(row);}
    });
  }
  function correctionControls(record, host=null, refreshed=null) {
    const ref=(kind,value)=>({entity_type:kind,id:value.id,version:value.version});
    const send=action=>api(`/api/patients/${encodeURIComponent(record.patient_id)}/corrections`,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':decodeURIComponent((document.cookie.split('; ').find(v=>v.startsWith('sanad_csrf='))||'').split('=')[1]||'')},body:JSON.stringify({command_id:crypto.randomUUID(),expected_binding_epoch:record.correction_authority.binding_epoch,expected_delivery_epoch:record.correction_authority.delivery_epoch,action})});
    const panel=document.createElement('section');panel.className='section correction-timeline';
    panel.innerHTML='<h2>Corrections and doctor decisions</h2>'+`<div>${(record.orders||[]).filter(o=>o.status==='stopped').map(o=>`<article data-stopped-order="${esc(o.id)}"><p>${instruction(o.current_version)} · stopped</p>${time(o.current_version?.confirmed_at)}</article>`).join('')}</div>`+`<details><summary>Retained fact history and detached records</summary>${(record.fact_history||[]).map(f=>`<article data-fact-history="${esc(f.id)}"><p>${bdi(f.payload?.text||'')}</p>${provenance(f.provenance)}${!(record.facts||[]).some(v=>v.id===f.id)&&(record.correctable_facts||[]).some(v=>v.id===f.id)?`<button data-correct-fact="${esc(f.id)}">Correct detached record</button>`:''}</article>`).join('')}</details>`+(record.corrections||[]).map(c=>`<article class="record-item" data-correction="${esc(c.id)}"><p class="timeline-date">${c.created_at?bdi(new Intl.DateTimeFormat(lang,{dateStyle:'medium',timeZone:state.zone}).format(new Date(c.created_at))):t('not_recorded')}</p><p class="correction-notice">${correctionProse(c)}</p>${[...(record.reviews||[]),...(record.review_history||[])].filter(r=>r.source_type==='correction'&&r.source_id===c.id).map(r=>`${c.affected_mission_ids?.length?`<button data-validate="${esc(c.id)}">Review and validate current evidence</button>`:''}${r.state!=='resolved'?`<button data-response="${esc(c.id)}">Record doctor follow-up decision</button>`:''}`).join('')}</article>`).join('');
    if(!(record.corrections||[]).length)panel.insertAdjacentHTML('beforeend',empty('no_corrections'));
    if(!host)$('content').appendChild(panel);
    document.getElementById('correction-dialog')?.remove();const dialog=document.createElement('dialog');dialog.id='correction-dialog';dialog.innerHTML='<form><h2>Review change</h2><div class="change-fields"></div><label>Reason<textarea name="reason" required maxlength="1000"></textarea></label><p class="change-error" role="alert"></p><button type="submit" class="primary">Confirm correction</button> <button type="button" class="cancel">Cancel</button></form>';document.body.appendChild(dialog);
    const form=dialog.querySelector('form'), fields=dialog.querySelector('.change-fields'), submit=form.querySelector('[type=submit]');
    dialog.querySelector('.cancel').onclick=()=>dialog.close();
    const open=(html,action,label='Confirm correction',needsReason=true)=>{
      fields.innerHTML=html;
      form.elements.reason.closest('label').hidden=!needsReason;
      form.elements.reason.disabled=!needsReason;
      dialog.querySelector('.change-error').textContent='';
      submit.textContent=label;
      form.onsubmit=async e=>{
        e.preventDefault();submit.disabled=true;
        try{
          const outcome=await action(new FormData(form));
          if(outcome?.status!=='accepted'||!Array.isArray(outcome.offers)||outcome.offers.length>1)throw new Error(t('failed'));
          if(outcome.offers.length===0){
            dialog.close();
            if(refreshed)await refreshed();else await load();
            toast('Change recorded.',link(record));
            return;
          }
          const offer=outcome.offers[0];
          open(`<p>${esc(offer.preview)}</p>`,()=>send({type:'ConfirmReopen',offer_ref:ref('correction_offer',offer)}),'Confirm reopening',false);
        }catch(error){dialog.querySelector('.change-error').textContent=error.message;}
        finally{submit.disabled=false;}
      };
      if(!dialog.open)dialog.showModal();
    };
    const change=(kind,value)=>{
      let html=`<p>Original retained: ${bdi(kind==='evidence'?value.extracted_values.map(v=>`${clinicalLabel(v.name||v.analyte)}: ${v.value||''} ${v.unit||''}`).join('; '):value.payload.text)}</p><label>Action<select name="operation"><option value="replace">Correct a value</option><option value="detach">Detach from current record</option></select></label>`;
      if(kind==='evidence') html+=`<label>Reading<select name="row_index">${value.extracted_values.map((v,i)=>`<option value="${i}">${esc(clinicalLabel(v.name||v.analyte))}</option>`).join('')}</select></label><label>Correct value<input name="value" value="${esc(value.extracted_values[0]?.value||'')}"></label><label>Unit<input name="unit" value="${esc(value.extracted_values[0]?.unit||'')}"></label>`;
      else html+=`<label>Correct text<textarea name="text">${esc(value.payload.text)}</textarea></label>${value.payload.analyte?`<label>Value<input name="value" value="${esc(value.payload.value||'')}"></label><label>Unit<input name="unit" value="${esc(value.payload.unit||'')}"></label>`:''}`;
      open(html,data=>{const detach=data.get('operation')==='detach';const changes=detach?{}:kind==='evidence'?{value:String(data.get('value')),unit:String(data.get('unit'))||null}:{text:String(data.get('text')),...(value.payload.analyte?{value:String(data.get('value')),unit:String(data.get('unit'))||null}:{})};return send({type:'CorrectRecord',predecessor:ref(kind==='evidence'?'evidence':'clinical_fact',value),operation:detach?'detach':'replace',reason:String(data.get('reason')),changes,...(!detach&&kind==='evidence'?{row_index:Number(data.get('row_index'))}:{})});});
      const select=form.elements.row_index;if(select)select.onchange=()=>{const v=value.extracted_values[Number(select.value)];form.elements.namedItem('value').value=v.value||'';form.elements.unit.value=v.unit||'';};
    };
    for(const b of document.querySelectorAll('[data-correct-fact]'))b.onclick=()=>{const f=(record.correctable_facts||record.facts).find(f=>f.id===b.dataset.correctFact);if(f.correction_evidence_id)change('evidence',state.evidence.find(e=>e.id===f.correction_evidence_id));else change('clinical_fact',f);};
    for(const b of document.querySelectorAll('[data-correct-evidence]'))b.onclick=()=>change('evidence',state.evidence.find(f=>f.id===b.dataset.correctEvidence));
    for(const b of (host||document).querySelectorAll('[data-amend]'))b.onclick=()=>{const o=record.orders.find(o=>o.id===b.dataset.amend).current_version;open(`<p>Earlier provider-accepted instructions cannot be unsent. This saves a new instruction version.</p>${['dose','frequency','duration','timing'].map(k=>`<label>${esc(k)}<input name="${k}" value="${esc(o.structured_instruction[k]||'')}"></label>`).join('')}`,data=>send({type:'AmendOrder',predecessor:ref('care_order_version',o),reason:String(data.get('reason')),changes:Object.fromEntries(['dose','frequency','duration','timing'].filter(k=>(o.structured_instruction[k]||'')!==String(data.get(k))).map(k=>[k,String(data.get(k))||null]))}));};
    for(const b of document.querySelectorAll('[data-reopen]'))b.onclick=()=>{const m=record.missions.find(m=>m.id===b.dataset.reopen);open('<label>New deadline (your browser timezone)<input type="datetime-local" name="due" required></label>',data=>send({type:'PreviewReopen',mission_ref:ref('mission',m),due_at:new Date(String(data.get('due'))).toISOString(),reason:String(data.get('reason'))}),'Preview what will resume');};
    for(const b of panel.querySelectorAll('[data-validate],[data-response]'))b.onclick=()=>{const id=b.dataset.validate||b.dataset.response,c=record.corrections.find(c=>c.id===id),r=[...(record.reviews||[]),...(record.review_history||[])].find(r=>r.source_type==='correction'&&r.source_id===id),m=record.missions.find(m=>c.affected_mission_ids.includes(m.id));open(`<p>${correctionProse(c)}</p><p>${b.dataset.validate?'The server will re-evaluate current evidence before restoring validity.':'Record the doctor-approved response as a separate action. This does not send a patient message or restore invalidated fulfilment.'}</p>`,data=>send({type:b.dataset.validate?'ValidateCorrection':'CorrectionResponse',correction_id:id,review_ref:ref('review',r),reason:String(data.get('reason')),...(b.dataset.validate?{mission_ref:ref('mission',m)}:{})}));};
  }
  function toast(message, href){
    let target=$('action-toast');if(!target){target=document.createElement('div');target.id='action-toast';target.setAttribute('role','status');target.setAttribute('aria-live','polite');document.body.append(target);}
    target.innerHTML=`${esc(message)} ${href?`<a href="${esc(href)}">Open record</a>`:''} <button type="button" aria-label="Dismiss notification">×</button>`;
    target.querySelector('button').onclick=()=>target.remove();
  }
  function avatar(name){return `<span class="av" aria-hidden="true">${esc(String(name||'').trim().split(/\s+/).filter(Boolean).slice(0,2).map(word=>Array.from(word)[0]).join('').toUpperCase())}</span>`;}
  let presentedQueue=null;
  function auroraPresentation(){
    document.querySelectorAll('.reading-chart .trend span').forEach(bar=>{bar.style.height=bar.dataset.height+'%';bar.style.setProperty('--bd',bar.dataset.delay+'ms');});
    if(patient){
      document.querySelectorAll('[data-doctor-avatar] .av').forEach(el=>el.textContent=doctorInitial());
      updatePatientColumns();
      document.querySelectorAll('.page-heading,#content>section,#patient-controls>section,#content a.summary-tile').forEach((el,i)=>{
        el.classList.add('rv');el.style.setProperty('--d',`${el.matches('.page-heading')?60:180+(i%4)*60}ms`);
      });
      reveal();return;
    }
    document.querySelectorAll('.page-heading,#content>.toolbar,#content>section,#content>.work-surface,#content>.inbox-groups>section,.summary-tile[data-summary],details.inbox-item,.tab-panel,.record-heading,.preferences').forEach(el=>{el.classList.add('rv');
      const delay=el.matches('.page-heading')?60:el.matches('.summary-tile[data-summary]')?180+[...el.parentElement.children].indexOf(el)*60:el.matches('.toolbar')?420:el.matches('.work-surface')?480:el.matches('.record-heading')?140:180;
      el.style.setProperty('--d',`${delay}ms`);
    });
    const queue=document.querySelector('#content>.work-surface');
    if(queue&&queue!==presentedQueue){
      // Paging and filtering update the already-entered queue in place visually.
      if(presentedQueue)queue.classList.add('in');
      presentedQueue=queue;
    }
    document.querySelectorAll('.clinical .patient-row').forEach(row=>{
      const link=row.querySelector('[data-record]');if(link&&!row.querySelector('.av'))link.insertAdjacentHTML('beforebegin',avatar(link.textContent));
      const last=row.lastElementChild;if(!last.querySelector('.go'))last.insertAdjacentHTML('beforeend','<span class="go" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><path d="M8 5l7 7-7 7"/></svg></span>');
    });
    reveal();
  }
  function readingTime(row){return row.observed_at||row.recorded_at;}
  function dateKey(value){const parts=new Intl.DateTimeFormat('en-CA',{timeZone:state.zone,year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(new Date(value));return ['year','month','day'].map(k=>parts.find(p=>p.type===k).value).join('-');}
  function numericParts(value){const parts=/^[-+]?\d+(?:\.\d+)?(?:\s*\/\s*[-+]?\d+(?:\.\d+)?)?$/.test(String(value).trim())?String(value).split('/').map(Number):[];return parts.every(Number.isFinite)?parts:[];}
  function readingCharts(rows,{weekly=false,record=null}={}){
    const now=Date.now(), today=dateKey(now), day=new Date(today+'T00:00:00Z');
    day.setUTCDate(day.getUTCDate()-((day.getUTCDay()+6)%7));const monday=day.toISOString().slice(0,10);
    const groups=new Map();
    for(const row of rows){const at=readingTime(row);if(!at||!Number.isFinite(+new Date(at)))continue;
      if(weekly&&(+new Date(at)>now||dateKey(at)<monday||dateKey(at)>today))continue;
      const key=JSON.stringify([row.metric,row.unit]);if(!groups.has(key))groups.set(key,[]);groups.get(key).push(row);
    }
    return [...groups.values()].map(all=>{
      all.sort((a,b)=>+new Date(readingTime(a))-+new Date(readingTime(b)));
      const values=weekly?all:all.slice(-10), {metric,unit}=values[0];
      const count=Math.max(0,...values.map(r=>numericParts(r.value).length));
      const series=Array.from({length:count},(_,component)=>{
        const numbers=values.flatMap(row=>{const n=numericParts(row.value);return n.length===count?[{row,value:n[component]}]:[];});
        if(!numbers.length)return '';
        const low=Math.min(...numbers.map(n=>n.value)),high=Math.max(...numbers.map(n=>n.value)),base=Math.min(0,low),top=Math.max(0,high),range=top-base||1;
        const label=count===2?(component===0?(en?'First component':'القيمة الأولى'):(en?'Second component':'القيمة التانية')):clinicalLabel(metric);
        const alerts=(record?.orders||[]).filter(o=>o.status==='active'&&o.current_version?.type==='value_alert').map(o=>o.current_version.structured_instruction).filter(a=>a.metric===metric&&a.unit===unit);
        const compare={gt:(a,b)=>a>b,ge:(a,b)=>a>=b,lt:(a,b)=>a<b,le:(a,b)=>a<=b};
        return `<div class="reading-series"><p class="chart-scale">${esc(label)} · ${en?'Scale':'المقياس'}: ${bdi(base)} – ${bdi(top)}${unit?' '+bdi(unit):''}</p><div class="trend" aria-hidden="true">${numbers.map(({row,value},i)=>{const hot=!weekly&&dateKey(readingTime(row))===today&&alerts.some(a=>{const threshold=numericParts(a.threshold);return threshold.length===count&&compare[a.comparator]?.(value,threshold[component]);});return `<span class="${hot?'hot':''}" data-height="${(value-base)/range*100}" data-delay="${i*60}"></span>`;}).join('')}</div><div class="kv"><span>${en?'Highest':'أعلى قيمة'} · ${esc(label)}</span><b>${bdi(high)}${unit?' '+bdi(unit):''}</b></div><div class="kv"><span>${en?'Lowest':'أقل قيمة'} · ${esc(label)}</span><b>${bdi(low)}${unit?' '+bdi(unit):''}</b></div></div>`;
      }).join('');
      return `<div class="reading-chart"><h4>${bdi(clinicalLabel(metric))}${unit?' · '+bdi(unit):''}</h4>${series}<ol class="reading-values" aria-label="${en?'Exact readings':'القراءات زي ما اتسجلت'}">${values.map(row=>`<li>${bdi(row.value)}${row.unit?' '+bdi(row.unit):''} · ${row.observed_at?'':en?'Recorded on ':'اتسجلت يوم '}${time(readingTime(row)).replace(/<small>.*<\/small>$/,'')}</li>`).join('')}</ol></div>`;
    }).join('');
  }
  function doctorReadings(record){
    const rows=new Map();
    for(const mission of record.missions||[]){const d=mission.details;if(d?.kind!=='MONITOR'||mission.state==='proposed')continue;
      for(const r of d.readings||[]){const ref=r.source_ref,key=ref?JSON.stringify([ref.entity_type,ref.id,r.reading_index]):JSON.stringify([mission.id,r.observed_at,r.value,r.slot]);
        if(!demo&&ref?.entity_type==='evidence'&&!state.evidence.some(e=>(e.id===ref.id||e.evidence_id===ref.id)&&e.version===ref.version&&!['detached','rejected'].includes(e.association_state)))continue;
        if(ref?.entity_type==='clinical_fact'&&record.facts&&!record.facts.some(f=>f.id===ref.id&&f.version===ref.version))continue;
        const previous=rows.get(key);if(previous&&previous.version>(ref?.version||0))continue;
        rows.set(key,{metric:d.metric,unit:d.unit,value:r.value,observed_at:r.observed_at,received_at:r.received_at,version:ref?.version||0});
      }
    }
    return [...rows.values()];
  }
  function fullProfile(record,heading){
    const card=document.createElement('article');card.className='card profile rv';card.id='patient-profile';
    const title=document.createElement('h2');title.className='htitle opened-title';title.textContent='One patient, opened';heading.before(title,card);heading.classList.remove('profile');heading.classList.add('top');card.append(heading);
    const grid=document.createElement('div');grid.className='grid3';card.append(grid);
    const plan=document.createElement('div');plan.className='cellp';plan.innerHTML='<h4>Current plan</h4>'+(record.orders||[]).filter(o=>o.status==='active').map(o=>`<div class="plan-item"><div>${instruction(o.current_version)}</div></div>`).join('');
    const readings=document.createElement('div');readings.className='cellp';readings.innerHTML='<h4>Last ten readings</h4>'+readingCharts(doctorReadings(record),{record});
    const work=document.createElement('div');work.className='cellp';grid.append(plan,readings,work);const obligation=$('what-to-do');obligation.classList.add('ask');work.append(obligation);
  }
  function recordDecorations(record){
    const heading=document.querySelector('.record-heading');heading.insertAdjacentHTML('afterbegin',avatar(record.display_name));
    heading.classList.add('profile');
    const copy=document.createElement('div');copy.className='profile-copy';
    [...heading.children].filter(el=>!el.matches('.av')).forEach(el=>copy.append(el));heading.append(copy);
    $('back').classList.add('button');$('workspace').prepend($('back'));if(demo)$('back').href='/demo'+location.search;
    document.querySelectorAll('[data-order],[data-held-order]').forEach(el=>{
      const copy=document.createElement('div');copy.className='plan-copy';copy.append(...el.childNodes);el.append(copy);
      el.classList.add('plan-item');const miss=el.hasAttribute('data-held-order');
      el.insertAdjacentHTML('afterbegin',`<span class="tick ${miss?'miss':''}" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><path d="${miss?'M6 6l12 12M18 6L6 18':'M5 12l4.5 4.5L19 7'}"/></svg></span>`);
    });
    document.querySelectorAll('[data-fact],.consent-binding p').forEach(el=>el.classList.add('kv'));
    document.querySelectorAll('[data-evidence-actions],.record-item:has([data-validate],[data-response])').forEach(el=>el.classList.add('ask'));
    fullProfile(record,heading);
    const comparisons={gt:(a,b)=>a>b,ge:(a,b)=>a>=b,lt:(a,b)=>a<b,le:(a,b)=>a<=b};
    for(const mission of record.missions||[]){
      const d=mission.details;if(d?.kind!=='MONITOR')continue;
      const table=document.getElementById(mission.id)?.querySelector('.reading-table');if(!table)continue;
      const readings=(d.slots||[]).flatMap((slot,i)=>{const r=(d.readings||[]).findLast(r=>r.slot===i);return r&&Number.isFinite(parseFloat(r.value))?[parseFloat(r.value)]:[];});
      if(!readings.length)continue;
      const alerts=(record.orders||[]).filter(o=>o.status==='active'&&o.current_version?.type==='value_alert').map(o=>o.current_version.structured_instruction).filter(a=>a.metric===d.metric&&a.unit===d.unit);
      const maximum=Math.max(...readings), bars=document.createElement('div');bars.className='trend';bars.setAttribute('aria-hidden','true');
      readings.forEach((value,i)=>{const bar=document.createElement('span');bar.style.height=`${maximum>0?value/maximum*100:0}%`;bar.style.setProperty('--bd',`${i*60}ms`);bar.classList.toggle('hot',alerts.some(a=>comparisons[a.comparator]?.(value,parseFloat(a.threshold))));bars.append(bar);});table.before(bars);
    }
    auroraPresentation();
  }
  function correctionProse(c){
    const labels={text:'Recorded text',value:'Reading',unit:'Unit',dose:'Dose',frequency:'Frequency',duration:'Duration',timing:'Timing',drug:'Medication',route:'Route',state:'Document use'};
    const date=c.created_at?new Intl.DateTimeFormat(lang,{dateStyle:'medium',timeZone:state.zone}).format(new Date(c.created_at)):'an unrecorded date';
    const lines=[];
    const compare=(a,b,prefix='')=>{for(const [key,label] of Object.entries(labels)){
      if(JSON.stringify(a?.[key])===JSON.stringify(b?.[key]))continue;
      if(![a?.[key],b?.[key]].every(v=>v==null||['string','number'].includes(typeof v)))continue;
      const show=v=>v==null?t('not_recorded'):key==='state'?t(v):String(v);
      lines.push(`${prefix}${label} changed from ${show(a?.[key])} to ${show(b?.[key])} by the doctor on ${date}.`);
    }};
    if(c.before&&c.after){compare(c.before,c.after);compare(c.before.payload,c.after.payload);compare(c.before.structured_instruction,c.after.structured_instruction);
      const old=c.before.values||[], next=c.after.values||[];for(let i=0;i<Math.max(old.length,next.length);i++)compare(old[i],next[i],old[i]?.name?`${old[i].name}: `:'');}
    return esc(lines.join(' ')||`Correction recorded on ${date}.`);
  }
  function summaryStrip(records,unassigned=[],interactive=false){
    if(view==='patients')records=patientRoster();
    const work=[...records.flatMap(record=>obligations(record).map(item=>({...item,removed:!!record.removed_at}))),...unassigned.map(r=>({due:r.review_at,review:r,urgent:r.review_kind==='incident_response'}))];
    return `<div class="summary-strip" aria-label="Outstanding work">${Object.entries(summaryLabels).map(([key,label])=>{
      const items=work.filter(x=>!(['due_today','overdue'].includes(key)&&x.removed)&&matchesSummary(x,key)), n=view==='patients'?patientRoster().filter(r=>obligations(r).some(item=>matchesSummary(item,key))).length:items.length;
      const oldest=items.map(x=>x.review?sourceDate(x.review,records.find(r=>r.patient_id===(x.review.patient_id||x.review.scope?.patient_id)||r.reviews?.some(v=>v.id===x.review.id))||{}):x.due).filter(Boolean).map(x=>+new Date(x)).filter(Number.isFinite).sort((a,b)=>a-b)[0];
      const days=oldest===undefined?0:Math.max(0,Math.floor((Date.now()-oldest)/86400000));
      const clause=key==='danger'?(n?`${n} ${n===1?'needs':'need'} a response`:'nothing urgent right now'):key==='due_today'?(n?`${n} due today`:'none today'):n?(oldest===undefined?`${n} ${key==='overdue'?'late':'waiting for you'}`:`oldest has waited ${days} ${days===1?'day':'days'}`):(key==='overdue'?'no patient is late':'nothing waiting on you');
      const weight=n&&(key==='danger'||key==='overdue')?(key==='danger'?'danger':'warning'):'';
      const tag=interactive?'button':'article', dashboard=interactive||view==='patients';
      return `<${tag} class="summary-tile ${weight}"${dashboard?` data-summary="${key}" aria-label="${n} ${label}"`: ''}${interactive?` type="button" aria-pressed="${state.filter===key}"`:''}><span class="tile-label">${icon(key==='danger'?'status-icon':key==='pending_review'?'review-icon':'clock-icon')}${label}</span><strong data-count="${n}">${n}</strong><span class="tile-clause">${clause}</span>${dashboard?'<span class="spark" aria-hidden="true"></span>':''}${dashboard&&weight==='danger'?'<span class="beacon" aria-hidden="true"></span>':''}</${tag}>`;
    }).join('')}</div>`;
  }
  function listPresentation(rows,updateSummary=true){
    if(updateSummary)$('content').insertAdjacentHTML('afterbegin',summaryStrip(state.records,state.reviews.filter(r=>!r.patient_id&&r.state!=='resolved'),true));
    const inbox=updateSummary&&document.querySelector('nav a[href="/a/inbox"]');if(inbox)inbox.innerHTML=`${icon('review-icon')}${t('inbox')} <span class="count">${state.records.reduce((n,r)=>n+reviewRows(r).length,0)+state.reviews.filter(r=>!r.patient_id).length}</span>`;
    document.querySelectorAll('button[data-summary]').forEach(button=>button.onclick=()=>{
      const key=button.dataset.summary;state.filter=view!=='patients'&&state.filter===key?'all':key;state.page=1;list();
      document.querySelector(`[data-summary="${key}"]`).focus();
      if(view==='inbox'&&key==='overdue')document.querySelector('.inbox-item')?.scrollIntoView({block:'nearest'});
    });
    const elements=[...document.querySelectorAll('.patient-row')];
    elements.forEach((row,i)=>{row.tabIndex=i===0?0:-1;row.onkeydown=e=>{let next;if(e.key==='ArrowDown')next=Math.min(elements.length-1,i+1);if(e.key==='ArrowUp')next=Math.max(0,i-1);if(next!==undefined){e.preventDefault();elements.forEach(x=>x.tabIndex=-1);elements[next].tabIndex=0;elements[next].focus();}if(e.key==='Enter'&&view!=='patients'){e.preventDefault();row.querySelector('[data-record],summary')?.click();}};});
    if(view==='inbox'){
      const visible=rows.slice((state.page-1)*50,state.page*50);const groups=[['Emergency',visible.filter(r=>r.item?.urgent)],['Waiting on you',visible.filter(r=>!r.item?.urgent)]];
      const target=document.createElement('div');target.className='inbox-groups';
      target.innerHTML=groups.map(([label,items])=>`<section class="section"><h2>${label} <span class="count ${label==='Emergency'&&items.length?'danger':''}">${items.length}</span></h2>${items.map(({record,item})=>inboxCard(record,item)).join('')||emptyState('Nothing needs attention here.',t('refresh'))}</section>`).join('');
      document.querySelector('.work-surface').replaceWith(target);
    }
  }
  function detailPresentation(record){
    const work=obligations(record);
    $('content').insertAdjacentHTML('afterbegin',`<section class="section" id="what-to-do"><h2>What to do now</h2>${work.length?work.map(item=>`<p data-obligation="${esc(item.id)}"><a href="#${demo?'patient='+encodeURIComponent(record.patient_id)+'&':''}${esc(itemTarget(item,record))}">${esc(sentence(item,record))}</a></p>`).join(''):`<p>${esc(prose('quiet'))}</p>`}</section>`);
    $('content').insertAdjacentHTML('afterbegin',`<div class="record-heading"><h2 id="title" class="patient-name">${bdi(record.display_name)}</h2><p id="subtitle">${record.age==null?'age not recorded':'age '+bdi(record.age)}</p><p>${esc(joining(record))}</p></div>`);
    const grid=document.querySelector('.detail-grid'), sections=[...grid.querySelectorAll(':scope > div > section')];
    const timeline=document.querySelector('.correction-timeline');
    const tabs=document.createElement('div');tabs.className='tabs';tabs.setAttribute('role','tablist');tabs.setAttribute('aria-label','Patient record');
    const groups=[['Medicines',[sections[0],sections[2]]],['Requests',[sections[1],sections[3]]],['Documents',[sections[4],sections[5]]],['History',[timeline]]];
    grid.replaceChildren();groups.forEach(([name,children],i)=>{const panel=document.createElement('div');panel.className='tab-panel';panel.id='record-panel-'+i;panel.setAttribute('role','tabpanel');panel.setAttribute('aria-labelledby','record-tab-'+i);panel.tabIndex=0;children.filter(Boolean).forEach(x=>panel.append(x));if(!panel.childNodes.length)panel.innerHTML=empty('no_corrections');grid.append(panel);const button=document.createElement('button');button.id='record-tab-'+i;button.textContent=name;button.setAttribute('role','tab');button.setAttribute('aria-controls',panel.id);tabs.append(button);});
    grid.before(tabs);const buttons=[...tabs.children];const select=i=>{state.detailTab=i;grid.querySelectorAll('button.primary').forEach(b=>b.classList.remove('primary'));grid.children[i].querySelector('[data-validate],[data-response]')?.classList.add('primary');buttons.forEach((b,n)=>{b.setAttribute('aria-selected',String(n===i));b.tabIndex=n===i?0:-1;grid.children[n].hidden=n!==i;});};
    buttons.forEach((b,i)=>{b.onclick=()=>select(i);b.onkeydown=e=>{const forward=document.documentElement.dir==='rtl'?'ArrowLeft':'ArrowRight';let next;if(e.key===forward)next=(i+1)%4;else if(['ArrowLeft','ArrowRight'].includes(e.key))next=(i+3)%4;else if(e.key==='Home')next=0;else if(e.key==='End')next=3;if(next!==undefined){e.preventDefault();select(next);buttons[next].focus();}};});const arrive=()=>{const fragment=new URLSearchParams(location.hash.slice(1)), tab=fragment.get('tab'), i={medicines:0,plan:0,requests:1,documents:2,evidence:2,history:3}[tab];select(i??state.detailTab??0);if(i!==undefined){const target=fragment.get('item')?document.getElementById(fragment.get('item')):grid.children[i];if(target&&grid.children[i].contains(target)){target.scrollIntoView({block:'start'});target.focus({preventScroll:true});}}};arrive();window.onhashchange=arrive;
    $('what-to-do').querySelectorAll('a').forEach(a=>a.addEventListener('click',()=>{if(a.hash===location.hash)arrive();}));
    const support=document.createElement('details');support.className='support';support.innerHTML='<summary>Details for support</summary><pre></pre>';support.querySelector('pre').textContent=JSON.stringify({record,evidence:state.evidence},null,2);$('content').append(support);
    document.querySelectorAll('[data-media]').forEach(a=>{if(a.dataset.media==='application/pdf'){a.download='document.pdf';return;}a.onclick=e=>{e.preventDefault();lightbox(e.currentTarget);};});
    // Pair each evidence card with its existing scoped original, without inventing a URL.
    recordDecorations(record);
    function pageImages(original, indices, article){
      if(!article||original.dataset.media!=='application/pdf')return;
      for(const index of indices){if(!Number.isInteger(index)||index<1||index>10)continue;
        const link=document.createElement('a'), url=new URL(original.href);url.searchParams.set('page',index);
        link.href=url.href;link.dataset.media='image/png';link.dataset.captured=original.dataset.captured||'';
        link.textContent='Page '+index;link.onclick=e=>{e.preventDefault();lightbox(link);};
        const image=document.createElement('img');image.src=url.href;image.alt='Page '+index;image.loading='lazy';
        image.style.maxWidth='100%';link.append(image);article.append(link);
      }
    }
    for(const media of record.media||[]){if(media.mime!=='application/pdf')continue;
      const original=document.querySelector(`[data-media][href$="/${encodeURIComponent(media.media_id)}"]`);
      if(original)pageImages(original,media.pages||[],original.closest('article'));
    }
    state.evidence.forEach((e,i)=>{const media=(record.media||[]).find(m=>m.media_id===e.media_id);if(!media){sections[4].querySelectorAll('article')[i]?.insertAdjacentHTML('beforeend','<p class="muted">Original unavailable.</p>');return;}const original=document.querySelector(`[data-media][href$="/${encodeURIComponent(media.media_id)}"]`);if(original){const copy=original.cloneNode(true);sections[4].querySelectorAll('article')[i]?.append(copy);copy.onclick=original.onclick;if(media.mime==='application/pdf')pageImages(copy,media.pages||[],sections[4].querySelectorAll('article')[i]);}});
    recordActions(record);
  }
  function recordActions(record){
    if(demo||!state.recordActionPages)return;
    const root=$('what-to-do'), pages=state.recordActionPages, w=pages[0]?.words||{};
    const endpoint=`/api/patients/${encodeURIComponent(record.patient_id)}/actions`;
    const button=(label,run)=>{const b=document.createElement('button');b.type='button';b.textContent=label;b.onclick=run;return b;};
    const paragraph=text=>{const p=document.createElement('p');p.textContent=text;return p;};
    const sources={...record};
    for(const key of ['missions','followups','corrections','evidence'])sources[key]=[...(record[key]||[])];
    for(const page of pages)for(const item of page.items||[]){
      const collection={mission:'missions',question:'missions',followup:'followups',correction:'corrections',evidence:'evidence',document:'evidence'}[item.review?.source_type];
      if(item.source&&collection&&!sources[collection].some(x=>x.id===item.source.id))sources[collection].push(item.source);
    }
    for(const page of pages)for(const item of page.items||[]){
      let p=[...root.querySelectorAll('p[data-obligation]')].find(x=>x.dataset.obligation===item.id);
      if(!p){p=document.createElement('p');p.dataset.obligation=item.id;const a=document.createElement('a');a.href='#'+itemTarget(item,sources);a.textContent=sentence(item,sources);p.append(a);root.append(p);}
      if(p.nextElementSibling?.classList.contains('item-actions')||!item.actions?.length)continue;
      const actions=document.createElement('div');actions.className='item-actions';actions.dataset.itemId=item.id;p.after(actions);
      for(const choice of item.actions)actions.append(button(choice.label,()=>open(item,choice,page.listing_token,actions)));
    }
    root.querySelector('[data-record-more]')?.remove();
    const cursor=pages.at(-1)?.cursor;
    if(cursor){const more=button(w.show_more,async()=>{more.disabled=true;const at=generation;try{const page=await api(endpoint+'?cursor='+encodeURIComponent(cursor));if(at!==generation)return;pages.push(page);recordActions(record);}catch(error){if([401,403,404].includes(error.status))await load();else toast(error.message);more.disabled=false;}});more.dataset.recordMore='';root.append(more);}
    function open(item,choice,token,actions){
      root.querySelectorAll('.record-action-form').forEach(f=>f.remove());
      const form=document.createElement('form');form.className='record-action-form';actions.append(form);
      const payload={item_kind:item.item_kind,item_id:item.id,action:choice.action,expected_version:item.version,command_id:crypto.randomUUID()};
      if(item.review){payload.listing_token=token;payload.expected_source_version=item.review.source_version;}
      if(choice.disposition)payload.disposition=choice.disposition;
      if(choice.mission_id)payload.mission_id=choice.mission_id;
      const field=(label,name,type,max)=>{const l=document.createElement('label');l.append(document.createTextNode(label));const input=document.createElement(type==='textarea'?'textarea':'input');if(type!=='textarea')input.type=type;input.name=name;input.required=true;if(max)input.maxLength=max;l.append(input);form.append(l);return input;};
      if(item.review&&choice.action==='resolve')form.append(paragraph(w.decision_only));
      const answer=choice.action==='answer'?field(w.your_answer,'answer','textarea',700):null;
      const due=choice.action==='extend'?field(w.deadline,'due_at','datetime-local'):null;
      const reason=['resolve','extend','close_unfulfilled','reject'].includes(choice.action)?field(w.reason,'reason','textarea',item.item_kind==='evidence'?1000:200):null;
      const preview=document.createElement('div');preview.className='record-action-preview';form.append(preview);
      let confirmed=false,inflight=false;
      const submit=button(answer?w.preview:due?w.preview_deadline:w.confirm);submit.type='submit';form.append(submit);
      form.append(button(w.cancel_form,()=>form.remove()));const error=paragraph('');error.setAttribute('role','alert');form.append(error);
      form.addEventListener('input',()=>{confirmed=false;preview.replaceChildren();submit.textContent=answer?w.preview:due?w.preview_deadline:w.confirm;});
      form.onsubmit=async event=>{
        event.preventDefault();if(inflight||!form.reportValidity())return;
        if(answer)payload.text=answer.value;
        if(reason)payload.reason=reason.value;
        if(due)payload.due_at=new Date(due.value).toISOString();
        if((answer||due)&&!confirmed){
          preview.replaceChildren();
          if(answer)preview.append(paragraph(answer.value));
          if(due){const date=new Date(payload.due_at),escalation=new Date(+date+1000*(item.mission.grace_seconds||0));const format=d=>d.toLocaleString(lang,{dateStyle:'full',timeStyle:'long'});preview.append(paragraph(w.deadline_preview.replace('{due}',format(date)).replace('{escalation}',format(escalation))));}
          confirmed=true;submit.textContent=answer?w.send_answer:w.confirm;return;
        }
        inflight=true;root.querySelectorAll('button').forEach(b=>b.disabled=true);form.querySelectorAll('input,textarea').forEach(x=>x.disabled=true);
        try{
          const response=await fetch(endpoint,{method:'POST',credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/json','X-CSRF-Token':decodeURIComponent(document.cookie.split('; ').find(x=>x.startsWith('sanad_csrf='))?.slice(11)||'')},body:JSON.stringify(payload)});
          const result=await response.json().catch(()=>({}));
          if([401,403,404].includes(response.status)){await load();return;}
          if(response.ok||[409,422].includes(response.status)){await load();toast(typeof result.detail==='string'?result.detail:errorText(response.status));return;}
          throw new Error(errorText(response.status));
        }catch(failure){error.textContent=failure.message||t('failed');}
        finally{inflight=false;root.querySelectorAll('button').forEach(b=>b.disabled=false);form.querySelectorAll('input,textarea').forEach(x=>x.disabled=false);}
      };
      form.querySelector('textarea,input,button')?.focus();
    }
  }
  function lightbox(anchor){
    const url=new URL(anchor.href);if(url.origin!==location.origin||!url.pathname.startsWith('/api/patients/'))return;
    const dialog=document.createElement('dialog');dialog.className='lightbox';dialog.setAttribute('aria-label','Original document');
    dialog.innerHTML=`<header class="lightbox-header"><h3>Original document</h3><button class="ghost icon-button" aria-label="Close original document">×</button></header><div class="lightbox-well"><p role="status">Loading original…</p><img alt="Original document" hidden></div><footer class="lightbox-footer"><span>${anchor.dataset.captured?time(anchor.dataset.captured):t('not_recorded')}</span><span>${esc(anchor.textContent)}</span></footer>`;
    document.body.append(dialog);dialog.querySelector('button').onclick=()=>dialog.close();dialog.onclose=()=>{dialog.remove();anchor.focus();};dialog.showModal();
    const img=dialog.querySelector('img'), unavailable=()=>{img.remove();dialog.querySelector('.lightbox-well').innerHTML=emptyState('Original unavailable.','Close this view and refresh the record.');};
    if(!anchor.dataset.media.startsWith('image/')){unavailable();return;}
    img.onload=()=>{img.hidden=false;dialog.querySelector('[role=status]').remove();};img.onerror=unavailable;img.src=url.href;
  }
  function questions(){
    const data=state.questions;if(!data)return;const root=document.createElement('section');root.className='section';root.id='questions';root.innerHTML='<h2>Patient questions</h2><p>Review a proposed reply, write an answer, or defer until at least tomorrow.</p>';
    for(const q of data.questions){const article=document.createElement('article');article.className='record-item question-card';article.innerHTML=`<h3>${bdi(q.patient_name)}</h3><p>${bdi(q.context_line)}</p><p>${bdi(q.text)}</p><small>${q.hours_waiting} hours waiting</small>${q.proposed_reply?`<blockquote>${bdi(q.proposed_reply.text)}</blockquote>`:'<p>No approved reply is available. Write an answer.</p>'}<div class="action-bar"><button data-action="send" ${q.proposed_reply?'':'disabled'}>Send</button><button data-action="answer">Answer</button><button data-action="defer">Defer</button></div><div class="question-confirm"></div>`;
      article.querySelectorAll('[data-action]').forEach(b=>b.onclick=()=>{const action=b.dataset.action,target=article.querySelector('.question-confirm');root.querySelectorAll('button.primary').forEach(x=>x.classList.remove('primary'));root.querySelectorAll('.question-confirm').forEach(x=>x.replaceChildren());target.innerHTML=`<form><p>${action==='defer'?'Defer until at least tomorrow? A later deadline stays unchanged.':'Confirm your answer for this patient.'}</p>${action==='answer'?'<label>Your answer<textarea name="answer" required maxlength="700"></textarea></label>':''}<button class="primary">Confirm</button><button type="button" class="cancel">Cancel</button><p role="alert"></p></form>`;target.querySelector('.cancel').onclick=()=>{target.replaceChildren();root.querySelector('[data-action=send]:not([disabled])')?.classList.add('primary');};target.querySelector('form').onsubmit=async event=>{event.preventDefault();const button=target.querySelector('.primary');button.disabled=true;try{const payload=action==='send'?{listing_token:data.listing_token,n:q.n,mission_version:q.version,reusable_id:q.proposed_reply.reusable_id,reusable_version:q.proposed_reply.version}:{expected_version:q.version,...(action==='answer'?{text:target.querySelector('textarea').value}:{})};const outcome=await api(`/api/questions/${encodeURIComponent(q.id)}/${action}`,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':decodeURIComponent(document.cookie.split('; ').find(x=>x.startsWith('sanad_csrf='))?.slice(11)||'')},body:JSON.stringify({command_id:crypto.randomUUID(),...payload})});await load();toast(outcome.reason==='patient_removed'?(state.records.find(r=>r.patient_id===q.patient_id)?.removal_words?.not_sent||'Not sent: this patient was removed.'):action==='defer'?'Question deferred.':'Answer recorded.','/a/inbox#questions');}catch(error){target.querySelector('[role=alert]').textContent=error.message;button.disabled=false;}};});root.append(article);}
    root.querySelector('[data-action=send]:not([disabled])')?.classList.add('primary');
    if(!data.questions.length)root.insertAdjacentHTML('beforeend',emptyState('No questions are waiting for an answer.',t('refresh')));$('content').querySelector('.summary-strip').after(root);
  }

  // Separate visual columns without moving the word-safe section/label boundaries.
  let updatePatientColumns=()=>{};
  function patientColumns(){
    const stage=document.querySelector('.two');if(!stage)return;
    let pending=false;const observed=new Set();
    const schedule=()=>{if(pending)return;pending=true;requestAnimationFrame(layout);};
    const observer=new ResizeObserver(schedule);
    function layout(){
      pending=false;
      const content=$('content'), controls=$('patient-controls');
      const sections=[...content.querySelectorAll(':scope>section')];
      const top=[...content.children].filter(el=>!el.matches('section,#patient-week')&&!content.hidden);
      const left=[sections[0],sections[1],controls.querySelector('.patient-talk'),controls.querySelector('.patient-upload'),sections[2]].filter(el=>el&&!el.hidden&&(!content.contains(el)||!content.hidden));
      const right=[sections[4],content.querySelector('#patient-week'),sections[3],sections[5],content.querySelector('#patient-agreement')].filter(el=>el&&!content.hidden);
      const settings=$('patient-settings');if(settings&&!settings.hidden)top.push(settings);
      const result=$('patient-action-result');
      const elements=[...top,...left,...right,result].filter(Boolean);
      for(const el of observed)if(!elements.includes(el)){observer.unobserve(el);observed.delete(el);}
      for(const el of elements)if(!observed.has(el)){observed.add(el);observer.observe(el);}
      if(innerWidth<=900){
        stage.style.minBlockSize='';
        elements.forEach(el=>['position','inline-size','inset-inline-start','inset-block-start','grid-area'].forEach(p=>el.style.removeProperty(p)));
        return;
      }
      const style=getComputedStyle(stage), gap=parseFloat(style.columnGap);
      const widths=style.gridTemplateColumns.split(' ').map(Number.parseFloat), width=stage.clientWidth;
      function place(el,x,y,w){
        el.style.position='absolute';el.style.gridArea='auto';el.style.inlineSize=`${w}px`;
        el.style.insetInlineStart=`${x}px`;el.style.insetBlockStart=`${y}px`;
        const cs=getComputedStyle(el);
        return y+el.offsetHeight+parseFloat(cs.marginBlockStart)+parseFloat(cs.marginBlockEnd)+gap;
      }
      let start=0;top.forEach(el=>{start=place(el,0,start,width);});
      let a=start,b=start;
      left.forEach(el=>{a=place(el,0,a,widths[0]);});
      right.forEach(el=>{b=place(el,widths[0]+gap,b,widths[1]);});
      const end=result?place(result,0,Math.max(a,b),width):Math.max(a,b);
      stage.style.minBlockSize=`${end-gap}px`;
    }
    observer.observe(stage);
    new MutationObserver(schedule).observe($('content'),{childList:true,attributes:true,attributeFilter:['hidden']});
    window.addEventListener('resize',schedule);
    updatePatientColumns=schedule;schedule();
  }
  function doctorInitial(){return Array.from((state.data?.plan?.doctor_name||'').replace(/^Dr\.?\s+/i,'').trim())[0]?.toUpperCase()||'S';}
  let patientPreferences=null;
  function patientView(data){
    const plan=data.plan||data;$('title').textContent=t('yourcare');
    // Explicit projection allow-list. No ids, kind names, raw objects or internal statuses.
    const orderHTML=(plan.orders||[]).map(o=>`<article class="record-item dose"><span class="pill" aria-hidden="true">${esc(Array.from(o.drug||'')[0]?.toUpperCase()||'')}</span><div class="dose-copy"><b>${bdi(o.drug)}</b><small>${[o.dose,o.timing,o.route,o.duration].filter(Boolean).map(bdi).join(' · ')}</small></div>${o.frequency?`<span class="status tag">${bdi(o.frequency)}</span>`:''}</article>`).join('')||empty('no_plan');
    const requests=(plan.next_missions||[]).map(m=>`<article class="record-item dose"><div class="dose-copy"><b>${bdi(m.title)}</b><small>${t('due')}: ${time(m.due_at)}</small></div></article>`).join('')||empty('no_requests');
    const reports=(plan.medication_reports||[]).map(r=>`<article class="record-item"><p>${bdi(r.text)}</p><small>${t('self_report')}</small></article>`).join('')||empty('no_reports');
    const prefs=patientPreferences||{...plan.preferences,reminders:plan.preferences?.routine_contact_enabled?"enabled":"paused"};
    const patientWords=JSON.parse($('patient-controls')?.dataset.words||'{}');
    $('content').innerHTML=`<div class="summary-strip" aria-label="Your recorded plan">${[[t('your_plan'),(plan.orders||[]).length,'patient-medicines'],[t('your_requests'),(plan.next_missions||[]).length,'patient-requests'],[t('questions'),(plan.open_questions||[]).length,'patient-questions']].map(([label,n,id])=>`<a class="summary-tile" href="#${id}"><span>${esc(label)}</span><strong data-count="${n}">0</strong></a>`).join('')}</div><p>${t('doctor')}: ${bdi(plan.doctor_name)}</p>${section('your_plan',orderHTML)}${section('your_requests',requests)}${section('reports',reports)}${section('last_reading',plan.last_reading?`<p class="record-item">${bdi(plan.last_reading.text)}</p>`:badge('missing'))}${section('reminders',`<p id="patient-reminder-summary">${esc(patientWords[prefs.reminders]||t(prefs.reminders))}</p><p id="patient-quiet-summary">${t('quiet')}: ${bdi((prefs.quiet_hours||[]).join(', '))} · ${bdi(prefs.timezone)}</p>${prefs.resume_at?`<p>${t('resume')}: ${time(prefs.resume_at)}</p>`:''}`)}${section('questions',(plan.open_questions||[]).map(q=>`<article class="record-item"><p>${bdi(q.question)}</p><small>${t('waiting')}</small></article>`).join('')||empty('no_questions'))}`;
    const charts=readingCharts(plan.reading_history||[],{weekly:true});
    if(charts)$('content').querySelectorAll(':scope>section')[4].insertAdjacentHTML('afterend',`<article class="section card pad rv" id="patient-week"><h3>${en?'This week':'الأسبوع ده'}</h3>${charts}</article>`);
    $('patient-week')?.style.setProperty('--d','300ms');
    const agreement=data.agreement;
    if(agreement){
      const date=time(agreement.accepted_at).replace(/<small>.*<\/small>$/,'');
      const sentence=agreement.text?`You agreed to Sanad's terms on ${date}.`:`Agreed on ${date}, version ${esc(agreement.version)}`;
      const terms=agreement.text?`<details><summary>Read the terms</summary>${agreement.text.split(/\n\n/).map(part=>`<p>${esc(part)}</p>`).join('')}</details>`:'';
      $('content').insertAdjacentHTML('beforeend',`<section class="section" id="patient-agreement" data-version="${esc(agreement.version)}"><p>${sentence}</p>${terms}</section>`);
    }
  }
  function focusPatientSections(){
    const sections=$('content').querySelectorAll(':scope > section');
    [[0,'patient-medicines'],[1,'patient-requests'],[5,'patient-questions']].forEach(([i,id])=>{sections[i].id=id;sections[i].tabIndex=-1;});
    $('content').querySelectorAll('.summary-tile').forEach(a=>a.onclick=()=>{const target=document.querySelector(a.hash);target.scrollIntoView({block:'start'});target.focus({preventScroll:true});});
  }
  function preferences(){
    const p=state.pref;$('content').innerHTML=`<section class="preferences"><p>${t('timezone')} ${bdi(state.zone)}</p><form id="language-form"><label for="language">${t('language')}<select id="language"><option value="en" ${p.language==='en'?'selected':''}>English</option><option value="ar" ${p.language==='ar'?'selected':''}>${en?'Arabic':'العربية'}</option></select></label>${p.contest_english?`<p class="muted">${t('contest')}</p>`:''}<button class="primary" type="submit">${t('save')}</button><p id="save-result" role="status"></p></form></section>`;
    document.querySelector('.preferences').insertAdjacentHTML('beforeend',`<form id="digest-form"><h2>Question digest</h2><label>Daily time (${esc(p.timezone)})<input id="digest-time" type="time" value="${esc(p.digest_time)}" required></label><label>Packing<select id="digest-packing"><option value="one" ${p.digest_packing==='one'?'selected':''}>One message</option><option value="each" ${p.digest_packing==='each'?'selected':''}>One per question</option></select></label><button type="submit">Save digest</button><p id="digest-result" role="status"></p></form>`);
    $('digest-time').setAttribute('aria-describedby','digest-result');
    const help=document.querySelector('#language-form > .muted');if(help){help.id='language-help';$('language').before(help);$('language').setAttribute('aria-describedby',help.id);}
    for(const form of document.querySelectorAll('.preferences form')){const row=document.createElement('div');row.className='submit-row';form.querySelector('button').before(row);row.append(form.querySelector('button'));}
    $('digest-form').onsubmit=async e=>{e.preventDefault();const input=$('digest-time'),result=$('digest-result');if(!/^([01]\d|2[0-3]):[0-5]\d$/.test(input.value)){input.setAttribute('aria-invalid','true');result.textContent='Choose a valid daily time.';return;}input.removeAttribute('aria-invalid');const button=e.target.querySelector('button');button.disabled=true;try{await api('/api/preferences',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':decodeURIComponent(document.cookie.split('; ').find(x=>x.startsWith('sanad_csrf='))?.slice(11)||'')},body:JSON.stringify({digest_time:input.value,digest_packing:$('digest-packing').value,expected_version:p.version,command_id:crypto.randomUUID()})});await load();toast('Digest preferences saved.');}catch(error){result.textContent=error.message;button.disabled=false;}};
    $('language-form').onsubmit=async e=>{e.preventDefault();const button=e.target.querySelector('button');button.disabled=true;const token=document.cookie.split('; ').find(s=>s.startsWith('sanad_csrf='))?.split('=')[1]||'';try{await api('/api/preferences',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':decodeURIComponent(token)},body:JSON.stringify({language:$('language').value,expected_version:p.version,command_id:crypto.randomUUID()})});$('save-result').textContent=t('saved');location.reload();}catch(error){$('save-result').className='error';$('save-result').textContent=error.message;button.disabled=false;}};
  }
  function render(){$('back')?.remove();document.querySelector('.record-heading')?.remove();if(view!=='patients'){document.querySelector('.patients-heading .hsub')?.remove();document.querySelector('.patients-heading')?.classList.remove('patients-heading');}const heading=document.querySelector('.page-heading h1');heading.id=view==='detail'?'page-title':'title';heading.textContent=patient?t('yourcare'):t(view);$('refresh').className='ghost';if(patient){patientView(state.data);focusPatientSections();}else if(view==='preferences')preferences();else if(view==='detail')detail(state.records[0]);else {window.onhashchange=null;if(view==='patients')patientsList(true);else list();}auroraPresentation();}
  async function load(){
    const request=++generation;currentAbort?.abort();currentAbort=new AbortController();const signal=currentAbort.signal;
    const first=!state.records.length&&!state.data&&!state.pref;
    $('feedback').textContent='';$('content').setAttribute('aria-busy','true');$('refresh').disabled=true;
    const timer=setTimeout(()=>{if(request===generation){$('feedback').innerHTML=`<p>${t('loading')}</p><div class="skeleton" aria-hidden="true"></div>`;$('feedback').className='loading';}},300);
    try{
      if(demo&&!patient){view='patients';state.records=await api('/assets/demo.json',{signal});const fragment=new URLSearchParams(location.hash.slice(1));const id=fragment.get('patient')||(!fragment.has('tab')?decodeURIComponent(location.hash.slice(1)):'');if(id){const r=state.records.find(r=>r.patient_id===id);if(r){view='detail';state.records=[r];state.evidence=r.evidence||[];}}}
      else if(patient){const [me,plan,agreement]=await Promise.all([api('/api/patient/me',{signal}),api('/api/patient/plan',{signal}),api('/api/patient/agreement',{signal})]);state.data={display_name:me.display_name,plan,agreement};}
      else if(view==='preferences'){state.pref=await api('/api/preferences',{signal});}
      else if(view==='detail'){const id=encodeURIComponent(body.dataset.patient);const [record,evidence,actions]=await Promise.all([api(`/api/patients/${id}`,{signal}),api(`/api/patients/${id}/evidence`,{signal}),api(`/api/patients/${id}/actions`,{signal})]);state.records=[record];state.evidence=evidence;state.recordActionPages=[actions];}
      else {const [panel,pref]=await Promise.all([api('/api/patients',{signal}),api('/api/preferences',{signal})]);const records=[];let index=0;await Promise.all(Array.from({length:Math.min(6,panel.length)},async()=>{while(index<panel.length){const p=panel[index++];records.push(await api(`/api/patients/${encodeURIComponent(p.patient_id)}`,{signal}));}}));if(request!==generation)return;state.records=records;if(view==='inbox'||view==='history')state.reviews=await api('/api/browser/reviews'+(view==='history'?'?history=true':''),{signal});if(view==='inbox'||view==='patients'){if(view==='inbox')state.questions=await api('/api/questions',{signal});state.evidence=(await Promise.all(records.map(async r=>{const evidence=await api(`/api/patients/${encodeURIComponent(r.patient_id)}/evidence`,{signal});if(view==='patients')r.evidence=evidence;return evidence;}))).flat();}}
      if(request!==generation)return;
      render();$('feedback').textContent='';$('freshness').textContent=!patient&&view==='patients'?'':`${t('updated')} ${new Intl.DateTimeFormat(lang,{hour:'2-digit',minute:'2-digit',second:'2-digit',timeZone:state.zone}).format(new Date())}`;
      if((first||demo)&&!patient&&['patients','inbox','history'].includes(view)){let position=history.state?.scroll||0;try{if(sessionStorage.getItem('sanad-list-return')===location.pathname+location.search)position=Number(sessionStorage.getItem('sanad-list-scroll'))||position;}catch(_){}if(position)scrollTo(0,position);}
    }catch(error){if(error.name==='AbortError')return;if(request!==generation)return;
      // Never retain sensitive clinical content after a revoked/expired session.
      if([401,403,404].includes(error.status)||first){$('content').replaceChildren();document.querySelectorAll('dialog,#action-toast').forEach(x=>x.remove());}
      if([401,403].includes(error.status))window.dispatchEvent(new Event('patient-session-expired'));
      $('feedback').innerHTML=`<p class="error" role="alert">${esc(!patient&&error.status===401?'You signed out. Send /login in Telegram for a new link.':error.status?error.message:t('failed'))}</p>`;
      $('freshness').textContent='';
    }finally{clearTimeout(timer);if(request===generation){$('feedback').className='';$('content').setAttribute('aria-busy','false');$('refresh').disabled=false;}}
  }
  function chrome(){
    const sprite=document.querySelector('.icon-sprite');
    sprite.insertAdjacentHTML('beforeend',`<symbol id="patients-icon" viewBox="0 0 16 16"><circle cx="6" cy="5" r="2.5"/><path d="M1 14v-2a5 5 0 0 1 10 0v2M11 3a2.5 2.5 0 0 1 0 5m1 2a4 4 0 0 1 3 4"/></symbol><symbol id="review-icon" viewBox="0 0 16 16"><path d="M4 2h8v12H4zM6 5h4M6 8h4M6 11h2"/></symbol><symbol id="clock-icon" viewBox="0 0 16 16"><circle cx="8" cy="8" r="6"/><path d="M8 4v4l3 2"/></symbol><symbol id="appearance-icon" viewBox="0 0 16 16"><circle cx="8" cy="8" r="6"/><path d="M8 2v12M8 2a6 6 0 0 1 0 12z"/></symbol><symbol id="refresh-icon" viewBox="0 0 16 16"><path d="M13 6A5 5 0 1 0 13 10M13 2v4H9"/></symbol><symbol id="menu-icon" viewBox="0 0 16 16"><path d="M2 4h12M2 8h12M2 12h12"/></symbol><symbol id="chevron-icon" viewBox="0 0 16 16"><path d="m4 10 4-4 4 4"/></symbol>`);
    const heading=document.querySelector('.page-heading'), refresh=document.querySelector('.refresh-bar'), rail=document.querySelector('.rail');
    $('eyebrow').remove();$('subtitle').remove();
    heading.append(refresh);$('workspace').prepend(heading);
    if(patient){heading.append($('appearance-label'),$('theme'));}
    else {const account=rail.querySelector('.account');if(account){account.insertAdjacentHTML('afterbegin',avatar(account.textContent));rail.querySelector('.foot').prepend(account);}}
    $('refresh').className='ghost';$('refresh').innerHTML=`${icon('refresh-icon')}<span>${t('refresh')}</span>`;
  }
  chrome();
  $('title').textContent=patient?t('yourcare'):t(view);
  if(demo){$('navigation').innerHTML=`<a href="${patient?'/demo/patient':'/demo'}" aria-current="page">${icon('patients-icon')}${t(patient?'yourcare':'patients')}</a>`;}
  else $('navigation').innerHTML=patient?`<a href="/pp" aria-current="page">${icon('patients-icon')}${t('yourcare')}</a>`:['patients','inbox','history','preferences'].map(k=>`<a href="${k==='patients'?'/a':'/a/'+k}" ${view===k?'aria-current="page"':''}>${icon(k==='patients'?'patients-icon':k==='history'?'clock-icon':k==='preferences'?'appearance-icon':'review-icon')}${t(k)}</a>`).join('');
  $('refresh').onclick=load;
  window.addEventListener('pageshow',event=>{if(event.persisted)load();});
  if(demo&&!patient)window.addEventListener('hashchange',()=>{if(location.hash==='#workspace'||(view==='detail'&&new URLSearchParams(location.hash.slice(1)).has('tab')))return;state.detailTab=0;view=location.hash?'detail':'patients';load();});
  function patientControls() {
    const root=$('patient-controls'); if(!root)return;
    const hide=(element,hidden)=>{element.hidden=hidden;if(demo)element.style.display=hidden?'none':'';};
    if(demo)root.querySelectorAll('[hidden]').forEach(element=>hide(element,true));
    const tabs=[...document.querySelectorAll('#patient-tabs [role=tab]')];
    const selectTab=index=>{tabs.forEach((tab,i)=>{tab.setAttribute('aria-selected',String(i===index));tab.tabIndex=i===index?0:-1;});hide($('patient-settings'),index===0);[$('content'),root.querySelector('.patient-talk'),root.querySelector('.patient-upload')].filter(Boolean).forEach(el=>hide(el,index===1));updatePatientColumns();};
    tabs.forEach((tab,i)=>{tab.onclick=()=>selectTab(i);tab.onkeydown=event=>{let next;if(['ArrowLeft','ArrowRight'].includes(event.key))next=1-i;else if(event.key==='Home')next=0;else if(event.key==='End')next=1;if(next!==undefined){event.preventDefault();selectTab(next);tabs[next].focus();}};});selectTab(0);
    const upload=$('patient-upload-form');
    const help=upload.previousElementSibling;if(help?.tagName==='P'){help.id='patient-file-help';help.className='field-help';$('patient-file').before(help);$('patient-file').setAttribute('aria-describedby',help.id+' patient-action-result');}
    root.querySelectorAll('input,textarea').forEach(control=>{if(!control.hasAttribute('aria-describedby'))control.setAttribute('aria-describedby','patient-action-result');});
    upload.addEventListener('dragover',e=>{e.preventDefault();upload.classList.add('dragging');});
    upload.addEventListener('dragleave',()=>upload.classList.remove('dragging'));
    upload.addEventListener('drop',e=>{e.preventDefault();upload.classList.remove('dragging');if(e.dataTransfer.files.length){$('patient-file').files=e.dataTransfer.files;$('patient-file').focus();}});
    $('patient-stop').setAttribute('role','switch');$('patient-stop').setAttribute('aria-label','Reminders');$('patient-stop').setAttribute('aria-checked','false');

    const words=JSON.parse(root.dataset.words), w=key=>words[key]||words.failed;
    const result=$('patient-action-result'), csrf=()=>decodeURIComponent(document.cookie.split('; ').find(x=>x.startsWith('sanad_csrf='))?.slice(11)||'');
    let cursor=null, confirmation=null, stopped=false, busy=false, viewingOlder=false, waitingSince=null, outgoing=new Set(), waitingFor=null;
    const ids=new Map();
    const command=(name,payload)=>{const value=JSON.stringify(payload), old=ids.get(name);if(old?.value===value)return old.id;const id=crypto.randomUUID();ids.set(name,{value,id});return id;};
    let changedElsewhere=false;
    const failure=error=>{changedElsewhere ||= Boolean(error.changedElsewhere);result.textContent=failureReasons[error.reason]|| (changedElsewhere?w('changed_elsewhere')+' '+w('expired'):w([401,403].includes(error.status)?'expired':error.status===409?'conflict':'failed'));if([401,403].includes(error.status)){stopped=true;$('content').replaceChildren();root.querySelectorAll('section').forEach(x=>x.remove());}};
    async function get(path,options){if(demo)return patientDemo(path,options);const response=await fetch(path,options);if(!response.ok){const error=new Error();error.status=response.status;const detail=await response.json().catch(()=>({}));error.reason=detail.reason;error.changedElsewhere=String(detail.detail||'').startsWith('Your access changed elsewhere.');throw error;}return response.json();}
    const post=(path,name,payload)=>get(path,{method:'POST',headers:demo?{}:{'Content-Type':'application/json','X-CSRF-Token':csrf()},body:JSON.stringify({...payload,command_id:command(name,payload)})});
    function line(target,text){const p=document.createElement('p');p.textContent=text;target.append(p);}
    const uploadText=u=>w(u.state)+(u.category?' · '+w(u.category):'');
    async function demoUpload(progress) {
      const data=await demoFixture('patient');
      for (const value of [25, 65, 100]) {
        await new Promise(resolve=>setTimeout(resolve,300));
        progress.value=value;
      }
      const at=new Date().toISOString(), file={id:crypto.randomUUID(),received_at:at,state:'read'};
      data.uploads.push(file);
      data.conversation.push({id:file.id,at,direction:'inbound',text:$('patient-caption').value,upload:{state:'read'}});
      return {state:'read'};
    }
    function patientTime(value){
      const instant=new Date(value), zone=state.zone;
      const day=new Intl.DateTimeFormat('en-CA',{timeZone:zone});
      const today=day.format(instant)===day.format(new Date());
      const date=new Intl.DateTimeFormat('en-US',{timeZone:zone,month:'short',day:'numeric'}).format(instant);
      const clock=new Intl.DateTimeFormat('en-GB',{timeZone:zone,hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).format(instant);
      return today?'Today '+clock:date+', '+clock;
    }
    async function history(older=false){viewingOlder=older;const data=await get('/api/patient/conversation'+(older&&cursor?'?cursor='+encodeURIComponent(cursor):''));const target=$('patient-conversation'),fragment=document.createDocumentFragment();const latest=new Set(data.items.filter(i=>i.direction==='outbound').map(i=>i.id));if(waitingFor&&[...latest].some(id=>!waitingFor.has(id))){waitingSince=null;waitingFor=null;result.textContent=w('sent');}else if(waitingSince!==null&&Date.now()-waitingSince>=20000){result.textContent=w('still_working');}outgoing=latest;for(const item of data.items){const article=document.createElement('article');article.className='record-item msg '+(item.direction==='outbound'?'outbound doc':'pat');const doctorAnswer=item.direction==='outbound'&&/^(?:Your doctor's answer to your question:|رد الدكتور على سؤالك:)/.test(item.text||'');if(doctorAnswer)article.dataset.doctorAvatar='true';const name=item.direction==='outbound'?(doctorAnswer?doctorInitial():'S'):document.querySelector('.account bdi')?.textContent;article.insertAdjacentHTML('afterbegin',avatar(name));const copy=document.createElement('div');copy.className='b';article.append(copy);line(copy,item.credential?w('credential')+' '+patientTime(item.at):item.legacy?w('legacy')+' '+patientTime(item.at):item.text||'');if(item.upload)line(copy,uploadText(item.upload));const time=document.createElement('small');time.textContent=patientTime(item.at);copy.append(time);fragment.append(article);}if(older)target.prepend(fragment);else target.replaceChildren(fragment);if(!target.childNodes.length)target.innerHTML=emptyState(w('no_messages'),t('patient_note'));cursor=data.cursor;hide($('patient-older'),!cursor);}
    function renderPreferences(prefs,forceInputs=false){
      const quietChanged=!patientPreferences||JSON.stringify(patientPreferences.quiet_hours)!==JSON.stringify(prefs.quiet_hours);
      patientPreferences=prefs;
      $('patient-stop').setAttribute('aria-checked',String(prefs.reminders==='enabled'));
      $('patient-preferences').textContent=w(prefs.reminders)+' · '+prefs.timezone;
      const summary=$('patient-reminder-summary');if(summary)summary.textContent=w(prefs.reminders);
      const quiet=$('patient-quiet-summary');if(quiet)quiet.textContent=t('quiet')+': '+prefs.quiet_hours.join(', ')+' · '+prefs.timezone;
      if(quietChanged||forceInputs){$('patient-quiet-start').value=prefs.quiet_hours[0];$('patient-quiet-end').value=prefs.quiet_hours[1];}
    }
    function setConfirmation(token){confirmation=token||null;hide($('patient-confirm'),!confirmation);$('patient-confirm').disabled=!confirmation;}
    async function preferenceReply(reply){
      setConfirmation(reply.confirmation_token);
      if(reply.preferences)renderPreferences(reply.preferences,true);
      if(reply.queued){
        result.textContent=w('pending');
        const until=Date.now()+20000,controller=new AbortController();
        const deadline=setTimeout(()=>controller.abort(),20000);
        try{
          while(Date.now()<until&&!stopped){
            await new Promise(resolve=>setTimeout(resolve,1000));
            let prefs;
            try{prefs=await get('/api/patient/preferences?token='+encodeURIComponent(reply.token),{signal:controller.signal});}
            catch(error){if(controller.signal.aborted)break;if(![409,500,502,503].includes(error.status))throw error;continue;}
            renderPreferences(prefs,!prefs.queued);setConfirmation(prefs.confirmation_token);
            if(!prefs.queued){result.textContent=w(confirmation?'confirm':'saved');return;}
          }
        }finally{clearTimeout(deadline);}
        result.textContent=w('still_working');return;
      }
      if(!reply.preferences){renderPreferences(await get('/api/patient/preferences'),true);}
      result.textContent=w(confirmation?'confirm':'saved');
    }
    let uploadsExpanded=false;
    function renderUploads(data){
      const target=$('patient-uploads');target.replaceChildren();$('patient-uploads-toggle')?.remove();
      const files=[...(Array.isArray(data)?data:data.items)].sort((a,b)=>(+new Date(b.received_at)||0)-(+new Date(a.received_at)||0));
      if(files.length){const heading=document.createElement('h3');heading.textContent=w('documents_sent');target.append(heading);}
      for(const file of (uploadsExpanded?files:files.slice(0,5)))line(target,patientTime(file.received_at)+' · '+w(file.state));
      if(files.length>5){const button=document.createElement('button');button.id='patient-uploads-toggle';button.type='button';button.setAttribute('aria-controls','patient-uploads');button.setAttribute('aria-expanded',String(uploadsExpanded));button.textContent=uploadsExpanded?w('show_latest'):w('show_all').replace('{N}',files.length);button.onclick=()=>{uploadsExpanded=!uploadsExpanded;renderUploads(data);$('patient-uploads-toggle').focus();};target.after(button);}
      updatePatientColumns();
    }
    async function refresh(){
      if(stopped)return;
      // Render each independent reply immediately; history failure cannot freeze preferences.
      await Promise.all([
        get('/api/patient/preferences').then(renderPreferences),
        get('/api/patient/uploads').then(renderUploads),history()
      ]);
    }
    async function act(work,refreshAfter=true){if(busy||stopped)return;busy=true;root.querySelectorAll('button').forEach(b=>b.disabled=true);try{await work();if(refreshAfter)await refresh();}catch(error){failure(error);}finally{busy=false;root.querySelectorAll('button').forEach(b=>b.disabled=false);$('patient-confirm').disabled=!confirmation;}}
    $('patient-message-form').onsubmit=e=>{e.preventDefault();act(async()=>{waitingSince=Date.now();waitingFor=new Set(outgoing);const reply=await post('/api/patient/messages','message',{text:$('patient-message').value});ids.delete('message');$('patient-message').value='';result.textContent=reply.emergency||w(reply.status==='accepted'?'sent':'pending');if(reply.emergency){waitingSince=null;waitingFor=null;}});};
    $('patient-older').onclick=()=>{history(true).catch(failure);};
    const reminder=action=>act(async()=>{const reply=await post('/api/patient/preferences',action,{reminders:action});ids.delete(action);await preferenceReply(reply);},false);
    const switchLabel=document.createElement('span');switchLabel.className='switch-label';switchLabel.textContent=en?'Reminders':'التذكيرات';$('patient-stop').before(switchLabel);$('patient-stop').innerHTML='<span class="switch-track" aria-hidden="true"><span></span></span>';$('patient-stop').onclick=()=>reminder(patientPreferences?.reminders==='enabled'?'stop':'resume');
    $('patient-confirm').onclick=()=>{if(!confirmation)return;act(async()=>{const reply=await post('/api/patient/preferences/confirm','confirm',{token:confirmation});ids.delete('confirm');setConfirmation(null);await preferenceReply(reply);},false);};
    $('patient-quiet-form').onsubmit=e=>{e.preventDefault();if($('patient-quiet-start').value===$('patient-quiet-end').value){result.textContent=w('quiet_invalid');return;}act(async()=>{const reply=await post('/api/patient/preferences','quiet',{quiet_hours:[$('patient-quiet-start').value,$('patient-quiet-end').value]});ids.delete('quiet');await preferenceReply(reply);},false);};
    $('patient-upload-form').onsubmit=e=>{e.preventDefault();act(async()=>{const file=$('patient-file').files[0];if(!file||(!file.type.startsWith('image/')&&file.type!=='application/pdf')){result.textContent=w('unsupported');return;}const pdf=file.type==='application/pdf';if(file.size>(pdf?20000000:8*1024*1024)){result.textContent=w(pdf?'document_too_large':'too_large');return;}if(!pdf){let bitmap;try{bitmap=await createImageBitmap(file);}catch{result.textContent=w('unreadable');return;}const tooLarge=bitmap.width>8000||bitmap.height>8000||bitmap.width*bitmap.height>20000000;bitmap.close();if(tooLarge){result.textContent=w('too_large');return;}}result.textContent=w('uploading');const progress=$('patient-upload-progress');hide(progress,false);progress.value=0;try{const reply=demo?await demoUpload(progress):await new Promise((resolve,reject)=>{const xhr=new XMLHttpRequest();xhr.open('POST','/api/patient/uploads');xhr.setRequestHeader('X-CSRF-Token',csrf());xhr.setRequestHeader('Content-Type',file.type);const bytes=new TextEncoder().encode($('patient-caption').value);xhr.setRequestHeader('X-Upload-Caption',btoa(Array.from(bytes,b=>String.fromCharCode(b)).join('')));xhr.upload.onprogress=event=>{if(event.lengthComputable)progress.value=100*event.loaded/event.total;};xhr.onerror=()=>reject(new Error());xhr.onload=()=>{let data;try{data=JSON.parse(xhr.responseText);}catch{reject(new Error());return;}if([401,403].includes(xhr.status)){const error=new Error();error.status=xhr.status;error.changedElsewhere=String(data.detail||'').startsWith('Your access changed elsewhere.');reject(error);}else if(xhr.status>=400&&data.category){resolve(data);}else if(xhr.status>=400){reject(Object.assign(new Error(),{status:xhr.status,reason:data.reason}));}else resolve(data);};xhr.send(file);});result.textContent=demo?w('read'):reply.category?w(reply.category):w('received');if(!reply.category){$('patient-file').value='';$('patient-caption').value='';}}finally{hide(progress,true);}});};
    window.addEventListener('patient-session-expired',()=>failure({status:401}));
    if(demo)window.addEventListener('patient-demo-updated',()=>refresh().catch(failure));
    $('refresh').addEventListener('click',()=>{viewingOlder=false;refresh().catch(failure);});
    refresh().catch(failure);
    const timer=setInterval(()=>{if(stopped){clearInterval(timer);return;}if(!busy&&!document.hidden&&!viewingOlder)refresh().catch(failure);},5000);
  }
  function patientSchedules() {
    const root=$('patient-schedules');if(!root||demo)return;
    const words=JSON.parse($('patient-controls').dataset.words),w=key=>words[key]||words.failed;
    let stopped=false;
    const csrf=()=>decodeURIComponent(document.cookie.split('; ').find(x=>x.startsWith('sanad_csrf='))?.slice(11)||'');
    async function request(path,body){
      const options=body?{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf()},body:JSON.stringify(body)}:{};
      const response=await fetch(path,options);
      if(!response.ok){if([401,403].includes(response.status)){stopped=true;root.replaceChildren();}throw new Error(w([401,403].includes(response.status)?'expired':response.status===409?'conflict':'failed'));}
      return response.json();
    }
    function showReply(target,data){
      target.replaceChildren();if(!data)return;const text=document.createElement('p');text.textContent=data.text||'';target.append(text);
      for(const row of data?.reply_markup?.inline_keyboard||[]){for(const choice of row){
        const button=document.createElement('button');button.type='button';button.textContent=choice.text;target.append(button);
        const command={command_id:crypto.randomUUID(),token:choice.callback_data};
        button.onclick=async()=>{
          target.querySelectorAll('button').forEach(b=>b.disabled=true);
          try{await result(target,await request('/api/patient/preferences/confirm',command));}
          catch(error){text.textContent=error.message;target.querySelectorAll('button').forEach(b=>b.disabled=false);}
        };
      }}
    }
    async function result(target,reply){
      if(reply.queued){
        target.textContent=w('pending');const deadline=Date.now()+20000;
        while(!stopped&&Date.now()<deadline){
          await new Promise(resolve=>setTimeout(resolve,1000));
          const current=await request('/api/patient/preferences?token='+encodeURIComponent(reply.token));
          if(!current.queued){showReply(target,current.schedule_reply);return;}
        }
        target.textContent=w('still_working');return;
      }
      showReply(target,reply.schedule_reply);
    }
    async function refresh(){
      if(stopped)return;const data=await request('/api/patient/schedules');root.replaceChildren();
      for(const plan of data.plans){
        const fieldset=document.createElement('fieldset'),legend=document.createElement('legend');
        legend.textContent=plan.title+' · '+plan.timezone;fieldset.append(legend);
        const open=document.createElement('button');open.type='button';open.textContent=w('schedule_change');fieldset.append(open);
        const form=document.createElement('form');form.hidden=true;form.style.display='none';
        for(const value of plan.times){const label=document.createElement('label');label.textContent=String(form.querySelectorAll('input').length+1);const input=document.createElement('input');input.type='time';input.className='clock';input.required=true;input.min='06:00';input.max='23:30';input.value=value;label.append(input);form.append(label);}
        const save=document.createElement('button');save.type='submit';save.textContent=w('schedule_save');form.append(save);fieldset.append(form);
        const output=document.createElement('div');output.setAttribute('role','status');fieldset.append(output);root.append(fieldset);
        open.onclick=()=>{form.hidden=false;form.style.display='';form.querySelector('input').focus();};
        let pending=null;
        form.onsubmit=async event=>{event.preventDefault();if(save.disabled)return;save.disabled=true;
          const times=Array.from(form.querySelectorAll('input'),input=>input.value);
          if(!pending||JSON.stringify(pending.times)!==JSON.stringify(times))pending={command_id:crypto.randomUUID(),mission_id:plan.id,version:plan.version,times};
          try{await result(output,await request('/api/patient/preferences',pending));pending=null;}
          catch(error){output.textContent=error.message;}finally{save.disabled=false;}
        };
      }
    }
    $('refresh').addEventListener('click',()=>refresh().catch(error=>root.textContent=error.message));
    window.addEventListener('patient-session-expired',()=>{stopped=true;root.replaceChildren();});
    refresh().catch(error=>root.textContent=error.message);
  }
  document.querySelectorAll('time[data-browser-instant]').forEach(el=>{el.outerHTML=time(el.dateTime);});
  patientControls();
  patientSchedules();
  if(patient)patientColumns();
  load();
})();
