'use strict';
(() => {
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
    empty:['No results for this view.','لا توجد نتائج في هذه الصفحة.'], no_patients:['No patients recorded yet. Add a patient through Telegram.','لم يُسجل مرضى بعد. أضف مريضاً من تيليجرام.'],
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
    patient_note:['Your doctor’s recorded plan. Use Telegram to send a message or upload a document.','الخطة المسجلة من دكتورك. استخدم تيليجرام لإرسال رسالة أو مستند.'],
    doctor:['Your doctor','دكتورك'], your_plan:['Your medication plan','خطة أدويتك'], your_requests:['What your doctor asked for','المطلوب منك'], reports:['What you reported about your medication','ما أبلغت به عن أدويتك'], last_reading:['Your last reported reading','آخر قراءة أبلغت بها'], reminders:['Reminders','التذكيرات'], questions:['Your open questions','أسئلتك المعلّقة'], waiting:['Waiting for your doctor','بانتظار دكتورك'], quiet:['Quiet hours','ساعات الهدوء'], resume:['Paused until','مؤجلة حتى'], paused:['Paused','مؤجلة'], opted_out:['Stopped','متوقفة'], frozen:['Access paused','التواصل مجمد'], unreachable:['Unreachable','تعذر التواصل'], no_reports:['No medication report recorded.','لا يوجد بلاغ عن الأدوية مسجل.'], no_questions:['No open questions recorded.','لا توجد أسئلة معلّقة مسجلة.'], self_report:['Self-reported; this does not prove the medicine was taken.','حسب البلاغ؛ لا يثبت تناول الدواء.'], retained:['Retained observation','ملاحظة محفوظة'], historical:['History — not an active order','تاريخ سابق وليس أمراً حالياً'], details:['Details and provenance','التفاصيل والمصدر'], overdue_by:['Overdue by','متأخر بمقدار'], no_due:['No due time recorded','لا يوجد موعد مسجل'], review:['Review','مراجعة'],
    result_review:['Result review','مراجعة نتيجة'], incident_response:['Danger response','متابعة خطر'], deadline_disposition:['Deadline follow-up','متابعة الموعد المتأخر'], evidence_association:['Document association','ربط مستند'], intake_clarification:['Intake clarification','استيضاح ملف'], question_answer:['Answer question','إجابة سؤال'], correction_disposition:['Correction review','مراجعة تصحيح'], media_failure:['File processing failure','تعذر تجهيز ملف'], followup_disposition:['Follow-up review','مراجعة المتابعة'],
    completed:['Request fulfilled','تم استيفاء الطلب'], invalidated_pending_review:['Fulfillment under review','استيفاء الطلب قيد المراجعة'], closed_unfulfilled:['Closed without fulfillment','أغلق دون استيفاء'], cancelled:['Cancelled','ملغى'], superseded:['Superseded','استُبدل'], waiting_patient:['Waiting for patient','بانتظار المريض'], proposed:['Proposed','مقترح'],
    enabled:['Enabled','مفعلة'], disabled:['Disabled','متوقفة'], until:['Until','حتى'], original:['Open original document','افتح المستند الأصلي'], source_files:['Source files','الملفات الأصلية'], sort_help:['Use the column buttons to sort.','استخدم أزرار الأعمدة للترتيب.']
  };
  const t = key => words[key]?.[en ? 0 : 1] || key.replaceAll('_',' ');
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
  function icon() {return '<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="6"/><path d="M8 4v5m0 2v1"/></svg>';}
  function badge(key, tone='') {return `<span class="status ${tone}">${icon()}<span>${esc(t(key))}</span></span>`;}
  function time(value, zone=state.zone) {
    if(!value) return `<span class="muted">${t('no_due')}</span>`;
    const date=new Date(value); if(Number.isNaN(+date)) return bdi(value);
    const minutes=Math.round((+date-Date.now())/60000), abs=Math.abs(minutes);
    const unit=abs>=1440?'day':abs>=60?'hour':'minute', amount=unit==='day'?Math.trunc(minutes/1440):unit==='hour'?Math.trunc(minutes/60):minutes;
    const absolute=new Intl.DateTimeFormat(lang,{year:'numeric',month:'short',day:'numeric',hour:'2-digit',minute:'2-digit',timeZone:zone}).format(date);
    const relative=new Intl.RelativeTimeFormat(lang,{numeric:'always'}).format(amount,unit);
    return `<time datetime="${esc(value)}">${bdi(absolute)}</time><small>${bdi(relative)}</small>`;
  }
  function stateOf(m) {
    if (m.fulfillment_validity==='invalidated_pending_review') return 'invalidated_pending_review';
    if (m.state==='fulfilled') return 'fulfilled';
    if (['resolved','acknowledged','cancelled','closed_unfulfilled','invalidated_pending_review','superseded','proposed'].includes(m.state)) return m.state;
    if (m.state==='blocked') return 'blocked';
    return m.due_at && +new Date(m.due_at)>Date.now()?'not_yet_due': 'missing';
  }
  function tone(key) {return key==='fulfilled'||key==='resolved'?'success':key==='overdue'||key==='blocked'||key==='unverifiable'?'warning':key==='pending_review'||key==='acknowledged'?'accent':'';}
  function reviewRows(record, history=false) {return (history?record.review_history:record.reviews)||[];}
  function obligations(record) {
    const reviews=reviewRows(record).map(r=>({id:r.id,title:t(r.review_kind),due:r.review_at,status:r.state==='open'?'pending_review':r.state,urgent:r.review_kind==='incident_response',review:r}));
    const missions=(record.missions||[]).filter(m=>!['fulfilled','cancelled','closed_unfulfilled','superseded'].includes(m.state)).map(m=>({id:m.id,title:m.title,due:m.due_at,status:stateOf(m),urgent:false}));
    return [...reviews,...missions].sort((a,b)=>Number(b.urgent)-Number(a.urgent)||(+new Date(a.due)-+new Date(b.due))||a.id.localeCompare(b.id));
  }
  function link(record) {return demo?`/demo#${encodeURIComponent(record.patient_id)}`:`/a/patients/${encodeURIComponent(record.patient_id)}`;}
  function tableRows() {
    let rows=[];
    for(const record of state.records){
      const work=obligations(record);
      if(view==='inbox'||view==='history') for(const r of reviewRows(record,view==='history')) rows.push({record,item:{id:r.id,title:t(r.review_kind),due:r.review_at,status:r.state,urgent:r.review_kind==='incident_response'&&r.state!=='resolved',review:r},count:1});
      else rows.push({record,item:work[0]||null,count:work.length});
    }
    if(view==='inbox'||view==='history')for(const r of state.reviews.filter(r=>!r.patient_id)){rows.push({record:{patient_id:'',display_name:t('unassigned'),reviews:[],missions:[]},item:{id:r.id,title:t(r.review_kind),due:r.review_at,status:r.state,urgent:r.review_kind==='incident_response'&&r.state!=='resolved',review:r},count:1});}
    rows=rows.filter(({record,item})=>(`${record.display_name} ${record.patient_id} ${item?.title||''}`).toLocaleLowerCase().includes(state.query.toLocaleLowerCase())&&(
      state.filter==='all'||state.filter===record.contact_status||(state.filter==='overdue'&&item?.due&&+new Date(item.due)<Date.now())||(state.filter==='blocked'&&record.missions?.some(m=>m.state==='blocked'))));
    const val=row=>state.sort==='patient'?row.record.display_name:state.sort==='age'?(Number(row.record.age)||0):state.sort==='outstanding'?(row.item?.title||''):state.sort==='status'?(row.item?.status||''):row.item?.due?+new Date(row.item.due):Infinity;
    rows.sort((a,b)=>{const av=val(a),bv=val(b); const cmp=typeof av==='string'?av.localeCompare(String(bv),lang):(av===bv?0:av<bv?-1:1);return (state.descending?-cmp:cmp)||a.record.patient_id.localeCompare(b.record.patient_id)||(a.item?.id||'').localeCompare(b.item?.id||'');});
    return rows;
  }
  function remember() {
    if(demo)return;
    const q=new URLSearchParams();if(state.query)q.set('q',state.query);if(state.filter!=='all')q.set('filter',state.filter);q.set('sort',state.sort);if(state.descending)q.set('desc','1');q.set('page',state.page);
    history.replaceState({...history.state,scroll:scrollY},'',`${location.pathname}?${q}`);
  }
  function list() {
    const rows=tableRows();state.page=Math.min(state.page,Math.max(1,Math.ceil(rows.length/50)));
    const columns=['patient','age','outstanding','due','status'];
    $('content').innerHTML=`<div class="toolbar"><label class="search">${t('search')}<input id="search" type="search" value="${esc(state.query)}" autocomplete="off"></label><label>${t('filter')}<select id="filter">${['all','active','overdue','blocked','awaiting_link'].map(k=>`<option value="${k}" ${state.filter===k?'selected':''}>${t(k)}</option>`).join('')}</select></label><span class="muted">${t('timezone')}: ${bdi(state.zone)}</span></div><div class="filter-summary"><span id="result-count">${rows.length} ${t('results')} · ${t(state.filter)}${state.query?' · '+bdi(state.query):''}</span><button id="clear">${t('clear')}</button></div><div class="work-surface"><table class="clinical" role="table"><caption>${t(view==='patients'?'outstanding':view)}. ${t('sort_help')}</caption><colgroup>${columns.map(()=>'<col>').join('')}</colgroup><thead><tr role="row">${columns.map(k=>`<th role="columnheader" scope="col" ${state.sort===k?`aria-sort="${state.descending?'descending':'ascending'}"`:''}><button data-sort="${k}">${t(k)} <span aria-hidden="true">${state.sort===k?(state.descending?'▼':'▲'):'◇'}</span></button></th>`).join('')}</tr></thead><tbody>${rows.slice((state.page-1)*50,state.page*50).map(({record,item,count})=>`<tr role="row" class="${item?.urgent?'urgent':''}"><td role="cell"><span class="stack-label">${t('patient')}</span>${record.patient_id?`<a data-record href="${esc(link(record))}">${bdi(record.display_name)}<small>${bdi(record.patient_id)}</small></a>`:`<span>${t('unassigned')}</span>`}</td><td role="cell"><span class="stack-label">${t('age')}</span>${bdi(record.age||t('missing'))}</td><td role="cell"><span class="stack-label">${t('outstanding')}</span>${bdi(item?.title||t('no_work'))}${count>1?`<small>${count-1} ${t('more')}</small>`:''}${item?.review?`<small>${t('source')}: ${bdi(item.review.source_type)} · ${bdi(item.review.source_id)}</small>`:''}${item?.review?.last_material_change_version>item?.review?.source_version?`<small>${t('changed')}</small>`:''}</td><td role="cell" class="due"><span class="stack-label">${t('due')}</span>${item?time(item.due):t('not_recorded')}</td><td role="cell"><span class="stack-label">${t('status')}</span>${item?badge(item.status,item.urgent?'danger':tone(item.status)):badge('not_recorded')}</td></tr>`).join('')}</tbody></table>${!rows.length?`<p class="empty">${t(state.records.length?'empty':'no_patients')}</p>`:''}</div><div class="pager"><button id="previous" ${state.page<=1?'disabled':''}><span class="turn-arrow" aria-hidden="true">←</span> ${t('previous')}</button><span>${t('page')} ${state.page} ${t('of')} ${Math.max(1,Math.ceil(rows.length/50))}</span><button id="next" ${state.page*50>=rows.length?'disabled':''}>${t('next')} <span class="turn-arrow" aria-hidden="true">→</span></button></div>`;
    $('search').addEventListener('input',e=>{const start=e.target.selectionStart;state.query=e.target.value;state.page=1;list();$('search').focus();$('search').setSelectionRange(start,start);});
    $('filter').onchange=e=>{state.filter=e.target.value;state.page=1;list();$('filter').focus();};
    $('clear').onclick=()=>{state.query='';state.filter='all';state.page=1;list();$('search').focus();};
    for(const button of document.querySelectorAll('[data-sort]'))button.onclick=()=>{state.descending=state.sort===button.dataset.sort?!state.descending:false;state.sort=button.dataset.sort;state.page=1;list();document.querySelector(`[data-sort="${state.sort}"]`).focus();};
    $('previous').onclick=()=>{state.page--;list();$('next').focus();};$('next').onclick=()=>{state.page++;list();$('previous').focus();};remember();
    for(const anchor of document.querySelectorAll('[data-record]'))anchor.onclick=()=>{if(!demo){try{sessionStorage.setItem('sanad-list-return',location.pathname+location.search);sessionStorage.setItem('sanad-list-scroll',String(scrollY));}catch(_){}}history.replaceState({...history.state,scroll:scrollY,focus:anchor.getAttribute('href')},'');};
  }
  function section(title, content){return `<section class="section"><h2>${t(title)}</h2>${content}</section>`;}
  function empty(key='empty'){return `<p class="empty">${t(key)}</p>`;}
  function provenance(value){if(!value)return ''; const rows=Array.isArray(value)?value:[value];return rows.map(p=>`<p class="provenance">${t('source')}: ${bdi(p.source_kind||p.kind||t('not_recorded'))} · ${bdi(p.source_observation_id||p.observation_id||'')} ${p.received_at?time(p.received_at):''}</p>`).join('');}
  function instruction(order){const i=order.structured_instruction||{};return [i.drug,i.dose,i.frequency,i.timing,i.route,i.duration,i.text,i.metric,i.comparator,i.threshold,i.unit].filter(v=>v!==undefined&&v!==null&&v!=='').map(bdi).join(' · ');}
  function monitor(m){const d=m.details||{};if(d.kind!=='MONITOR')return '';return `<table class="reading-table" role="table"><caption>${t('monitoring')} · ${bdi(d.metric)} · ${bdi(d.unit)} · ${bdi(state.zone)}</caption><thead><tr><th scope="col">${t('slot')}</th><th scope="col">${t('reading')}</th><th scope="col">${t('source')}</th></tr></thead><tbody>${(d.slots||[]).map((slot,index)=>{const r=(d.readings||[]).find(r=>r.slot===index);return `<tr role="row"><th role="rowheader" scope="row">${time(slot)}</th><td role="cell"><span class="stack-label">${t('reading')}</span>${r?bdi(r.value)+' '+bdi(d.unit):badge(+new Date(slot)>Date.now()?'not_yet_due':'missing')}</td><td role="cell"><span class="stack-label">${t('source')}</span>${r?`${bdi(r.source_ref?.id)} · v${bdi(r.source_ref?.version)}<small>${t('received')}: ${time(r.received_at)}</small>`:t('missing')}</td></tr>`;}).join('')}</tbody></table>${(d.readings||[]).some(r=>r.slot===null)?`<h3>${t('extras')}</h3>${d.readings.filter(r=>r.slot===null).map(r=>`<p>${bdi(r.value)} ${bdi(d.unit)} ${time(r.observed_at)}</p>`).join('')}`:''}`;}
  function detail(record){
    $('title').textContent=record.display_name;$('subtitle').textContent=`${record.patient_id} · ${t('age')}: ${record.age||t('missing')} · ${t(record.contact_status)}`;
    const orders=(record.orders||[]).filter(o=>o.status==='active');
    const ordersHTML=orders.map(o=>`<article class="record-item"><p>${instruction(o.current_version||{})}</p><small>${t('version')} ${esc(o.current_version?.order_version)} · ${t(o.status)}</small>${provenance(o.current_version?.provenance)}<details><summary>${t('history')}</summary>${(o.history||[]).filter(h=>h.id!==o.current_version?.id).map(h=>`<p>${t('historical')} · ${instruction(h)}</p>${provenance(h.provenance)}`).join('')||empty()}</details></article>`).join('')||empty('no_plan');
    const missions=(record.missions||[]).map(m=>`<article class="record-item"><h3>${bdi(m.title)}</h3>${badge(stateOf(m),tone(stateOf(m)))} <p>${t('due')}: ${time(m.due_at)}</p><p>${t('review')}: ${badge(m.review_status==='pending'?'pending_review':m.review_status==='reviewed'?'resolved':m.review_status==='acknowledged'?'acknowledged':m.review_status)}</p>${monitor(m)}</article>`).join('')||empty('no_requests');
    const facts=(record.facts||[]).map(f=>`<article class="record-item"><p>${bdi(f.payload?.text||f.payload?.clinical_en||f.text||t('retained'))}</p><small>${bdi(f.category)} · ${bdi(f.visibility)}</small>${provenance(f.provenance)}</article>`).join('')||empty();
    const evidence=state.evidence.map(e=>`<article class="record-item"><h3>${bdi(e.category.replaceAll('_',' '))}</h3>${badge(e.association_state==='accepted_pending_identity'?'unverifiable':e.association_state==='candidate'?'pending_review':e.association_state,['accepted_pending_identity','rejected'].includes(e.association_state)?'warning':'')}<p>${t('printed')}: ${bdi(e.printed_date||t('not_recorded'))}</p><p>${t('received')}: ${time(e.provenance?.received_at)}</p>${(e.extracted_values||[]).map(v=>`<p>${bdi(v.name||v.analyte||'')} · ${bdi(v.value??t('missing'))} ${bdi(v.unit||t('missing'))}</p>`).join('')}<p class="provenance">${t('source')}: ${bdi(e.observation_id)} · ${t('version')} ${e.version} · ${bdi(e.patient_match_provenance)}</p>${provenance(e.provenance)}</article>`).join('')||empty();
    const reviews=[...reviewRows(record),...reviewRows(record,true)].map(r=>`<article class="record-item"><h3>${t(r.review_kind)}</h3>${badge(r.state,r.review_kind==='incident_response'&&r.state!=='resolved'?'danger':tone(r.state))}<p>${time(r.review_at)}</p><p class="provenance">${t('source')}: ${bdi(r.source_type)} · ${bdi(r.source_id)} · ${t('version')} ${r.source_version}</p>${r.last_material_change_version>r.source_version?`<p>${t('changed')}</p>`:''}<p>${t('notice')}: ${r.first_notice_at?time(r.first_notice_at):t('not_recorded')}</p>${r.resolved_reason?`<p>${t('reason')}: ${bdi(r.resolved_reason)}</p>`:''}</article>`).join('')||empty();
    $('content').innerHTML=`<a class="back" id="back" href="${demo?'/demo':'/a'}"><span class="turn-arrow" aria-hidden="true">←</span> ${t('back')}</a><div class="detail-grid"><div>${section('plan',ordersHTML)}${section('missions',missions)}${section('facts',facts)}</div><div>${section('inbox',reviews)}${section('evidence',evidence)}${section('source_files',(!demo?record.media||[]:[]).map(m=>`<article class="record-item"><a href="/api/patients/${encodeURIComponent(record.patient_id)}/media/${encodeURIComponent(m.media_id)}">${t('original')} · ${bdi(m.kind)}</a><p>${time(m.date)}</p></article>`).join('')||empty())}</div></div>`;
    $('back').onclick=e=>{if(!demo){try{const saved=sessionStorage.getItem('sanad-list-return');if(saved&&/^\/a(?:\/(?:inbox|history))?(?:\?|$)/.test(saved)){e.preventDefault();location.assign(saved);}}catch(_){}}};
  }
  function patientView(data){
    const plan=data.plan||data;$('title').textContent=t('yourcare');$('subtitle').textContent=t('patient_note');
    // Explicit projection allow-list. No ids, kind names, raw objects or internal statuses.
    const orderHTML=(plan.orders||[]).map(o=>`<article class="record-item"><p>${[o.drug,o.dose,o.frequency,o.timing,o.route,o.duration].filter(Boolean).map(bdi).join(' · ')}</p></article>`).join('')||empty('no_plan');
    const requests=(plan.next_missions||[]).map(m=>`<article class="record-item"><p>${bdi(m.title)}</p><p>${t('due')}: ${time(m.due_at,plan.preferences?.timezone||'UTC')}</p></article>`).join('')||empty('no_requests');
    const reports=(plan.medication_reports||[]).map(r=>`<article class="record-item"><p>${bdi(r.text)}</p><small>${t('self_report')}</small></article>`).join('')||empty('no_reports');
    const prefs=plan.preferences||{};
    $('content').innerHTML=`<p>${t('doctor')}: ${bdi(plan.doctor_name)}</p>${section('your_plan',orderHTML)}${section('your_requests',requests)}${section('reports',reports)}${section('last_reading',plan.last_reading?`<p class="record-item">${bdi(plan.last_reading.text)}</p>`:badge('missing'))}${section('reminders',`<p>${t(prefs.routine_contact_enabled?'enabled':'disabled')} · ${t(prefs.contact_status||'not_recorded')}</p><p>${t('quiet')}: ${bdi((prefs.quiet_hours||[]).join(' – '))} · ${bdi(prefs.timezone)}</p>${prefs.resume_at?`<p>${t('resume')}: ${time(prefs.resume_at,prefs.timezone)}</p>`:''}`)}${section('questions',(plan.open_questions||[]).map(q=>`<article class="record-item"><p>${bdi(q.question)}</p><small>${t('waiting')}</small></article>`).join('')||empty('no_questions'))}`;
  }
  function preferences(){
    const p=state.pref;$('content').innerHTML=`<section class="preferences"><form id="language-form"><label for="language">${t('language')}<select id="language"><option value="en" ${p.language==='en'?'selected':''}>English</option><option value="ar" ${p.language==='ar'?'selected':''}>العربية</option></select></label>${p.contest_english?`<p class="muted">${t('contest')}</p>`:''}<button class="primary" type="submit">${t('save')}</button><p id="save-result" role="status"></p></form></section>`;
    $('language-form').onsubmit=async e=>{e.preventDefault();const button=e.target.querySelector('button');button.disabled=true;const token=document.cookie.split('; ').find(s=>s.startsWith('sanad_csrf='))?.split('=')[1]||'';try{await api('/api/preferences',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':decodeURIComponent(token)},body:JSON.stringify({language:$('language').value,expected_version:p.version,command_id:crypto.randomUUID()})});$('save-result').textContent=t('saved');location.reload();}catch(error){$('save-result').className='error';$('save-result').textContent=error.message;button.disabled=false;}};
  }
  function render(){if(patient)patientView(state.data);else if(view==='preferences')preferences();else if(view==='detail')detail(state.records[0]);else list();}
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
      else {const [panel,pref]=await Promise.all([api('/api/patients',{signal}),api('/api/preferences',{signal})]);state.zone=pref.timezone;const records=[];let index=0;await Promise.all(Array.from({length:Math.min(6,panel.length)},async()=>{while(index<panel.length){const p=panel[index++];records.push(await api(`/api/patients/${encodeURIComponent(p.patient_id)}`,{signal}));}}));if(request!==generation)return;state.records=records;if(view==='inbox'||view==='history')state.reviews=await api('/api/browser/reviews'+(view==='history'?'?history=true':''),{signal});}
      if(request!==generation)return;
      render();$('feedback').textContent='';$('freshness').textContent=`${t('updated')} ${new Intl.DateTimeFormat(lang,{hour:'2-digit',minute:'2-digit',second:'2-digit'}).format(new Date())}`;
      if(first&&!patient&&['patients','inbox','history'].includes(view)){let position=history.state?.scroll||0;try{if(sessionStorage.getItem('sanad-list-return')===location.pathname+location.search)position=Number(sessionStorage.getItem('sanad-list-scroll'))||position;}catch(_){}if(position)scrollTo(0,position);}
    }catch(error){if(error.name==='AbortError')return;if(request!==generation)return;
      // Never retain sensitive clinical content after a revoked/expired session.
      if([401,403,404].includes(error.status)||first)$('content').replaceChildren();
      $('feedback').innerHTML=`<p class="error" role="alert">${esc(error.status?error.message:t('failed'))}</p>`;
      $('freshness').textContent='';
    }finally{clearTimeout(timer);if(request===generation){$('feedback').className='';$('content').setAttribute('aria-busy','false');$('refresh').disabled=false;}}
  }
  $('theme').value=document.documentElement.dataset.themeChoice;
  $('eyebrow').textContent=patient?t('doctor'):t('clinic');$('title').textContent=patient?t('yourcare'):t(view);$('subtitle').textContent=(view==='inbox'||view==='history')?t('review_note'):t('subtitle');
  if(demo){$('banner').innerHTML=`<div class="demo-banner"><strong>${t('demo')}</strong><p>${t('demo_note')}</p></div>`;$('navigation').innerHTML=`<a href="/demo" aria-current="page">${t('patients')}</a>`;}
  else $('navigation').innerHTML=patient?`<a href="/pp" aria-current="page">${t('yourcare')}</a>`:['patients','inbox','history','preferences'].map(k=>`<a href="${k==='patients'?'/a':'/a/'+k}" ${view===k?'aria-current="page"':''}>${t(k)}</a>`).join('');
  $('refresh').onclick=load;
  window.addEventListener('pageshow',event=>{if(event.persisted)load();});
  if(demo)window.addEventListener('hashchange',()=>{view=location.hash?'detail':'patients';load();});
  load();
})();
