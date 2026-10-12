/*
 * Drives the admin screen (/admin): the platform settings and the taxonomy (categories, tags, publishers) on one screen.
 *
 * Two kinds of thing are edited here: settings held in the settings table (GET/PATCH /api/admin/settings), and taxonomy, which has tables of its own (/api/admin/{categories,tags,publishers}).
 * Settings are saved in one batch because the job is "survey the whole thing and fix a few of them"; taxonomy commits immediately per row because the job is "add / edit / delete one". That difference is exactly the difference between the tabs.
 *
 * Settings that name roles (admin.role_ids, the breaking-news roles) and a publisher's affiliation role are picked by name and colour out of the roles the bot imported (GET /api/admin/roles) rather than typed in as IDs. Picking rather than typing is worth most on admin.role_ids, where a paste error is itself a lockout from this very screen.
 * Direct ID entry stays available alongside for roles the import has not caught up with: the bot mirrors a role when it receives the role-create event, so a freshly created role can still be missing from the listing.
 *
 * The server's registry owns every setting's key, type, range and reference target, and this screen hardcodes no setting key at all. Ranges come from the response's minimum / maximum / exclusive_minimum, and whether a role picker is needed comes from ref.
 * Copying any of that into the UI would drift from the registry, and the drift would surface as "you only learn the maximum by pressing Save and getting a 400".
 */

import { get, post, patch, del, me, escapeHtml, formatDateTime, renderAdSlot, ApiError } from '/static/js/api.js';
import { mountChrome, listOf, errorMessage, loginUrl, tagIconHtml, pubLogoHtml } from '/static/js/chrome.js';

/* State */

let viewer = null;
/* The per-setting descriptors returned by GET /api/admin/settings. Also the baseline for deciding what changed on save, so it is swapped out every time the screen is redrawn. */
let defs = [];
/* The result of GET /api/admin/roles, shared by the settings role pickers and by a publisher's affiliation role. Left null when the fetch fails, and the role fields then fall back to direct ID entry. */
let roles = null;
/* Taxonomy is read the first time its tab is opened. Unlike the settings, it is not needed until then. */
const loaded = { categories: false, tags: false, publishers: false };
/* The rows from the last taxonomy listing read, kept so that opening a row's editor can pull the current values. */
const entityCache = { categories: [], tags: [], publishers: [] };
/* Whether the settings tab has unsaved changes. The taxonomy tabs commit immediately per row, so only the settings tab carries unsaved state. */
let settingsDirty = false;

const TABS = [
  { id: 'settings', label: '設定' },
  { id: 'categories', label: 'カテゴリ' },
  { id: 'tags', label: 'タグ' },
  { id: 'publishers', label: '報道社' },
];

/* The headings that group the settings, split on the first segment of the key. A prefix missing from this map is used verbatim as the heading, so a new family of keys in the registry can never make the fields themselves disappear just because nobody added a heading. */
const GROUP_LABELS = {
  discord: 'Discord',
  admin: 'プラットフォーム管理',
  articles: '記事',
  media: '画像',
};

/* Notices attached to particular settings only. The registry owns the value's description; what goes here is the operational warning of what can happen if you touch this field. */
const EXTRA_NOTICES = {
  'admin.role_ids': '<div class="notice"><span class="ic">⚠</span><div>' +
    '<b>この欄の指定を誤ると、あなた自身がこの画面から締め出されます。</b>' +
    'Discord の Administrator 権限を持つロールの保有者と guild のオーナーは、この設定に関わらず管理者として扱われますが、どちらにも当てはまらない場合、ここから自分のロールを外した時点で管理操作を行えなくなります。' +
    'その状態はこの画面からは直せません。復旧は、DB へ到達できる者が CLI から <code>python -m server.cli set-setting admin.role_ids \'["ロール ID"]\'</code> を実行する経路だけになります。' +
    '</div></div>',
};

/* Small helpers */

function warnHtml(html) {
  return `<div class="notice"><span class="ic">⚠</span><div>${html}</div></div>`;
}

function infoHtml(html) {
  return `<div class="notice info"><span class="ic">ℹ</span><div>${html}</div></div>`;
}

/** Replace the panel body with a single card of text. For states where leaving the input fields up is pointless: the load failed, there is no permission, and so on. */
function renderMessage(title, bodyHtml) {
  document.querySelector('[data-main]').innerHTML =
    `<section class="card"><div class="section-head"><h2>${escapeHtml(title)}</h2></div><div class="body">${bodyHtml}</div></section>`;
}

/** Turn a role colour (24-bit) into a CSS colour. No colour (0) is the default colour on Discord too, so the swatch is left transparent and only its faint box tells it apart. */
function roleSwatch(color) {
  const hex = color ? '#' + Number(color).toString(16).padStart(6, '0') : 'transparent';
  return `<span class="role-swatch" style="background:${escapeHtml(hex)}"></span>`;
}

/* Role pickers */

/**
 * Multi-select for roles (for id_list settings with ref=role). Lists the roles the bot imported by name and colour.
 * A selected ID that is absent from the listing (deleted, or not imported yet) is kept checked and marked as not fetched: dropping it would delete it on save even though nobody touched it in the UI.
 * If the listing itself could not be fetched, falls back to direct ID entry.
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

/** Read the selected role IDs (multi). Combines the checked roles with directly entered IDs; duplicates are collapsed. */
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
 * Single select for a role (for a publisher's role_id: zero or one). The API rejects an affiliation role that does not already exist, so there is no room to assign a role that is absent from the listing and the options (the imported roles) are enough.
 * The current value is kept as an option too, in case it has vanished from the listing (the role was deleted).
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

/* Settings tab */

/** Express an int / number range as input attributes. An exclusive minimum on an int is "greater than", which +1 turns into min. An exclusive minimum on a number cannot be expressed as min, so no attribute is emitted and the value is rejected when read instead. */
function numberAttrs(d) {
  const attrs = [d.kind === 'int' ? 'step="1"' : 'step="any"'];
  if (d.minimum != null) {
    const min = (d.exclusive_minimum && d.kind === 'int') ? d.minimum + 1 : d.minimum;
    if (!(d.exclusive_minimum && d.kind === 'number')) attrs.push(`min="${escapeHtml(min)}"`);
  }
  if (d.maximum != null) attrs.push(`max="${escapeHtml(d.maximum)}"`);
  return attrs.join(' ');
}

/** The range put into words. Stated as prose on top of the input attributes, because some ranges cannot be expressed as attributes at all, such as an exclusive minimum on a number. */
function rangeHint(d) {
  if (d.kind !== 'int' && d.kind !== 'number') return '';
  const parts = [];
  if (d.minimum != null) parts.push(d.exclusive_minimum ? `${d.minimum} より大きい` : `${d.minimum} 以上`);
  if (d.maximum != null) parts.push(`${d.maximum} 以下`);
  return parts.length ? `値域: ${parts.join('、')}。` : '';
}

/** The display string for rendering a not-editable setting read-only. */
function readonlyText(d) {
  if (d.kind === 'id_list') return (d.value || []).map(String).join(', ');
  return d.value === null || d.value === undefined ? '' : String(d.value);
}

/**
 * The input control. The registry defines the kind and the server does the validation; picking a control per kind here is for ease of input, not a substitute for validation.
 * Handling IDs (id / id_list) as text is a requirement: a number input would route the value through JavaScript's Number, and a snowflake beyond 2^53 silently has its low digits corrupted.
 */
function controlHtml(d, id) {
  const field = `id="${id}" data-field="${escapeHtml(d.key)}"`;
  if (!d.editable) {
    return `<input type="text" ${field} value="${escapeHtml(readonlyText(d))}" readonly>`;
  }
  if (d.kind === 'secret') {
    /* The current value is not in the response: returning it would hand the bot token to everyone who can open this screen. Blank means "leave unchanged". */
    return `<input type="password" ${field} autocomplete="new-password" placeholder="変更するときだけ新しい値を入力">`;
  }
  if (d.kind === 'int' || d.kind === 'number') {
    return `<input type="number" ${numberAttrs(d)} ${field} value="${escapeHtml(d.value)}">`;
  }
  if (d.ref === 'role' && d.kind === 'id_list') {
    /* Role multi-select. data-field goes on the container, and readRolesByKey does the reading. */
    return `<div ${field}>${rolePickerMulti(d.key, d.value)}</div>`;
  }
  if (d.kind === 'id_list') {
    return `<textarea ${field} rows="3" placeholder="ID をカンマ区切り、または 1 行に 1 件。空にすると「指定なし」になります。">${escapeHtml((d.value || []).map(String).join('\n'))}</textarea>`;
  }
  const numeric = d.kind === 'id' ? ' inputmode="numeric"' : '';
  return `<input type="text" ${field}${numeric} value="${escapeHtml(d.value)}">`;
}

/** The badges showing a setting's state. */
function badgesHtml(d) {
  const out = [];
  if (!d.editable) out.push('<span class="badge vis-members">変更不可</span>');
  if (d.restart_required) out.push('<span class="badge vis-restricted">再起動が必要</span>');
  if (d.secret) out.push(d.is_set ? '<span class="badge vis-public">設定済み</span>' : '<span class="badge vis-restricted">未設定</span>');
  return out.join(' ');
}

/** The last-updated line. Who changed it is recorded because changing a setting is an act that may have to be accounted for after the fact. */
function metaText(d) {
  const at = formatDateTime(d.updated_at);
  const parts = [at ? `最終更新 ${at}` : '最終更新の記録はありません'];
  parts.push(d.updated_by ? `変更した利用者の ID: ${d.updated_by}` : '画面からの変更の記録はありません');
  return parts.join(' ・ ');
}

/** One setting's row. */
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

/** Group the settings by the first segment of the key. The order of the response (the registry's order) is preserved as-is. */
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

/** Render the settings tab. */
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
  // What changed is re-checked on every input. Both input and change are listened to so that a role checkbox and direct text entry are picked up by the same path. The delegation is attached once to the panel element, which is never re-created, so listeners do not stack up across saves.
  if (!panel.dataset.wired) {
    panel.addEventListener('input', refreshSettingsDirty);
    panel.addEventListener('change', refreshSettingsDirty);
    panel.dataset.wired = '1';
  }
  refreshSettingsDirty();
}

/** Shape a field's value into the form sent to the API. Returns {changed, value} or {error}. */
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
    /* Blank means "leave unchanged": sending an empty string would overwrite the current secret with nothing. */
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

/** The range check. The server runs the same check, but rejecting with the same wording before Save is pressed lets it be fixed without waiting for a 400 to come back. */
function checkRange(d, num) {
  if (d.minimum != null) {
    if (d.exclusive_minimum && num <= d.minimum) return `${d.label}: ${d.minimum} より大きい値にしてください。`;
    if (!d.exclusive_minimum && num < d.minimum) return `${d.label}: ${d.minimum} 以上にしてください。`;
  }
  if (d.maximum != null && num > d.maximum) return `${d.label}: ${d.maximum} 以下にしてください。`;
  return null;
}

/** Whether two sets of IDs are equal, regardless of order. A role picker's order can differ from the registry's, so comparing in order would report a change that is not there. */
function sameIdSet(a, b) {
  if (a.length !== b.length) return false;
  const set = new Set(a);
  return b.every((x) => set.has(x));
}

/** Mark the changed fields and update the unsaved flag and count. */
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

/** Show the settings tab's save result or input errors. */
function showSettingsStatus(html) {
  const box = document.querySelector('[data-settings-status]');
  if (!box) return;
  box.innerHTML = html;
  box.hidden = false;
}

/** Collect only the changed fields and send them. */
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
    /* The server writes nothing if even one value is invalid, so the input is left as it is. Throwing the values away and re-reading would wipe out entries that would have gone through once fixed. */
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

  /* The response is the updated listing itself, so it is shown as-is without re-reading. */
  defs = listOf(data);
  settingsDirty = false;
  renderSettingsPanel();

  const done = [`${updatedKeys.length} 件の設定を変更しました。`];
  if (restartLabels.length > 0) done.push(`次の設定は、対象のプロセスを再起動するまで反映されません: ${restartLabels.join('・')}。`);
  showSettingsStatus(infoHtml(escapeHtml(done.join(' '))));
}

/* Taxonomy tabs (categories, tags, publishers) */

/** An input-field snippet, tied to the reading side by its data-f attribute. */
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

/** The notify channel field. The bot does not import channels and no options can be offered, so direct ID entry gets a note instead. */
function channelHint(kind) {
  return `Discord のチャンネル ID を貼り付けます(未設定ならこの${kind}は通知しません)。開発者モードを有効にし、チャンネルを右クリックして「ID をコピー」で得られます。取り違えても bot のログに出るだけで、ここには誤りが現れません。`;
}

/** Where the three taxonomy kinds differ: rendering and reading the fields, the listing summary, and which fields creation requires. The scaffolding they can share (listing, delete, opening and closing a row) is handled by the functions below. */
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

/** Show that taxonomy panel's status. */
function showEntityStatus(name, html) {
  const box = document.querySelector(`[data-panel="${name}"] [data-status]`);
  if (!box) return;
  box.innerHTML = html;
  box.hidden = false;
}

/** One row of a taxonomy listing: name and slug, the summary, edit and delete. Pressing edit opens an editor below that same row. */
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

/** Render a taxonomy panel: the create form, the listing and the status area. */
function renderEntityPanel(name, rows) {
  const spec = ENTITIES[name];
  const panel = document.querySelector(`[data-panel="${name}"]`);
  const list = rows.length === 0
    ? '<div class="body">まだありません。上のフォームから作成してください。</div>'
    : '<div class="table-scroll">' +
      `<table class="keys"><thead><tr><th>${escapeHtml(spec.label)}</th><th>設定</th><th></th></tr></thead>` +
      `<tbody data-list>${rows.map((r) => entityRowHtml(name, r)).join('')}</tbody></table></div>`;

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

/** Take the edit / delete / save / cancel clicks inside the listing. The rows get re-rendered, so the container takes them. */
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

/** Open or close a row's editor, building the form from the current values on open. */
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

/** Check on the client too that the fields creation requires are filled in. The server checks as well, but this catches it before the round trip. */
function missingRequired(spec, payload) {
  return spec.required.filter((key) => payload[key] == null || payload[key] === '');
}

/** Create a taxonomy entry. */
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

/** Save a row's edits. */
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

/** Delete a taxonomy entry. When articles reference it the server returns 409 with the reason (the count), which is shown as-is. */
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

/** Re-read a taxonomy listing and re-render it. */
async function reloadEntity(name) {
  const spec = ENTITIES[name];
  const rows = listOf(await get(spec.path), name);
  entityCache[name] = rows;
  renderEntityPanel(name, rows);
}

/* Tab switching */

/** Decide the active tab from the hash. Unknown or empty means settings. */
function currentTab() {
  const hash = (location.hash || '').replace(/^#/, '');
  return TABS.some((t) => t.id === hash) ? hash : 'settings';
}

/** Show a tab. A taxonomy tab loads the first time it is opened. The settings tab's input is never discarded: switching tabs does not re-render, it only adds and removes hidden. */
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

/* Screen skeleton */

function renderTabs() {
  const bar = TABS.map((t) => `<a href="#${t.id}" data-tab="${t.id}">${escapeHtml(t.label)}</a>`).join('');
  const panels = TABS.map((t) => `<div data-panel="${t.id}" hidden></div>`).join('');
  document.querySelector('[data-main]').innerHTML = `<nav class="tabs">${bar}</nav>${panels}`;
}

/** Tell a logged-out user what this screen is for and send them to log in. */
function renderLoginRequired() {
  renderMessage('ログインが必要です',
    '<p>プラットフォームの設定の確認と変更には、Discord でのログインが必要です。</p>' +
    `<a class="btn-discord" href="${escapeHtml(loginUrl())}">🎮 Discord でログイン</a>`);
}

/** The explanation for a non-admin user. Having no permission is not itself hidden: the existence of this screen is no secret; the setting values are, and the API does not hand those over. */
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

  /* Settings and roles are both in hand before the settings tab is built: the settings include role multi-selects, so the roles have to come first. If only the roles cannot be fetched the fields fall back to direct ID entry, so the settings are still rendered. */
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

  defs = listOf(settings);
  renderTabs();
  renderSettingsPanel();
  window.addEventListener('hashchange', () => showTab(currentTab()));
  await showTab(currentTab());

  /* Hold the user back if they try to leave with unsaved changes on the settings tab. The taxonomy tabs commit immediately per row, so they never fall under this. */
  window.addEventListener('beforeunload', (ev) => {
    if (settingsDirty) {
      ev.preventDefault();
      ev.returnValue = '';
    }
  });
}

main();
