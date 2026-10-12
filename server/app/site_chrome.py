"""The logo and the top bar every page carries, rendered server-side from the site.* settings.

Each page template holds one marker for each (TOPBAR_PLACEHOLDER, LOGO_PLACEHOLDER), so the markup exists in one place and every page shows the same values. Rendering here rather than in the browser means the configured text is in the first paint and readable without JavaScript.

Every value is admin-typed text and goes through html.escape. Colours are interpolated into a style attribute, which is safe because the registry accepts nothing but #rrggbb.
"""

from __future__ import annotations

import logging
from html import escape
from typing import Final

from common.settings import LogoConfig, PromoConfig, SiteConfig

logger = logging.getLogger(__name__)

TOPBAR_PLACEHOLDER: Final = '<!-- site:topbar -->'
LOGO_PLACEHOLDER: Final = '<!-- site:logo -->'


def _color_vars(logo: LogoConfig) -> str:
    """CSS custom properties for the colours that are set. style.css falls back to the theme's colour for any property left undefined."""
    pairs = (
        ('--logo-primary-light', logo.primary_color_light),
        ('--logo-primary-dark', logo.primary_color_dark),
        ('--logo-secondary-light', logo.secondary_color_light),
        ('--logo-secondary-dark', logo.secondary_color_dark),
    )
    return ';'.join(f'{name}:{value}' for name, value in pairs if value is not None)


def render_logo(logo: LogoConfig) -> str:
    """The masthead logo link. A blank secondary part or tagline leaves its element out."""
    style = _color_vars(logo)
    style_attr = f' style="{escape(style)}"' if style else ''
    secondary = f'<span>{escape(logo.secondary)}</span>' if logo.secondary.strip() else ''
    tagline = f'<span class="tag">{escape(logo.tagline)}</span>' if logo.tagline.strip() else ''
    return f'<a class="logo" href="/"{style_attr}><span class="mark">{escape(logo.primary)}{secondary}</span>{tagline}</a>'


def render_topbar(promo: PromoConfig) -> str:
    """The top bar with every message already in place; js/chrome.js rotates them and slots in the promo ad.

    Only the first message is current, and the rest are hidden from assistive technology until their turn. With no message the bar is rendered hidden, and chrome.js reveals it only if the promo slot has an ad to show.
    """
    items = ''.join(
        f'<span class="promo-item is-current">{escape(message)}</span>' if index == 0 else f'<span class="promo-item" aria-hidden="true">{escape(message)}</span>'
        for index, message in enumerate(promo.messages)
    )
    hidden = '' if promo.messages else ' hidden'
    return (
        f'<div class="topbar" data-promo-bar{hidden}>'
        '<div class="container">'
        f'<div class="promo" data-promo data-interval="{promo.interval_seconds}" aria-live="off">{items}</div>'
        '<button class="promo-pause" type="button" data-promo-pause aria-pressed="false" aria-label="トップバーの切り替えを止める" hidden>⏸</button>'
        '</div>'
        '</div>'
    )


def apply_site_chrome(template: str, filename: str, site: SiteConfig) -> str:
    """Replace the template's markers with the logo and the top bar. A missing marker is logged and that part is left out, since the page itself still works."""
    for marker, html in ((TOPBAR_PLACEHOLDER, render_topbar(site.promo)), (LOGO_PLACEHOLDER, render_logo(site.logo))):
        if marker in template:
            template = template.replace(marker, html, 1)
        else:
            logger.error('%s に %s がありません。この部分を表示せずに返します。', filename, marker)
    return template
