/*
 * instant-news API client. Every page imports this module to wire the UI to the API.
 * There is no build step, so it loads directly from <script type="module"> as a plain ES module.
 *
 * The server always returns the IDs in JSON responses (DB BIGINTs, Discord snowflakes) as strings.
 * JavaScript's Number is exact only up to 2^53, so treating a snowflake as a number silently corrupts its low digits.
 * Keep received IDs as strings, never pass them through Number() or parseInt(), and compare them as strings.
 */

const HTML_ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

/** Pseudo-status for an unreachable network. A failure of fetch itself is folded into ApiError too, so callers can handle it by the same path as an HTTP error. */
const STATUS_NETWORK_ERROR = 0;

/** Thrown when the API returned an error, or could not be reached. status holds the HTTP status, body the parsed response body. */
export class ApiError extends Error {
  constructor(status, body) {
    super(buildErrorMessage(status, body));
    this.name = 'ApiError';
    this.status = status;
    this.body = body;
  }
}

/** Build the exception message: the server's error description when it sent one, otherwise the status alone. */
function buildErrorMessage(status, body) {
  if (typeof body === 'string' && body !== '') return `${status}: ${body}`;
  if (body && typeof body === 'object') {
    const detail = body.detail ?? body.message ?? body.error;
    if (typeof detail === 'string' && detail !== '') return `${status}: ${detail}`;
  }
  if (status === STATUS_NETWORK_ERROR) return 'ネットワークに到達できませんでした';
  return `HTTP ${status}`;
}

/** Parse the response body: null for 204 and for an empty body, a parsed object for JSON, a string otherwise. */
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
 * Thin wrapper around fetch. credentials is pinned to same-origin so the session cookie is sent.
 * A plain object in options.body is sent as JSON. FormData / Blob / string / URLSearchParams are sent as-is, and Content-Type must not be set by hand for them: multipart in particular needs the browser to pick the boundary string.
 * Throws ApiError on any non-2xx response, and on a failure of fetch itself.
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
 * Upload an image. The response is {id, url, ...}, where id is a string and url is the delivery URL.
 * Set isPublic only for images that may be served without authentication, such as cover images, ads, logos and icons. Images inside an article body are ingested non-public so that they get the same visibility check as the article.
 */
export async function upload(file, { isPublic = false } = {}) {
  const form = new FormData();
  form.append('file', file);
  return await api('/api/media' + qs({ is_public: isPublic ? 'true' : 'false' }), { method: 'POST', body: form });
}

/** Return the logged-in user, or null when not logged in. Swallowing the 401 into null is what keeps the header from having to wrap its login entry point in exception handling. Any error other than 401 is rethrown. */
export async function me() {
  try {
    return await get('/api/me');
  } catch (e) {
    if (e instanceof ApiError && e.status === 401) return null;
    throw e;
  }
}

/** Delivery URL for an image. It is content-addressed, and the hash changes whenever the content does, so this URL may be cached forever. */
export function mediaUrl(hash, variant = 'orig') {
  return `/media/${encodeURIComponent(hash)}/${encodeURIComponent(variant)}.webp`;
}

/* Shape of a URL pointing at an already-ingested image, the same one service.media_url builds (/media/{64 hex digits}/{variant}.webp). It matches the hash's digit count and character set as well, so that nothing outside this shape is ever rewritten. */
const MEDIA_URL_PATTERN = /^\/media\/([0-9a-f]{64})\/[^/]+\.webp$/;

/**
 * Rewrite an already-ingested image URL to the given variant. Used where the image is shown small, such as in a listing or a card.
 *
 * **A URL outside that shape is returned unchanged.** Cover images, publisher logos and tag icons are free-form columns that can hold an absolute URL on an external host with no ingest involved: a cover image can be given any URL through the API, and logos and icons are typed in by hand on the admin screen. Appending /400w.webp to an external URL would of course point at nothing.
 *
 * The variant need not exist. No variant wider than the original size is produced, and animations only ever have orig, but when the serving side cannot find the variant it falls back to orig (_read_variant in server/app/routers/media.py). So asking for 400w means only that 400px is enough and it should be served if it exists; a missing variant does not break anything.
 */
export function mediaVariant(url, variant) {
  if (!url) return url;
  const m = MEDIA_URL_PATTERN.exec(String(url));
  return m ? mediaUrl(m[1], variant) : url;
}

/** URL of an article page. */
export function articleUrl(slug) {
  return `/articles/${encodeURIComponent(slug)}`;
}

/**
 * Format an ISO 8601 (UTC) timestamp as 2026-07-15 15:04:32 in the viewer's local time.
 * Seconds are shown because several articles or revisions can fall in the same minute, and without them their order becomes unreadable. In news reporting the publication and correction times are themselves the record, and must not be rounded.
 * An unparseable value yields an empty string, so that a row with a missing timestamp does not render as Invalid Date.
 */
export function formatDateTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

/** Escape HTML special characters. Anything interpolated into innerHTML must be passed through this. null / undefined become an empty string. */
export function escapeHtml(s) {
  if (s === null || s === undefined) return '';
  return String(s).replace(/[&<>"']/g, (c) => HTML_ESCAPES[c]);
}

/** Turn an object into a query string. null / undefined / empty string are dropped, so that an unset filter does not become a filter on the empty string; 0 and false are kept as meaningful values. The result includes the leading ? and is an empty string when there are no entries, so it can be concatenated onto a path directly. */
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
 * Render an ad slot. With no candidate ({ad: null}) the slot element itself is removed: an empty slot left in place still carries the Sponsored label, which would present an empty box as an ad.
 *
 * The Sponsored label is drawn by CSS, by .adslot::after in css/style.css. api.js injects only the slot's contents and the label is not part of that HTML, so neither the advertiser nor an external ad-management app can remove it from the creative side.
 * To preserve that structure the .adslot class is never taken off here, and is re-added when missing. On a news platform with several member publishers, an ad that cannot be told apart from a headline directly damages the articles' credibility. The label is a requirement, not decoration.
 *
 * The link goes through /ads/{id}/click to track the click and forwards from there to the advertiser's URL. Given target="_blank", rel="noopener" is mandatory, or the opened page could manipulate this one through window.opener. noreferrer is added as well so the referrer is not passed either.
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
  // Even for an ad with no alt text set, alt cannot be left empty when the link's only content is the image: a screen reader would not convey where the link goes. Fall back to the advertiser name.
  img.alt = ad.alt_text || ad.advertiser_name || '広告';
  link.appendChild(img);

  el.replaceChildren(link);
  el.dataset.adId = ad.id;
}
