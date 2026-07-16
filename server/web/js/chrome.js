/*
 * instant-news の画面の共通部品。api.js が HTTP の層(fetch・JSON・ID の文字列化)を担うのに対し、
 * こちらは DOM の層を担う。ヘッダのアカウント欄・グローバルナビ・利用者やタグの見た目の断片など、
 * 複数のページが同じ見た目で描くものを 1 箇所に集める。
 *
 * ここに集約する前は、これらの関数が admin / settings / tag / publisher の各ページへ写し取られていた。
 * 写し取られたものは食い違う。実際、利用者のアバターを組み立てる avatarHtml は、存在しない user.avatar_url を
 * 参照していたために全ページで一度も実画像を出せておらず、頭文字へ黙って落ちていた。正しい実装(avatar_hash と
 * discord_user_id から Discord の CDN の URL を組み立てる)を 1 つ持てば、この種のずれは構造的に起こらない。
 *
 * api.js と同じく、ID は必ず文字列として扱う。数値化するとスノーフレークの下位桁が黙って壊れる。
 */

import { get, post, me, qs, escapeHtml } from '/static/js/api.js';

// Discord がアバター未設定のアカウントへ割り当てる既定アバターの種類数。ハッシュを持たない利用者はこの剰余で絵が決まるが、その URL の組み立ては呼び出し側の関心ではないため、ここでは頭文字へ落とす(既定アバターは誰にとっても同じ数種類の絵であり、頭文字の方が誰であるかを表す)。

/** ログインへの導線。認証を終えたら今見ているページへ戻す。戻り先は現在のパスとクエリ(ハッシュはサーバーへ送られないため含めない)。 */
export function loginUrl() {
  return '/auth/login' + qs({ redirect_to: location.pathname + location.search });
}

/**
 * CSS の url() の値を組み立てる。URL は記事・利用者・タグに由来するため、CSS 文字列としての引用符とバックスラッシュを退避する。
 * JSON.stringify が CSS 文字列としての引用符とバックスラッシュを潰し、escapeHtml が属性値としての引用符を潰す。二段で囲わないと、引用符を含む URL から style 属性の外へ抜け出せてしまう。返り値は style 属性へそのまま差し込んでよい。
 */
export function cssUrl(url) {
  return `url(${escapeHtml(JSON.stringify(String(url)))})`;
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

/** 報道社のロゴ。未設定なら社名の頭文字を出す。 */
export function pubLogoHtml(pub) {
  if (!pub) return '';
  if (pub.logo_url) return `<span class="pub-logo ph" style="background-image:${cssUrl(pub.logo_url)}"></span>`;
  const initial = escapeHtml(Array.from(String(pub.name || '?'))[0]);
  return `<span class="pub-logo ${paletteClass('p', 4, pub.slug || pub.id || pub.name)}">${initial}</span>`;
}

/** タグのアイコン。未設定なら何も出さない(意味の無い記号で埋めない)。 */
export function tagIconHtml(tag) {
  if (!tag || !tag.icon_url) return '';
  return `<span class="tag-icon ph" style="background-image:${cssUrl(tag.icon_url)}"></span>`;
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
  return e && e.message ? e.message : 'エラーが発生しました。';
}

/**
 * 管理者にだけ、プラットフォーム全体の設定(/admin)への導線をナビへ足す。
 * 判定はサーバーが返す is_admin をそのまま使い、画面側でロールから組み立て直さない(管理者は guild のオーナー・Administrator 権限を持つロール・管理ロールのいずれかで決まり、その規則を画面へ写すと二重管理になる)。
 * 導線を出さないことは権限の代わりにはならない。/admin を直接開くことは妨げないが、設定の API が要求のたびに管理者資格を検査して拒否する。
 */
function mountAdminNav(viewer) {
  if (!viewer || !viewer.is_admin) return;
  // 既に管理への導線を持つページ(管理画面自身。現在地として active を付けて出している)には足さない。二重に並べない。
  if (document.querySelector('.globalnav a[href="/admin"]')) return;
  const write = document.querySelector('.globalnav a.write');
  if (write) {
    write.insertAdjacentHTML('beforebegin', '<a class="util" href="/admin">🛠 管理</a>');
    return;
  }
  // 「記事を書く」導線を持たないページでは、ナビの末尾(spacer の直後)へ足す。
  const spacer = document.querySelector('[data-globalnav] .spacer');
  if (spacer) spacer.insertAdjacentHTML('afterend', '<a class="util" href="/admin">🛠 管理</a>');
}

/** ヘッダのアカウント欄を描く。ログイン済みなら利用者名とログアウト、未ログインならログインの導線を出す。 */
function mountAccount(actions, viewer) {
  if (!actions) return;
  if (!viewer) {
    actions.insertAdjacentHTML('afterbegin', `<a class="btn-discord" href="${escapeHtml(loginUrl())}">🎮 Discord でログイン</a>`);
    return;
  }
  const user = viewer.user || viewer;
  // 利用者名は設定へ、その隣にログアウト。ログアウトの経路(POST /auth/logout)はどのページからも要るが、これまではトップにしか無かった。
  actions.insertAdjacentHTML(
    'afterbegin',
    `<a class="publisher" href="/settings">${avatarHtml(user)}${escapeHtml(displayName(user))}</a>` +
    '<button class="btn" type="button" data-logout>ログアウト</button>',
  );
  const logout = actions.querySelector('[data-logout]');
  if (logout) {
    logout.addEventListener('click', async () => {
      try {
        await post('/auth/logout');
      } catch (e) {
        // 失敗しても読み込み直す。状態はサーバーから取り直すため、破棄できていなければログイン済みのまま表示され、利用者に伝わる。
        console.error('ログアウトに失敗しました', e);
      }
      location.reload();
    });
  }
}

/** グローバルナビへカテゴリ(記事の主分類)を position 順に足す。引けなくてもページ本体は読めるため、ナビだけを諦める。 */
async function mountCategories() {
  const spacer = document.querySelector('[data-globalnav] .spacer');
  if (!spacer) return;
  try {
    const cats = listOf(await get('/api/categories'), 'categories');
    const ordered = [...cats].sort((a, b) => (a.position - b.position) || String(a.name).localeCompare(String(b.name), 'ja'));
    const html = ordered.map((c) => `<a href="/categories/${encodeURIComponent(c.slug)}">${escapeHtml(c.name)}</a>`).join('');
    spacer.insertAdjacentHTML('beforebegin', html);
  } catch (e) {
    console.error('カテゴリを取得できませんでした', e);
  }
}

/**
 * ヘッダのアカウント欄・管理導線・グローバルナビのカテゴリを結線する。
 * `data-actions`(ヘッダの操作群)と `data-globalnav`(青帯のナビ、中に `.spacer` を持つ)を備えたページで使う。
 *
 * viewer は呼び出し側が取得済みなら渡す(GET /api/me を二度引かないため)。渡されなければ自分で取得する。取得できないときはログインの導線を出す(ログイン済みだと誤って見せるより、もう一度ログインを促す方が害が無い)。
 * 戻り値は解決した viewer(未ログインなら null)。呼び出し側は続けて自分の画面を描くのにこれを使える。
 */
export async function mountChrome(viewer) {
  let resolved = viewer;
  if (resolved === undefined) {
    try {
      resolved = await me();
    } catch (e) {
      resolved = null;
    }
  }
  mountAccount(document.querySelector('[data-actions]'), resolved);
  mountAdminNav(resolved);
  await mountCategories();
  return resolved;
}
