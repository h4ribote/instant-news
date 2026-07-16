/*
 * instant-news の画面の共通部品。api.js が HTTP の層(fetch・JSON・ID の文字列化)を担うのに対し、
 * こちらは DOM の層を担う。ヘッダのアカウント欄・グローバルナビ・利用者やタグの見た目の断片など、
 * 複数のページが同じ見た目で描くものを 1 箇所に集める。
 *
 * ここに集約する前は、これらの関数が全ページへ写し取られていた。写し取られたものは食い違う。実際、利用者の
 * アバターを組み立てる avatarHtml は、admin / settings / tag / publisher では存在しない user.avatar_url を参照
 * していたために一度も実画像を出せず頭文字へ黙って落ちており、index / article / editor では avatar_hash から
 * URL を組み立てる正しい実装が、三者三様の書き方で並んでいた。同じものを 1 つ持てば、この種のずれは構造的に
 * 起こらない。
 *
 * api.js と同じく、ID は必ず文字列として扱う。数値化するとスノーフレークの下位桁が黙って壊れる。
 */

import { get, post, me, qs, escapeHtml, mediaVariant } from '/static/js/api.js';

// Discord がアバター未設定のアカウントへ割り当てる既定アバターの種類数。ハッシュを持たない利用者はこの剰余で絵が決まるが、その URL の組み立ては呼び出し側の関心ではないため、ここでは頭文字へ落とす(既定アバターは誰にとっても同じ数種類の絵であり、頭文字の方が誰であるかを表す)。

/** ログインへの導線。認証を終えたら今見ているページへ戻す。戻り先は現在のパスとクエリ(ハッシュはサーバーへ送られないため含めない)。 */
export function loginUrl() {
  return '/auth/login' + qs({ redirect_to: location.pathname + location.search });
}

/**
 * CSS の url() の値そのもの。URL は記事・利用者・タグに由来するため、JSON.stringify で CSS 文字列としての引用符とバックスラッシュを潰す。
 * **要素の style プロパティへ直接代入する場合(el.style.backgroundImage = ...)はこちらを使う。**HTML を経由しない代入に cssUrl を使うと、エスケープした &quot; がそのまま CSS の文字列へ入り、値ごと無効になって画像が出ない。
 */
export function cssUrlRaw(url) {
  return `url(${JSON.stringify(String(url))})`;
}

/**
 * style 属性へ HTML として差し込む url()。**innerHTML で組み立てる断片にはこちらを使う。**
 * JSON.stringify が CSS 文字列としての引用符とバックスラッシュを潰し、escapeHtml が属性値としての引用符を潰す。二段で囲わないと、引用符を含む URL から style 属性の外へ抜け出せてしまう。
 */
export function cssUrl(url) {
  return escapeHtml(cssUrlRaw(url));
}

/**
 * 文字列の ID から配色の連番(prefix1..prefixN)を決める。同じ相手には常に同じ色が付く。
 * ID は文字列のまま畳む(数値化するとスノーフレークの下位桁が壊れ、別人へ同じ色が付きうる)。
 */
export function paletteClass(prefix, count, key) {
  const s = String(key == null ? '' : key);
  let n = 0;
  for (let i = 0; i < s.length; i++) n = (n * 31 + s.charCodeAt(i)) % count;
  return prefix + (n + 1);
}

/** 一覧の応答から配列を取り出す。応答が配列そのものでも、ページ情報を伴うオブジェクト({items|<key>: [...]})でも受け取れるようにする。 */
export function listOf(data, key) {
  if (Array.isArray(data)) return data;
  if (data && Array.isArray(data.items)) return data.items;
  if (data && key && Array.isArray(data[key])) return data[key];
  return [];
}

/** 利用者の表示名。Discord の表示名があればそれを、無ければユーザー名を出す。 */
export function displayName(user) {
  if (!user) return '';
  return user.global_name || user.username || '';
}

/**
 * 利用者のアバター画像の URL。既定アバター(avatar_hash を持たない)の利用者には null を返し、呼び出し側が頭文字で代替する。
 * ハッシュが a_ で始まるものはアニメーション付きで拡張子が異なる。ID は文字列のまま URL へ入れる。
 */
export function avatarUrl(user) {
  if (!user || !user.avatar_hash || !user.discord_user_id) return null;
  const ext = String(user.avatar_hash).startsWith('a_') ? 'gif' : 'png';
  return `https://cdn.discordapp.com/avatars/${encodeURIComponent(user.discord_user_id)}/${encodeURIComponent(user.avatar_hash)}.${ext}?size=64`;
}

/**
 * 利用者のアバターの断片。画像があれば背景に、無ければ頭文字を出す。extraStyle を渡すと寸法などを重ねられる。
 * user は discord_user_id と avatar_hash(と表示名)を持つ形。id は色の決定に使うが、無ければ表示名から決める。
 */
export function avatarHtml(user, extraStyle = '') {
  const name = displayName(user);
  const url = avatarUrl(user);
  const extra = extraStyle ? escapeHtml(extraStyle) : '';
  if (url) {
    return `<span class="avatar ph" style="background-image:${cssUrl(url)};${extra}"></span>`;
  }
  const initial = name ? escapeHtml(Array.from(name)[0].toUpperCase()) : '?';
  const key = (user && (user.discord_user_id || user.id)) || name;
  return `<span class="avatar ${paletteClass('a', 5, key)}" style="${extra}">${initial}</span>`;
}

/**
 * 報道社のロゴ。未設定なら社名の頭文字を出す。
 * ロゴが出る最大の枠は報道社ページの見出しの 72px であり、400w で足りる。取り込みを経ていない外部ホストの URL(管理画面で手入力できる)は mediaVariant がそのまま通す。
 */
export function pubLogoHtml(pub) {
  if (!pub) return '';
  if (pub.logo_url) return `<span class="pub-logo ph" style="background-image:${cssUrl(mediaVariant(pub.logo_url, '400w'))}"></span>`;
  const initial = escapeHtml(Array.from(String(pub.name || '?'))[0]);
  return `<span class="pub-logo ${paletteClass('p', 4, pub.slug || pub.id || pub.name)}">${initial}</span>`;
}

/** タグのアイコン。未設定なら何も出さない(意味の無い記号で埋めない)。14px の枠であり、原寸を落とす理由が無い。 */
export function tagIconHtml(tag) {
  if (!tag || !tag.icon_url) return '';
  return `<span class="tag-icon ph" style="background-image:${cssUrl(mediaVariant(tag.icon_url, '400w'))}"></span>`;
}

/**
 * サーバーが返したエラーの文言を取り出す。エラーの応答は {"error": {"code", "message"}} の形であり、
 * ApiError.message はこの形から文言を拾わない(本文の detail / message / error が文字列である場合だけを見る)ため、本文から直に読む。
 * どの操作がなぜ失敗したのかはサーバーが日本語で返す。これを握り潰して定型文だけを出すと、利用者は原因に辿り着けない。
 */
export function errorMessage(e) {
  const body = e && e.body;
  const err = body && body.error;
  if (err && typeof err.message === 'string' && err.message !== '') return err.message;
  // 入力の検証に落ちた要求は FastAPI が {"detail": [{loc, msg}, ...]} の形で返す。ApiError.message は detail が文字列のときしか拾わないため、配列はここで畳む。どの項目がなぜ弾かれたのかは、この配列にしか書かれていない。
  const detail = body && body.detail;
  if (Array.isArray(detail)) {
    const parts = detail.map((d) => (d && d.msg ? `${(d.loc || []).join('.')}: ${d.msg}` : JSON.stringify(d))).filter(Boolean);
    if (parts.length > 0) return parts.join(' / ');
  }
  return e && e.message ? e.message : 'エラーが発生しました。';
}

/**
 * 機能系の導線(タグ・連載・管理・記事を書く)。カテゴリ以外でナビへ並ぶものはここが唯一の出どころであり、青帯とドロワーの双方をこの一覧から描く。
 *
 * 以前はこれらが各ページの HTML へ直書きされており、トップだけが タグ・連載・記事を書く の 3 つを持ち、残る 6 ページは 記事を書く だけを持っていた。同じ帯が同じ場所で違うものを出していたわけで、これは chrome.js が存在する理由そのものの再演である。描く先が 2 つ(青帯とドロワー)へ増える今、出どころを 1 つにしないと食い違いは倍に増える。
 *
 * 管理への導線は管理者にだけ出す。判定はサーバーが返す is_admin をそのまま使い、画面側でロールから組み立て直さない(管理者は guild のオーナー・Administrator 権限を持つロール・管理ロールのいずれかで決まり、その規則を画面へ写すと二重管理になる)。導線を出さないことは権限の代わりにはならない。/admin を直接開くことは妨げないが、設定の API が要求のたびに管理者資格を検査して拒否する。
 */
function utilLinks(viewer) {
  // タグ・連載はトップの中の節であり、他のページからは / を経由して辿る。トップに居るときだけ素の断片にするのは、?q= や ?series= で絞り込んだ状態を保ったまま節へ送るため(/#tags と書くとクエリが落ち、絞り込みが解ける)。
  const home = location.pathname === '/' ? '' : '/';
  const links = [
    { href: `${home}#tags`, label: 'タグ', kind: 'util' },
    { href: `${home}#series`, label: '連載', kind: 'util' },
  ];
  if (viewer && viewer.is_admin) links.push({ href: '/admin', label: '🛠 管理', kind: 'util' });
  links.push({ href: '/editor', label: '✍ 記事を書く', kind: 'write' });
  return links;
}

/** 機能系の導線を青帯の右側(spacer の後ろ)へ並べる。現在地は自分のパスで決める。 */
function mountUtilLinks(viewer) {
  const nav = document.querySelector('[data-globalnav]');
  if (!nav) return;
  const html = utilLinks(viewer).map((l) => {
    const active = l.href === location.pathname ? ' active' : '';
    return `<a class="${l.kind}${active}" href="${escapeHtml(l.href)}">${escapeHtml(l.label)}</a>`;
  }).join('');
  nav.insertAdjacentHTML('beforeend', html);
}

/**
 * ヘッダのアカウント欄を描く。ログイン済みなら利用者名とログアウト、未ログインならログインの導線を出す。
 * acct を付けた要素はスマホで白帯から降り、ドロワーが同じものを引き取る(style.css の .masthead .actions .acct)。白帯にはロゴと 🔍 🌙 ☰ しか残らない。
 */
function mountAccount(actions, viewer) {
  if (!actions) return;
  if (!viewer) {
    actions.insertAdjacentHTML('afterbegin', `<a class="btn-discord acct" href="${escapeHtml(loginUrl())}">🎮 Discord でログイン</a>`);
    return;
  }
  const user = viewer.user || viewer;
  // 利用者名は設定へ、その隣にログアウト。ログアウトの経路(POST /auth/logout)はどのページからも要るが、これまではトップにしか無かった。
  actions.insertAdjacentHTML(
    'afterbegin',
    `<a class="publisher acct" href="/settings">${avatarHtml(user)}${escapeHtml(displayName(user))}</a>` +
    '<button class="btn acct" type="button" data-logout>ログアウト</button>',
  );
  wireLogout(actions);
}

/** ログアウトの押下を結線する。白帯とドロワーの双方に同じ導線が居るため、両方から呼ぶ。 */
function wireLogout(root) {
  root.querySelectorAll('[data-logout]').forEach((el) => {
    el.addEventListener('click', async () => {
      try {
        await post('/auth/logout');
      } catch (e) {
        // 失敗しても読み込み直す。状態はサーバーから取り直すため、破棄できていなければログイン済みのまま表示され、利用者に伝わる。
        console.error('ログアウトに失敗しました', e);
      }
      location.reload();
    });
  });
}

/**
 * グローバルナビへカテゴリ(記事の主分類)を position 順に足す。引けなくてもページ本体は読めるため、ナビだけを諦める。
 *
 * activeCategory にカテゴリの slug を渡すと、その欄を現在地として示し、併せてトップの現在地表示を降ろす(トップと欄の双方が現在地に見える状態を作らない)。渡された slug がどの欄にも当たらなければ何も動かさない。存在しない欄の URL を開いたときに、現在地がどこにも無いナビを出さないためである。
 * source には取得済みのカテゴリを渡せる(配列でも、まだ解決していない Promise でもよい)。記事一覧のために同じ一覧を既に引いているページが、二度目の要求を出さずに済むようにある。省けば自分で引く。
 */
async function mountCategories(activeCategory, source) {
  const spacer = document.querySelector('[data-globalnav] .spacer');
  if (!spacer) return;
  try {
    const cats = listOf(await (source === undefined ? get('/api/categories') : source), 'categories');
    const ordered = [...cats].sort((a, b) => (a.position - b.position) || String(a.name).localeCompare(String(b.name), 'ja'));
    const html = ordered.map((c) => {
      const active = c.slug === activeCategory ? ' class="active"' : '';
      return `<a href="/categories/${encodeURIComponent(c.slug)}"${active}>${escapeHtml(c.name)}</a>`;
    }).join('');
    spacer.insertAdjacentHTML('beforebegin', html);
    if (ordered.some((c) => c.slug === activeCategory)) {
      const top = document.querySelector('[data-globalnav] a[href="/"]');
      if (top) top.classList.remove('active');
    }
  } catch (e) {
    console.error('カテゴリを取得できませんでした', e);
  }
}

/**
 * スマホのドロワー。青帯から降ろした機能系の導線と、白帯から降ろしたアカウント欄を引き取る。
 *
 * 601px 以上では style.css が存在ごと消す(display: none)。閉じているだけにすると、広い画面で Tab を送った利用者が見えない導線の中へ潜り込む。
 * 中身は utilLinks から描くため、青帯と食い違わない。
 */
function mountDrawer(viewer) {
  const actions = document.querySelector('[data-actions]');
  if (!actions) return;

  const user = viewer ? (viewer.user || viewer) : null;
  const acct = user
    ? `${avatarHtml(user)}<span class="name">${escapeHtml(displayName(user))}</span>`
    : `<a class="btn-discord" href="${escapeHtml(loginUrl())}">🎮 Discord でログイン</a>`;
  const links = utilLinks(viewer).map((l) => {
    const active = l.href === location.pathname ? ' class="active"' : '';
    return `<a href="${escapeHtml(l.href)}"${active}>${escapeHtml(l.label)}</a>`;
  }).join('');
  // 設定とログアウトはログイン済みのときだけ意味を持つ。未ログインでは上のログイン導線が同じ場所を占める。
  const tail = user
    ? '<span class="sep"></span><a href="/settings">⚙ 設定</a><button class="logout" type="button" data-logout>ログアウト</button>'
    : '';

  document.body.insertAdjacentHTML(
    'beforeend',
    '<div class="drawer-overlay" data-drawer-overlay></div>' +
    '<aside class="drawer" data-drawer role="dialog" aria-modal="true" aria-label="メニュー">' +
      '<div class="drawer-head"><span>メニュー</span>' +
      '<button class="icon-btn" type="button" data-drawer-close aria-label="メニューを閉じる">✕</button></div>' +
      `<div class="drawer-acct">${acct}</div>` +
      `<nav class="drawer-nav">${links}${tail}</nav>` +
    '</aside>',
  );

  const drawer = document.querySelector('[data-drawer]');
  const overlay = document.querySelector('[data-drawer-overlay]');
  wireLogout(drawer);

  actions.insertAdjacentHTML('beforeend', '<button class="icon-btn" type="button" data-drawer-toggle aria-label="メニューを開く" aria-expanded="false">☰</button>');
  const toggle = actions.querySelector('[data-drawer-toggle]');

  // 開く前に焦点を持っていた要素。閉じたらここへ返す。返さないと、ドロワーを閉じた利用者の焦点が文書の先頭へ飛び、キーボードで辿っていた位置を失う。
  let restoreFocus = null;

  function open() {
    restoreFocus = document.activeElement;
    drawer.classList.add('open');
    overlay.classList.add('open');
    toggle.setAttribute('aria-expanded', 'true');
    // 背後の記事が指の下で流れないようにする。ドロワーの中身が長ければドロワー自身が縦に流れる(.drawer の overflow-y)。
    document.body.style.overflow = 'hidden';
    const first = drawer.querySelector('[data-drawer-close]');
    if (first) first.focus();
  }

  function close() {
    drawer.classList.remove('open');
    overlay.classList.remove('open');
    toggle.setAttribute('aria-expanded', 'false');
    document.body.style.overflow = '';
    if (restoreFocus && restoreFocus.focus) restoreFocus.focus();
    restoreFocus = null;
  }

  toggle.addEventListener('click', () => (drawer.classList.contains('open') ? close() : open()));
  overlay.addEventListener('click', close);
  drawer.querySelector('[data-drawer-close]').addEventListener('click', close);

  document.addEventListener('keydown', (e) => {
    if (!drawer.classList.contains('open')) return;
    if (e.key === 'Escape') {
      close();
      return;
    }
    if (e.key !== 'Tab') return;
    // 焦点をドロワーの中へ閉じ込める。閉じ込めないと、開いたまま Tab を送った利用者の焦点が背後の記事へ抜け、どこを触っているのか分からなくなる。
    const items = drawer.querySelectorAll('a[href], button:not([disabled])');
    if (items.length === 0) return;
    const first = items[0];
    const last = items[items.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  });

  // 画面が広がったらドロワーを畳む。開いたまま 601px を跨ぐと、style.css がドロワーを消す一方で body の overflow: hidden だけが残り、閉じる手段が無いままページが縦に固まる。
  window.addEventListener('resize', () => {
    if (window.innerWidth > 600 && drawer.classList.contains('open')) close();
  });
}

/**
 * スマホの検索の開閉。白帯はロゴとアカウント欄で埋まるため検索欄を常設できず、🔍 の押下まで畳んでおく。
 * 開いた検索欄は折り返してロゴの下に自分の行を取る(style.css の [data-search-open] .search)。
 */
function mountSearchToggle() {
  const masthead = document.querySelector('.masthead');
  const actions = document.querySelector('[data-actions]');
  const search = document.querySelector('.search');
  if (!masthead || !actions || !search) return;

  actions.insertAdjacentHTML('afterbegin', '<button class="icon-btn" type="button" data-search-toggle aria-label="検索を開く" aria-expanded="false">🔍</button>');
  const toggle = actions.querySelector('[data-search-toggle]');
  toggle.addEventListener('click', () => {
    const open = masthead.toggleAttribute('data-search-open');
    toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    toggle.setAttribute('aria-label', open ? '検索を閉じる' : '検索を開く');
    if (open) {
      const input = search.querySelector('input');
      if (input) input.focus();
    }
  });

  // 検索語を伴って開かれたページでは、初めから開いておく。畳んだままだと、「『◯◯』の検索結果」と出ているのに何で絞ったのかが読めず、直す手段も無い。
  const input = search.querySelector('input');
  if (input && input.value) {
    masthead.setAttribute('data-search-open', '');
    toggle.setAttribute('aria-expanded', 'true');
    toggle.setAttribute('aria-label', '検索を閉じる');
  }
}

/**
 * カテゴリの帯の端に、続きがあることを示すフェードを出す。
 * カテゴリはデプロイごとに数が異なり、狭幅では必ず溢れる。溢れていることが見えなければ、読者は最初の数欄しか無いものとして扱う。
 * 幅が足りているときはどちらの端も付けない(続きが無いのにあるように見せない)。
 */
function mountNavFade() {
  const nav = document.querySelector('[data-globalnav]');
  if (!nav) return;
  const update = () => {
    // 端ちょうどで 1px 未満の誤差が残る環境があるため、遊びを持たせて判定する。
    const max = nav.scrollWidth - nav.clientWidth;
    nav.classList.toggle('fade-start', nav.scrollLeft > 1);
    nav.classList.toggle('fade-end', nav.scrollLeft < max - 1);
  };
  update();
  nav.addEventListener('scroll', update, { passive: true });
  window.addEventListener('resize', update);
  // 現在地の欄が溢れた先に居ることがある(カテゴリが多い環境で末尾の欄を開いた場合)。開いた時点で見えていないと、どこに居るのか分からない。
  const active = nav.querySelector('a.active');
  if (active) {
    active.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    update();
  }
}

/**
 * ヘッダのアカウント欄・機能系の導線・グローバルナビのカテゴリ・スマホのドロワーと検索を結線する。
 * `data-actions`(ヘッダの操作群)と `data-globalnav`(青帯のナビ、中に `.spacer` を持つ)を備えたページで使う。
 *
 * viewer は呼び出し側が取得済みなら渡す(GET /api/me を二度引かないため)。渡されなければ自分で取得する。取得できないときはログインの導線を出す(ログイン済みだと誤って見せるより、もう一度ログインを促す方が害が無い)。
 * options.activeCategory は現在地として示す欄の slug、options.categories は取得済みのカテゴリ(配列または Promise)。どちらも mountCategories がそのまま受ける。
 * 戻り値は解決した viewer(未ログインなら null)。呼び出し側は続けて自分の画面を描くのにこれを使える。
 */
export async function mountChrome(viewer, { activeCategory = null, categories } = {}) {
  let resolved = viewer;
  if (resolved === undefined) {
    try {
      resolved = await me();
    } catch (e) {
      resolved = null;
    }
  }
  mountAccount(document.querySelector('[data-actions]'), resolved);
  mountUtilLinks(resolved);
  mountSearchToggle();
  mountDrawer(resolved);
  // カテゴリはネットワークを待つため最後に置く。帯の端のフェードは、カテゴリが並び終わって初めて正しい幅を測れる。
  await mountCategories(activeCategory, categories);
  mountNavFade();
  return resolved;
}
