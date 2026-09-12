() => {
  const failures = [];
  const forbidden = /[a-f0-9]{32}|\b(?:entity_type|source_type|mission|missions|objective|predicate|patient_released|patient_report)\b|\b[a-zA-Z]+_[a-zA-Z_]+\b/ig;
  const patient = document.body.dataset.audience === 'patient';
  if (patient && document.querySelector('details.support')) failures.push('Patient support disclosure');
  const excluded = element => element.closest('script,style') || (!patient && element.closest('details.support'));
  const check = text => { for (const match of text.matchAll(forbidden)) failures.push(match[0]); };
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) if (!excluded(walker.currentNode.parentElement)) check(walker.currentNode.textContent);
  for (const element of document.querySelectorAll('[aria-label],[title]')) {
    if (!excluded(element)) { check(element.getAttribute('aria-label') || ''); check(element.getAttribute('title') || ''); }
  }
  return failures;
}
