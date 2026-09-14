# Synthetic environment testing instructions

**Availability:** the hosted synthetic environment runs at `https://btx35drwcqejwxymdcwfzwku5m0cynoz.lambda-url.us-east-1.on.aws` and stays available, unchanged, until 9 October 2026. The doctor access link (the Sanad bot with an access code) is supplied privately with the contest submission. Everything in this environment is synthetic: use fictional names and documents only, and never send real patient information or credentials.

Below, `BASE` means the address above.

**Prepare:** a browser; your own Telegram account, which becomes a doctor; a second Telegram account for the patient (your own second account or a colleague's); one clear typed prescription image and one clear typed lab image in Latin script, showing a fictional patient. A short typed PDF lab report (up to 10 pages) is optional. Use the same fictional name on the record and the documents.

**Timing labels used below:** **Minute tick** means durable background work is checked every minute, with additional processing time possible. It is not a one-minute delivery guarantee. **Digest wait** means questions must first become overdue and then reach the configured digest time. **Compressed time** means a prepared synthetic scenario or recording advances a wait; it never means the live product changed a deadline. There is no tester clock control. Use the immediate views below to inspect pending work without changing clocks.

**Quick look without any account:** `BASE/demo`, `BASE/demo/patient` and `BASE/demo/admin` show fictional records in the real pages.

## 1. Telegram as the doctor

1. Open the doctor access link on the device where your Telegram account is signed in, and press **Start** in the Sanad chat. You receive a message that your doctor account is approved. If you instead receive a message that your application was received, the access code was not included: open the access link again rather than typing `/start` yourself.
2. Send `/login`. Open the one-use link in your own browser and press **Continue**. It expires after ten minutes; request another `/login` link if it expires or has been used. Opening the preview alone does not sign you in. The dashboard opens at `BASE/a`; keep it open for section 3. Never share the link.
3. Send `/new Taylor Test`, then inspect the name and press **Confirm**. If that name already exists in your account, select the existing record rather than creating a duplicate. Use the same full name below.
4. Type: `Taylor Test, start atorvastatin 20 mg once daily. Request potassium and creatinine tomorrow. Check blood pressure twice a day for three days.` Inspect the proposed medication, dose, tests, monitoring schedule and visible deadlines. Correct any uncertain field before confirming. Nothing becomes an active instruction merely because a message arrived.
5. Send a short English voice note: `Taylor Test, change atorvastatin to 40 mg once daily.` Check the name, old and new dose, and action before pressing **Confirm**. **Minute tick:** if background recovery is needed, the saved request can stay pending until a later tick; do not resend while it is still processing.
6. Send the typed prescription image. Check the extracted rows and any questions where the two readings disagree. Select or clarify the patient if asked, and correct the card before confirming. A historical prescription must not silently replace an active order. **Minute tick:** image processing can finish after receipt; a receipt is not an accepted prescription.
7. Send `/corrections`, then `/corrections PATIENT_ID`, using the ID shown for Taylor Test. Inspect the correction guidance for that record. Use the browser controls in section 3 for an actual correction.
8. Send `/questions`. Inspect the numbered list or the empty state; section 2 creates a question. Send `/digest`, then `/digest HH:MM one`, replacing `HH:MM` with a time five minutes ahead in the displayed doctor timezone. `/digest HH:MM each` selects individual messages instead. Confirm the saved time and packing.
9. **Digest wait:** a new question normally has a 48-hour deadline, then waits for the configured digest time. Moving the digest five minutes ahead does not age a new question. Inspect a fresh question immediately with `/questions`; do not expect a digest yet.
10. After section 2 creates a question, run `/questions` again. Send `/answer N Please contact the clinic to discuss this question.`, using its current number. The patient receives that exact answer. Send `/reuse` and check the saved reusable-answer response. Use `/send N` only for a current proposal you intend to send. **Minute tick:** delivery may await background recovery.
11. Send `/inbox` after the patient image and danger checks below. Inspect the evidence and the urgent review separately. Acknowledging a review leaves it unresolved; resolution needs the displayed action and a reason. **Minute tick:** ordinary due reviews can appear after a tick. Danger never waits for a digest. Unresolved ordinary reviews join a weekly bundle after seven days; showing that bundle immediately would need **Compressed time: seven-day review wait**, so inspect it with `/inbox` instead.

## 2. Telegram as the patient

1. From the doctor chat send `/qr Taylor Test`. Open the invitation on your second Telegram account, follow the Telegram handoff, read the consent and accept it. Then, on the doctor account, confirm that this is the intended patient. Before that confirmation the patient cannot see the plan or record. One Telegram patient identity cannot bind to more than one record.
2. As the patient, ask `What is my plan?`, then `What is cholesterol?`. Inspect the active plan and any source-labelled general explanation. If the approved sources do not cover a question, expect a clear limitation and a referral to the doctor, with no invented instructions.
3. Ask `Can I double my dose?`. Expect the question to be referred to the doctor, with no dose change. **Digest wait:** no immediate ordinary question notification is promised; return to section 1 and use `/questions` to answer it now.
4. Send the typed lab image, or the PDF. Check the receipt and the later reading status. A partial report stays incomplete. If the printed identity is uncertain, the doctor confirms whose document it is before completion. **Minute tick:** reading and delivery can continue after receipt. Medical review stays separate from receiving or completing the report.
5. Send `stop reminders`. Then send `resume` and press the confirmation button. Send `quiet hours 22:00 to 07:00`. Check each saved preference. Pausing reminders never changes an instruction or erases an unresolved review.
6. In this fictional record only, send `chest pain now`. Check that immediate safety guidance appears and that the doctor receives a danger report straight away, independently of any digest. Delivery does not prove the doctor has read it; the review stays visible until resolved.
7. Check that the answer sent in section 1 appears unchanged. To test medication follow-up, send `I started atorvastatin today` and inspect the reported-start acknowledgment. The start report does not erase the separate day-three check-in; showing that check-in immediately would need **Compressed time: day-three check-in**.

## 3. Doctor dashboard

1. Return to `BASE/a`, requesting a fresh `/login` link if needed. The boxes at the top count patients who need you now, are waiting on you, are late, or have tasks due today; press a box to show only that group. Beside the search, **Needs me**, **All** and **Settled** switch the list. Search for Taylor Test. Long lists show 20 patients per page.
2. Click the patient row to open the card inside the list, with the latest readings and what is waiting. Choose **Open the full record**. Check the current plan, outstanding work, documents beside what was read from them, the reading chart and history. Compare medication values with the last confirmed card. Open the lab document and close its viewer with Escape. Use **Back to patients** to return.
3. In **Inbox**, inspect the lab review, confirm its identity if asked and associate it with the correct tests. Check the difference between received, incomplete, complete and reviewed. For an active instruction choose **Amend instruction**, give a reason, inspect the change and confirm it. For accepted evidence use **Correct or detach** and the displayed validation controls. History keeps the earlier value. A message already accepted by the patient cannot be recalled by correcting the record.
4. Inspect open and acknowledged reviews in **Inbox**, and resolved reviews in **History**. Acknowledgment alone never moves work into resolved history. From the record, reply to the patient's question; an answered question is never sent twice.
5. In **Preferences**, change the digest time and packing, then send `/digest` in Telegram and compare. **Digest wait:** saving is immediate, but a digest still needs overdue questions and its scheduled time. Switch light and dark appearance.
6. Open a made-up patient address under `BASE/a/patients/`. Expect a refusal or a not-found page, never another doctor's record.
7. Administrator access is separate and is not needed for these checks. Your doctor account has no account-management powers. `BASE/demo/admin` shows fictional account applications.

## 4. Patient page

1. From the patient chat send `/login`, open the one-use link and press **Continue**. The patient page opens at `BASE/pp`. Check that it shows the same medication plan and requests as Telegram.
2. Upload a different synthetic typed image or a PDF. Inspect the received and processing status and the later outcome, and check the same item on the doctor record. **Minute tick:** reading can take later ticks. Processing is not acceptance.
3. Send `What is my plan?` through the conversation box. Check the reply and the kept conversation. The sign-in link never appears in the conversation.
4. In **Settings**, pause reminders, resume, set quiet hours and change a reading time. Check the saved preference after each action. **Minute tick:** scheduled contact follows the saved settings when next due. Sign out and confirm that private content needs a fresh sign-in.

## 5. Voice and photo availability

Voice notes and photos are read through a hosted model service with a limited quota. If a voice or photo request reports temporary unavailability or asks for text, note that response, stop repeating media and continue with text or another check. A rate limit never becomes an invented transcription or an accepted image. Danger screening of text never depends on media availability.

When reporting a result, name the step, what was visible and whether timing was real or compressed. Keep sign-in links, access details and private messages out of reports and recordings.
