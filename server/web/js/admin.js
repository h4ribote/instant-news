/*
 * 管理画面(/admin)の制御。プラットフォームの設定と、分類(カテゴリ・タグ・報道社)を 1 つの画面で扱う。
 *
 * この画面が触るのは 2 種類のものである。settings 表の設定(GET/PATCH /api/admin/settings)と、専用の表を持つ
 * 分類(/api/admin/{categories,tags,publishers})である。前者は「全体を見渡して数個を直す」ため一括で保存し、
 * 後者は「1 件を足す・直す・消す」ため行ごとに即座に確定する。この違いがそのままタブの操作の違いになっている。
 *
 * ロールを指す設定(admin.role_ids・速報の各ロール)と報道社の所属ロールは、ID の直接入力ではなく、bot が
 * 取り込んだロール(GET /api/admin/roles)から名前と色で選ばせる。とりわけ admin.role_ids は貼り間違いが
 * そのまま自分の締め出しになるため、選択にする意味が大きい。取り込みが間に合っていないロールのために、ID の
 * 直接入力も併せて残す(bot はロールの作成イベントを受けて写すため、作った直後はまだ一覧に無いことがある)。
 *
 * 設定のキー・型・値域・参照先はすべてサーバーの登録簿が持ち、この画面には設定のキーを 1 つも書かない。値域は
 * 応答の minimum / maximum / exclusive_minimum から、ロール選択の要否は ref から決める。画面がこれらを写し取ると
 * 登録簿と食い違い、その食い違いが「保存を押して初めて 400 で上限を知る」形で現れる。
 */

import { get, post, patch, del, me, escapeHtml, formatDateTime, renderAdSlot, ApiError } from '/static/js/api.js';
import { mountChrome, listOf, errorMessage, loginUrl, tagIconHtml, pubLogoHtml } from '/static/js/chrome.js';

/* ---------------------------------------------------------------- 状態 */

let viewer = null;
/* GET /api/admin/settings が返した各設定の記述。保存時に「変更されたか」を判定する基準でもあるため、画面を描き直すたびに入れ替える。 */
let defs = [];
/* GET /api/admin/roles の結果。設定のロール選択と報道社の所属ロールが共有する。取得に失敗した場合は null のままで、ロールの欄は ID の直接入力へ落とす。 */
let roles = null;
/* 分類は初めてそのタブを開いたときに読む。設定と違い、開くまで要らない。 */
const loaded = { categories: false, tags: false, publishers: false };
/* 分類の一覧の最後に読んだ行。行の編集を開くときに現在値を引くために保持する。 */
const entityCache = { categories: [], tags: [], publishers: [] };
/* 設定タブに未保存の変更があるか。分類タブは行ごとに即座に確定するため、未保存の状態を持つのは設定タブだけである。 */
let settingsDirty = false;

const TABS = [
  { id: 'settings', label: '設定' },
  { id: 'categories', label: 'カテゴリ' },
  { id: 'tags', label: 'タグ' },
  { id: 'publishers', label: '報道社' },
];

/* 設定を束ねる見出し。キーの最初の区切りまでで分ける。ここに無い区切りはその文字列をそのまま見出しにするため、登録簿に新しい系統のキーが増えても、見出しの追加を忘れて欄そのものが消えることはない。 */
const GROUP_LABELS = {
  discord: 'Discord',
  admin: 'プラットフォーム管理',
  articles: '記事',
  media: '画像',
};

/* 特定の設定にだけ添える注意。値の説明(description)は登録簿が持つが、ここに書くのは「この欄を触ると何が起きうるか」という操作の側の警告である。 */
const EXTRA_NOTICES = {
  'admin.role_ids': '<div class="notice"><span class="ic">⚠</span><div>' +
    '<b>この欄の指定を誤ると、あなた自身がこの画面から締め出されます。</b>' +
    'Discord の Administrator 権限を持つロールの保有者と guild のオーナーは、この設定に関わらず管理者として扱われますが、どちらにも当てはまらない場合、ここから自分のロールを外した時点で管理操作を行えなくなります。' +
    'その状態はこの画面からは直せません。復旧は、DB へ到達できる者が CLI から <code>python -m server.cli set-setting admin.role_ids \'["ロール ID"]\'</code> を実行する経路だけになります。' +
    '</div></div>',
};

/* ---------------------------------------------------------------- 小さな部品 */

function warnHtml(html) {
  return `<div class="notice"><span class="ic">⚠</span><div>${html}</div></div>`;
}

function infoHtml(html) {
  return `<div class="notice info"><span class="ic">ℹ</span><div>${html}</div></div>`;
}

/** パネル本体を 1 枚のカードの文言で置き換える。読み込めない・権限が無いなど、入力欄を残す意味が無い状態に使う。 */
function renderMessage(title, bodyHtml) {
  document.querySelector('[data-main]').innerHTML =
    `<section class="card"><div class="section-head"><h2>${escapeHtml(title)}</h2></div><div class="body">${bodyHtml}</div></section>`;
}

/** ロールの色(24bit)を CSS の色にする。色なし(0)は Discord 上でも既定色であり、見分けのための淡い枠だけにする。 */
function roleSwatch(color) {
  const hex = color ? '#' + Number(color).toString(16).padStart(6, '0') : 'transparent';
  return `<span class="role-swatch" style="background:${escapeHtml(hex)}"></span>`;
}

/* ---------------------------------------------------------------- ロール選択 */

/**
 * ロールの複数選択(設定の id_list、ref=role 用)。bot が取り込んだロールを名前と色で並べ、選択済みの ID のうち
 * 一覧に無いもの(削除された・まだ取り込まれていない)は「未取得」として checked のまま残す。落とすと、画面で
 * 触っていないのに保存で消えてしまう。一覧そのものを取得できていない場合は ID の直接入力へ落とす。
 */
function rolePickerMulti(key, selectedIds) {
  const selected = (selectedIds || []).map(String);
  const rawHint = '一覧に無いロールの ID を追加できます(カンマまたは空白区切り)。bot が取り込む前のロールはここに書きます。';
  if (!roles) {
    return `<textarea data-rolefallback="${escapeHtml(key)}" rows="3" placeholder="ロール ID をカンマ区切り、または 1 行に 1 件。">${escapeHtml(selected.join('\n'))}</textarea>` +
      `<div class="hint">ロールの一覧を取得できなかったため、ID を直接入力してください。</div>`;
  }
  const selectedSet = new Set(selected);
  const known = new Set(roles.map((r) => String(r.id)));
  const boxes = roles.map((r) => roleCheckbox(r, selectedSet.has(String(r.id))));
  const orphans = selected.filter((id) => !known.has(id)).map((id) => orphanCheckbox(id));
  const body = (boxes.length || orphans.length)
    ? `<div class="rolepick" data-rolepick="${escapeHtml(key)}">${boxes.join('')}${orphans.join('')}</div>`
    : '<div class="rolepick-empty">bot が取り込んだロールがまだありません。下の欄に ID を直接入力できます。</div>';
  return body +
    `<input class="rolepick-raw" type="text" inputmode="numeric" data-rolepick-raw="${escapeHtml(key)}" placeholder="${escapeHtml(rawHint)}">`;
}

function roleCheckbox(role, checked) {
  const admin = role.is_administrator ? '<span class="role-admin">管理者</span>' : '';
  return `<label>${roleSwatch(role.color)}<input type="checkbox" data-role-id="${escapeHtml(role.id)}"${checked ? ' checked' : ''}>@${escapeHtml(role.name)}${admin}</label>`;
}

function orphanCheckbox(id) {
  return `<label class="orphan">${roleSwatch(0)}<input type="checkbox" data-role-id="${escapeHtml(id)}" checked>未取得のロール: ${escapeHtml(id)}</label>`;
}

/** 選択済みのロール ID を読み取る(複数)。チェックされたロールと、直接入力された ID を併せる。重複は畳む。 */
function readRolesByKey(key) {
  const ids = [];
  const box = document.querySelector(`[data-rolepick="${key}"]`);
  if (box) for (const cb of box.querySelectorAll('input[type="checkbox"]:checked')) ids.push(String(cb.dataset.roleId));
  const raw = document.querySelector(`[data-rolepick-raw="${key}"]`);
  if (raw) for (const t of raw.value.split(/[\s,]+/)) if (t) ids.push(String(t));
  const fallback = document.querySelector(`[data-rolefallback="${key}"]`);
  if (fallback) for (const t of fallback.value.split(/[\s,]+/)) if (t) ids.push(String(t));
  return [...new Set(ids)];
}

/**
 * ロールの単一選択(報道社の role_id 用。0 個か 1 個)。所属ロールは既存でなければ API が弾くため、一覧に無い
 * ロールを新たに割り当てる余地は無く、選択肢(取り込み済みのロール)だけで足りる。現在値が一覧から消えている
 * (ロールが削除された)場合に備え、その ID も選択肢として残す。
 */
function roleSelectSingle(selectedId) {
  const sel = selectedId == null ? '' : String(selectedId);
  const opts = ['<option value="">(ロールなし / 解散)</option>'];
  let found = false;
  for (const r of (roles || [])) {
    const v = String(r.id);
    if (v === sel) found = true;
    opts.push(`<option value="${escapeHtml(v)}"${v === sel ? ' selected' : ''}>@${escapeHtml(r.name)}${r.is_administrator ? '(管理者)' : ''}</option>`);
  }
  if (sel && !found) opts.push(`<option value="${escapeHtml(sel)}" selected>未取得のロール: ${escapeHtml(sel)}</option>`);
  return `<select data-f="role_id">${opts.join('')}</select>`;
}

/* ---------------------------------------------------------------- 設定タブ */

/** 値域(int / number)を入力欄の属性にする。exclusive の整数は「より大きい」を +1 して min にできる。number の exclusive は min で表せないため付けず、読み取り時に弾く。 */
function numberAttrs(d) {
  const attrs = [d.kind === 'int' ? 'step="1"' : 'step="any"'];
  if (d.minimum != null) {
    const min = (d.exclusive_minimum && d.kind === 'int') ? d.minimum + 1 : d.minimum;
    if (!(d.exclusive_minimum && d.kind === 'number')) attrs.push(`min="${escapeHtml(min)}"`);
  }
  if (d.maximum != null) attrs.push(`max="${escapeHtml(d.maximum)}"`);
  return attrs.join(' ');
}

/** 値域を言葉にした注記。入力欄の制約に加えて文でも示す(number の exclusive のように属性で表せないものもあるため)。 */
function rangeHint(d) {
  if (d.kind !== 'int' && d.kind !== 'number') return '';
  const parts = [];
  if (d.minimum != null) parts.push(d.exclusive_minimum ? `${d.minimum} より大きい` : `${d.minimum} 以上`);
  if (d.maximum != null) parts.push(`${d.maximum} 以下`);
  return parts.length ? `値域: ${parts.join('、')}。` : '';
}

/** 変更不可の設定を読み取り専用で出すときの表示文字列。 */
function readonlyText(d) {
  if (d.kind === 'id_list') return (d.value || []).map(String).join(', ');
  return d.value === null || d.value === undefined ? '' : String(d.value);
}

/**
 * 入力欄。型(kind)は登録簿が定義し、検証もサーバーが行う。ここでの型の使い分けは入力のしやすさのためであり、検証の代わりではない。
 * ID(id / id_list)をテキストで扱うのは要件である。数値の入力欄にすると、値は JavaScript の Number を経由することになり、2^53 を超えるスノーフレークは黙って下位桁が壊れる。
 */
function controlHtml(d, id) {
  const field = `id="${id}" data-field="${escapeHtml(d.key)}"`;
  if (!d.editable) {
    return `<input type="text" ${field} value="${escapeHtml(readonlyText(d))}" readonly>`;
  }
  if (d.kind === 'secret') {
    /* 現在値は応答に含まれない(値を返した時点で、この画面を開ける者すべてに bot のトークンが渡る)。空欄は「変更しない」を意味する。 */
    return `<input type="password" ${field} autocomplete="new-password" placeholder="変更するときだけ新しい値を入力">`;
  }
  if (d.kind === 'int' || d.kind === 'number') {
    return `<input type="number" ${numberAttrs(d)} ${field} value="${escapeHtml(d.value)}">`;
  }
  if (d.ref === 'role' && d.kind === 'id_list') {
    /* ロールの複数選択。data-field はロールの入れ物に付け、値の読み取りは readRolesByKey が担う。 */
    return `<div ${field}>${rolePickerMulti(d.key, d.value)}</div>`;
  }
  if (d.kind === 'id_list') {
    return `<textarea ${field} rows="3" placeholder="ID をカンマ区切り、または 1 行に 1 件。空にすると「指定なし」になります。">${escapeHtml((d.value || []).map(String).join('\n'))}</textarea>`;
  }
  const numeric = d.kind === 'id' ? ' inputmode="numeric"' : '';
  return `<input type="text" ${field}${numeric} value="${escapeHtml(d.value)}">`;
}

/** 設定の状態を示すバッジ。 */
function badgesHtml(d) {
  const out = [];
  if (!d.editable) out.push('<span class="badge vis-members">変更不可</span>');
  if (d.restart_required) out.push('<span class="badge vis-restricted">再起動が必要</span>');
  if (d.secret) out.push(d.is_set ? '<span class="badge vis-public">設定済み</span>' : '<span class="badge vis-restricted">未設定</span>');
  return out.join(' ');
}

/** 最終更新の表示。誰が変えたのかを残すのは、設定の変更が事後に説明を求められる操作だからである。 */
function metaText(d) {
  const at = formatDateTime(d.updated_at);
  const parts = [at ? `最終更新 ${at}` : '最終更新の記録はありません'];
  parts.push(d.updated_by ? `変更した利用者の ID: ${d.updated_by}` : '画面からの変更の記録はありません');
  return parts.join(' ・ ');
}

/** 設定 1 つの行。 */
function settingRowHtml(d, index) {
  const id = `set-${index}`;
  const key = escapeHtml(d.key);
  const desc = escapeHtml(d.description);
  const badges = badgesHtml(d);
  const parts = [`<label class="lbl" for="${id}">${escapeHtml(d.label)}${badges ? ` ${badges}` : ''}</label>`];
  parts.push(controlHtml(d, id));
  parts.push(d.editable ? `<div class="hint"><code>${key}</code> — ${desc}</div>` : `<div class="hint"><code>${key}</code></div>`);
  const range = rangeHint(d);
  if (range) parts.push(`<div class="hint">${escapeHtml(range)}</div>`);
  parts.push(`<div class="hint">${escapeHtml(metaText(d))}</div>`);
  if (!d.editable) parts.push(`<div class="notice info"><span class="ic">🔒</span><div><b>この設定は画面から変更できません。</b>${desc}</div></div>`);
  if (d.editable && d.secret) parts.push('<div class="hint">現在の値は画面からは読み出せません。空欄のまま保存すれば、この設定は変更されません。</div>');
  if (d.restart_required) parts.push('<div class="hint">この設定は起動時にだけ読まれます。保存しても、対象のプロセスを再起動するまで反映されません。</div>');
  if (EXTRA_NOTICES[d.key]) parts.push(EXTRA_NOTICES[d.key]);
  return `<div class="form-row" data-row="${escapeHtml(d.key)}">${parts.join('')}</div>`;
}

/** キーの最初の区切りで設定を束ねる。並びは応答(登録簿)の順をそのまま保つ。 */
function settingGroups(list) {
  const groups = [];
  for (const d of list) {
    const prefix = String(d.key).split('.')[0];
    let group = groups.find((g) => g.prefix === prefix);
    if (!group) {
      group = { prefix, items: [] };
      groups.push(group);
    }
    group.items.push(d);
  }
  return groups;
}

/** 設定タブを描く。 */
function renderSettingsPanel() {
  const panel = document.querySelector('[data-panel="settings"]');
  if (defs.length === 0) {
    panel.innerHTML = '<section class="card"><div class="body">設定が 1 つも返りませんでした。設定の初期値の投入(起動時の bootstrap)が済んでいない可能性があります。</div></section>';
    return;
  }
  let index = 0;
  const cards = settingGroups(defs).map((group) => {
    const rows = group.items.map((d) => settingRowHtml(d, index++)).join('');
    return `<section class="card"><div class="section-head"><h2>${escapeHtml(GROUP_LABELS[group.prefix] || group.prefix)}</h2></div>${rows}</section>`;
  }).join('');
  panel.innerHTML = cards +
    '<section class="card"><div class="body" data-settings-status hidden></div>' +
    '<div class="form-actions"><button class="btn primary" type="button" data-save>変更を保存</button>' +
    '<span class="spacer"></span><span class="hint" data-dirty-count></span></div></section>';

  panel.querySelector('[data-save]').addEventListener('click', (ev) => saveSettings(ev.currentTarget));
  // 変更の有無は入力のたびに見直す。ロールの選択(checkbox)も直接入力(input)も同じ経路で拾えるよう、input と change の両方を購読する。委譲は再生成されない panel 要素に一度だけ張る(保存のたびに張り直すと重複して積み上がる)。
  if (!panel.dataset.wired) {
    panel.addEventListener('input', refreshSettingsDirty);
    panel.addEventListener('change', refreshSettingsDirty);
    panel.dataset.wired = '1';
  }
  refreshSettingsDirty();
}

/** 入力欄の値を、API へ送る形へ整える。{changed, value} か {error} を返す。 */
function readSettingField(d) {
  if (d.ref === 'role' && d.kind === 'id_list') {
    const ids = readRolesByKey(d.key);
    const before = (d.value || []).map(String);
    const changed = !sameIdSet(ids, before);
    return { changed, value: ids };
  }

  const el = document.querySelector(`[data-field="${d.key}"]`);
  if (!el) return { changed: false };

  if (d.kind === 'id_list') {
    const items = el.value.split(/[\s,]+/).filter((s) => s !== '');
    const before = (d.value || []).map(String);
    const changed = !sameIdSet(items, before);
    return { changed, value: items };
  }

  const text = el.value.trim();

  if (d.kind === 'secret') {
    /* 空欄は「変更しない」。空文字を送ると、現在の秘密が空で上書きされてしまう。 */
    return { changed: text !== '', value: text };
  }

  if (d.kind === 'int' || d.kind === 'number') {
    if (text === '') return { error: `${d.label}: 値を入力してください。` };
    if (d.kind === 'int' && !/^-?\d+$/.test(text)) return { error: `${d.label}: 整数を入力してください。` };
    const num = Number(text);
    if (!Number.isFinite(num)) return { error: `${d.label}: 数値を入力してください。` };
    const rangeError = checkRange(d, num);
    if (rangeError) return { error: rangeError };
    return { changed: num !== Number(d.value), value: num };
  }

  return { changed: text !== String(d.value === null || d.value === undefined ? '' : d.value), value: text };
}

/** 値域の検査。サーバーも同じ検査をするが、保存を押す前に同じ文言で弾いておけば、送って 400 が返るのを待たずに直せる。 */
function checkRange(d, num) {
  if (d.minimum != null) {
    if (d.exclusive_minimum && num <= d.minimum) return `${d.label}: ${d.minimum} より大きい値にしてください。`;
    if (!d.exclusive_minimum && num < d.minimum) return `${d.label}: ${d.minimum} 以上にしてください。`;
  }
  if (d.maximum != null && num > d.maximum) return `${d.label}: ${d.maximum} 以下にしてください。`;
  return null;
}

/** ID の集合が(順序に依らず)等しいか。ロール選択は並びが登録簿の順と異なりうるため、順序で比べると偽の「変更あり」になる。 */
function sameIdSet(a, b) {
  if (a.length !== b.length) return false;
  const set = new Set(a);
  return b.every((x) => set.has(x));
}

/** 変更された欄に印を付け、未保存の有無と件数を更新する。 */
function refreshSettingsDirty() {
  let changed = 0;
  for (const d of defs) {
    if (!d.editable) continue;
    const read = readSettingField(d);
    const isChanged = !read.error && read.changed;
    if (isChanged) changed++;
    const row = document.querySelector(`[data-row="${d.key}"]`);
    if (row) {
      row.classList.toggle('changed', isChanged);
      const control = row.querySelector(`[data-field="${d.key}"]`);
      if (control && control.classList) control.classList.toggle('field-changed', isChanged);
    }
  }
  settingsDirty = changed > 0;
  const counter = document.querySelector('[data-dirty-count]');
  if (counter) counter.textContent = changed > 0 ? `${changed} 件の変更が未保存です` : '';
}

/** 設定タブの保存の結果や入力の誤りを出す。 */
function showSettingsStatus(html) {
  const box = document.querySelector('[data-settings-status]');
  if (!box) return;
  box.innerHTML = html;
  box.hidden = false;
}

/** 変更された欄だけを集めて送る。 */
async function saveSettings(btn) {
  const updates = {};
  const errors = [];
  for (const d of defs) {
    if (!d.editable) continue;
    const read = readSettingField(d);
    if (read.error) {
      errors.push(read.error);
      continue;
    }
    if (!read.changed) continue;
    updates[d.key] = read.value;
  }

  if (errors.length > 0) {
    showSettingsStatus(warnHtml(`<b>入力を確認してください。</b>${errors.map((m) => escapeHtml(m)).join(' / ')}`));
    return;
  }
  const keys = Object.keys(updates);
  if (keys.length === 0) {
    showSettingsStatus(infoHtml('変更された欄がありません。'));
    return;
  }

  btn.disabled = true;
  let data;
  try {
    data = await patch('/api/admin/settings', { updates });
  } catch (e) {
    if (e instanceof ApiError && e.status === 401) {
      location.href = loginUrl();
      return;
    }
    /* サーバーは 1 つでも不正なら何も書かないため、入力はそのまま残す。値を捨てて読み直すと、直せば通る入力まで消えてしまう。 */
    showSettingsStatus(warnHtml(`<b>設定を保存できませんでした。</b>${escapeHtml(errorMessage(e))}`));
    return;
  } finally {
    btn.disabled = false;
  }

  const updatedKeys = listOf(data, 'updated_keys');
  const restartLabels = updatedKeys
    .map((key) => defs.find((d) => d.key === key))
    .filter((d) => d && d.restart_required)
    .map((d) => d.label);

  /* 応答は更新後の一覧そのものであるため、読み直さずにそのまま映す。 */
  defs = listOf(data, 'settings');
  settingsDirty = false;
  renderSettingsPanel();

  const done = [`${updatedKeys.length} 件の設定を変更しました。`];
  if (restartLabels.length > 0) done.push(`次の設定は、対象のプロセスを再起動するまで反映されません: ${restartLabels.join('・')}。`);
  showSettingsStatus(infoHtml(escapeHtml(done.join(' '))));
}

/* ---------------------------------------------------------------- 分類タブ(カテゴリ・タグ・報道社) */

/** 入力欄の断片。data-f 属性で読み取り側と結びつける。 */
function textInput(name, value, opts = {}) {
  const attr = opts.numeric ? ' inputmode="numeric"' : '';
  return `<input type="text" data-f="${name}"${attr} value="${escapeHtml(value == null ? '' : value)}"${opts.placeholder ? ` placeholder="${escapeHtml(opts.placeholder)}"` : ''}>`;
}

function textArea(name, value, placeholder) {
  return `<textarea data-f="${name}" rows="2" placeholder="${escapeHtml(placeholder || '')}">${escapeHtml(value == null ? '' : value)}</textarea>`;
}

function numberInput(name, value) {
  return `<input type="number" step="1" data-f="${name}" value="${escapeHtml(value == null ? '' : value)}">`;
}

function checkbox(name, checked) {
  return `<input type="checkbox" data-f="${name}"${checked ? ' checked' : ''} style="width:auto">`;
}

function fieldRow(label, control, hint) {
  return `<div class="form-row"><label class="lbl">${escapeHtml(label)}</label>${control}${hint ? `<div class="hint">${escapeHtml(hint)}</div>` : ''}</div>`;
}

function readText(container, name) {
  const el = container.querySelector(`[data-f="${name}"]`);
  const v = el ? el.value.trim() : '';
  return v === '' ? null : v;
}

function readNumber(container, name, fallback) {
  const el = container.querySelector(`[data-f="${name}"]`);
  const v = el ? el.value.trim() : '';
  if (v === '') return fallback;
  const n = Number(v);
  return Number.isFinite(n) ? n : fallback;
}

function readCheckbox(container, name) {
  const el = container.querySelector(`[data-f="${name}"]`);
  return Boolean(el && el.checked);
}

/** 通知先チャンネルの欄。チャンネルは bot が取り込んでおらず選択肢を示せないため、ID の直接入力に注記を添える。 */
function channelHint(kind) {
  return `Discord のチャンネル ID を貼り付けます(未設定ならこの${kind}は通知しません)。開発者モードを有効にし、チャンネルを右クリックして「ID をコピー」で得られます。取り違えても bot のログに出るだけで、ここには誤りが現れません。`;
}

/** 3 つの分類の差分。フィールドの描画・読み取り・一覧の要約・作成に要る項目名を持つ。共有できる足場(一覧・削除・行の開閉)は下の関数が担う。 */
const ENTITIES = {
  categories: {
    label: 'カテゴリ',
    path: '/api/admin/categories',
    required: ['slug', 'name'],
    listNote: 'カテゴリは記事の主分類(単一必須)です。カテゴリが 1 つも無いと記事を作れません。通知先チャンネルを持つカテゴリは、その欄の記事をそのチャンネルへ流します。',
    fields(row) {
      return [
        fieldRow('slug', textInput('slug', row && row.slug, { placeholder: 'seiji' }), 'URL に使う識別子。空白やパスの区切りに化ける文字は使えません。'),
        fieldRow('表示名', textInput('name', row && row.name, { placeholder: '政治' })),
        fieldRow('並び順', numberInput('position', row ? row.position : 0), 'ナビゲーションでの並び。小さいほど先に出ます。'),
        fieldRow('通知先チャンネル ID', textInput('notify_channel_id', row && row.notify_channel_id, { numeric: true }), channelHint('欄')),
        fieldRow('トップの新着に載せる', checkbox('include_in_latest', row ? row.include_in_latest : true), '外すと、相場のような定期記事の欄がトップの新着一覧を埋めなくなります。その欄のページには出ます。'),
      ].join('');
    },
    read(container) {
      return {
        slug: readText(container, 'slug'),
        name: readText(container, 'name'),
        position: readNumber(container, 'position', 0),
        notify_channel_id: readText(container, 'notify_channel_id'),
        include_in_latest: readCheckbox(container, 'include_in_latest'),
      };
    },
    summary(row) {
      const parts = [`並び ${row.position}`];
      parts.push(row.notify_channel_id ? `通知先 ${row.notify_channel_id}` : '通知なし');
      parts.push(row.include_in_latest ? '新着に掲載' : '新着から除外');
      return parts.join(' ・ ');
    },
  },

  tags: {
    label: 'タグ',
    path: '/api/admin/tags',
    required: ['slug', 'name'],
    listNote: 'タグは主分類以外の分類軸です。記事は複数のタグを持てます。通知先チャンネルを持つタグは、カテゴリが決める宛先に加えて追加の宛先になります。',
    fields(row) {
      return [
        fieldRow('slug', textInput('slug', row && row.slug, { placeholder: 'election' }), 'URL に使う識別子。'),
        fieldRow('表示名', textInput('name', row && row.name, { placeholder: '選挙' })),
        fieldRow('説明', textArea('description', row && row.description, 'タグページに出す説明文(任意)。'), ''),
        fieldRow('アイコン URL', textInput('icon_url', row && row.icon_url), 'タグページ・一覧に出すアイコン(任意)。'),
        fieldRow('通知先チャンネル ID', textInput('notify_channel_id', row && row.notify_channel_id, { numeric: true }), channelHint('タグ')),
      ].join('');
    },
    read(container) {
      return {
        slug: readText(container, 'slug'),
        name: readText(container, 'name'),
        description: readText(container, 'description'),
        icon_url: readText(container, 'icon_url'),
        notify_channel_id: readText(container, 'notify_channel_id'),
      };
    },
    summary(row) {
      return row.notify_channel_id ? `通知先 ${row.notify_channel_id}` : '通知なし(購読者への DM は届く)';
    },
    icon(row) {
      return tagIconHtml(row);
    },
  },

  publishers: {
    label: '報道社',
    path: '/api/admin/publishers',
    required: ['slug', 'name'],
    listNote: '報道社は記事の署名です。所属は Discord のロールで決まります。記事のある報道社は削除できません。解散を表すには、削除ではなく所属ロールを「ロールなし」にします(過去記事の署名が残ります)。',
    fields(row) {
      const stance = row && row.stance === 'state' ? 'state' : 'independent';
      const stanceSelect =
        '<select data-f="stance">' +
        `<option value="independent"${stance === 'independent' ? ' selected' : ''}>独立系</option>` +
        `<option value="state"${stance === 'state' ? ' selected' : ''}>国営(記事に「国営」ラベルを添える)</option>` +
        '</select>';
      return [
        fieldRow('slug', textInput('slug', row && row.slug, { placeholder: 'daily-star' }), 'URL に使う識別子。'),
        fieldRow('表示名', textInput('name', row && row.name, { placeholder: 'デイリースター' })),
        fieldRow('所属ロール', `<div class="rolepick single">${roleSelectSingle(row && row.role_id)}</div>`, '記者がこの報道社を名乗れるかどうかを決める Discord ロール。1 つのロールを 2 つの報道社の所属にはできません。ロールなしは「解散」を表します。'),
        fieldRow('立場', stanceSelect, '国営メディア等は記事面に「国営」ラベルが添えられます。'),
        fieldRow('説明', textArea('description', row && row.description, '報道社のプロフィール(任意)。'), ''),
        fieldRow('ロゴ URL', textInput('logo_url', row && row.logo_url), '報道社のロゴ(任意)。'),
      ].join('');
    },
    read(container) {
      const roleEl = container.querySelector('[data-f="role_id"]');
      const stanceEl = container.querySelector('[data-f="stance"]');
      return {
        slug: readText(container, 'slug'),
        name: readText(container, 'name'),
        role_id: roleEl && roleEl.value ? roleEl.value : null,
        stance: stanceEl ? stanceEl.value : 'independent',
        description: readText(container, 'description'),
        logo_url: readText(container, 'logo_url'),
      };
    },
    summary(row) {
      const parts = [row.role_id ? `所属ロール ${row.role_id}` : '解散(ロールなし)'];
      if (row.stance === 'state') parts.push('国営');
      return parts.join(' ・ ');
    },
    icon(row) {
      return pubLogoHtml(row);
    },
  },
};

/** その分類パネルの状態を出す。 */
function showEntityStatus(name, html) {
  const box = document.querySelector(`[data-panel="${name}"] [data-status]`);
  if (!box) return;
  box.innerHTML = html;
  box.hidden = false;
}

/** 分類の一覧の 1 行。名前と slug、要約、編集と削除。編集を押すと同じ行の下に編集域が開く。 */
function entityRowHtml(name, row) {
  const spec = ENTITIES[name];
  const icon = spec.icon ? spec.icon(row) : '';
  return `<tr data-id="${escapeHtml(row.id)}">` +
    `<td><div class="tax-name">${icon}<b>${escapeHtml(row.name)}</b></div><div class="tax-slug">${escapeHtml(row.slug)}</div></td>` +
    `<td class="s-count">${escapeHtml(spec.summary(row))}</td>` +
    `<td><button class="btn" type="button" data-edit>編集</button> <button class="btn danger" type="button" data-del data-name="${escapeHtml(row.name)}">削除</button></td>` +
    '</tr>' +
    `<tr class="tax-editrow" data-editrow hidden><td colspan="3"></td></tr>`;
}

/** 分類パネルを描く。新規フォーム・一覧・状態表示を持つ。 */
function renderEntityPanel(name, rows) {
  const spec = ENTITIES[name];
  const panel = document.querySelector(`[data-panel="${name}"]`);
  const list = rows.length === 0
    ? '<div class="body">まだありません。上のフォームから作成してください。</div>'
    : `<table class="keys"><thead><tr><th>${escapeHtml(spec.label)}</th><th>設定</th><th></th></tr></thead>` +
      `<tbody data-list>${rows.map((r) => entityRowHtml(name, r)).join('')}</tbody></table>`;

  panel.innerHTML =
    `<section class="card"><div class="section-head"><h2>${escapeHtml(spec.label)}を追加</h2></div>` +
    `<div data-new>${spec.fields(null)}</div>` +
    '<div class="form-actions"><button class="btn primary" type="button" data-create>作成する</button><span class="spacer"></span></div></section>' +
    '<section class="card"><div class="body" data-status hidden></div>' +
    `<div class="section-head"><h2>${escapeHtml(spec.label)}の一覧</h2></div>` +
    `<div class="hint" style="padding:0 16px 8px;">${escapeHtml(spec.listNote)}</div>${list}</section>`;

  panel.querySelector('[data-create]').addEventListener('click', (ev) => createEntity(name, ev.currentTarget));
  const body = panel.querySelector('[data-list]');
  if (body) body.addEventListener('click', (ev) => onEntityListClick(name, ev));
}

/** 一覧内の編集・削除・保存・取消のクリックを受ける。行は描き直されるため、入れ物の側で受ける。 */
function onEntityListClick(name, ev) {
  const editBtn = ev.target.closest('[data-edit]');
  if (editBtn) return toggleEntityEditor(name, editBtn.closest('tr'));
  const delBtn = ev.target.closest('[data-del]');
  if (delBtn) return deleteEntity(name, delBtn.closest('tr').dataset.id, delBtn.dataset.name);
  const saveBtn = ev.target.closest('[data-save-edit]');
  if (saveBtn) return saveEntityEdit(name, saveBtn);
  const cancelBtn = ev.target.closest('[data-cancel-edit]');
  if (cancelBtn) {
    const editRow = cancelBtn.closest('[data-editrow]');
    editRow.hidden = true;
    editRow.querySelector('td').innerHTML = '';
  }
}

/** 行の編集域を開く/閉じる。開くときに現在値でフォームを組む。 */
function toggleEntityEditor(name, tr) {
  const spec = ENTITIES[name];
  const editRow = tr.nextElementSibling;
  if (!editRow || !editRow.matches('[data-editrow]')) return;
  if (!editRow.hidden) {
    editRow.hidden = true;
    editRow.querySelector('td').innerHTML = '';
    return;
  }
  const row = (entityCache[name] || []).find((r) => String(r.id) === String(tr.dataset.id));
  editRow.querySelector('td').innerHTML =
    `<div class="tax-edit" data-edit-form>${spec.fields(row || null)}` +
    '<div class="form-actions"><button class="btn primary" type="button" data-save-edit>保存</button>' +
    '<button class="btn" type="button" data-cancel-edit>取消</button><span class="spacer"></span></div></div>';
  editRow.hidden = false;
}

/** 作成に必須の項目が入っているかを画面側でも確かめる。サーバーも確かめるが、往復の前に気付ける。 */
function missingRequired(spec, payload) {
  return spec.required.filter((key) => payload[key] == null || payload[key] === '');
}

/** 分類を作成する。 */
async function createEntity(name, btn) {
  const spec = ENTITIES[name];
  const container = document.querySelector(`[data-panel="${name}"] [data-new]`);
  const payload = spec.read(container);
  const missing = missingRequired(spec, payload);
  if (missing.length) {
    showEntityStatus(name, warnHtml(`<b>作成できません。</b>次の項目を入力してください: ${escapeHtml(missing.join(', '))}`));
    return;
  }
  btn.disabled = true;
  try {
    await post(spec.path, payload);
  } catch (e) {
    if (e instanceof ApiError && e.status === 401) {
      location.href = loginUrl();
      return;
    }
    showEntityStatus(name, warnHtml(`<b>${escapeHtml(spec.label)}を作成できませんでした。</b>${escapeHtml(errorMessage(e))}`));
    return;
  } finally {
    btn.disabled = false;
  }
  await reloadEntity(name);
  showEntityStatus(name, infoHtml(`${escapeHtml(spec.label)}を作成しました。`));
}

/** 行の編集を保存する。 */
async function saveEntityEdit(name, btn) {
  const spec = ENTITIES[name];
  const tr = btn.closest('[data-editrow]').previousElementSibling;
  const container = btn.closest('[data-edit-form]');
  const payload = spec.read(container);
  const missing = missingRequired(spec, payload);
  if (missing.length) {
    showEntityStatus(name, warnHtml(`<b>保存できません。</b>次の項目を入力してください: ${escapeHtml(missing.join(', '))}`));
    return;
  }
  btn.disabled = true;
  try {
    await patch(`${spec.path}/${encodeURIComponent(tr.dataset.id)}`, payload);
  } catch (e) {
    if (e instanceof ApiError && e.status === 401) {
      location.href = loginUrl();
      return;
    }
    showEntityStatus(name, warnHtml(`<b>${escapeHtml(spec.label)}を保存できませんでした。</b>${escapeHtml(errorMessage(e))}`));
    return;
  } finally {
    btn.disabled = false;
  }
  await reloadEntity(name);
  showEntityStatus(name, infoHtml(`${escapeHtml(spec.label)}を更新しました。`));
}

/** 分類を削除する。記事が参照している場合はサーバーが 409 で理由(件数)を返すため、それをそのまま出す。 */
async function deleteEntity(name, id, label) {
  const spec = ENTITIES[name];
  if (!confirm(`${spec.label}「${label}」を削除します。よろしいですか?`)) return;
  try {
    await del(`${spec.path}/${encodeURIComponent(id)}`);
  } catch (e) {
    if (e instanceof ApiError && e.status === 401) {
      location.href = loginUrl();
      return;
    }
    showEntityStatus(name, warnHtml(`<b>${escapeHtml(spec.label)}を削除できませんでした。</b>${escapeHtml(errorMessage(e))}`));
    return;
  }
  await reloadEntity(name);
  showEntityStatus(name, infoHtml(`${escapeHtml(spec.label)}を削除しました。`));
}

/** 分類の一覧を読み直して描き直す。 */
async function reloadEntity(name) {
  const spec = ENTITIES[name];
  const rows = listOf(await get(spec.path), name);
  entityCache[name] = rows;
  renderEntityPanel(name, rows);
}

/* ---------------------------------------------------------------- タブの切り替え */

/** タブの現在地を hash から決める。未知・空なら設定。 */
function currentTab() {
  const hash = (location.hash || '').replace(/^#/, '');
  return TABS.some((t) => t.id === hash) ? hash : 'settings';
}

/** タブを表示する。分類タブは初めて開いたときに読み込む。設定タブの入力は消さない(タブの切り替えでは描き直さず、hidden の付け外しだけを行う)。 */
async function showTab(name) {
  for (const link of document.querySelectorAll('[data-tab]')) {
    link.classList.toggle('active', link.dataset.tab === name);
  }
  for (const panel of document.querySelectorAll('[data-panel]')) {
    panel.hidden = panel.dataset.panel !== name;
  }
  if (name !== 'settings' && !loaded[name]) {
    loaded[name] = true;
    try {
      await reloadEntity(name);
    } catch (e) {
      loaded[name] = false;
      if (e instanceof ApiError && e.status === 401) {
        location.href = loginUrl();
        return;
      }
      document.querySelector(`[data-panel="${name}"]`).innerHTML =
        `<section class="card"><div class="body">${warnHtml(`${escapeHtml(ENTITIES[name].label)}の一覧を読み込めませんでした。${escapeHtml(errorMessage(e))}`)}</div></section>`;
    }
  }
}

/* ---------------------------------------------------------------- 画面の骨格 */

function renderTabs() {
  const bar = TABS.map((t) => `<a href="#${t.id}" data-tab="${t.id}">${escapeHtml(t.label)}</a>`).join('');
  const panels = TABS.map((t) => `<div data-panel="${t.id}" hidden></div>`).join('');
  document.querySelector('[data-main]').innerHTML = `<nav class="tabs">${bar}</nav>${panels}`;
}

/** ログインしていない利用者に、この画面が何のためにあるかを示してログインへ送る。 */
function renderLoginRequired() {
  renderMessage('ログインが必要です',
    '<p>プラットフォームの設定の確認と変更には、Discord でのログインが必要です。</p>' +
    `<a class="btn-discord" href="${escapeHtml(loginUrl())}">🎮 Discord でログイン</a>`);
}

/** 管理者でない利用者への説明。権限が無いこと自体は隠さない(この画面の存在は秘密ではなく、秘密なのは設定の値の方であり、それは API が渡さない)。 */
function renderForbidden() {
  renderMessage('この画面は管理者だけが開けます',
    '<p style="font-size:13px;">プラットフォームの設定と分類を読み書きできるのは管理者だけです。あなたの現在の資格ではこの画面の内容を取得できません。</p>' +
    '<p style="font-size:13px;">管理者として扱われるのは、guild のオーナー、Discord の Administrator 権限を持つロールの保有者、そして管理ロール(admin.role_ids)に指定されたロールの保有者です。権限は要求のたびに現在のロールから判定されます。</p>' +
    '<p style="font-size:13px;"><a href="/settings">設定・API キー</a>では、自分のアカウントと API キーを扱えます。</p>');
}

async function main() {
  try {
    viewer = await me();
  } catch (e) {
    viewer = null;
  }
  mountChrome(viewer);
  renderAdSlot(document.querySelector('[data-adslot]'), 'sidebar');

  if (!viewer) {
    renderLoginRequired();
    return;
  }

  /* 設定とロールを揃えてから設定タブを組む。設定にはロールの複数選択があるため、ロールが先に要る。ロールだけ引けない場合は ID 直接入力へ落とすため、失敗しても設定は出す。 */
  let settings;
  try {
    [settings, roles] = await Promise.all([
      get('/api/admin/settings'),
      get('/api/admin/roles').then((r) => listOf(r, 'roles')).catch(() => null),
    ]);
  } catch (e) {
    if (e instanceof ApiError && e.status === 401) {
      renderLoginRequired();
      return;
    }
    if (e instanceof ApiError && e.status === 403) {
      renderForbidden();
      return;
    }
    renderMessage('設定を読み込めませんでした', `<p style="font-size:13px;">${escapeHtml(errorMessage(e))}</p>`);
    return;
  }

  defs = listOf(settings, 'settings');
  renderTabs();
  renderSettingsPanel();
  window.addEventListener('hashchange', () => showTab(currentTab()));
  await showTab(currentTab());

  /* 設定タブに未保存の変更があるまま画面を離れようとしたら引き止める。分類タブは行ごとに即座に確定するため、この対象にならない。 */
  window.addEventListener('beforeunload', (ev) => {
    if (settingsDirty) {
      ev.preventDefault();
      ev.returnValue = '';
    }
  });
}

main();
