'use strict';
(() => {
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
  if (document.getElementById('admin-applications')) {
    const target = document.getElementById('admin-applications');
    const status = document.getElementById('admin-result');
    const csrf = () => decodeURIComponent(document.cookie.split('; ').find(x => x.startsWith('sanad_csrf='))?.slice(11) || '');
    const send = async (path, body) => {
      const response = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json', 'X-CSRF-Token':csrf()}, body:JSON.stringify(body)});
      const result = await response.json();
      status.textContent = response.ok ? 'Account action recorded.' : 'Action refused. Refresh and check the account before retrying.';
      return response;
    };
    const load = async () => {
      const response = await fetch('/api/admin/applications');
      if (!response.ok) { status.textContent = 'Sign in again.'; return; }
      const rows = await response.json();
      target.replaceChildren();
      const summary=document.createElement('div');summary.className='summary-strip';
      for(const [label,value] of [['Applications',rows.length],['Awaiting review',rows.filter(r=>r.status==='pending').length],['Approved',rows.filter(r=>r.status==='approved').length]]){const tile=document.createElement('article');tile.className='summary-tile';const caption=document.createElement('span');caption.textContent=label;const count=document.createElement('strong');count.textContent=String(value);tile.append(caption,count);summary.append(tile);}target.append(summary);
      if(!rows.length){const empty=document.createElement('div');empty.className='empty';empty.innerHTML='<svg class="icon" viewBox="0 0 16 16" aria-hidden="true"><path d="M4 2h8v12H4zM6 5h4M6 8h4M6 11h2"/></svg><p class="empty-title">No applications are waiting.</p><p class="empty-next">New applications will appear here.</p>';target.append(empty);}
      for (const row of rows) {
        const item = document.createElement('section'); item.className='section';
        const label = document.createElement('p');
        label.textContent = `${row.name} · ${row.specialty} · ${row.city} · ${row.applied_at} · ${row.status}`;
        item.append(label);
        for (const verb of ['approve','reject','suspend','reinstate']) {
          const button = document.createElement('button'); button.textContent = verb;
          const clinicalId = verb === 'suspend' || verb === 'reinstate';
          button.disabled = clinicalId ? !row.doctor_id : row.status !== 'pending';
          let reason;
          if (verb === 'reject' || verb === 'suspend') {
            reason = document.createElement('select'); reason.setAttribute('aria-label', `${verb} reason`);
            for (const value of verb === 'reject' ? ['admin_rejected','unverified'] : ['coverage']) {
              const option = document.createElement('option'); option.value = value; option.textContent = value.replaceAll('_',' '); reason.append(option);
            }
            item.append(reason);
          }
          button.onclick = async () => {
            button.disabled = true;
            try {
              await send(`/api/admin/${clinicalId ? 'doctors' : 'applications'}/${encodeURIComponent(clinicalId ? row.doctor_id : row.id)}/${verb}`, {command_id:crypto.randomUUID(), expected_version:clinicalId ? row.doctor_version : row.version, ...(reason ? {reason_code:reason.value} : {})});
              await load();
            } catch { status.textContent = 'Request failed. Refresh before retrying.'; button.disabled = false; }
          };
          item.append(button);
        }
        target.append(item);
      }
    };
    document.getElementById('admin-logout').onclick = async () => {
      try { const response = await send('/api/admin/logout', {}); if (response.ok) { target.replaceChildren(); status.textContent = 'Signed out everywhere.'; } }
      catch { status.textContent = 'Sign out failed. Try again.'; }
    };
    load().catch(() => { status.textContent = 'Could not load applications.'; });
    return;
  }
  const body = document.body, lang = document.documentElement.lang;
  const en = lang === 'en', patient = body.dataset.audience === 'patient', demo = body.dataset.demo === 'true';
  let view = body.dataset.view;
  const words = {
    patients:['Patients','المرضى'], inbox:['Inbox','المراجعات'], history:['Review history','سجل المراجعات'], preferences:['Preferences','التفضيلات'],
    detail:['Patient record','ملف المريض'], yourcare:['Your care','خطتك'], clinic:['Your clinical workspace','مساحة المتابعة'],
    subtitle:['A clear view of what needs your attention.','المطلوب متابعته في مكان واحد.'],
    search:['Search patients','ابحث عن مريض'], filter:['Show','اعرض'], all:['All patients','كل المرضى'], active:['Active','نشط'], overdue:['Overdue','متأخر'], blocked:['Blocked','متعطل'], awaiting_link:['Awaiting link','بانتظار الربط'],
    clear:['Clear filters','إلغاء الفلاتر'], results:['results','نتيجة'], patient:['Patient','المريض'], age:['Age','العمر'], outstanding:['Outstanding','المطلوب'], due:['Due','الموعد'], status:['Status','الحالة'],
    missing:['Missing','غير موجود'], not_yet_due:['Not yet due','لم يحن الموعد'], processing:['Received but processing','وصل وجار تجهيزه'], unverifiable:['Unverifiable','غير قابل للتحقق'], fulfilled:['Fulfilled','تم استيفاء المطلوب'], pending_review:['Pending review','بانتظار المراجعة'], resolved:['Resolved','تمت المراجعة'], open:['Open','مفتوح'], acknowledged:['Acknowledged','تم الاطلاع'],
    unmatched:['Unmatched document','مستند غير مرتبط'], accepted:['Accepted document','مستند مقبول'], rejected:['Rejected document','مستند مرفوض'], not_required:['No review required','لا تتطلب مراجعة'], correction_requested:['Correction requested','مطلوب تصحيح'],
    unassigned:['No patient associated','لم يرتبط بمريض بعد'], no_work:['No outstanding obligation recorded','لا توجد متابعة معلّقة مسجلة'], more:['other outstanding items','متابعات معلّقة أخرى'],
    refresh:['Refresh','تحديث'], updated:['Updated','تم التحديث'], timezone:['Times shown in','التوقيت المعروض'], previous:['Previous','السابق'], next:['Next','التالي'], page:['Page','صفحة'], of:['of','من'],
    empty:['No patient needs anything in this view. Clear filters to see all patients.','لا توجد نتائج في هذه الصفحة.'], no_patients:['No patients recorded yet. Add a patient through Telegram.','لم يُسجل مرضى بعد. أضف مريضاً من تيليجرام.'],
    failed:['Could not load this view. Refresh to try again or return to Patients.','تعذر تحميل الصفحة. حدّثها للمحاولة أو ارجع للمرضى.'],
    expired:['Your session has expired. Open Telegram and use /login to sign in again.','انتهت جلسة الدخول. افتح تيليجرام واستخدم /login للدخول من جديد.'],
    denied:['This record is unavailable to your account. Return to Patients.','هذا الملف غير متاح لحسابك. ارجع للمرضى.'],
    stale:['The information changed in another tab. Refresh, check it, and try again.','تغيرت البيانات في صفحة أخرى. حدّثها وراجعها ثم حاول مجدداً.'],
    loading:['Loading your page…','جار تحميل الصفحة…'], plan:['Active orders','التعليمات الحالية'], missions:['Care requests','طلبات المتابعة'], facts:['Record and history','البيانات والتاريخ المرضي'], evidence:['Evidence and provenance','المستندات ومصادرها'],
    no_plan:['No active medication is recorded.','لا يوجد دواء حالي مسجل.'], no_requests:['No upcoming request is recorded.','لا يوجد طلب قادم مسجل.'],
    received:['Received','وصل'], source:['Source','المصدر'], version:['Version','النسخة'], printed:['Printed date','التاريخ على المستند'], not_recorded:['Not recorded','غير مسجل'],
    monitoring:['Monitoring readings','قراءات المتابعة'], slot:['Scheduled time','الموعد المحدد'], reading:['Reading','القراءة'], extras:['Extra readings (outside scheduled slots)','قراءات إضافية خارج المواعيد'],
    changed:['Material change since the original review','تغير مهم منذ المراجعة الأصلية'], notice:['First notice','أول إشعار'], reason:['Resolution reason','سبب انتهاء المراجعة'], review_note:['Acknowledgment does not resolve a review. Actions remain in Telegram.','الاطلاع لا ينهي المراجعة. إجراءات المراجعة في تيليجرام.'],
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
    intake_telegram:['Handled from the intake message in Telegram.','اتعامل معاه من رسالة الملف الجديد في تيليجرام.'],
    review_telegram:['Handled from the review message in Telegram.','اتعامل معاه من رسالة المراجعة في تيليجرام.'],
    due_now:['due now','ميعاده دلوقتي'], due_today:['due today','ميعاده النهارده'], due_in:['due in','فاضل عليه'], late_by:['overdue by','متأخر بقاله'],
    day:['day','يوم'], days:['days','أيام'], hour:['hour','ساعة'], hours:['hours','ساعات'],
    current_plan:['Current plan','الخطة الحالية'], outstanding_work:['Outstanding work','المتابعة المطلوبة'], drawer_evidence:['Evidence','المستندات'], drawer_history:['History','السجل'],
    no_evidence:['No evidence is recorded for this patient.','مفيش مستندات مسجلة للمريض ده.'], no_corrections:['No corrections are recorded for this patient.','مفيش تصحيحات مسجلة للمريض ده.'],
    followups:['Follow-ups','المتابعات'], MEDICATION_DAY3:['Day-three medication follow-up','متابعة الدواء في اليوم التالت'], CLINICAL_CHECKIN:['Clinical check-in','متابعة الحالة'], awaiting_anchor:['Waiting for the start date','مستني تاريخ البداية'], scheduled:['Scheduled','متحدد ميعاده'], waiting_response:['Waiting for a response','مستني الرد'], contact_suppressed:['Contact paused','التواصل متوقف'],
    full_record:['Open full patient record','افتح ملف المريض بالكامل']
  });
  const t = key => words[key]?.[en ? 0 : 1] || words.not_recorded[en ? 0 : 1];
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const bdi = value => `<bdi>${esc(value)}</bdi>`;
  const $ = id => document.getElementById(id);
  const state = {records:[], reviews:[], evidence:[], pref:null, data:null, sort:'due', descending:false, page:1, query:'', filter:'all', zone:'UTC'};
  const params = new URLSearchParams(location.search);
  if (!demo) {state.query=params.get('q')||'';state.filter=params.get('filter')||'all';state.sort=params.get('sort')||'due';state.descending=params.get('desc')==='1';state.page=Math.max(1,Number(params.get('page'))||1);}
  let generation=0, currentAbort=null;
  const errorText = status => status === 401 || status === 403 ? t('expired') : status === 404 ? t('denied') : status === 409 ? t('stale') : t('failed');
  async function api(path, options={}) {
    const response = await fetch(path,{credentials:demo?'omit':'same-origin',cache:'no-store',...options});
    if(!response.ok) throw Object.assign(new Error(errorText(response.status)),{status:response.status});
    return response.json();
  }
  function icon(name='status-icon') {return `<svg class="icon" viewBox="0 0 16 16" aria-hidden="true"><use href="#${name}"/></svg>`;}
  function badge(key, weight=tone(key)) {return `<span class="status ${weight}"><span>${esc(t(key))}</span></span>`;}
  function time(value, zone=state.zone, compact=false) {
    if(!value) return `<span class="muted">${t('no_due')}</span>`;
    const date=new Date(value); if(Number.isNaN(+date)) return bdi(value);
    const minutes=Math.round((+date-Date.now())/60000), abs=Math.abs(minutes);
    const unit=abs>=1440?'day':abs>=60?'hour':'minute', amount=unit==='day'?Math.trunc(minutes/1440):unit==='hour'?Math.trunc(minutes/60):minutes;
    const absolute=new Intl.DateTimeFormat(lang,{...(compact?{}:{year:'numeric',month:'short',day:'numeric'}),hour:'2-digit',minute:'2-digit',timeZone:zone}).format(date);
    const relative=new Intl.RelativeTimeFormat(lang,{numeric:'always'}).format(amount,unit);
    return `<time datetime="${esc(value)}">${bdi(absolute)}</time><small>${bdi(relative)}</small>`;
  }
  function reviewTime(value, now=Date.now(), zone=state.zone) {
    const due=+new Date(value), delta=due-now;
    if(!value||!Number.isFinite(due))return t('no_due');
    const calendar=new Intl.DateTimeFormat('en-CA',{timeZone:zone});
    if(delta>0&&calendar.format(new Date(due))===calendar.format(new Date(now)))return t('due_today');
    const hours=Math.floor(Math.abs(delta)/3600000);
    if(hours===0)return t('due_now');
    const days=Math.floor(hours/24), amount=days||hours, unit=days?(days===1?'day':'days'):(hours===1?'hour':'hours');
    return `${t(delta<0?'late_by':'due_in')} ${amount} ${t(unit)}`;
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
    const r=item.review, [word,action,tab]=reviewPresentation(r), scope=intakeReview(r)?'unassigned_intake':'your_account';
    const name=r.patient_id?record.display_name:t(scope);
    const href=tab==='questions'?'#questions':`${link(record)}#tab=${tab}`;
    return `<details class="inbox-item" id="inbox-${esc(r.id)}" ${item.urgent||+new Date(r.review_at)<Date.now()?'open':''}><summary><span class="review-line">${esc(t(word))} · ${bdi(name)} · ${esc(reviewTime(r.review_at))}</span> ${badge(item.status,item.urgent?'danger':tone(item.status))}</summary><p>${time(r.review_at)}</p>${tab?`<a class="button" href="${esc(href)}">${t(action)}</a>`:`<p class="review-action">${t(action)}</p>`}</details>`;
  }
  function stateOf(m) {
    if (m.fulfillment_validity==='invalidated_pending_review') return 'invalidated_pending_review';
    if (m.state==='fulfilled') return 'fulfilled';
    if (['resolved','acknowledged','cancelled','closed_unfulfilled','invalidated_pending_review','superseded','proposed'].includes(m.state)) return m.state;
    if (m.state==='blocked') return 'blocked';
    return m.due_at && +new Date(m.due_at)>Date.now()?'not_yet_due': 'missing';
  }
  function tone(key) {return ['danger','incident_response','unverifiable'].includes(key)?'danger':['missing','overdue'].includes(key)?'warning':['processing','pending_review','pending','open','invalidated_pending_review'].includes(key)?'info':['fulfilled','resolved','satisfied','completed'].includes(key)?'success':'quiet';}
  const summaryLabels=Object.assign(Object.create(null),{danger:'Danger',overdue:'Overdue',pending_review:'Needs review',due_today:'Due today'});
  function matchesSummary(item,filter){
    if(filter==='danger')return Boolean(item.urgent);
    if(filter==='pending_review')return Boolean(item.review);
    if(!item.due)return false;
    if(filter==='overdue')return +new Date(item.due)<Date.now();
    const calendar=new Intl.DateTimeFormat('en-CA',{timeZone:state.zone});
    return filter==='due_today'&&calendar.format(new Date(item.due))===calendar.format(new Date());
  }

  function reviewRows(record, history=false) {return (history?record.review_history:record.reviews)||[];}
  function obligations(record) {
    const reviews=reviewRows(record).map(r=>({id:r.id,title:t(r.review_kind),due:r.review_at,status:r.state==='open'?'pending_review':r.state,urgent:r.review_kind==='incident_response',review:r}));
    const missions=(record.missions||[]).filter(m=>!['fulfilled','cancelled','closed_unfulfilled','superseded'].includes(m.state)).map(m=>({id:m.id,title:m.title,due:m.due_at,status:stateOf(m),urgent:false}));
    const followups=(record.followups||[]).filter(f=>!['fulfilled','cancelled'].includes(f.state)).map(f=>({id:f.id,title:t(f.kind),due:f.due_at||f.review_at,status:f.state,urgent:false}));
    return [...reviews,...missions,...followups].sort((a,b)=>Number(b.urgent)-Number(a.urgent)||(+new Date(a.due)-+new Date(b.due))||a.id.localeCompare(b.id));
  }
  function link(record) {return demo?`/demo#${encodeURIComponent(record.patient_id)}`:`/a/patients/${encodeURIComponent(record.patient_id)}`;}
  function tableRows() {
    let rows=[];
    for(const record of state.records){
      const work=obligations(record);
      if(view==='inbox'||view==='history') for(const r of reviewRows(record,view==='history')) rows.push({record,item:{id:r.id,title:t(r.review_kind),due:r.review_at,status:r.state,urgent:r.review_kind==='incident_response'&&r.state!=='resolved',review:r},count:1});
      else rows.push({record,item:(state.filter in summaryLabels?work.find(x=>matchesSummary(x,state.filter)):work[0])||null,count:work.length});
    }
    if(view==='inbox'||view==='history')for(const r of state.reviews.filter(r=>!r.patient_id)){rows.push({record:{patient_id:'',display_name:t('unassigned'),reviews:[],missions:[]},item:{id:r.id,title:t(r.review_kind),due:r.review_at,status:r.state,urgent:r.review_kind==='incident_response'&&r.state!=='resolved',review:r},count:1});}
    rows=rows.filter(({record,item})=>(`${record.display_name} ${record.patient_id} ${item?.title||''}`).toLocaleLowerCase().includes(state.query.toLocaleLowerCase())&&(
      state.filter==='all'||state.filter===record.contact_status||(state.filter in summaryLabels&&item&&matchesSummary(item,state.filter))||(state.filter==='blocked'&&record.missions?.some(m=>m.state==='blocked'))));
    const val=row=>state.sort==='patient'?row.record.display_name:state.sort==='age'?(Number(row.record.age)||0):state.sort==='last_activity'?(+new Date(row.record.last_activity_at)||0):state.sort==='outstanding'?(row.item?.title||''):state.sort==='status'?(row.item?.status||''):row.item?.due?+new Date(row.item.due):Infinity;
    rows.sort((a,b)=>{if(view==='inbox'&&a.item?.urgent!==b.item?.urgent)return Number(b.item?.urgent)-Number(a.item?.urgent);const av=val(a),bv=val(b); const cmp=typeof av==='string'?av.localeCompare(String(bv),lang):(av===bv?0:av<bv?-1:1);return (state.descending?-cmp:cmp)||a.record.patient_id.localeCompare(b.record.patient_id)||(a.item?.id||'').localeCompare(b.item?.id||'');});
    return rows;
  }
  function remember() {
    if(demo)return;
    const q=new URLSearchParams();if(state.query)q.set('q',state.query);if(state.filter!=='all')q.set('filter',state.filter);q.set('sort',state.sort);if(state.descending)q.set('desc','1');q.set('page',state.page);
    history.replaceState({...history.state,scroll:scrollY},'',`${location.pathname}?${q}`);
  }
  function list() {
    const rows=tableRows();state.page=Math.min(state.page,Math.max(1,Math.ceil(rows.length/50)));
    const columns=['patient','age','outstanding','due','last_activity','status'];
    $('content').innerHTML=`<div class="toolbar"><label class="search">${t('search')}<input id="search" type="search" value="${esc(state.query)}" autocomplete="off"></label><label>${t('filter')}<select id="filter">${['all','active','danger','overdue','pending_review','due_today','blocked','awaiting_link'].map(k=>`<option value="${k}" ${state.filter===k?'selected':''}>${summaryLabels[k]||t(k)}</option>`).join('')}</select></label><span class="muted">${t('timezone')}: ${bdi(state.zone)}</span></div><div class="filter-summary"><span id="result-count">${rows.length} ${t('results')} · ${summaryLabels[state.filter]||t(state.filter)}${state.query?' · '+bdi(state.query):''}</span><button id="clear">${t('clear')}</button></div><div class="work-surface"><table class="clinical" role="table"><caption>${t(view==='patients'?'outstanding':view)}. ${t('sort_help')}</caption><colgroup>${columns.map(()=>'<col>').join('')}</colgroup><thead><tr role="row">${columns.map(k=>`<th role="columnheader" scope="col" ${state.sort===k?`aria-sort="${state.descending?'descending':'ascending'}"`:''}><button data-sort="${k}">${k==='last_activity'?'Last activity':t(k)} <svg class="icon sort-chevron" viewBox="0 0 16 16" aria-hidden="true"><use href="#chevron-icon"/></svg></button></th>`).join('')}</tr></thead><tbody>${rows.slice((state.page-1)*50,state.page*50).map(({record,item,count})=>`<tr role="row" class="patient-row ${item?.urgent?'urgent':''}"><td role="cell"><span class="stack-label">${t('patient')}</span>${record.patient_id?`<a data-record href="${esc(link(record))}">${bdi(record.display_name)}<small>${esc(t(record.contact_status))}</small></a>`:`<span>${t('unassigned')}</span>`}</td><td role="cell" class="age"><span class="stack-label">${t('age')}</span><span class="age-value ${record.age==null?'muted':''}">${bdi(record.age??t('not_recorded'))}</span></td><td role="cell"><span class="stack-label">${t('outstanding')}</span>${bdi(item?.title||t('no_work'))}${count>1?`<small>${count-1} ${t('more')}</small>`:''}${item?.review?.last_material_change_version>item?.review?.source_version?`<small>${t('changed')}</small>`:''}</td><td role="cell" class="due"><span class="stack-label">${t('due')}</span>${item?time(item.due,state.zone,state.sort==='due'):t('not_recorded')}</td><td role="cell" class="muted"><span class="stack-label">Last activity</span>${activity(record.last_activity_at)}</td><td role="cell"><span class="stack-label">${t('status')}</span>${item?badge(item.status,item.urgent?'danger':tone(item.status)):badge('not_recorded')}</td></tr>`).join('')}</tbody></table>${!rows.length?`${emptyState(t(state.records.length?'empty':'no_patients'),t('refresh'),Boolean(state.query||state.filter!=='all'))}`:''}</div><div class="pager"><button id="previous" ${state.page<=1?'disabled':''}><span class="turn-arrow" aria-hidden="true">←</span> ${t('previous')}</button><span>${t('page')} ${state.page} ${t('of')} ${Math.max(1,Math.ceil(rows.length/50))}</span><button id="next" ${state.page*50>=rows.length?'disabled':''}>${t('next')} <span class="turn-arrow" aria-hidden="true">→</span></button></div>`;
    $('search').addEventListener('input',e=>{const start=e.target.selectionStart;state.query=e.target.value;state.page=1;list();$('search').focus();$('search').setSelectionRange(start,start);});
    document.querySelector('[data-clear]')?.addEventListener('click',()=>$('clear').click());
    $('filter').onchange=e=>{state.filter=e.target.value;state.page=1;list();$('filter').focus();};
    $('clear').onclick=()=>{state.query='';state.filter='all';state.page=1;list();$('search').focus();};
    for(const button of document.querySelectorAll('[data-sort]'))button.onclick=()=>{state.descending=state.sort===button.dataset.sort?!state.descending:false;state.sort=button.dataset.sort;state.page=1;list();document.querySelector(`[data-sort="${state.sort}"]`).focus();};
    $('previous').onclick=()=>{state.page--;list();$('next').focus();};$('next').onclick=()=>{state.page++;list();$('previous').focus();};remember();
    if(state.sort==='due'){
      const visible=rows.slice((state.page-1)*50,state.page*50);
      const calendar=new Intl.DateTimeFormat(lang,{year:'numeric',month:'short',day:'numeric',timeZone:state.zone});
      const day=x=>x.item?.due?calendar.format(new Date(x.item.due)):t('no_due');
      const elements=[...document.querySelectorAll('.clinical .patient-row')];
      visible.forEach((row,i)=>{
        const label=day(row);if(i&&day(visible[i-1])===label)return;
        let count=1;while(i+count<visible.length&&day(visible[i+count])===label)count++;
        elements[i].insertAdjacentHTML('beforebegin',`<tr class="day-group" role="row"><th scope="rowgroup" colspan="6">${bdi(label)} <span class="count">${count}</span></th></tr>`);
      });
    }
    listPresentation(rows);
    for(const anchor of document.querySelectorAll('[data-record]'))anchor.onclick=e=>{if(!e.ctrlKey&&!e.metaKey&&!e.shiftKey){e.preventDefault();openDrawer(anchor);return;}if(!demo){try{sessionStorage.setItem('sanad-list-return',location.pathname+location.search);sessionStorage.setItem('sanad-list-scroll',String(scrollY));}catch(_){}}history.replaceState({...history.state,scroll:scrollY,focus:anchor.getAttribute('href')},'');};
    if(view==='inbox')questions();
  }
  function activity(value){
    if(!value)return t('not_recorded');
    const minutes=Math.floor((Date.now()-new Date(value))/60000), amount=Math.abs(minutes);
    const [number,unit]=amount<60?[minutes,'minute']:amount<1440?[Math.floor(minutes/60),'hour']:[Math.floor(minutes/1440),'day'];
    return `<time datetime="${esc(value)}">${esc(new Intl.RelativeTimeFormat(lang,{numeric:'auto'}).format(-number,unit))}</time>`;
  }
  function section(title, content){return `<section class="section"><h2>${t(title)}</h2>${content}</section>`;}
  function empty(key='empty'){return emptyState(key==='empty'?t('not_recorded')+'.':t(key), patient?t('patient_note'):t('refresh'));}
  function emptyState(sentence, next, clear=false){
    if(next===t('refresh'))next='Refresh to see the latest information.';
    return `<div class="empty">${icon('review-icon')}<p class="empty-title">${esc(sentence)}</p><p class="empty-next">${esc(next)}</p>${clear?`<button class="secondary" data-clear>${t('clear')}</button>`:''}</div>`;
  }
  function provenance(value){if(!value)return ''; const rows=Array.isArray(value)?value:[value];return rows.filter(p=>p.received_at).map(p=>`<p class="provenance">${t('received')}: ${time(p.received_at)}</p>`).join('');}
  function clinicalLabel(value){const text=String(value??'');return words[text]?t(text):text.includes('_')?t('not_recorded'):text;}
  function instruction(order){const i=order.structured_instruction||{};const comparisons={gt:'above',ge:'at least',lt:'below',le:'at most'};return ['drug','dose','frequency','timing','route','duration','text','metric','comparator','threshold','unit'].filter(k=>i[k]!==undefined&&i[k]!==null&&i[k]!=='').map(k=>bdi(k==='comparator'?comparisons[i[k]]||t('not_recorded'):k==='metric'?clinicalLabel(i[k]):i[k])).join(' · ');}
  function monitor(m){const d=m.details||{};if(d.kind!=='MONITOR')return '';return `<table class="reading-table" role="table"><caption>${t('monitoring')} · ${bdi(clinicalLabel(d.metric))} · ${bdi(d.unit)} · ${bdi(state.zone)}</caption><thead><tr><th scope="col">${t('slot')}</th><th scope="col">${t('reading')}</th><th scope="col">${t('source')}</th></tr></thead><tbody>${(d.slots||[]).map((slot,index)=>{const r=(d.readings||[]).findLast(r=>r.slot===index);return `<tr role="row"><th role="rowheader" scope="row">${time(slot)}</th><td role="cell"><span class="stack-label">${t('reading')}</span>${r?bdi(r.value)+' '+bdi(d.unit):badge(+new Date(slot)>Date.now()?'not_yet_due':'missing')}</td><td role="cell"><span class="stack-label">${t('source')}</span>${r?`${t('received')}<small>${t('received')}: ${time(r.received_at)}</small>`:t('missing')}</td></tr>`;}).join('')}</tbody></table>${(d.readings||[]).some(r=>r.slot===null)?`<h3>${t('extras')}</h3>${d.readings.filter(r=>r.slot===null).map(r=>`<p>${bdi(r.value)} ${bdi(d.unit)} ${time(r.observed_at)}</p>`).join('')}`:''}`;}
  function detail(record){
    const orders=(record.orders||[]).filter(o=>o.status==='active');
    const consentHTML=`<div class="consent-binding"><h3>Consent and linking</h3>${(record.consents||[]).map(c=>`<p>Consent version ${bdi(c.version)} (${bdi(c.policy_text_version)}), accepted ${time(c.accepted_at)}${c.withdrawn_at?` · Withdrawn ${time(c.withdrawn_at)}`:''}</p>`).join('')||'<p>No consent recorded.</p>'}${(record.bindings||[]).map(b=>`<p>Patient binding: ${esc(t(b.status))} · ${time(b.confirmed_at)}</p>`).join('')||'<p>Patient binding: awaiting link.</p>'}</div>`;
    const ordersHTML=orders.map(o=>`<article class="record-item" data-order="${esc(o.id)}"><p>${instruction(o.current_version||{})}</p><small>${t(o.status)}</small>${provenance(o.current_version?.provenance)}${!demo?`<button data-amend="${esc(o.id)}">Amend instruction</button>`:''}<details><summary>${t('history')}</summary>${(o.history||[]).filter(h=>h.id!==o.current_version?.id).map(h=>`<p>${t('historical')} · ${instruction(h)}</p>${provenance(h.provenance)}`).join('')||empty()}</details></article>`).join('')||empty('no_plan');
    const missions=(record.missions||[]).map(m=>`<article class="record-item" data-mission="${esc(m.id)}"><h3>${bdi(m.title)}</h3>${badge(stateOf(m),tone(stateOf(m)))} <p>${t('due')}: ${time(m.due_at)}</p><p>${t('review')}: ${badge(m.review_status==='pending'?'pending_review':m.review_status==='reviewed'?'resolved':m.review_status==='acknowledged'?'acknowledged':m.review_status)}</p>${monitor(m)}${!demo&&['fulfilled','cancelled','closed_unfulfilled'].includes(m.state)?`<button data-reopen="${esc(m.id)}">Preview reopening</button>`:''}</article>`).join('')||empty('no_requests');
    const followups=(record.followups||[]).map(f=>`<article class="record-item" data-followup="${esc(f.id)}"><h3>${esc(t(f.kind))}</h3>${badge(f.state)}<p>${t('due')}: ${time(f.due_at)}</p></article>`).join('');
    const facts=(record.facts||[]).map(f=>`<article class="record-item" data-fact="${esc(f.id)}"><p>${bdi(f.payload?.text||f.payload?.clinical_en||f.text||t('retained'))}</p>${provenance(f.provenance)}${!demo?`<button data-correct-fact="${esc(f.id)}">Correct or detach</button>`:''}</article>`).join('')||empty();
    const evidence=state.evidence.map(e=>`<article class="record-item" data-evidence="${esc(e.id)}"><h3>${esc(t(e.category))}</h3>${badge(e.association_state==='accepted_pending_identity'?'unverifiable':e.association_state==='candidate'?'pending_review':e.association_state)}<p>${t('printed')}: ${bdi(e.printed_date||t('not_recorded'))}</p><p>${t('received')}: ${time(e.provenance?.received_at)}</p>${(e.extracted_values||[]).map(v=>`<p>${bdi(clinicalLabel(v.name||v.analyte||''))} · ${v.dose?bdi(v.dose):bdi(v.value??t('missing'))} ${v.dose?'':bdi(v.unit||t('missing'))}${v.frequency?' · '+bdi(v.frequency):''}</p>`).join('')}${provenance(e.provenance)}${!demo&&['accepted','detached'].includes(e.association_state)?`<button data-correct-evidence="${esc(e.id)}">Correct or detach</button>`:''}</article>`).join('')||empty('no_evidence');
    const reviews=[...reviewRows(record),...reviewRows(record,true)].map(r=>`<article class="record-item" data-review="${esc(r.id)}"><h3>${t(r.review_kind)}</h3>${badge(r.state,r.review_kind==='incident_response'&&r.state!=='resolved'?'danger':tone(r.state))}<p>${time(r.review_at)}</p>${r.last_material_change_version>r.source_version?`<p>${t('changed')}</p>`:''}<p>${t('notice')}: ${r.first_notice_at?time(r.first_notice_at):t('not_recorded')}</p>${r.resolved_reason?`<p>${t('reason')}: ${/^[a-z]+(?:_[a-z]+)+$/.test(r.resolved_reason)?'Recorded by the doctor':bdi(r.resolved_reason)}</p>`:''}</article>`).join('')||empty();
    $('content').innerHTML=`<a class="back" id="back" href="${demo?'/demo':'/a'}"><span class="turn-arrow" aria-hidden="true">←</span> ${t('back')}</a><div class="detail-grid"><div>${section('plan',ordersHTML+consentHTML)}${section('missions',missions+(followups?`<h3>${t('followups')}</h3>${followups}`:''))}${section('facts',facts)}</div><div>${section('inbox',reviews)}${section('evidence',evidence)}${section('source_files',(!demo?record.media||[]:[]).map(m=>`<article class="record-item"><a data-media="${esc(m.mime||'')}" data-captured="${esc(m.date||'')}" href="/api/patients/${encodeURIComponent(record.patient_id)}/media/${encodeURIComponent(m.media_id)}">${t('original')} · ${m.uploaded_by_you?'Uploaded by you on '+esc(new Intl.DateTimeFormat(lang,{dateStyle:'medium',timeZone:state.zone}).format(new Date(m.date))):esc(t(m.kind))}</a><p>${time(m.date)}</p></article>`).join('')||empty())}</div></div>`;
    if(!demo) correctionControls(record);
    recordAnatomy($('content'));
    detailPresentation(record);
    $('back').onclick=e=>{if(!demo){try{const saved=sessionStorage.getItem('sanad-list-return');if(saved&&/^\/a(?:\/(?:inbox|history))?(?:\?|$)/.test(saved)){e.preventDefault();location.assign(saved);}}catch(_){}}};
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
    panel.innerHTML='<h2>Corrections and doctor decisions</h2>'+`<details><summary>Retained fact history and detached records</summary>${(record.fact_history||[]).map(f=>`<article data-fact-history="${esc(f.id)}"><p>${bdi(f.payload?.text||'')}</p>${provenance(f.provenance)}${!(record.facts||[]).some(v=>v.id===f.id)&&(record.correctable_facts||[]).some(v=>v.id===f.id)?`<button data-correct-fact="${esc(f.id)}">Correct detached record</button>`:''}</article>`).join('')}</details>`+(record.corrections||[]).map(c=>`<article class="record-item" data-correction="${esc(c.id)}"><p class="timeline-date">${c.created_at?bdi(new Intl.DateTimeFormat(lang,{dateStyle:'medium',timeZone:state.zone}).format(new Date(c.created_at))):t('not_recorded')}</p><p class="correction-notice">${correctionProse(c)}</p>${[...(record.reviews||[]),...(record.review_history||[])].filter(r=>r.source_type==='correction'&&r.source_id===c.id).map(r=>`${c.affected_mission_ids?.length?`<button data-validate="${esc(c.id)}">Review and validate current evidence</button>`:''}${r.state!=='resolved'?`<button data-response="${esc(c.id)}">Record doctor follow-up decision</button>`:''}`).join('')}</article>`).join('');
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
    const work=[...records.flatMap(obligations),...unassigned.map(r=>({due:r.review_at,review:r,urgent:r.review_kind==='incident_response'}))];
    return `<div class="summary-strip" aria-label="Outstanding work">${Object.entries(summaryLabels).map(([key,label])=>{
      const items=work.filter(x=>matchesSummary(x,key)), n=items.length;
      const oldest=items.filter(x=>x.due).map(x=>+new Date(x.due)).sort((a,b)=>a-b)[0];
      const days=oldest===undefined?0:Math.max(0,Math.floor((Date.now()-oldest)/86400000));
      const clause=key==='danger'?(n?'needs a response':'nothing urgent right now'):key==='overdue'?(n?`oldest ${days} ${days===1?'day':'days'} ago`:'none overdue'):key==='pending_review'?(n?'awaiting your review':'none awaiting review'):(n?'scheduled for today':'none today');
      const weight=n&&(key==='danger'||key==='overdue')?(key==='danger'?'danger':'warning'):'';
      const tag=interactive?'button':'article';
      return `<${tag} class="summary-tile ${weight}"${interactive?` type="button" data-summary="${key}" aria-pressed="${state.filter===key}" aria-label="${n} ${label}"`:''}><span class="tile-label">${icon(key==='danger'?'status-icon':key==='pending_review'?'review-icon':'clock-icon')}${label}</span><strong>${n}</strong><span class="tile-clause">${clause}</span></${tag}>`;
    }).join('')}</div>`;
  }
  function listPresentation(rows){
    $('content').insertAdjacentHTML('afterbegin',summaryStrip(state.records,state.reviews.filter(r=>!r.patient_id&&r.state!=='resolved'),true));
    const inbox=document.querySelector('nav a[href="/a/inbox"]');if(inbox)inbox.innerHTML=`${icon('review-icon')}${t('inbox')} <span class="count">${state.records.reduce((n,r)=>n+reviewRows(r).length,0)+state.reviews.filter(r=>!r.patient_id).length}</span>`;
    document.querySelectorAll('[data-summary]').forEach(button=>button.onclick=()=>{
      const key=button.dataset.summary;state.filter=state.filter===key?'all':key;state.page=1;list();
      document.querySelector(`[data-summary="${key}"]`).focus();
      if(view==='inbox'&&key==='overdue')document.querySelector('.inbox-item')?.scrollIntoView({block:'nearest'});
    });
    const elements=[...document.querySelectorAll('.clinical .patient-row')];
    elements.forEach((row,i)=>{row.tabIndex=i===0?0:-1;row.onkeydown=e=>{let next;if(e.key==='ArrowDown')next=Math.min(elements.length-1,i+1);if(e.key==='ArrowUp')next=Math.max(0,i-1);if(next!==undefined){e.preventDefault();elements.forEach(x=>x.tabIndex=-1);elements[next].tabIndex=0;elements[next].focus();}if(e.key==='Enter'){e.preventDefault();row.querySelector('[data-record],summary')?.click();}};});
    if(view==='inbox'){
      const visible=rows.slice((state.page-1)*50,state.page*50);const groups=[['Danger',visible.filter(r=>r.item?.urgent)],['Needs attention',visible.filter(r=>!r.item?.urgent)]];
      const target=document.createElement('div');target.className='inbox-groups';
      target.innerHTML=groups.map(([label,items])=>`<section class="section"><h2>${label} <span class="count ${label==='Danger'&&items.length?'danger':''}">${items.length}</span></h2>${items.map(({record,item})=>inboxCard(record,item)).join('')||emptyState('Nothing needs attention here.',t('refresh'))}</section>`).join('');
      document.querySelector('.work-surface').replaceWith(target);
    }
  }
  async function openDrawer(anchor){
    if(!demo){try{sessionStorage.setItem('sanad-list-return',location.pathname+location.search);sessionStorage.setItem('sanad-list-scroll',String(scrollY));}catch(_){}history.replaceState({...history.state,scroll:scrollY,focus:anchor.getAttribute('href')},'');}
    document.getElementById('patient-drawer')?.remove();const dialog=document.createElement('dialog');dialog.id='patient-drawer';dialog.className='drawer';dialog.setAttribute('aria-labelledby','drawer-title');
    dialog.innerHTML='<header class="drawer-header"><div><h2 id="drawer-title">Patient details</h2><div class="drawer-identity"></div></div><button class="close ghost icon-button" aria-label="Close patient details">×</button></header><div class="drawer-content" aria-live="polite"><p>Loading patient details…</p><div class="skeleton" aria-hidden="true"></div></div><footer class="drawer-footer"></footer>';
    document.body.append(dialog);const close=()=>{if(matchMedia('(prefers-reduced-motion: reduce)').matches){dialog.close();return;}dialog.classList.add('leaving');setTimeout(()=>{if(dialog.open)dialog.close();},180);};dialog.querySelector('.close').onclick=close;dialog.addEventListener('cancel',e=>{e.preventDefault();close();});dialog.addEventListener('click',e=>{const r=dialog.getBoundingClientRect();if(e.target===dialog&&(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom))close();});dialog.onclose=()=>{anchor.closest('tr')?.removeAttribute('aria-selected');dialog.remove();anchor.focus();};anchor.closest('tr')?.setAttribute('aria-selected','true');dialog.showModal();
    try{const id=decodeURIComponent(new URL(anchor.href).pathname.split('/').pop());const [r,evidence]=demo?[state.records.find(r=>link(r)===anchor.getAttribute('href')),[]]:await Promise.all([api(`/api/patients/${encodeURIComponent(id)}`),api(`/api/patients/${encodeURIComponent(id)}/evidence`)]);if(!dialog.open)return;
      dialog.querySelector('#drawer-title').innerHTML=bdi(r.display_name);
      dialog.querySelector('.drawer-identity').innerHTML=`<span>${t('age')}: ${bdi(r.age??t('not_recorded'))}</span>${badge(r.contact_status)}`;
      const work=obligations(r);
      const stats=`<div class="inline-stats">${['overdue','pending_review','due_today','danger'].map(key=>{const n=work.filter(x=>matchesSummary(x,key)).length;return `<span class="${key==='danger'&&n?'danger':''}"><span>${summaryLabels[key]}</span> <strong>${n}</strong></span>`;}).join('')}</div>`;
      dialog.querySelector('.drawer-content').innerHTML=stats+`<section><h3>${t('current_plan')}</h3>${(r.orders||[]).filter(o=>o.status==='active').map(o=>`<article data-order="${esc(o.id)}"><p>${instruction(o.current_version)}</p>${!demo?`<button data-amend="${esc(o.id)}">Amend instruction</button>`:''}</article>`).join('')||empty('no_plan')}</section><section><h3>${t('outstanding_work')}</h3>${work.map(x=>`<article class="record-item"><div class="chip-row">${badge(x.status,x.urgent?'danger':tone(x.status))}</div><p>${esc(x.title)}</p><div class="record-meta">${time(x.due)}</div></article>`).join('')||empty('no_work')}</section><section><h3>${t('drawer_evidence')}</h3>${evidence.map(e=>`<article class="record-item"><div class="chip-row">${badge(e.association_state==='candidate'?'pending_review':e.association_state==='accepted_pending_identity'?'unverifiable':e.association_state)}</div><p>${esc(t(e.category))}</p><div class="record-meta">${time(e.provenance?.received_at)}</div></article>`).join('')||empty('no_evidence')}</section><section><h3>${t('drawer_history')}</h3>${(r.corrections||[]).map(c=>`<article class="record-item"><p>${correctionProse(c)}</p><div class="record-meta">${time(c.created_at)}</div></article>`).join('')||empty('no_corrections')}</section>`;
      dialog.querySelector('.drawer-footer').innerHTML=`<a class="button primary" href="${esc(anchor.href)}">${t('full_record')}</a>`;
      if(!demo)correctionControls(r,dialog,async()=>{dialog.close();await load();const next=[...document.querySelectorAll('[data-record]')].find(a=>a.getAttribute('href')===anchor.getAttribute('href'));if(next)await openDrawer(next);});
    }catch(error){if(!dialog.open)return;dialog.querySelector('.drawer-content').innerHTML=`<p class="error" role="alert">${esc(error.message)}</p><button class="secondary" data-retry>${t('refresh')}</button>`;dialog.querySelector('[data-retry]').onclick=()=>{dialog.close();openDrawer(anchor);};}
  }
  function detailPresentation(record){
    $('content').insertAdjacentHTML('afterbegin',summaryStrip([record]));
    $('content').insertAdjacentHTML('afterbegin',`<div class="record-heading"><h2 id="title" class="patient-name">${bdi(record.display_name)}</h2><p id="subtitle">${t('age')}: ${bdi(record.age||t('missing'))} ${badge(record.contact_status)}</p></div>`);
    const grid=document.querySelector('.detail-grid'), sections=[...grid.querySelectorAll(':scope > div > section')];
    const timeline=document.querySelector('.correction-timeline');
    const tabs=document.createElement('div');tabs.className='tabs';tabs.setAttribute('role','tablist');tabs.setAttribute('aria-label','Patient record');
    const groups=[['Plan',[sections[0],sections[2]]],['Requests',[sections[1],sections[3]]],['Evidence',[sections[4],sections[5]]],['History',[timeline]]];
    grid.replaceChildren();groups.forEach(([name,children],i)=>{const panel=document.createElement('div');panel.className='tab-panel';panel.id='record-panel-'+i;panel.setAttribute('role','tabpanel');panel.setAttribute('aria-labelledby','record-tab-'+i);panel.tabIndex=0;children.filter(Boolean).forEach(x=>panel.append(x));if(!panel.childNodes.length)panel.innerHTML=empty('no_corrections');grid.append(panel);const button=document.createElement('button');button.id='record-tab-'+i;button.textContent=name;button.setAttribute('role','tab');button.setAttribute('aria-controls',panel.id);tabs.append(button);});
    grid.before(tabs);const buttons=[...tabs.children];const select=i=>{state.detailTab=i;grid.querySelectorAll('button.primary').forEach(b=>b.classList.remove('primary'));grid.children[i].querySelector('[data-validate],[data-response]')?.classList.add('primary');buttons.forEach((b,n)=>{b.setAttribute('aria-selected',String(n===i));b.tabIndex=n===i?0:-1;grid.children[n].hidden=n!==i;});};
    buttons.forEach((b,i)=>{b.onclick=()=>select(i);b.onkeydown=e=>{const forward=document.documentElement.dir==='rtl'?'ArrowLeft':'ArrowRight';let next;if(e.key===forward)next=(i+1)%4;else if(['ArrowLeft','ArrowRight'].includes(e.key))next=(i+3)%4;else if(e.key==='Home')next=0;else if(e.key==='End')next=3;if(next!==undefined){e.preventDefault();select(next);buttons[next].focus();}};});const fragmentTab=['plan','requests','evidence','history'].indexOf(new URLSearchParams(location.hash.slice(1)).get('tab'));select(fragmentTab>=0?fragmentTab:state.detailTab||0);window.onhashchange=()=>{const i=['plan','requests','evidence','history'].indexOf(new URLSearchParams(location.hash.slice(1)).get('tab'));if(i>=0)select(i);};
    const support=document.createElement('details');support.className='support';support.innerHTML='<summary>Details for support</summary><pre></pre>';support.querySelector('pre').textContent=JSON.stringify({record,evidence:state.evidence},null,2);$('content').append(support);
    document.querySelectorAll('[data-media]').forEach(a=>{if(a.dataset.media==='application/pdf'){a.target='_blank';a.rel='noopener noreferrer';return;}a.onclick=e=>{e.preventDefault();lightbox(e.currentTarget);};});
    // Pair each evidence card with its existing scoped original, without inventing a URL.
    state.evidence.forEach((e,i)=>{const media=(record.media||[]).find(m=>m.media_id===e.media_id);if(!media){sections[4].querySelectorAll('article')[i]?.insertAdjacentHTML('beforeend','<p class="muted">Original unavailable.</p>');return;}const original=document.querySelector(`[data-media][href$="/${encodeURIComponent(media.media_id)}"]`);if(original){const copy=original.cloneNode(true);sections[4].querySelectorAll('article')[i]?.append(copy);copy.onclick=original.onclick;}});
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
      article.querySelectorAll('[data-action]').forEach(b=>b.onclick=()=>{const action=b.dataset.action,target=article.querySelector('.question-confirm');root.querySelectorAll('button.primary').forEach(x=>x.classList.remove('primary'));root.querySelectorAll('.question-confirm').forEach(x=>x.replaceChildren());target.innerHTML=`<form><p>${action==='defer'?'Defer until at least tomorrow? A later deadline stays unchanged.':'Confirm your answer for this patient.'}</p>${action==='answer'?'<label>Your answer<textarea name="answer" required maxlength="700"></textarea></label>':''}<button class="primary">Confirm</button><button type="button" class="cancel">Cancel</button><p role="alert"></p></form>`;target.querySelector('.cancel').onclick=()=>{target.replaceChildren();root.querySelector('[data-action=send]:not([disabled])')?.classList.add('primary');};target.querySelector('form').onsubmit=async event=>{event.preventDefault();const button=target.querySelector('.primary');button.disabled=true;try{const payload=action==='send'?{listing_token:data.listing_token,n:q.n,mission_version:q.version,reusable_id:q.proposed_reply.reusable_id,reusable_version:q.proposed_reply.version}:{expected_version:q.version,...(action==='answer'?{text:target.querySelector('textarea').value}:{})};await api(`/api/questions/${encodeURIComponent(q.id)}/${action}`,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':decodeURIComponent(document.cookie.split('; ').find(x=>x.startsWith('sanad_csrf='))?.slice(11)||'')},body:JSON.stringify({command_id:crypto.randomUUID(),...payload})});await load();toast(action==='defer'?'Question deferred.':'Answer recorded.','/a/inbox#questions');}catch(error){target.querySelector('[role=alert]').textContent=error.message;button.disabled=false;}};});root.append(article);}
    root.querySelector('[data-action=send]:not([disabled])')?.classList.add('primary');
    if(!data.questions.length)root.insertAdjacentHTML('beforeend',emptyState('No questions are waiting for an answer.',t('refresh')));$('content').querySelector('.summary-strip').after(root);
  }

  function patientView(data){
    const plan=data.plan||data;$('title').textContent=t('yourcare');
    // Explicit projection allow-list. No ids, kind names, raw objects or internal statuses.
    const orderHTML=(plan.orders||[]).map(o=>`<article class="record-item"><p>${[o.drug,o.dose,o.frequency,o.timing,o.route,o.duration].filter(Boolean).map(bdi).join(' · ')}</p></article>`).join('')||empty('no_plan');
    const requests=(plan.next_missions||[]).map(m=>`<article class="record-item"><p>${bdi(m.title)}</p><p>${t('due')}: ${time(m.due_at,plan.preferences?.timezone||'UTC')}</p></article>`).join('')||empty('no_requests');
    const reports=(plan.medication_reports||[]).map(r=>`<article class="record-item"><p>${bdi(r.text)}</p><small>${t('self_report')}</small></article>`).join('')||empty('no_reports');
    const prefs=plan.preferences||{};
    $('content').innerHTML=`<div class="summary-strip" aria-label="Your recorded plan">${[[t('your_plan'),(plan.orders||[]).length],[t('your_requests'),(plan.next_missions||[]).length],[t('questions'),(plan.open_questions||[]).length]].map(([label,n])=>`<article class="summary-tile"><span>${esc(label)}</span><strong>${n}</strong></article>`).join('')}</div><p>${t('doctor')}: ${bdi(plan.doctor_name)}</p>${section('your_plan',orderHTML)}${section('your_requests',requests)}${section('reports',reports)}${section('last_reading',plan.last_reading?`<p class="record-item">${bdi(plan.last_reading.text)}</p>`:badge('missing'))}${section('reminders',`<p>${t(prefs.routine_contact_enabled?'enabled':'disabled')} · ${t(prefs.contact_status||'not_recorded')}</p><p>${t('quiet')}: ${bdi((prefs.quiet_hours||[]).join(', '))} · ${bdi(prefs.timezone)}</p>${prefs.resume_at?`<p>${t('resume')}: ${time(prefs.resume_at,prefs.timezone)}</p>`:''}`)}${section('questions',(plan.open_questions||[]).map(q=>`<article class="record-item"><p>${bdi(q.question)}</p><small>${t('waiting')}</small></article>`).join('')||empty('no_questions'))}`;
  }
  function preferences(){
    const p=state.pref;$('content').innerHTML=`<section class="preferences"><form id="language-form"><label for="language">${t('language')}<select id="language"><option value="en" ${p.language==='en'?'selected':''}>English</option><option value="ar" ${p.language==='ar'?'selected':''}>العربية</option></select></label>${p.contest_english?`<p class="muted">${t('contest')}</p>`:''}<button class="primary" type="submit">${t('save')}</button><p id="save-result" role="status"></p></form></section>`;
    document.querySelector('.preferences').insertAdjacentHTML('beforeend',`<form id="digest-form"><h2>Question digest</h2><label>Daily time (${esc(p.timezone)})<input id="digest-time" type="time" value="${esc(p.digest_time)}" required></label><label>Packing<select id="digest-packing"><option value="one" ${p.digest_packing==='one'?'selected':''}>One message</option><option value="each" ${p.digest_packing==='each'?'selected':''}>One per question</option></select></label><button type="submit">Save digest</button><p id="digest-result" role="status"></p></form>`);
    $('digest-time').setAttribute('aria-describedby','digest-result');
    const help=document.querySelector('#language-form > .muted');if(help){help.id='language-help';$('language').before(help);$('language').setAttribute('aria-describedby',help.id);}
    for(const form of document.querySelectorAll('.preferences form')){const row=document.createElement('div');row.className='submit-row';form.querySelector('button').before(row);row.append(form.querySelector('button'));}
    $('digest-form').onsubmit=async e=>{e.preventDefault();const input=$('digest-time'),result=$('digest-result');if(!/^([01]\d|2[0-3]):[0-5]\d$/.test(input.value)){input.setAttribute('aria-invalid','true');result.textContent='Choose a valid daily time.';return;}input.removeAttribute('aria-invalid');const button=e.target.querySelector('button');button.disabled=true;try{await api('/api/preferences',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':decodeURIComponent(document.cookie.split('; ').find(x=>x.startsWith('sanad_csrf='))?.slice(11)||'')},body:JSON.stringify({digest_time:input.value,digest_packing:$('digest-packing').value,expected_version:p.version,command_id:crypto.randomUUID()})});await load();toast('Digest preferences saved.');}catch(error){result.textContent=error.message;button.disabled=false;}};
    $('language-form').onsubmit=async e=>{e.preventDefault();const button=e.target.querySelector('button');button.disabled=true;const token=document.cookie.split('; ').find(s=>s.startsWith('sanad_csrf='))?.split('=')[1]||'';try{await api('/api/preferences',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':decodeURIComponent(token)},body:JSON.stringify({language:$('language').value,expected_version:p.version,command_id:crypto.randomUUID()})});$('save-result').textContent=t('saved');location.reload();}catch(error){$('save-result').className='error';$('save-result').textContent=error.message;button.disabled=false;}};
  }
  function render(){document.querySelector('.record-heading')?.remove();const heading=document.querySelector('.top-bar h1');heading.id=view==='detail'?'page-title':'title';heading.textContent=patient?t('yourcare'):t(view);$('refresh').className='ghost icon-button';if(patient)patientView(state.data);else if(view==='preferences')preferences();else if(view==='detail')detail(state.records[0]);else list();}
  async function load(){
    const request=++generation;currentAbort?.abort();currentAbort=new AbortController();const signal=currentAbort.signal;
    const first=!state.records.length&&!state.data&&!state.pref;
    $('feedback').textContent='';$('content').setAttribute('aria-busy','true');$('refresh').disabled=true;
    const timer=setTimeout(()=>{if(request===generation){$('feedback').innerHTML=`<p>${t('loading')}</p><div class="skeleton" aria-hidden="true"></div>`;$('feedback').className='loading';}},300);
    try{
      if(demo){state.records=await api('/assets/demo.json',{signal});const id=decodeURIComponent(location.hash.slice(1));if(id){const r=state.records.find(r=>r.patient_id===id);if(r){view='detail';state.records=[r];}}}
      else if(patient){const [me,plan]=await Promise.all([api('/api/patient/me',{signal}),api('/api/patient/plan',{signal})]);state.data={display_name:me.display_name,plan};state.zone=plan.preferences?.timezone||'UTC';}
      else if(view==='preferences'){state.pref=await api('/api/preferences',{signal});}
      else if(view==='detail'){const id=encodeURIComponent(body.dataset.patient);const [record,evidence]=await Promise.all([api(`/api/patients/${id}`,{signal}),api(`/api/patients/${id}/evidence`,{signal})]);state.records=[record];state.evidence=evidence;state.zone=record.timezone||'UTC';}
      else {const [panel,pref]=await Promise.all([api('/api/patients',{signal}),api('/api/preferences',{signal})]);state.zone=pref.timezone;const records=[];let index=0;await Promise.all(Array.from({length:Math.min(6,panel.length)},async()=>{while(index<panel.length){const p=panel[index++];records.push(await api(`/api/patients/${encodeURIComponent(p.patient_id)}`,{signal}));}}));if(request!==generation)return;state.records=records;if(view==='inbox'||view==='history')state.reviews=await api('/api/browser/reviews'+(view==='history'?'?history=true':''),{signal});if(view==='inbox')state.questions=await api('/api/questions',{signal});}
      if(request!==generation)return;
      render();$('feedback').textContent='';$('freshness').textContent=`${t('updated')} ${new Intl.DateTimeFormat(lang,{hour:'2-digit',minute:'2-digit',second:'2-digit'}).format(new Date())}`;
      if(first&&!patient&&['patients','inbox','history'].includes(view)){let position=history.state?.scroll||0;try{if(sessionStorage.getItem('sanad-list-return')===location.pathname+location.search)position=Number(sessionStorage.getItem('sanad-list-scroll'))||position;}catch(_){}if(position)scrollTo(0,position);}
    }catch(error){if(error.name==='AbortError')return;if(request!==generation)return;
      // Never retain sensitive clinical content after a revoked/expired session.
      if([401,403,404].includes(error.status)||first){$('content').replaceChildren();document.querySelectorAll('dialog,#action-toast').forEach(x=>x.remove());}
      if([401,403].includes(error.status))window.dispatchEvent(new Event('patient-session-expired'));
      $('feedback').innerHTML=`<p class="error" role="alert">${esc(error.status?error.message:t('failed'))}</p>`;
      $('freshness').textContent='';
    }finally{clearTimeout(timer);if(request===generation){$('feedback').className='';$('content').setAttribute('aria-busy','false');$('refresh').disabled=false;}}
  }
  function chrome(){
    const sprite=document.querySelector('.icon-sprite');
    sprite.insertAdjacentHTML('beforeend',`<symbol id="patients-icon" viewBox="0 0 16 16"><circle cx="6" cy="5" r="2.5"/><path d="M1 14v-2a5 5 0 0 1 10 0v2M11 3a2.5 2.5 0 0 1 0 5m1 2a4 4 0 0 1 3 4"/></symbol><symbol id="review-icon" viewBox="0 0 16 16"><path d="M4 2h8v12H4zM6 5h4M6 8h4M6 11h2"/></symbol><symbol id="clock-icon" viewBox="0 0 16 16"><circle cx="8" cy="8" r="6"/><path d="M8 4v4l3 2"/></symbol><symbol id="appearance-icon" viewBox="0 0 16 16"><circle cx="8" cy="8" r="6"/><path d="M8 2v12M8 2a6 6 0 0 1 0 12z"/></symbol><symbol id="refresh-icon" viewBox="0 0 16 16"><path d="M13 6A5 5 0 1 0 13 10M13 2v4H9"/></symbol><symbol id="menu-icon" viewBox="0 0 16 16"><path d="M2 4h12M2 8h12M2 12h12"/></symbol><symbol id="chevron-icon" viewBox="0 0 16 16"><path d="m4 10 4-4 4 4"/></symbol>`);
    const heading=document.querySelector('.page-heading'), refresh=document.querySelector('.refresh-bar'), rail=document.querySelector('.rail');
    heading.className='top-bar';$('eyebrow').remove();$('subtitle').remove();
    const appearance=document.createElement('label');appearance.className='appearance';
    appearance.innerHTML=`${icon('appearance-icon')}<span class="visually-hidden">${document.querySelector('label[for=theme]').textContent}</span>`;
    appearance.append($('theme'));document.querySelector('label[for=theme]').remove();
    refresh.insertBefore(appearance,$('refresh'));heading.append(refresh);$('workspace').prepend(heading);
    $('refresh').className='ghost icon-button';$('refresh').innerHTML=`${icon('refresh-icon')}<span class="visually-hidden">${t('refresh')}</span>`;
    const menu=document.createElement('button');menu.className='ghost icon-button nav-disclosure';menu.type='button';menu.setAttribute('aria-label',$('navigation').getAttribute('aria-label'));menu.setAttribute('aria-controls','navigation');menu.setAttribute('aria-expanded','false');menu.innerHTML=icon('menu-icon');menu.onclick=()=>menu.setAttribute('aria-expanded',String(menu.getAttribute('aria-expanded')!=='true'));
    rail.insertBefore(menu,$('navigation'));
    const mobile=matchMedia('(max-width:959px)');const place=()=>{if(mobile.matches)rail.insertBefore(appearance,$('navigation'));else refresh.insertBefore(appearance,$('refresh'));};mobile.addEventListener('change',place);place();
  }
  $('theme').value=document.documentElement.dataset.themeChoice;
  chrome();
  $('title').textContent=patient?t('yourcare'):t(view);
  if(demo){$('banner').innerHTML=`<div class="demo-banner"><strong>${t('demo')}</strong><p>${t('demo_note')}</p></div>`;$('navigation').innerHTML=`<a href="/demo" aria-current="page">${icon('patients-icon')}${t('patients')}</a>`;}
  else $('navigation').innerHTML=patient?`<a href="/pp" aria-current="page">${icon('patients-icon')}${t('yourcare')}</a>`:['patients','inbox','history','preferences'].map(k=>`<a href="${k==='patients'?'/a':'/a/'+k}" ${view===k?'aria-current="page"':''}>${icon(k==='patients'?'patients-icon':k==='history'?'clock-icon':k==='preferences'?'appearance-icon':'review-icon')}${t(k)}</a>`).join('');
  $('refresh').onclick=load;
  window.addEventListener('pageshow',event=>{if(event.persisted)load();});
  if(demo)window.addEventListener('hashchange',()=>{if(location.hash==='#workspace')return;view=location.hash?'detail':'patients';load();});
  function patientControls() {
    const root=$('patient-controls'); if(!root)return;
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
    const failure=error=>{result.textContent=w([401,403].includes(error.status)?'expired':error.status===409?'conflict':'failed');if([401,403].includes(error.status)){stopped=true;$('content').replaceChildren();root.querySelectorAll('section').forEach(x=>x.remove());}};
    async function get(path,options){const response=await fetch(path,options);if(!response.ok){const error=new Error();error.status=response.status;throw error;}return response.json();}
    const post=(path,name,payload)=>get(path,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf()},body:JSON.stringify({...payload,command_id:command(name,payload)})});
    function line(target,text){const p=document.createElement('p');p.textContent=text;target.append(p);}
    const uploadText=u=>w(u.state)+(u.category?' · '+w(u.category):'');
    async function history(older=false){viewingOlder=older;const data=await get('/api/patient/conversation'+(older&&cursor?'?cursor='+encodeURIComponent(cursor):''));const target=$('patient-conversation'),fragment=document.createDocumentFragment();const latest=new Set(data.items.filter(i=>i.direction==='outbound').map(i=>i.id));if(waitingFor&&[...latest].some(id=>!waitingFor.has(id))){waitingSince=null;waitingFor=null;result.textContent=w('sent');}else if(waitingSince!==null&&Date.now()-waitingSince>=20000){result.textContent=w('still_working');}outgoing=latest;for(const item of data.items){const article=document.createElement('article');article.className='record-item'+(item.direction==='outbound'?' outbound':'');line(article,item.credential?w('credential')+' '+item.at:item.legacy?w('legacy')+' '+item.at:item.text||'');if(item.upload)line(article,uploadText(item.upload));const time=document.createElement('small');time.textContent=item.at;article.append(time);fragment.append(article);}if(older)target.prepend(fragment);else target.replaceChildren(fragment);if(!target.childNodes.length)target.innerHTML=emptyState(w('no_messages'),t('patient_note'));cursor=data.cursor;$('patient-older').hidden=!cursor;}
    async function refresh(){if(stopped)return;await history();const [files,prefs]=await Promise.all([get('/api/patient/uploads'),get('/api/patient/preferences')]);$('patient-uploads').replaceChildren();for(const file of files)line($('patient-uploads'),file.received_at+' · '+uploadText(file));$('patient-stop').setAttribute('aria-checked',String(prefs.reminders==='enabled'));$('patient-preferences').textContent=w(prefs.reminders)+' · '+prefs.timezone;if(!$('patient-quiet-start').value){$('patient-quiet-start').value=prefs.quiet_hours[0];$('patient-quiet-end').value=prefs.quiet_hours[1];}}
    async function act(work){if(busy||stopped)return;busy=true;root.querySelectorAll('button').forEach(b=>b.disabled=true);try{await work();await refresh();}catch(error){failure(error);}finally{busy=false;root.querySelectorAll('button').forEach(b=>b.disabled=false);}}
    $('patient-message-form').onsubmit=e=>{e.preventDefault();act(async()=>{waitingSince=Date.now();waitingFor=new Set(outgoing);const reply=await post('/api/patient/messages','message',{text:$('patient-message').value});ids.delete('message');$('patient-message').value='';result.textContent=reply.emergency||w(reply.status==='accepted'?'sent':'pending');if(reply.emergency){waitingSince=null;waitingFor=null;}});};
    $('patient-older').onclick=()=>{history(true).catch(failure);};
    for(const action of ['stop','resume'])$('patient-'+action).onclick=()=>act(async()=>{const reply=await post('/api/patient/preferences',action,{reminders:action});ids.delete(action);confirmation=reply.confirmation_token;$('patient-confirm').hidden=!confirmation;result.textContent=w(confirmation?'confirm':reply.status==='accepted'?'saved':'pending');});
    const stopAction=$('patient-stop').onclick;const switchLabel=document.createElement('span');switchLabel.className='switch-label';switchLabel.textContent=en?'Reminders':'التذكيرات';$('patient-stop').before(switchLabel);$('patient-stop').innerHTML='<span class="switch-track" aria-hidden="true"><span></span></span>';$('patient-stop').onclick=()=>{if($('patient-stop').getAttribute('aria-checked')==='true')stopAction();else $('patient-resume').click();};
    $('patient-confirm').onclick=()=>act(async()=>{await post('/api/patient/preferences/confirm','confirm',{token:confirmation});ids.delete('confirm');confirmation=null;$('patient-confirm').hidden=true;result.textContent=w('saved');});
    $('patient-quiet-form').onsubmit=e=>{e.preventDefault();if($('patient-quiet-start').value===$('patient-quiet-end').value){result.textContent=w('quiet_invalid');return;}act(async()=>{const reply=await post('/api/patient/preferences','quiet',{quiet_hours:[$('patient-quiet-start').value,$('patient-quiet-end').value]});ids.delete('quiet');result.textContent=w(reply.status==='accepted'?'saved':'pending');});};
    $('patient-upload-form').onsubmit=e=>{e.preventDefault();act(async()=>{const file=$('patient-file').files[0];if(!file||!file.type.startsWith('image/')){result.textContent=w('unsupported');return;}if(file.size>8*1024*1024){result.textContent=w('too_large');return;}let bitmap;try{bitmap=await createImageBitmap(file);}catch{result.textContent=w('unreadable');return;}const tooLarge=bitmap.width>8000||bitmap.height>8000||bitmap.width*bitmap.height>20000000;bitmap.close();if(tooLarge){result.textContent=w('too_large');return;}result.textContent=w('uploading');const progress=$('patient-upload-progress');progress.hidden=false;progress.value=0;try{const reply=await new Promise((resolve,reject)=>{const xhr=new XMLHttpRequest();xhr.open('POST','/api/patient/uploads');xhr.setRequestHeader('X-CSRF-Token',csrf());xhr.setRequestHeader('Content-Type',file.type);const bytes=new TextEncoder().encode($('patient-caption').value);xhr.setRequestHeader('X-Upload-Caption',btoa(Array.from(bytes,b=>String.fromCharCode(b)).join('')));xhr.upload.onprogress=event=>{if(event.lengthComputable)progress.value=100*event.loaded/event.total;};xhr.onerror=()=>reject(new Error());xhr.onload=()=>{let data;try{data=JSON.parse(xhr.responseText);}catch{reject(new Error());return;}if([401,403].includes(xhr.status)){const error=new Error();error.status=xhr.status;reject(error);}else if(xhr.status>=400&&data.category){resolve(data);}else if(xhr.status>=400){reject(new Error());}else resolve(data);};xhr.send(file);});result.textContent=reply.category?w(reply.category):w('received');if(!reply.category){$('patient-file').value='';$('patient-caption').value='';}}finally{progress.hidden=true;}});};
    window.addEventListener('patient-session-expired',()=>failure({status:401}));
    $('refresh').addEventListener('click',()=>{viewingOlder=false;refresh().catch(failure);});
    refresh().catch(failure);
    const timer=setInterval(()=>{if(stopped){clearInterval(timer);return;}if(!busy&&!document.hidden&&!viewingOlder)refresh().catch(failure);},5000);
  }
  patientControls();
  load();
})();
