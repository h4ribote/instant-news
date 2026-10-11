"""pydantic models for the HTTP API's requests and responses.

IDs always cross the wire as strings. A DB BIGINT and a Discord snowflake are both 64-bit, and JavaScript's Number represents integers exactly only up to 2^53: carried as a JSON number, the low digits are silently rewritten, and the corrupted ID does not raise an error but behaves as an article that does not exist or as a different user. Use IdOut for IDs in a response and IdIn for IDs in a request. IdIn refuses numbers because a value that already lost precision on the client cannot be detected here.

Datetimes are handled as naive UTC to match the DB's DATETIME and emitted in JSON as ISO 8601 with a trailing Z (UtcDateTime).

The *Write models of the taxonomy and ads serve both creation (POST) and partial update (PATCH), so every field is optional and the service layer checks the fields that creation requires. Articles split the two into ArticleCreate and ArticleUpdate, so the required fields and the non-nullable fields are stated in the types and in the OpenAPI schema. Either way, telling "omitted" from "explicitly set to null" is done with pydantic's model_fields_set (the field name being in payload.model_fields_set means it was specified).
"""

from __future__ import annotations

import unicodedata
from datetime import date, datetime, timezone
from typing import Annotated, Any, Final, Generic, Literal, TypeAlias, TypeVar

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, PlainSerializer, field_validator

from common.settings import SettingKind, SettingRef
from common.snowflake import to_snowflake

# Shared value types


def _validate_id_text(value: str) -> str:
    """Validate an ID string and normalize it to decimal notation."""
    return str(to_snowflake(value))


def _coerce_id_text(value: Any) -> Any:
    """Coerce an ID bound for a response into a string.

    DB rows hand back IDs as int, so absorb them here and make JSON always carry a string. Enforcing str as the type alone would merely turn every site that passes an int into a validation error, leaving no room for a number to leak; absorbing it here makes a numeric ID in a response structurally impossible.
    """
    if isinstance(value, bool):
        raise ValueError('ID に真偽値は使えません。')
    if isinstance(value, int):
        return str(to_snowflake(value))
    return value


# An ID received in a request. Strings only.
IdIn = Annotated[str, AfterValidator(_validate_id_text)]
# An ID emitted in a response. Also accepts int and always normalizes to a string.
IdOut = Annotated[str, BeforeValidator(_coerce_id_text), AfterValidator(_validate_id_text)]

# Unicode categories of characters that draw nothing on screen. Cc is control, Cf is format (the U+200B zero-width space, U+FEFF, bidi controls and the like), Co is private use, Cs is surrogate.
_INVISIBLE_CATEGORIES: Final[frozenset[str]] = frozenset({'Cc', 'Cf', 'Co', 'Cs'})


def visible_text(value: str) -> str:
    """Return the value reduced to its displayable characters. Used to decide whether a value is effectively empty.

    str.strip() is not enough. strip only removes characters for which str.isspace() is true, and U+200B (zero-width space) and U+FEFF are not isspace(), so they survive strip and masquerade as a non-empty string. Yet they draw nothing on screen, so for a field that requires a name for disclosure or identification there is no point telling them apart from a whitespace-only value.

    Folds compatibility characters with NFKC, drops the invisible categories, and strips surrounding whitespace. An empty result means the value shows the reader nothing.
    """
    normalized = unicodedata.normalize('NFKC', value)
    return ''.join(char for char in normalized if unicodedata.category(char) not in _INVISIBLE_CATEGORIES).strip()


def _require_visible_text(value: str) -> str:
    """Require at least one displayable character. The value itself is never rewritten (how it is spelled belongs to whoever wrote it)."""
    if not visible_text(value):
        raise ValueError('表示できる文字を 1 文字以上含めてください。空白やゼロ幅文字だけの値は指定できません。')
    return value


# A string holding at least one displayable character. For fields where showing the reader something is a requirement.
VisibleStr = Annotated[str, AfterValidator(_require_visible_text)]


def _as_naive_utc(value: datetime) -> datetime:
    """Convert an aware value to UTC and drop its tzinfo.

    MySQL's DATETIME carries no timezone. Passing an aware value straight through makes the driver ignore the tzinfo and write the wall-clock value, silently shifting the time. A value without tzinfo is already taken to be UTC.
    """
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _isoformat_z(value: datetime) -> str:
    """Render a naive UTC value as ISO 8601 with a trailing Z."""
    aware = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    return aware.isoformat(timespec='seconds').replace('+00:00', 'Z')


# A datetime. Input is coerced to naive UTC; output is always ISO 8601 with a trailing Z.
UtcDateTime = Annotated[
    datetime,
    AfterValidator(_as_naive_utc),
    PlainSerializer(_isoformat_z, return_type=str, when_used='unless-none'),
]

# The Literals below mirror the DB's ENUMs. The source of truth for the value sets is migrations/0001_initial.sql.
ArticleStatus = Literal['draft', 'in_review', 'scheduled', 'published', 'archived']
Visibility = Literal['public', 'members', 'restricted']
BylineMode = Literal['author', 'publisher']
PublisherStance = Literal['independent', 'state']
Scope = Literal['articles:write', 'articles:write:any', 'ads:admin', 'taxonomy:admin', 'settings:admin']

# The base quantity an article ranking orders on. 'views' ranks by view count (アクセスランキング); 'featured' by the reader-facing score plus replies (注目). Both are then weighted by recency in the query; the weighting is not a value set the client chooses. See app.article_query.fetch_ranked_articles.
ArticleRankingMetric = Literal['views', 'featured']

# A slug appears verbatim as a URL path segment, so it must hold no whitespace and no character that turns into a path or query separator.
SLUG_PATTERN = r'^[^\s/?#&=%]+$'


class _OutModel(BaseModel):
    """Base for response bodies. Buildable directly from a DB row (dict) or a dataclass."""

    model_config = ConfigDict(from_attributes=True)


class _WriteModel(BaseModel):
    """Base for request bodies.

    An unknown key is rejected with 422 rather than silently dropped. If a misspelled field name went through as ignored and unapplied, the sender would read that as success and the mistake would never surface.
    """

    model_config = ConfigDict(extra='forbid')


ItemT = TypeVar('ItemT')


class Page(_OutModel, Generic[ItemT]):
    """A listing response. total is the total count after filtering."""

    items: list[ItemT]
    page: int
    per_page: int
    total: int
    has_next: bool


# Users and roles


class UserBrief(_OutModel):
    """The minimal representation of a user, for an article's byline and for listings of a publisher's reporters."""

    id: IdOut
    discord_user_id: IdOut
    username: str
    global_name: str | None = None
    avatar_hash: str | None = None


class UserOut(UserBrief):
    """Your own information. Guild membership and ownership are exposed because they bear directly on visibility and permissions."""

    is_guild_member: bool
    is_guild_owner: bool


class RoleOut(_OutModel):
    """A Discord role. The permission bitfield is not exposed (whether you are an admin is handed over already resolved, as MeOut.is_admin)."""

    id: IdOut
    name: str
    color: int
    position: int


class AdminRoleOut(RoleOut):
    """The guild roles the admin screen lists as choices.

    is_administrator is the resolved Administrator bit of discord_roles.permissions. **The permission bits themselves are not exposed.** Once the UI starts interpreting bits, the definition of an admin splits between principals.is_admin and the UI, and one of the two is always left behind (the same reason RoleOut withholds the permission bits).

    The UI needs this so the admin roles field (admin.role_ids) can show that a role holding Administrator is treated as an admin without being put there. Without that, adding an already-admin role to the admin roles looks like a useful thing to do, and admin.role_ids fills up with roles nobody can explain the presence of.
    """

    is_administrator: bool


# Taxonomy (categories, tags, publishers, series)


class CategoryOut(_OutModel):
    id: IdOut
    slug: str
    name: str
    position: int
    notify_channel_id: IdOut | None = None
    include_in_latest: bool


class CategoryWrite(_WriteModel):
    slug: str | None = Field(default=None, min_length=1, max_length=64, pattern=SLUG_PATTERN)
    name: str | None = Field(default=None, min_length=1, max_length=64)
    position: int | None = None
    notify_channel_id: IdIn | None = None
    include_in_latest: bool | None = None


class TagOut(_OutModel):
    id: IdOut
    slug: str
    name: str
    description: str | None = None
    icon_url: str | None = None
    notify_channel_id: IdOut | None = None


class TagDetailOut(TagOut):
    """The information shown on the tag page."""

    article_count: int


class TagWrite(_WriteModel):
    slug: str | None = Field(default=None, min_length=1, max_length=64, pattern=SLUG_PATTERN)
    name: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=1000)
    icon_url: str | None = Field(default=None, max_length=1000)
    notify_channel_id: IdIn | None = None


class PublisherOut(_OutModel):
    """A publisher. role_id is the Discord role that marks affiliation; a dissolved publisher has role_id NULL."""

    id: IdOut
    role_id: IdOut | None = None
    slug: str
    name: str
    description: str | None = None
    logo_url: str | None = None
    stance: PublisherStance


class PublisherWrite(_WriteModel):
    role_id: IdIn | None = None
    slug: str | None = Field(default=None, min_length=1, max_length=64, pattern=SLUG_PATTERN)
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=1000)
    logo_url: str | None = Field(default=None, max_length=1000)
    stance: PublisherStance | None = None


class PublisherStats(_OutModel):
    """A publisher's track record. score is the ranking order itself; the rule that computes it is stated on PublisherRankingItem."""

    article_count: int
    up_count: int
    down_count: int
    comment_count: int
    score: int


class PublisherDetailOut(PublisherOut):
    """A publisher's profile, reporters and track record. Affiliation is looked up from the users holding role_id."""

    members: list[UserBrief] = Field(default_factory=list)
    stats: PublisherStats


class PublisherRankingItem(_OutModel):
    """One row of the publisher ranking.

    score is the sum of a value computed per article by the following rule (up-votes as up, down-votes as down, comment count as c).

      - 2 × down ≥ up … the article is being piled on. score = up + c (down-votes are not added)
      - 2 × down < up  … it is not being piled on. score = up + (down ÷ 2, floored) + c (down-votes are added at half weight, as evidence of interest)

    Down-votes never subtract from the score. A down-vote marks "read, and disagreed with", not "not read", so as long as it stays small relative to the up-votes it counts as a measure of attention. An article whose down-votes come close to its up-votes, on the other hand, does not get that attention counted towards the track record.

    The test runs per article because being piled on is a property of the article. Testing it against the publisher's totals would let a single pile-on change the rating of every other article of that publisher at once.

    up_count / down_count / comment_count are the raw totals over the period, not a breakdown of score (score passes through a per-article test, so it cannot be recomputed from these three).
    """

    publisher: PublisherOut
    article_count: int
    up_count: int
    down_count: int
    comment_count: int
    score: int


class SeriesBrief(_OutModel):
    id: IdOut
    slug: str
    title: str


class SeriesOut(SeriesBrief):
    description: str | None = None
    cover_image_url: str | None = None
    owner: UserBrief
    created_at: UtcDateTime
    updated_at: UtcDateTime


# Revision history


class RevisionListItem(_OutModel):
    """An item in the revision listing.

    editor is whose name it was written under; via_token_id is which key actually posted it. The two are different things. Since articles:write:any can publish an article under someone else's byline, accountability cannot be traced without the latter.
    """

    id: IdOut
    revision_number: int
    editor: UserBrief
    via_token_id: IdOut | None = None
    title: str
    excerpt: str | None = None
    note: str | None = None
    is_correction: bool
    created_at: UtcDateTime


class RevisionOut(RevisionListItem):
    """The past revision itself."""

    body_md: str


class RevisionDiffOut(_OutModel):
    """The diff against the immediately preceding revision. diff is a unified diff split into lines."""

    revision_number: int
    previous_revision_number: int | None = None
    diff: list[str]


# Articles


class ArticleListItem(_OutModel):
    """An article as it appears in a listing.

    up_count / down_count / comment_count are the totals across all of the article's notification posts (one article can be notified to several channels). score is up_count − down_count and may be negative. The score shown to readers is this subtraction, a different thing from the publisher ranking's score (for that rule see PublisherRankingItem).
    """

    id: IdOut
    slug: str
    title: str
    excerpt: str | None = None
    cover_image_url: str | None = None
    author: UserBrief
    publisher: PublisherOut | None = None
    category: CategoryOut
    series: SeriesBrief | None = None
    series_position: int | None = None
    tags: list[TagOut] = Field(default_factory=list)
    visibility: Visibility
    byline_mode: BylineMode
    is_breaking: bool
    is_sponsored: bool
    sponsor_name: str | None = None
    status: ArticleStatus
    view_count: int
    up_count: int
    down_count: int
    # The score shown to readers: up_count − down_count, and it may be negative.
    score: int
    comment_count: int
    published_at: UtcDateTime | None = None
    created_at: UtcDateTime
    updated_at: UtcDateTime


class DiscordPostOut(_OutModel):
    """One notification post for an article. One article can be notified to several channels, so the article detail holds an array of these.

    The server assembles thread_url. A Discord thread URL needs the target guild's ID, which exists only in the settings (discord.guild_id); the UI has no way to know it, so the job of assembling the URL cannot be handed to the UI. It is null on rows that are unsent (message_id NULL) or whose thread does not exist yet (thread_id NULL).
    """

    channel_id: IdOut
    message_id: IdOut | None = None
    thread_id: IdOut | None = None
    # The URL for opening the thread. null when thread_id is absent (the bot has not opened the thread yet).
    thread_url: str | None = None
    up_count: int
    down_count: int
    # The score for this notification post alone (up_count − down_count). May be negative.
    score: int
    comment_count: int
    posted_at: UtcDateTime | None = None


class CommentAuthorOut(_OutModel):
    """The commenter.

    Some are not in users (a user who left the guild, another bot, a webhook). For those, all that can be returned is the Discord user ID: username and avatar_hash are both null. The UI has to be built on that premise.
    """

    discord_user_id: IdOut
    username: str | None = None
    global_name: str | None = None
    avatar_hash: str | None = None


class CommentOut(_OutModel):
    """One comment on an article. The real comment is a reply in a Discord thread; this is a mirror of it.

    body holds the Discord message's content in Discord's own markup. The server does not format it (interpreting Discord's markup here would mean carrying a second implementation that disagrees with what Discord displays). The UI must always escape it before rendering.

    score is up_count − down_count and may be negative.
    """

    id: IdOut
    message_id: IdOut
    author: CommentAuthorOut
    body: str
    up_count: int
    down_count: int
    score: int
    posted_at: UtcDateTime
    edited_at: UtcDateTime | None = None


class CommentListOut(_OutModel):
    """The listing of an article's comments.

    total is not the number returned but the article's total comment count (so the UI can decide whether to offer a read-more control). thread_url is the entry point to the Discord thread, the only place a comment can be written (this API only reads, and accepts no writes).
    """

    items: list[CommentOut]
    total: int
    thread_url: str | None = None


class ArticleOut(ArticleListItem):
    """An article's detail. corrections is the listing of revisions recorded as corrections, shown at the top of the article page.

    id is the article's immutable identifier and the one the write endpoints (PATCH, publish) and GET /api/articles/by-id/{id} take; slug is the permalink and may change. version identifies the editable state and is what the ETag header carries; counting a view does not advance it.
    """

    version: int
    body_md: str
    body_html: str | None = None
    visibility_role_ids: list[IdOut] = Field(default_factory=list)
    scheduled_at: UtcDateTime | None = None
    corrections: list[RevisionListItem] = Field(default_factory=list)
    # A reference per notification post. Used by the article page to render the open-thread entry point and the number of notification targets. Held here as the breakdown, separate from the totals up_count / down_count / comment_count.
    discord_posts: list[DiscordPostOut] = Field(default_factory=list)


class SeriesDetailOut(SeriesOut):
    articles: list[ArticleListItem] = Field(default_factory=list)


def _drop_default(schema: dict[str, Any]) -> None:
    """Remove the default from a field's JSON schema. Used for fields whose default None stands for "omitted" rather than for a value a client may send."""
    schema.pop('default', None)


def _omittable(**kwargs: Any) -> Any:
    """A field that may be omitted but never set to null.

    pydantic does not validate defaults, so an omitted field keeps None without error while an explicit null fails the non-nullable type with 422. The None never reaches the service layer as a value: it reads only the fields in model_fields_set.
    """
    return Field(default=None, json_schema_extra=_drop_default, **kwargs)


class _ArticleWriteBase(_WriteModel):
    """What creating and updating an article have in common: the metadata of the revision the save records, and the normalization of the list fields.

    The service layer decides what may be specified at all (permission to flag breaking news, which publisher may be published under, whether someone else can be made the author). Only format can be expressed here; a rule that turns on eligibility cannot be decided without knowing the principal the request came from.

    tags is an array of tag slugs. Slugs rather than IDs, because a slug is an identifier an auto-posting client can write down stably. A slug that does not exist is turned into an error by the service layer (creating tags falls under taxonomy:admin; posting an article does not add them of its own accord).

    sponsor_name tells the reader whose ad it is, so it requires displayable characters. A name of nothing but zero-width characters would put out the Sponsored label alone while withholding the advertiser, meeting the disclosure requirement (is_sponsored=1 requires sponsor_name) in form only.
    """

    # The note left on the revision. For a correction, write what was corrected.
    revision_note: str | None = Field(default=None, max_length=500)
    is_correction: bool = False

    @field_validator('tags', 'visibility_role_ids', check_fields=False)
    @classmethod
    def _unique_items(cls, value: list[str]) -> list[str]:
        """Collapse duplicates. The primary key of article_tags / article_visibility_roles is (article, target), so inserting duplicates hits a duplicate key."""
        return list(dict.fromkeys(value))


class ArticleCreate(_ArticleWriteBase):
    """Request body for creating an article (POST /api/articles).

    title / body_md / category_id are required. The other non-nullable fields default to the same values as the DB columns. slug null means generate one from the title; author_id null means the principal.
    """

    title: str = Field(min_length=1, max_length=300)
    body_md: str
    excerpt: str | None = Field(default=None, max_length=500)
    category_id: IdIn
    publisher_id: IdIn | None = None
    series_id: IdIn | None = None
    series_position: int | None = None
    tags: list[str] = Field(default_factory=list)
    visibility: Visibility = 'public'
    visibility_role_ids: list[IdIn] = Field(default_factory=list)
    cover_image_url: str | None = Field(default=None, max_length=1000)
    byline_mode: BylineMode = 'author'
    is_breaking: bool = False
    is_sponsored: bool = False
    sponsor_name: VisibleStr | None = Field(default=None, max_length=100)
    slug: str | None = Field(default=None, min_length=1, max_length=200, pattern=SLUG_PATTERN)
    status: ArticleStatus = 'draft'
    scheduled_at: UtcDateTime | None = None
    author_id: IdIn | None = None


class ArticleUpdate(_ArticleWriteBase):
    """Request body for a partial update of an article (PATCH /api/articles/{id}).

    Only the fields the request carries are changed; an omitted field keeps its current value. null erases the value of a nullable field (excerpt, publisher_id, series_id, series_position, cover_image_url, sponsor_name, scheduled_at). A field that cannot be null is declared with _omittable, so sending null to it is a 422 rather than an erasure. tags and visibility_role_ids replace the whole set; send [] to empty them.
    """

    title: str = _omittable(min_length=1, max_length=300)
    body_md: str = _omittable()
    excerpt: str | None = Field(default=None, max_length=500)
    category_id: IdIn = _omittable()
    publisher_id: IdIn | None = None
    series_id: IdIn | None = None
    series_position: int | None = None
    tags: list[str] = _omittable()
    visibility: Visibility = _omittable()
    visibility_role_ids: list[IdIn] = _omittable()
    cover_image_url: str | None = Field(default=None, max_length=1000)
    byline_mode: BylineMode = _omittable()
    is_breaking: bool = _omittable()
    is_sponsored: bool = _omittable()
    sponsor_name: VisibleStr | None = Field(default=None, max_length=100)
    slug: str = _omittable(min_length=1, max_length=200, pattern=SLUG_PATTERN)
    status: ArticleStatus = _omittable()
    scheduled_at: UtcDateTime | None = None
    author_id: IdIn = _omittable()


class MediaFailure(_OutModel):
    """An image that could not be ingested while saving an article.

    A URL that failed to ingest stays in the body as it is (a temporary fault at a remote host must never stop an article going out). Since it stays, that URL will eventually expire and the image will vanish, so this is never silently treated as success: the writer is always told.
    """

    source_url: str
    reason: str


class ArticleSaveOut(_OutModel):
    """The response to saving an article."""

    article: ArticleOut
    media_failures: list[MediaFailure] = Field(default_factory=list)


ArticleListOut: TypeAlias = Page[ArticleListItem]


# Me, API keys and subscriptions


class MeOut(_OutModel):
    """Your own information. publishers are the publishers you are affiliated with, looked up from the roles you hold."""

    user: UserOut
    roles: list[RoleOut] = Field(default_factory=list)
    publishers: list[PublisherOut] = Field(default_factory=list)
    is_admin: bool
    subscriptions: list[TagOut] = Field(default_factory=list)


class TokenOut(_OutModel):
    """The representation used in the API key listing. The raw value cannot be returned: it is not in the DB (only the SHA-256 is stored)."""

    id: IdOut
    token_prefix: str
    name: str
    scopes: list[Scope] = Field(default_factory=list)
    created_at: UtcDateTime
    last_used_at: UtcDateTime | None = None
    expires_at: UtcDateTime | None = None
    revoked_at: UtcDateTime | None = None


class TokenCreated(TokenOut):
    """The response right after issuing a key. The raw value is returned in this response only; after that it can never be retrieved again."""

    token: str


class TokenCreate(_WriteModel):
    """A request to issue an API key.

    Only someone who meets admin eligibility at the moment of the request may issue the admin scopes (articles:write:any / ads:admin / taxonomy:admin / settings:admin). The service layer performs that check.

    settings:admin can rewrite the admin roles (admin.role_ids), that is, hand out admin eligibility itself, which makes it stronger than any other scope. A key granted it is effectively a delegation of admin permissions, and its reach cannot be narrowed to ads only or taxonomy only the way the other admin scopes can.
    """

    name: str = Field(min_length=1, max_length=100)
    scopes: list[Scope] = Field(min_length=1)
    # The expiry is capped so that a value beyond DATETIME's range is rejected. Omitted means no expiry.
    expires_days: int | None = Field(default=None, ge=1, le=3650)

    @field_validator('scopes')
    @classmethod
    def _unique_scopes(cls, value: list[Scope]) -> list[Scope]:
        """Collapse duplicates. The primary key of api_token_scopes is (token_id, scope), so inserting duplicates hits a duplicate key."""
        return list(dict.fromkeys(value))


# Images


class MediaOut(_OutModel):
    """An ingested image. url is the delivery URL (/media/{hash}/{variant}.webp)."""

    id: IdOut
    content_hash: str
    url: str


# Ads


class AdSlotOut(_OutModel):
    """An ad slot. The recommended dimensions are returned so that an external management app can reproduce the appearance itself before submitting a creative."""

    id: IdOut
    slug: str
    name: str
    description: str | None = None
    recommended_width: int
    recommended_height: int


class AdOut(_OutModel):
    id: IdOut
    slot: AdSlotOut
    advertiser_name: str
    image_url: str
    link_url: str
    alt_text: str | None = None
    weight: int
    starts_at: UtcDateTime | None = None
    ends_at: UtcDateTime | None = None
    is_active: bool
    created_at: UtcDateTime
    updated_at: UtcDateTime


class AdWrite(_WriteModel):
    """Submitting or updating an ad. Stopping the run is is_active=false, not deleting the row (so the performance stats survive)."""

    slot_id: IdIn | None = None
    advertiser_name: str | None = Field(default=None, min_length=1, max_length=100)
    image_url: str | None = Field(default=None, min_length=1, max_length=1000)
    link_url: str | None = Field(default=None, min_length=1, max_length=1000)
    alt_text: str | None = Field(default=None, max_length=200)
    # The weight for the weighted random selection. Held at 1 or above because allowing 0 would let a state exist where candidates are present yet the weights total 0 (to stop the run, use is_active=false).
    weight: int | None = Field(default=None, ge=1)
    starts_at: UtcDateTime | None = None
    ends_at: UtcDateTime | None = None
    is_active: bool | None = None


class AdServed(_OutModel):
    """One served ad. click_url is returned rather than link_url so that click tracking can be interposed."""

    id: IdOut
    slot: str
    advertiser_name: str
    image_url: str
    click_url: str
    alt_text: str | None = None


class AdServeOut(_OutModel):
    """The serve response for a slot. ad is null when there is no candidate; the frontend then removes the slot itself."""

    ad: AdServed | None = None


class AdStatDay(_OutModel):
    stat_date: date
    impressions: int
    clicks: int


class AdStatsOut(_OutModel):
    ad_id: IdOut
    days: list[AdStatDay] = Field(default_factory=list)
    total_impressions: int
    total_clicks: int


# Platform settings


class SettingOut(_OutModel):
    """One setting's definition and current value. Assembled by SettingsStore.describe in common.settings.

    The UI generates its input fields from this response. The definition (kind, label, description, whether it is editable) is returned together with the value so that the UI never hardcodes keys. Hardcoded, every added setting would have to be fixed in both the registry and the UI, and whichever side was forgotten would be left settable but absent from the UI, or present in the UI but unsavable.

    value is a JSON value matching kind: id is a string, id_list an array of strings. **A setting whose secret is true always has value null and reports only is_set.** The moment the raw value is returned, the bot token is handed to everyone who can open the UI. Eligibility to read the settings and being able to carry off the secret itself are different things.
    """

    key: str
    kind: SettingKind
    label: str
    description: str
    # A setting that cannot be changed. The UI renders it read-only, and description states why it cannot be changed.
    editable: bool
    # A setting the process does not re-read when it changes. Taking effect requires a restart.
    restart_required: bool
    secret: bool
    # Which entity an ID points at (meaningful only for settings whose kind is id / id_list). The UI reads this and offers an entity picker instead of direct ID entry. null means there is no entity whose choices can be listed, so the ID is entered directly.
    ref: SettingRef | None = None
    # The range (meaningful only for settings whose kind is int / number). The UI uses it as the input field's constraint. When exclusive_minimum is true, minimum means greater than, and the value itself is not allowed.
    minimum: float | None = None
    exclusive_minimum: bool = False
    maximum: float | None = None
    # The value, matching kind. Always null for a secret setting.
    value: Any = None
    # Whether a value is present. Exists so that a secret setting can report whether it is set without returning the raw value.
    is_set: bool
    updated_at: UtcDateTime | None = None
    # The user who last changed it. null for the seeding of seed values and for changes from the CLI (neither is a change by a platform user).
    updated_by: IdOut | None = None


class SettingsOut(_OutModel):
    """Every setting. The order is the registry's order, so the order in the UI never varies from request to request."""

    settings: list[SettingOut] = Field(default_factory=list)


class SettingsUpdate(_WriteModel):
    """Request body for a partial update of the settings.

    Only the keys carried in updates are changed; an omitted key keeps its current value. **Being a partial update is also the only way to express leaving a secret setting unchanged.** Since the UI cannot read a secret's raw value, it cannot resend a secret it is not changing, and a send-everything scheme would always corrupt the secrets.

    The value's type, its range and whether that key may be changed are not checked here. Types differ per key, and the source of truth for those definitions is the settings registry (common.settings). Writing the types here would split the definition across two places, and any setting added to the registry but forgotten here would be rejected with 422 before it ever reached the registry's own check. SettingsStore.set_many performs the check, and its ValueError is mapped to 400.
    """

    updates: dict[str, Any]


class SettingsUpdateOut(SettingsOut):
    """The response to a settings update.

    updated_keys are the keys actually changed. settings returns every setting as it stands after the update. Returning only the updated values would leave the UI redrawing from the values it sent, and the results of normalization (removing duplicate IDs, dropping a trailing / from a URL, and so on) would not reach the UI until the next reload.
    """

    updated_keys: list[str] = Field(default_factory=list)
