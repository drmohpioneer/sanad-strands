/* Blocking same-origin script: explicit choice wins before the first paint. */
(() => {
  let choice = 'system';
  try { choice = localStorage.getItem('sanad-theme') || 'system'; } catch (_) {}
  if (!['light', 'dark', 'system'].includes(choice)) choice = 'system';
  const system = matchMedia('(prefers-color-scheme: dark)');
  const apply = () => {
    document.documentElement.dataset.theme = choice === 'system' ? (system.matches ? 'dark' : 'light') : choice;
    document.documentElement.dataset.themeChoice = choice;
  };
  apply();
  document.addEventListener('DOMContentLoaded', () => {
    // Freeze only fonts that miss the optional loading window. Chromium can
    // otherwise recalculate variable-font metrics in controls after first paint.
    setTimeout(() => {
      const loaded = family => [...document.fonts].some(face => face.family === family && face.status === 'loaded');
      for (const [token, families, fallback] of [
        ['--font-ui', ['Inter', 'ArabicUI'], ['system-ui', 'sans-serif']],
        ['--font-display', ['Playfair', 'ArabicUI'], ['serif']]
      ]) {
        if (families.some(family => !loaded(family))) {
          document.documentElement.style.setProperty(token, [...families.filter(loaded), ...fallback].join(','));
        }
      }
    }, 120);
  });
  document.addEventListener('DOMContentLoaded',()=>{const select=document.getElementById('theme');if(select)select.value=choice;});
  system.addEventListener('change', apply);
  document.addEventListener('change', event => {
    if (event.target.id !== 'theme') return;
    choice = event.target.value;
    try { localStorage.setItem('sanad-theme', choice); } catch (_) {}
    apply();
  });
})();
