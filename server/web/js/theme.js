/*
 * テーマ切り替え。既定は OS の設定(prefers-color-scheme)に従い、
 * 手動で切り替えた場合のみ data-theme 属性で上書きして localStorage に残す。
 * ビルド工程を持たないため、素の JS を各ページから読み込む。
 */
(function () {
  var KEY = 'instant-news:theme';

  var saved = null;
  try { saved = localStorage.getItem(KEY); } catch (e) { /* プライベートモード等では保存しない */ }
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
    try { localStorage.setItem(KEY, theme); } catch (e) { /* 保存できなくても切り替えは効く */ }
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
