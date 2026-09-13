/* Blocking same-origin script: explicit choice wins before the first paint. */
(() => {
  let choice = 'dark';
  try { choice = localStorage.getItem('sanad-theme') || 'dark'; } catch (_) {}
  if (!['light', 'dark'].includes(choice)) choice = 'dark';
  const apply = () => {
    document.documentElement.dataset.theme = choice;
    document.documentElement.dataset.themeChoice = choice;
    document.querySelector('meta[name="color-scheme"]')?.setAttribute('content', choice);
    document.querySelectorAll('[data-theme-set]').forEach(button => {
      button.setAttribute('aria-pressed', String(button.dataset.themeSet === choice));
    });
  };
  apply();
  document.addEventListener('DOMContentLoaded', apply);
  document.addEventListener('click', event => {
    const button = event.target.closest('[data-theme-set]');
    if (!button || !['dark', 'light'].includes(button.dataset.themeSet)) return;
    choice = button.dataset.themeSet;
    try { localStorage.setItem('sanad-theme', choice); } catch (_) {}
    apply();
  });
})();
