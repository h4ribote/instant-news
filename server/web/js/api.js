/*
 * instant-news の API クライアント。各ページはこのモジュールを import して画面を API に結線する。
 * ビルド工程を持たないため、素の ES module として <script type="module"> から直接読み込む。
 *
 * JSON 応答に含まれる ID(DB の BIGINT・Discord のスノーフレーク)は、サーバーが必ず文字列で返す。
 * JavaScript の Number は 2^53 までしか正確に表せず、スノーフレークを数値として扱うと黙って下位桁が壊れる。
 * 受け取った ID は文字列のまま扱い、Number() や parseInt() に通さないこと。比較は文字列として行う。
 */

const HTML_ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

/** ネットワーク到達不能を表す擬似ステータス。呼び出し側が HTTP のエラーと同じ経路で扱えるようにするため、fetch 自体の失敗も ApiError に畳む。 */
const STATUS_NETWORK_ERROR = 0;

/** API がエラーを返した(あるいは到達できなかった)ことを表す例外。status に HTTP ステータス、body に解析済みの応答本体を持つ。 */
export class ApiError extends Error {
  constructor(status, body) {
    super(buildErrorMessage(status, body));
    this.name = 'ApiError';
    this.status = status;
    this.body = body;
  }
}

/** 例外のメッセージを組み立てる。サーバーがエラーの説明を返していればそれを使い、無ければステータスだけを示す。 */
function buildErrorMessage(status, body) {
  if (typeof body === 'string' && body !== '') return `${status}: ${body}`;
  if (body && typeof body === 'object') {
    const detail = body.detail ?? body.message ?? body.error;
    if (typeof detail === 'string' && detail !== '') return `${status}: ${detail}`;
  }
  if (status === STATUS_NETWORK_ERROR) return 'ネットワークに到達できませんでした';
  return `HTTP ${status}`;
}

/** 応答本体を解析する。204 と空の本体は null、JSON は解析済みのオブジェクト、それ以外は文字列として返す。 */
async function readBody(res) {
  if (res.status === 204) return null;
  const contentType = res.headers.get('Content-Type') || '';
  if (contentType.includes('application/json')) {
    try {
      return await res.json();
    } catch (e) {
      return null;
    }
  }
  const text = await res.text();
  return text === '' ? null : text;
}

/**
 * fetch の薄いラッパ。セッション Cookie を送るため credentials は same-origin に固定する。
 * options.body にプレーンなオブジェクトを渡すと JSON として送る。FormData / Blob / 文字列 / URLSearchParams はそのまま送る
 * (特に multipart は境界文字列をブラウザに決めさせる必要があるため、Content-Type を自分で付けてはならない)。
 * 2xx 以外、および fetch 自体の失敗では ApiError を throw する。
 */
export async function api(path, options = {}) {
  const { body, headers, ...rest } = options;
  const init = {
    credentials: 'same-origin',
    ...rest,
    headers: { Accept: 'application/json', ...(headers || {}) },
  };

  if (body !== undefined && body !== null) {
    const isRawBody = body instanceof FormData || body instanceof Blob || body instanceof URLSearchParams || typeof body === 'string';
    if (isRawBody) {
      init.body = body;
    } else {
      init.body = JSON.stringify(body);
      init.headers['Content-Type'] = 'application/json';
    }
  }

  let res;
  try {
    res = await fetch(path, init);
  } catch (e) {
    throw new ApiError(STATUS_NETWORK_ERROR, { detail: String(e && e.message ? e.message : e) });
  }

  const data = await readBody(res);
  if (!res.ok) throw new ApiError(res.status, data);
  return data;
}

export const get = (path) => api(path);
export const post = (path, body) => api(path, { method: 'POST', body });
export const patch = (path, body) => api(path, { method: 'PATCH', body });
export const del = (path) => api(path, { method: 'DELETE' });

/**
 * 画像をアップロードする。応答は {id, url, ...}(id は文字列、url は配信 URL)。
 * isPublic はカバー・広告・ロゴ・アイコンのように認証なしで配信してよい画像にのみ立てる。本文中の画像は非公開のまま取り込み、記事の閲覧範囲と同じ判定を受けさせる。
 */
export async function upload(file, { isPublic = false } = {}) {
  const form = new FormData();
  form.append('file', file);
  return await api('/api/media' + qs({ is_public: isPublic ? 'true' : 'false' }), { method: 'POST', body: form });
}

/** 現在のログイン利用者を返す。未ログインなら null(401 を握って null にするのは、ヘッダのログイン導線の出し分けが例外処理を強いられないようにするため)。401 以外のエラーはそのまま throw する。 */
export async function me() {
  try {
    return await get('/api/me');
  } catch (e) {
    if (e instanceof ApiError && e.status === 401) return null;
    throw e;
  }
}

/** 画像の配信 URL。内容アドレスであり、内容が変われば hash も変わるため、この URL は永久にキャッシュしてよい。 */
export function mediaUrl(hash, variant = 'orig') {
  return `/media/${encodeURIComponent(hash)}/${encodeURIComponent(variant)}.webp`;
}

/** 記事ページの URL。 */
export function articleUrl(slug) {
  return `/articles/${encodeURIComponent(slug)}`;
}

/**
 * ISO 8601(UTC)の日時を閲覧者のローカル時刻の「2026-07-15 15:04:32」形式へ整形する。
 * 秒まで出すのは、同じ分内に複数の記事や改版が並んだときに前後関係が読めなくなるため。報道では公開・訂正の時刻そのものが記録であり、丸めてはならない。
 * 解析できない値では空文字を返す(日時が欠けている行で「Invalid Date」と表示させない)。
 */
export function formatDateTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

/** HTML の特殊文字をエスケープする。innerHTML に値を差し込む箇所では必ず通すこと。null / undefined は空文字にする。 */
export function escapeHtml(s) {
  if (s === null || s === undefined) return '';
  return String(s).replace(/[&<>"']/g, (c) => HTML_ESCAPES[c]);
}

/** オブジェクトをクエリ文字列にする。null / undefined / 空文字は落とす(未指定の絞り込みを「空文字で絞り込む」意味にしないため)。0 と false は意味のある値として残す。先頭の ? を含めて返し、要素が無ければ空文字を返すので、パスへそのまま連結できる。 */
export function qs(params) {
  const sp = new URLSearchParams();
  for (const [key, value] of Object.entries(params || {})) {
    if (value === null || value === undefined || value === '') continue;
    sp.append(key, String(value));
  }
  const s = sp.toString();
  return s ? `?${s}` : '';
}

/**
 * 広告枠を描画する。候補が無ければ({ad: null})枠そのものを取り除く(中身の無い枠だけが「広告」ラベルを付けて残ると、何も無い箱を広告として見せることになる)。
 *
 * 「広告」ラベルは css/style.css の .adslot::after が CSS で描画する。api.js が差し込むのは枠の中身だけであり、ラベルは中身の HTML には存在しないため、広告主も外部の広告管理アプリもクリエイティブ側からラベルを消せない。
 * この構造を守るため、ここでは .adslot クラスを外さず、むしろ付いていなければ付け直す。複数の報道社が加盟するニュースプラットフォームにおいて、広告が見出しと見分けられない状態は記事の信頼性を直接損なう。ラベルは装飾ではなく要件である。
 *
 * リンクは /ads/{id}/click を経由させてクリックを計測し、そこから広告主の URL へ転送する。target="_blank" を付ける以上 rel="noopener" は必須(遷移先が window.opener からこのページを操作できてしまう)。参照元も渡さないため noreferrer も添える。
 */
export async function renderAdSlot(el, slug) {
  if (!el) return;

  let ad = null;
  try {
    const data = await get('/api/ads/serve' + qs({ slot: slug }));
    ad = data && data.ad ? data.ad : null;
  } catch (e) {
    ad = null;
  }

  if (!ad) {
    el.remove();
    return;
  }

  el.classList.add('adslot');

  const link = document.createElement('a');
  link.className = 'creative';
  link.href = `/ads/${encodeURIComponent(ad.id)}/click`;
  link.target = '_blank';
  link.rel = 'noopener noreferrer';

  const img = document.createElement('img');
  img.src = ad.image_url;
  // 代替テキストが未設定の広告でも、リンクの中身が画像だけである以上 alt を空にはできない(読み上げでリンクの行き先が分からなくなる)。広告主名へ退避する。
  img.alt = ad.alt_text || ad.advertiser_name || '広告';
  link.appendChild(img);

  el.replaceChildren(link);
  el.dataset.adId = ad.id;
}
