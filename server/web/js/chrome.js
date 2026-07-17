/*
 * Shared UI parts for instant-news. api.js owns the HTTP layer (fetch, JSON, stringifying IDs); this file owns the DOM layer.
 * It collects in one place what several pages have to render identically: the header's account area, the global nav, and the presentational snippets for users and tags.
 *
 * Before this was centralized these functions were copied into every page, and copies drift. avatarHtml is the proof: on admin / settings / tag / publisher it read user.avatar_url, a field that does not exist, so it never once produced a real image and fell back to the initial silently, while index / article / editor each built the URL from avatar_hash correctly but in three different ways. Holding one copy makes that class of drift structurally impossible.
 *
 * As in api.js, IDs are always handled as strings: converting one to a number silently corrupts the low digits of a snowflake.
 */

import { get, post, me, qs, escapeHtml, mediaVariant } from '/static/js/api.js';

// Discord gives an account with no avatar set one of a fixed number of default avatars, chosen by a modulus over that count. Building that URL is not the caller's concern here, so a user with no hash falls back to the initial instead: every default avatar is one of the same few pictures for everyone, whereas the initial says who the user is.

/** The login entry point. Once auth is done, return to the page being viewed: redirect_to is the current path and query (the fragment is never sent to the server, so it cannot be included). */
export function loginUrl() {
  return '/auth/login' + qs({ redirect_to: location.pathname + location.search });
}

/**
 * A bare CSS url() value. The URL comes from article, user or tag data, so JSON.stringify neutralizes quotes and backslashes for the CSS string.
 * **Use this when assigning straight to an element's style property (el.style.backgroundImage = ...).** Using cssUrl for an assignment that never passes through HTML puts the escaped &quot; into the CSS string itself, invalidating the whole value so no image appears.
 */
export function cssUrlRaw(url) {
  return `url(${JSON.stringify(String(url))})`;
}

/**
 * A url() interpolated into a style attribute as HTML. **Use this for snippets built with innerHTML.**
 * JSON.stringify neutralizes quotes and backslashes for the CSS string, and escapeHtml neutralizes quotes for the attribute value. Without both layers, a URL containing a quote can break out of the style attribute.
 */
export function cssUrl(url) {
  return escapeHtml(cssUrlRaw(url));
}

/**
 * Derives the palette number (prefix1..prefixN) from a string ID, so the same subject always gets the same colour.
 * The ID is folded as a string: converting it to a number corrupts the low digits of a snowflake, which can give two different people the same colour.
 */
export function paletteClass(prefix, count, key) {
  const s = String(key == null ? '' : key);
  let n = 0;
  for (let i = 0; i < s.length; i++) n = (n * 31 + s.charCodeAt(i)) % count;
  return prefix + (n + 1);
}

/** Pulls the array out of a listing response, accepting either a bare array or an object carrying page info ({items|<key>: [...]}). */
export function listOf(data, key) {
  if (Array.isArray(data)) return data;
  if (data && Array.isArray(data.items)) return data.items;
  if (data && key && Array.isArray(data[key])) return data[key];
  return [];
}

/** A user's display name: Discord's display name when there is one, otherwise the username. */
export function displayName(user) {
  if (!user) return '';
  return user.global_name || user.username || '';
}

/**
 * URL of a user's avatar image. Returns null for a user on the default avatar (no avatar_hash) so the caller can substitute the initial.
 * A hash starting with a_ is animated and takes a different extension. The ID goes into the URL as a string.
 */
export function avatarUrl(user) {
  if (!user || !user.avatar_hash || !user.discord_user_id) return null;
  const ext = String(user.avatar_hash).startsWith('a_') ? 'gif' : 'png';
  return `https://cdn.discordapp.com/avatars/${encodeURIComponent(user.discord_user_id)}/${encodeURIComponent(user.avatar_hash)}.${ext}?size=64`;
}

/**
 * A user's avatar snippet: the image as a background if there is one, otherwise the initial. Passing extraStyle layers on dimensions and the like.
 * user is the shape holding discord_user_id and avatar_hash (and the display name). The id decides the colour, falling back to the display name when absent.
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
 * A publisher's logo, falling back to the initial of the publisher name when unset.
 * The largest box the logo appears in is the 72px heading on the publisher page, so 400w is enough. URLs on external hosts that never went through ingest (the admin screen allows typing one in by hand) are passed straight through by mediaVariant.
 */
export function pubLogoHtml(pub) {
  if (!pub) return '';
  if (pub.logo_url) return `<span class="pub-logo ph" style="background-image:${cssUrl(mediaVariant(pub.logo_url, '400w'))}"></span>`;
  const initial = escapeHtml(Array.from(String(pub.name || '?'))[0]);
  return `<span class="pub-logo ${paletteClass('p', 4, pub.slug || pub.id || pub.name)}">${initial}</span>`;
}

/** A tag's icon; nothing at all when unset (do not fill the space with a meaningless glyph). The box is 14px, so there is no reason to pull the original size. */
export function tagIconHtml(tag) {
  if (!tag || !tag.icon_url) return '';
  return `<span class="tag-icon ph" style="background-image:${cssUrl(mediaVariant(tag.icon_url, '400w'))}"></span>`;
}

/**
 * Pulls the message out of an error the server returned. Error responses have the shape {"error": {"code", "message"}}, and ApiError.message does not read a message out of that shape (it only looks at the response body's detail / message / error when they are strings), so read the body directly.
 * The server states in Japanese which operation failed and why. Swallow that and print only a canned line, and the user cannot reach the cause.
 */
export function errorMessage(e) {
  const body = e && e.body;
  const err = body && body.error;
  if (err && typeof err.message === 'string' && err.message !== '') return err.message;
  // FastAPI returns a request that failed input validation as {"detail": [{loc, msg}, ...]}. ApiError.message picks detail up only when it is a string, so the array is collapsed here. Which item was rejected and why is written nowhere but this array.
  const detail = body && body.detail;
  if (Array.isArray(detail)) {
    const parts = detail.map((d) => (d && d.msg ? `${(d.loc || []).join('.')}: ${d.msg}` : JSON.stringify(d))).filter(Boolean);
    if (parts.length > 0) return parts.join(' / ');
  }
  return e && e.message ? e.message : 'エラーが発生しました。';
}

/**
 * The utility links (tags, series, admin, write an article). Everything that sits in the nav and is not a category originates here alone, and both the nav bar and the drawer are drawn from this list.
 *
 * These used to be hardcoded into each page's HTML: home carried three of them (tags, series, write an article) while the remaining six pages carried only write an article. The same bar was showing different things in the same place — a replay of the very reason chrome.js exists. Now that there are two places to draw them (the nav bar and the drawer), anything but a single origin doubles the divergence.
 *
 * The admin link is shown to admins only. The check uses the is_admin the server returns as-is; the UI never reassembles it from roles, because an admin is the guild owner, or holds a role with the Administrator permission, or holds an admin role, and duplicating that rule in the UI means maintaining it in two places. Withholding the link is no substitute for permission: opening /admin directly is not prevented, but the settings API checks admin eligibility on every request and refuses.
 */
function utilLinks(viewer) {
  // tags and series are anchor sections inside home; every other page reaches them via /. The link is a bare fragment only while on home, so that a view narrowed by ?q= or ?series= keeps its filter when jumping to the section — writing /#tags would drop the query and clear the filter.
  const home = location.pathname === '/' ? '' : '/';
  const links = [
    { href: `${home}#tags`, label: 'タグ', kind: 'util' },
    { href: `${home}#series`, label: '連載', kind: 'util' },
  ];
  if (viewer && viewer.is_admin) links.push({ href: '/admin', label: '🛠 管理', kind: 'util' });
  links.push({ href: '/editor', label: '✍ 記事を書く', kind: 'write' });
  return links;
}

/** Lays the utility links out on the right of the nav bar, after the spacer. The current location is decided from the page's own path. */
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
 * Renders the header's account area: the user name and logout when signed in, the login entry point when not.
 * On phones the elements marked acct come out of the masthead and the drawer takes over the same ones (.masthead .actions .acct in style.css), leaving the masthead with nothing but the logo and 🔍 🌙 ☰.
 */
function mountAccount(actions, viewer) {
  if (!actions) return;
  if (!viewer) {
    actions.insertAdjacentHTML('afterbegin', `<a class="btn-discord acct" href="${escapeHtml(loginUrl())}">🎮 Discord でログイン</a>`);
    return;
  }
  const user = viewer.user || viewer;
  // The user name links to settings, with logout beside it. The logout route (POST /auth/logout) is needed from every page, but until now existed only on home.
  actions.insertAdjacentHTML(
    'afterbegin',
    `<a class="publisher acct" href="/settings">${avatarHtml(user)}${escapeHtml(displayName(user))}</a>` +
    '<button class="btn acct" type="button" data-logout>ログアウト</button>',
  );
  wireLogout(actions);
}

/** Wires up presses of logout. The same entry point lives in both the masthead and the drawer, so this is called from both. */
function wireLogout(root) {
  root.querySelectorAll('[data-logout]').forEach((el) => {
    el.addEventListener('click', async () => {
      try {
        await post('/auth/logout');
      } catch (e) {
        // Reload even on failure. The state is re-fetched from the server, so a session that was not destroyed still renders as signed in and the user is told as much.
        console.error('ログアウトに失敗しました', e);
      }
      location.reload();
    });
  });
}

/**
 * Appends the categories (an article's primary classification) to the global nav in position order. The page body still reads without them, so a failed fetch gives up on the nav alone.
 *
 * Passing a category slug as activeCategory marks that section as the current location and clears the active marker from home, so home and the section are never both current. A slug matching no section moves nothing: opening the URL of a section that does not exist must not produce a nav whose current location is nowhere.
 * source takes categories already fetched — an array, or a Promise not yet resolved — so a page that already pulled the same listing for its article list need not issue a second request. Omit it and this fetches them itself.
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
 * The phone drawer. It takes over the utility links dropped from the nav bar and the account area dropped from the masthead.
 *
 * At 601px and up style.css removes it altogether (display: none). Merely leaving it closed would let a user tabbing on a wide viewport wander into links they cannot see.
 * Its contents are drawn from utilLinks, so it cannot diverge from the nav bar.
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
  // Settings and logout mean something only when signed in. When signed out, the login entry point above occupies the same place.
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

  // The element that held focus before opening; focus returns here on close. Without that, closing the drawer throws the user's focus to the top of the document and loses the position they had reached by keyboard.
  let restoreFocus = null;

  function open() {
    restoreFocus = document.activeElement;
    drawer.classList.add('open');
    overlay.classList.add('open');
    toggle.setAttribute('aria-expanded', 'true');
    // Keeps the article behind from scrolling under the finger. If the drawer's contents are long, the drawer itself scrolls vertically (overflow-y on .drawer).
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
    // Trap focus inside the drawer. Without the trap, tabbing while it is open takes focus out into the article behind and the user can no longer tell what they are touching.
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

  // Close the drawer once the viewport widens. Crossing 601px while it is open leaves style.css hiding the drawer while body overflow: hidden survives on its own, freezing the page's vertical scroll with no way left to close it.
  window.addEventListener('resize', () => {
    if (window.innerWidth > 600 && drawer.classList.contains('open')) close();
  });
}

/**
 * The phone search toggle. The masthead is filled by the logo and the account area, leaving no room for a permanent search field, so it stays folded away until 🔍 is pressed.
 * Once open, the search field wraps onto a row of its own below the logo ([data-search-open] .search in style.css).
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

  // Open from the start on a page opened with a search term. Left folded away, the page announces search results for a term the user can neither read back nor correct.
  const input = search.querySelector('input');
  if (input && input.value) {
    masthead.setAttribute('data-search-open', '');
    toggle.setAttribute('aria-expanded', 'true');
    toggle.setAttribute('aria-label', '検索を閉じる');
  }
}

/**
 * Fades the ends of the category bar to show that it continues.
 * The number of categories differs per deployment and always overflows at narrow widths. If the overflow is invisible, readers treat the first few sections as all there is.
 * Neither end is faded when the width is sufficient, so nothing suggests more where there is none.
 */
function mountNavFade() {
  const nav = document.querySelector('[data-globalnav]');
  if (!nav) return;
  const update = () => {
    // Some environments leave a sub-pixel error exactly at the end, so the check keeps 1px of slack.
    const max = nav.scrollWidth - nav.clientWidth;
    nav.classList.toggle('fade-start', nav.scrollLeft > 1);
    nav.classList.toggle('fade-end', nav.scrollLeft < max - 1);
  };
  update();
  nav.addEventListener('scroll', update, { passive: true });
  window.addEventListener('resize', update);
  // The current section can sit beyond the overflow (a trailing section opened where there are many categories). If it is not visible on load, the reader cannot tell where they are.
  const active = nav.querySelector('a.active');
  if (active) {
    active.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    update();
  }
}

/**
 * Wires up the header's account area, the utility links, the global nav's categories, and the phone drawer and search.
 * For pages carrying `data-actions` (the header's action group) and `data-globalnav` (the nav bar, holding a `.spacer`).
 *
 * Pass viewer when the caller has already fetched it, so GET /api/me is not pulled twice; otherwise this fetches it. When it cannot be fetched, the login entry point is shown: prompting for a login again does less harm than wrongly showing someone as signed in.
 * options.activeCategory is the slug of the section to mark as the current location; options.categories is already-fetched categories (array or Promise). mountCategories takes both as-is.
 * Returns the resolved viewer (null when signed out), which the caller can use to go on and render its own page.
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
  // Categories go last because they wait on the network. The fade at the ends of the bar can only measure the right width once the categories are laid out.
  await mountCategories(activeCategory, categories);
  mountNavFade();
  return resolved;
}
