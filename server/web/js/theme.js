/*
 * Theme switching. The default follows the OS setting (prefers-color-scheme); only an explicit toggle overrides it through the data-theme attribute and keeps the choice in localStorage.
 * There is no build step, so each page loads this as plain JS.
 */
(function () {
  var KEY = 'instant-news:theme';

  var saved = null;
  try { saved = localStorage.getItem(KEY); } catch (e) { /* private browsing and the like: nothing was saved */ }
  if (saved === 'dark' || saved === 'light') {
    document.documentElement.setAttribute('data-theme', saved);
  }

  function current() {
    var attr = document.documentElement.getAttribute('data-theme');
    if (attr) return attr;
    return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }

  function apply(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    try { localStorage.setItem(KEY, theme); } catch (e) { /* not saved, but the switch still takes effect */ }
    var btn = document.querySelector('[data-theme-toggle]');
    if (btn) {
      btn.textContent = theme === 'dark' ? '☀' : '🌙';
      btn.setAttribute('aria-label', theme === 'dark' ? 'ライトテーマに切り替える' : 'ダークテーマに切り替える');
    }
  }

  document.addEventListener('DOMContentLoaded', function () {
    apply(current());
    var btn = document.querySelector('[data-theme-toggle]');
    if (!btn) return;
    btn.addEventListener('click', function () {
      apply(current() === 'dark' ? 'light' : 'dark');
    });
  });
})();
