-- instant-news initial schema.
-- One deployment serves one Discord community (1 deployment = 1 guild). Everything assumes the single Discord server named by discord.guild_id in the settings table (seeded from config.yaml), so no column spans guilds.
-- utf8mb4 throughout, because emoji and multiple languages have to fit. Discord snowflake IDs fit in unsigned 64-bit, so they are held as BIGINT UNSIGNED.
--
-- The Discord role snapshot (discord_roles / user_roles) is used for two things that are handled in exactly opposite ways.
--   Byline (articles.publisher_id): frozen at publication. A reporter transferring never moves the byline of a past article.
--   Permission checks (visibility, API key eligibility): the current snapshot is consulted on every request. A user who loses admin has their keys stop working from that moment.

SET NAMES utf8mb4;

-- Authentication and users

-- Writers and viewers. There is no password of our own: the Discord account is the unique identifier.
-- Guild membership and roles are fetched at login and snapshotted here, and the visibility check reads them.
-- The bot's reconcile refreshes every guild member to match the current state and then follows join, leave and update events, so members who have never logged in appear here too (articles:write:any names someone else as the author, and that person has to exist).
CREATE TABLE users (
    id                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    discord_user_id   BIGINT UNSIGNED NOT NULL,                 -- Discord user snowflake.
    username          VARCHAR(32)     NOT NULL,                 -- Discord username (e.g. someone).
    global_name       VARCHAR(32)     NULL,                     -- Discord display name (NULL when unset).
    avatar_hash       VARCHAR(64)     NULL,                     -- Hash used to build the avatar URL.
    is_guild_member   TINYINT(1)      NOT NULL DEFAULT 0,       -- Whether the user is currently in the target guild. Every non-public visibility check rests on it: members and restricted alike require current membership. The bot updates it immediately on join and leave events, and reconcile corrects the drift.
    is_guild_owner    TINYINT(1)      NOT NULL DEFAULT 0,       -- Whether the user is the guild owner. An owner holds full authority regardless of roles, so the admin check needs this. The bot mirrors it from guild update events (ownership handover) and reconcile refreshes it to match the current state.
    created_at        DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at        DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    last_login_at     DATETIME        NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_users_discord_user_id (discord_user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Cache of the target guild's role listing. Used by the role picker for access restriction and to resolve display names.
-- The source of truth is Discord: the bot mirrors role create, update and delete events immediately. Against missed events it also reconciles every role periodically and corrects the drift.
-- permissions is Discord's permission bitfield. It is needed to decide eligibility to issue an API key from the Administrator permission; without it there is no way to know whether a role is an administrator.
CREATE TABLE discord_roles (
    id          BIGINT UNSIGNED NOT NULL,                       -- Discord role snowflake.
    name        VARCHAR(100)    NOT NULL,
    color       INT UNSIGNED    NOT NULL DEFAULT 0,             -- Discord role color (24-bit RGB).
    position    INT             NOT NULL DEFAULT 0,             -- Ordering within Discord.
    permissions BIGINT UNSIGNED NOT NULL DEFAULT 0,             -- Discord permission bitfield. Administrator is 1 << 3.
    synced_at   DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP, -- When an event or a reconcile last wrote this row.
    PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Snapshot of the guild roles a user holds. Refreshed to match the current state at login, after which the bot follows member update events. A missed event is corrected by the bot's reconcile.
CREATE TABLE user_roles (
    user_id   BIGINT UNSIGNED NOT NULL,
    role_id   BIGINT UNSIGNED NOT NULL,
    PRIMARY KEY (user_id, role_id),
    KEY idx_user_roles_role_id (role_id),
    CONSTRAINT fk_user_roles_user FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE,
    CONSTRAINT fk_user_roles_role FOREIGN KEY (role_id) REFERENCES discord_roles (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Server-side session. The cookie carries a random opaque token and the DB stores its SHA-256; the raw value is never kept, so a leak of this table cannot reconstruct a session directly.
CREATE TABLE sessions (
    token_hash    CHAR(64)        NOT NULL,                     -- SHA-256 of the session token (64 hex digits).
    user_id       BIGINT UNSIGNED NOT NULL,
    created_at    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at    DATETIME        NOT NULL,
    PRIMARY KEY (token_hash),
    -- Session lookup goes by token_hash (the primary key) and expires_at is checked on that row. Expired rows are swept when their user next logs in, deleting only that user's own rows by user_id. So no query path reaches rows across all users on expires_at alone, and there is no index for it; if a sweep over the whole table is ever added, add the index along with it.
    KEY idx_sessions_user_id (user_id),
    CONSTRAINT fk_sessions_user FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Transient state during the OAuth2 authorization flow: the anti-CSRF state and the PKCE code_verifier, held briefly.
CREATE TABLE oauth_states (
    state          CHAR(64)      NOT NULL,                      -- Random anti-CSRF state.
    code_verifier  VARCHAR(128)  NULL,                          -- PKCE code_verifier.
    redirect_to    VARCHAR(512)  NULL,                          -- Redirect target path after login.
    created_at     DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at     DATETIME      NOT NULL,
    PRIMARY KEY (state),
    KEY idx_oauth_states_expires_at (expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Platform settings

-- The source of truth for setting values. config.yaml holds only the values needed before settings can be read from the DB (server, database) and the values that are part of the runtime environment and decided outside the container (the image storage path, which means nothing unless it matches the named volume's mount point); every other setting lives in this table, and an admin can change it from the frontend.
--
-- The matching values in config.yaml are read as seed values at first boot only and are never consulted again. Overwriting from the config file on every boot would silently revert a value changed from the frontend at the next restart, so one side has to be authoritative.
--
-- Values are JSON because settings are not of one type: integers, strings, arrays of role IDs. Type, default, validation, whether it is secret and whether a change requires a restart are all defined by the code-side registry (common/settings.py); the table holds the value alone.
-- Keys absent from the registry are never read or written, so stale rows left here are ignored.
CREATE TABLE settings (
    setting_key  VARCHAR(64)     NOT NULL,                     -- Setting identifier (e.g. discord.bot.token).
    value        JSON            NOT NULL,                     -- The value. The registry definition decides the type, and it is validated on write.
    updated_at   DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    updated_by   BIGINT UNSIGNED NULL,                         -- The user who last changed it (NULL when a seed value was bootstrapped).
    PRIMARY KEY (setting_key),
    KEY idx_settings_updated_by (updated_by),
    CONSTRAINT fk_settings_updated_by FOREIGN KEY (updated_by) REFERENCES users (id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- API keys (authentication for machine clients)

-- Credentials for the machine clients that post articles automatically and administer the platform.
-- A key belongs to the user who issued it. Rather than adding accounts of its own, the machine borrows an existing user's permissions, so responsibility always stays with a human.
-- The raw value is not kept; only its SHA-256 is stored, following the same design as sessions.
-- Eligibility at issue time must not be frozen: permissions are decided on every request from user_roles / users.
CREATE TABLE api_tokens (
    id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    user_id       BIGINT UNSIGNED NOT NULL,                     -- The issuer. Used both for permission checks and as the default byline.
    token_hash    CHAR(64)        NOT NULL,                     -- SHA-256 of the key (64 hex digits).
    token_prefix  VARCHAR(12)     NOT NULL,                     -- The leading characters in the clear, so the key can be identified in the listing.
    name          VARCHAR(100)    NOT NULL,                     -- A name that says what it is for (for example market-prices auto-post, or ad management app).
    created_at    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_used_at  DATETIME        NULL,
    expires_at    DATETIME        NULL,
    revoked_at    DATETIME        NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_api_tokens_token_hash (token_hash),
    KEY idx_api_tokens_user_id (user_id),
    CONSTRAINT fk_api_tokens_user FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- The scopes granted to a key. Eligibility to issue (who may hand out a key) and scope (what the key may do) are separate axes.
--   articles:write      Create and update articles. The author is fixed to the key's owner. A poster can issue this for themselves.
--   articles:write:any  Create and update articles naming any author and publisher. Only an admin can issue it.
--   ads:admin           Register, update and stop ads, and read their performance stats. Only an admin can issue it.
--   taxonomy:admin      Manage categories, tags and publishers. Only an admin can issue it.
--   settings:admin      Read and change the platform settings (the settings table). Only an admin can issue it.
-- The scopes an admin can issue are kept separate rather than bundled into one. An ad management app is given ads:admin and nothing else; the power to forge an article's byline (articles:write:any) is a different scope.
-- settings:admin can rewrite admin.role_ids, that is, hand out admin eligibility itself, which makes it stronger than any other scope. Grant it knowing that it is effectively delegating admin permissions.
CREATE TABLE api_token_scopes (
    token_id  BIGINT UNSIGNED NOT NULL,
    scope     ENUM('articles:write','articles:write:any','ads:admin','taxonomy:admin','settings:admin') NOT NULL,
    PRIMARY KEY (token_id, scope),
    CONSTRAINT fk_api_token_scopes_token FOREIGN KEY (token_id) REFERENCES api_tokens (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Idempotency-Key records for the POSTs that create a resource (an article, an ad). A client that lost the response to a network fault resends with the same key and gets the resource the first request created instead of a duplicate.
-- The row is written in the same transaction as the resource, so a key is recorded exactly when its resource exists. A concurrent resend waits on the unique index until the first commits and then replays it.
-- The scope is the user, not the credential: a session and the user's keys follow one rule, and the foreign key stays exact. Keys are expected to be random (a UUID), so two requests of one user never pick the same value by chance.
-- Rows are kept for 24 hours; the scheduler sweeps older ones, and a lookup ignores them.
CREATE TABLE idempotency_keys (
    id               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    user_id          BIGINT UNSIGNED NOT NULL,                  -- The user the request was made as.
    endpoint         VARCHAR(64)     NOT NULL,                  -- The operation the key belongs to (for example POST /api/articles).
    idempotency_key  VARCHAR(255)    NOT NULL,                  -- The Idempotency-Key header value.
    request_hash     CHAR(64)        NOT NULL,                  -- SHA-256 of the normalized request body. A resend with a different body under the same key is refused.
    resource_id      BIGINT UNSIGNED NOT NULL,                  -- The ID of the resource the first request created.
    replay           JSON            NULL,                      -- Response parts that cannot be rebuilt from the resource (the images an article save could not ingest).
    created_at       DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_idempotency_keys_user_endpoint_key (user_id, endpoint, idempotency_key),
    KEY idx_idempotency_keys_created_at (created_at),
    CONSTRAINT fk_idempotency_keys_user FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Images

-- Catalogue of ingested images. The blob itself sits content-addressed on the filesystem (media/{hash[0:2]}/{hash[2:4]}/{hash}.{variant}.webp).
-- The received bytes are never stored as they arrived: only the result of decoding them and re-encoding to WebP is. That destroys polyglot files, drops EXIF, and lets us decide the Content-Type we serve.
-- content_hash is the SHA-256 of the converted WebP, not a hash of the original pre-conversion file, so it matches what is actually served.
-- Public versus non-public is decided when serving and does not split storage: the same bytes can be both a public cover image and an image inside a body, and splitting storage would duplicate the blob.
CREATE TABLE media (
    id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    content_hash  CHAR(64)        NOT NULL,                     -- SHA-256 of the converted WebP (the full-size rendition). Decides the storage path and the URL.
    width         INT UNSIGNED    NOT NULL,
    height        INT UNSIGNED    NOT NULL,
    byte_size     BIGINT UNSIGNED NOT NULL,                     -- Byte size of the full-size rendition. Used to tally quota usage.
    is_public     TINYINT(1)      NOT NULL DEFAULT 0,           -- 1 means served without authentication (cover images, ads, logos, icons).
    is_animated   TINYINT(1)      NOT NULL DEFAULT 0,           -- Whether this is an animated WebP. No variants are generated for one, so serving has to tell them apart.
    is_lossless   TINYINT(1)      NOT NULL DEFAULT 0,           -- Whether it was encoded losslessly (line art such as logos and icons).
    source_url    VARCHAR(1000)   NULL,                         -- Where it was ingested from, when it was fetched at save time. Kept for tracing.
    uploaded_by   BIGINT UNSIGNED NOT NULL,
    via_token_id  BIGINT UNSIGNED NULL,                         -- The issuing key when it came in through the API (NULL from the web UI).
    created_at    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_media_content_hash (content_hash),
    KEY idx_media_uploaded_by (uploaded_by),
    -- No index on source_url. Dedupe keys on the post-conversion content_hash rather than the ingest URL (the same image arriving from two URLs collapses to one blob), so no query path reaches a row through this column; when the ingest source is wanted, reading the column off a row already looked up is enough. A 255-character prefix of VARCHAR(1000) runs to 1020 bytes per entry under utf8mb4, and every image ingest would pay that cost for an index no one reads.
    CONSTRAINT fk_media_uploader FOREIGN KEY (uploaded_by)  REFERENCES users (id)      ON DELETE RESTRICT,
    CONSTRAINT fk_media_token    FOREIGN KEY (via_token_id) REFERENCES api_tokens (id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Taxonomy (publishers, categories, tags, series)

-- The publishers that publish articles. Affiliation is expressed by a Discord role, so it is referenced through role_id.
-- A role yields only its members, name, color and ordering; the logo, slug, description and stance cannot be derived from it, which is why this table exists. The role is referenced rather than made the primary key.
-- role_id is not ON DELETE CASCADE. Deleting a role in Discord makes the bot drop the discord_roles row on the role delete event, and under CASCADE the publisher would vanish with it and every past article's byline would be lost. The past articles of a dissolved publisher are meant to survive.
CREATE TABLE publishers (
    id           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    role_id      BIGINT UNSIGNED NULL,                          -- The Discord role that expresses affiliation. Nullable because a publisher outlives its dissolution.
    slug         VARCHAR(64)     NOT NULL,
    name         VARCHAR(100)    NOT NULL,                      -- Display name, held independently of the role name.
    description  VARCHAR(1000)   NULL,
    logo_url     VARCHAR(1000)   NULL,
    stance       ENUM('independent','state') NOT NULL DEFAULT 'independent',  -- Stance. state covers state media and the like; the article page carries a label.
    created_at   DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_publishers_slug (slug),
    UNIQUE KEY uq_publishers_role_id (role_id),
    CONSTRAINT fk_publishers_role FOREIGN KEY (role_id) REFERENCES discord_roles (id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- The article's primary classification. An article always has exactly one category (articles.category_id is NOT NULL), so which section an article belongs to has a single answer and notification routing never multiplies.
-- The set of categories differs from deployment to deployment, so the core does not fix it: it is edited through the admin API under the taxonomy:admin scope.
-- It lives in the DB rather than the config file because articles.category_id is a foreign key to this entity, and referential integrity cannot be kept against a config file.
CREATE TABLE categories (
    id                 BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    slug               VARCHAR(64)     NOT NULL,
    name               VARCHAR(64)     NOT NULL,
    position           INT             NOT NULL DEFAULT 0,      -- Ordering in the navigation.
    notify_channel_id  BIGINT UNSIGNED NULL,                    -- The primary channel this section's articles go to (unset means the section is not notified).
    include_in_latest  TINYINT(1)      NOT NULL DEFAULT 1,      -- 0 keeps the section out of the home page's latest listing, so recurring articles such as market prices do not fill the site-wide latest feed.
    PRIMARY KEY (id),
    UNIQUE KEY uq_categories_slug (slug),
    KEY idx_categories_position (position)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- A classification axis other than the primary classification. An article can carry several tags (article_tags).
-- A tag with notify_channel_id set becomes an additional destination channel on top of the primary channel the category decides.
-- The recipients are the deduplicated set of the category's and the tags' notify_channel_id, and each channel receives exactly one message.
-- description and icon_url are shown on the tag page, the topic's own hub.
CREATE TABLE tags (
    id                 BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    slug               VARCHAR(64)     NOT NULL,
    name               VARCHAR(64)     NOT NULL,
    description        VARCHAR(1000)   NULL,                    -- Description shown on the tag page.
    icon_url           VARCHAR(1000)   NULL,                    -- Icon shown on the tag page and in listings.
    notify_channel_id  BIGINT UNSIGNED NULL,                    -- An additional Discord channel that receives the latest articles carrying this tag (unset means the tag takes no part in routing).
    PRIMARY KEY (id),
    UNIQUE KEY uq_tags_slug (slug)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Tag subscriptions. Where tags.notify_channel_id pushes per channel, this pushes per person: the bot DMs the subscriber when an article carrying a subscribed tag is published.
-- It works for a small tag that has no channel of its own, so the two are not exclusive.
CREATE TABLE tag_subscriptions (
    user_id     BIGINT UNSIGNED NOT NULL,
    tag_id      BIGINT UNSIGNED NOT NULL,
    created_at  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, tag_id),
    KEY idx_tag_subscriptions_tag_id (tag_id),
    CONSTRAINT fk_tag_subscriptions_user FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE,
    CONSTRAINT fk_tag_subscriptions_tag  FOREIGN KEY (tag_id)  REFERENCES tags (id)  ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Series. An axis for an ordered group of articles, coexisting alongside categories and tags.
CREATE TABLE series (
    id               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    slug             VARCHAR(64)     NOT NULL,
    title            VARCHAR(200)    NOT NULL,
    description      VARCHAR(1000)   NULL,
    cover_image_url  VARCHAR(1000)   NULL,
    owner_id         BIGINT UNSIGNED NOT NULL,
    created_at       DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at       DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_series_slug (slug),
    KEY idx_series_owner_id (owner_id),
    CONSTRAINT fk_series_owner FOREIGN KEY (owner_id) REFERENCES users (id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Articles

-- The article itself. The body is held as GitHub Flavored Markdown source and the display HTML is a cache of the render; body_md is authoritative because the HTML can always be regenerated from it.
-- status is the state transition draft -> in review -> (scheduled) -> published -> archived.
-- visibility is the Discord-role-based visibility:
--   public      anyone, including logged-out visitors
--   members     only logged-in users who are in the target guild
--   restricted  only logged-in users in the target guild who also hold one of the roles listed in article_visibility_roles
-- The cover image (cover_image_url) is the exception: it goes into the Discord embed, so it is served without authentication. Even on a restricted article the cover is visible to anyone who knows its URL, so never choose a cover that gives away the contents of a confidential article. Images inside the body go through article_media and get the same check as the body.
-- publisher_id is frozen at publication. Deriving the publisher from the author's current roles would rewrite the bylines of past articles the moment a reporter transfers.
-- version cannot be replaced by article_revisions.revision_number: publishing (by hand or by the scheduler) rewrites status, publisher_id and published_at without creating a revision.
CREATE TABLE articles (
    id               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    slug             VARCHAR(200)    NOT NULL,                  -- Unique slug for the permalink.
    author_id        BIGINT UNSIGNED NOT NULL,                  -- Where responsibility rests. Even a machine-posted article is always tied to a human.
    publisher_id     BIGINT UNSIGNED NULL,                      -- The publisher that published it. Frozen at publication.
    category_id      BIGINT UNSIGNED NOT NULL,                  -- Primary classification. Exactly one, required.
    series_id        BIGINT UNSIGNED NULL,
    series_position  INT             NULL,                      -- Position within the series.
    title            VARCHAR(300)    NOT NULL,
    excerpt          VARCHAR(500)    NULL,                      -- Excerpt. Shared by the listing, the Discord embed and the lede on the article page.
    body_md          LONGTEXT        NOT NULL,                  -- GFM source (authoritative).
    body_html        LONGTEXT        NULL,                      -- Cache of the rendered HTML.
    cover_image_url  VARCHAR(1000)   NULL,
    status           ENUM('draft','in_review','scheduled','published','archived') NOT NULL DEFAULT 'draft',
    visibility       ENUM('public','members','restricted')     NOT NULL DEFAULT 'public',
    byline_mode      ENUM('author','publisher') NOT NULL DEFAULT 'author',  -- publisher displays the publisher name only. author_id is retained and stays traceable in an audit.
    is_breaking      TINYINT(1)      NOT NULL DEFAULT 0,        -- Breaking news. The notification mentions the configured role.
    is_sponsored     TINYINT(1)      NOT NULL DEFAULT 0,        -- A sponsored article. The core draws the Sponsored label so that it cannot be removed.
    sponsor_name     VARCHAR(100)    NULL,                      -- Whose ad it is. Required when is_sponsored=1: the Sponsored label on its own is not a disclosure.
    view_count       BIGINT UNSIGNED NOT NULL DEFAULT 0,
    version          INT UNSIGNED    NOT NULL DEFAULT 1,        -- Version of the editable state, carried by the ETag. Every save and every publication that changes the row advances it; counting a view does not.
    scheduled_at     DATETIME        NULL,                      -- When it is due to publish, for status=scheduled.
    published_at     DATETIME        NULL,                      -- When it was first published.
    created_at       DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at       DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_articles_slug (slug),
    -- Listings filtered by author or by publisher both carry status='published' and sort by published_at descending. Taking status and published_at into the index lets the filter and the sort come from the index alone (equality on status, ordering on published_at). author_id / publisher_id stay leftmost, so these double as the index the foreign key requires.
    KEY idx_articles_author_status_published_at (author_id, status, published_at),
    KEY idx_articles_publisher_status_published_at (publisher_id, status, published_at),
    -- A series listing pushes NULL series_position to the end before sorting, which does not match the index order (NULLs first), so adding series_position would not serve the sort; this stays an index for the foreign key only.
    KEY idx_articles_series_id (series_id),
    -- The main query path for the latest listing. The primary key (id) is implicitly appended, so scanning this index backwards yields published_at descending and, within the same instant, id descending, exactly matching the listing's ORDER BY published_at DESC, id DESC. A descending index would leave id ascending and break the ordering of ties, so it is held ascending.
    KEY idx_articles_status_published_at (status, published_at),
    -- The query path by which the navigation counts articles per section (equality on category_id and status). For a ?category= listing it also carries the published_at ordering.
    KEY idx_articles_category_status_published_at (category_id, status, published_at),
    -- Scheduled publishing picks up rows with status='scheduled' whose scheduled_at is at or before now, in scheduled_at order. An index on scheduled_at alone would narrow nothing: scheduled_at is left in place after publication, so the range would sweep in every past schedule, another index leading with status would be chosen instead, and this one would go unused while still making writes heavier. Leading with status keeps both the range and the ordering inside the index, and this query, which reads nothing but id, never touches the row body.
    KEY idx_articles_status_scheduled_at (status, scheduled_at),
    FULLTEXT KEY ftx_articles_title_body (title, body_md) WITH PARSER ngram,  -- Full-text search, Japanese included.
    CONSTRAINT fk_articles_author    FOREIGN KEY (author_id)    REFERENCES users (id)      ON DELETE RESTRICT,
    CONSTRAINT fk_articles_publisher FOREIGN KEY (publisher_id) REFERENCES publishers (id) ON DELETE RESTRICT,
    CONSTRAINT fk_articles_category  FOREIGN KEY (category_id)  REFERENCES categories (id) ON DELETE RESTRICT,
    CONSTRAINT fk_articles_series    FOREIGN KEY (series_id)    REFERENCES series (id)     ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Slugs an article used to have. /articles/{old} and /api/articles/{old}/... answer 301 to the current slug, so links already handed out (the Discord embed among them) keep working after a slug change.
-- A slug held here is reserved for its article: no other article may take it, so an old URL never comes to point at a different article. The article itself may take it back, which deletes the row.
CREATE TABLE article_slug_history (
    slug        VARCHAR(200)    NOT NULL,
    article_id  BIGINT UNSIGNED NOT NULL,
    created_at  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,  -- When the article moved off this slug.
    PRIMARY KEY (slug),
    KEY idx_article_slug_history_article_id (article_id),
    CONSTRAINT fk_article_slug_history_article FOREIGN KEY (article_id) REFERENCES articles (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Many-to-many between articles and tags.
CREATE TABLE article_tags (
    article_id  BIGINT UNSIGNED NOT NULL,
    tag_id      BIGINT UNSIGNED NOT NULL,
    PRIMARY KEY (article_id, tag_id),
    KEY idx_article_tags_tag_id (tag_id),
    CONSTRAINT fk_article_tags_article FOREIGN KEY (article_id) REFERENCES articles (id) ON DELETE CASCADE,
    CONSTRAINT fk_article_tags_tag     FOREIGN KEY (tag_id)     REFERENCES tags (id)     ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- For a visibility=restricted article, the set of Discord roles allowed to read it. Holding one of these roles is necessary but not sufficient: server/app/visibility.py requires current guild membership before the role, because a user who has left the guild can leave a user_roles row behind and would otherwise go on reading.
-- A publisher is itself a Discord role, so naming that publisher's role here yields an article readable only inside that publisher.
CREATE TABLE article_visibility_roles (
    article_id  BIGINT UNSIGNED NOT NULL,
    role_id     BIGINT UNSIGNED NOT NULL,
    PRIMARY KEY (article_id, role_id),
    KEY idx_article_visibility_roles_role_id (role_id),
    CONSTRAINT fk_avr_article FOREIGN KEY (article_id) REFERENCES articles (id)      ON DELETE CASCADE,
    CONSTRAINT fk_avr_role    FOREIGN KEY (role_id)    REFERENCES discord_roles (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Revision history of an article. Every edit appends a snapshot of the title, body and excerpt, which is what makes restore and diff display possible.
-- via_token_id is the audit trail of who actually posted; editor_id is whose name it was written under. The two are different things, and since articles:write:any can publish an article under someone else's byline, accountability cannot be traced without via_token_id.
-- is_correction separates a plain update from a correction. A corrected article says so at the top of the article page.
CREATE TABLE article_revisions (
    id               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    article_id       BIGINT UNSIGNED NOT NULL,
    revision_number  INT             NOT NULL,                  -- Sequential from 1 within the article.
    editor_id        BIGINT UNSIGNED NOT NULL,
    via_token_id     BIGINT UNSIGNED NULL,                      -- The issuing key when it came in through the API (NULL for an edit from the web UI).
    title            VARCHAR(300)    NOT NULL,
    body_md          LONGTEXT        NOT NULL,
    excerpt          VARCHAR(500)    NULL,
    note             VARCHAR(500)    NULL,                      -- Optional note on the edit. For a correction, it records what was corrected.
    is_correction    TINYINT(1)      NOT NULL DEFAULT 0,        -- Whether this is a correction. Distinguished from a plain update and shown at the top of the article page.
    created_at       DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_article_revisions_article_number (article_id, revision_number),
    KEY idx_article_revisions_editor_id (editor_id),
    -- The per-API-key rate limit counts the revisions that key created in the last hour and takes the timestamp of the oldest. Taking created_at into the index keeps both the time-range filter and the count inside the index, with no need to read the row body. via_token_id stays leftmost, so this doubles as the index the foreign key requires.
    KEY idx_article_revisions_via_token_id_created_at (via_token_id, created_at),
    CONSTRAINT fk_article_revisions_article FOREIGN KEY (article_id)   REFERENCES articles (id)   ON DELETE CASCADE,
    CONSTRAINT fk_article_revisions_editor  FOREIGN KEY (editor_id)    REFERENCES users (id)      ON DELETE RESTRICT,
    CONSTRAINT fk_article_revisions_token   FOREIGN KEY (via_token_id) REFERENCES api_tokens (id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- The images an article references. Serving decides permissions from this, so it is recorded as fact rather than inferred.
-- Rows are written by scanning body_md when the body is saved, and are never deleted: an edit that drops an image from the body leaves past revisions still referencing it, and a deleted row would 404 the image the moment someone opens the revision history.
CREATE TABLE article_media (
    article_id  BIGINT UNSIGNED NOT NULL,
    media_id    BIGINT UNSIGNED NOT NULL,
    PRIMARY KEY (article_id, media_id),
    KEY idx_article_media_media_id (media_id),
    CONSTRAINT fk_article_media_article FOREIGN KEY (article_id) REFERENCES articles (id) ON DELETE CASCADE,
    CONSTRAINT fk_article_media_media   FOREIGN KEY (media_id)   REFERENCES media (id)    ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Discord integration (notification posts, threads, reactions)

-- The notification recipients when an article is published, and the mapping to the notification message and thread the bot sent.
-- Comments get no table of their own and are delegated to Discord threads, so the reference point to them is held here.
--
-- This table doubles as the send queue (an outbox). At publication the server only resolves the recipients and enqueues rows with message_id = NULL; the bot does the sending and fills message_id in.
-- Pushing events from server to bot directly would miss every event that arrived while the bot was down. With the DB as the queue, the bot picks up the unsent rows once it is back.
--   message_id IS NULL      unsent; what the bot picks up.
--   message_id IS NOT NULL  sent.
--
-- The recipients are the deduplicated set of the notify_channel_id of the article's category and tags, so one article can be notified to several channels. Then this table holds several rows for that article, each keeping its thread and its votes independently, and the article's up/down votes are the sum over all of its rows.
-- An article with zero recipients is notified nowhere; there is no fallback to a default channel.
--
-- A vote is an :arrow_up: (up-vote) or :arrow_down: (down-vote) reaction on the notification message, and no other emoji counts. The bot recounts on Discord's reaction add and remove events and overwrites with the actual count at that moment; it never does delta arithmetic, because some events carry no delta.
-- A row that missed an event is recounted through the same path and corrected by the bot's reconcile, as long as it is within a fixed window after publication.
-- The bot adds both emoji itself first to invite voting, but that one reaction of its own is not counted here.
--
-- up_count and down_count are counts, so they are held unsigned. The subtraction (up-vote minus down-vote) can go negative, so it is not a column: the read side casts to SIGNED (in MySQL, subtracting one unsigned from another fails the statement rather than clamping the value).
--
-- The comment count is not held here. Comments are mirrored one at a time into article_comments and that row count is the only source of truth; a second count of the mirror kept here could disagree with the number of comments the UI lists.
CREATE TABLE article_discord_posts (
    id                 BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    article_id         BIGINT UNSIGNED NOT NULL,
    channel_id         BIGINT UNSIGNED NOT NULL,                -- The Discord channel the notification goes to.
    message_id         BIGINT UNSIGNED NULL,                    -- The notification message the bot sent (NULL while unsent).
    thread_id          BIGINT UNSIGNED NULL,                    -- The thread opened for comments (NULL until created).
    up_count           INT UNSIGNED    NOT NULL DEFAULT 0,      -- Count of :arrow_up: on the notification message, excluding the bot's own one.
    down_count         INT UNSIGNED    NOT NULL DEFAULT 0,      -- Count of :arrow_down: on the notification message, excluding the bot's own one.
    enqueued_at        DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    posted_at          DATETIME        NULL,                    -- When it was sent (NULL while unsent).
    reactions_seeded_at DATETIME       NULL,                    -- When the bot finished adding the vote emoji (NULL until done).
    last_synced_at     DATETIME        NULL,                    -- When the votes were last recounted.
    PRIMARY KEY (id),
    UNIQUE KEY uq_adp_article_channel (article_id, channel_id), -- Prevents double sending on the DB side as well.
    UNIQUE KEY uq_adp_message_id (message_id),                  -- MySQL allows several NULLs in a UNIQUE index, so this coexists with unsent rows.
    -- A post to any thread in the guild arrives as an event and the bot looks a row up by this column every time, so without an index each message in a thread unrelated to any article costs a full table scan. It is UNIQUE because one thread never serves as the comment thread of two notification posts (several NULLs are allowed, so this coexists with rows whose thread is not created yet).
    UNIQUE KEY uq_adp_thread_id (thread_id),
    KEY idx_adp_posted_at (posted_at),
    -- Read as the queue of rows whose emoji seeding was missed. The bot picks up rows with reactions_seeded_at NULL, limited to those whose posted_at falls within a fixed window, in posted_at order, so taking posted_at into the index keeps both the filter and the ordering inside it.
    KEY idx_adp_reactions_seeded_at_posted_at (reactions_seeded_at, posted_at),
    CONSTRAINT fk_adp_article FOREIGN KEY (article_id) REFERENCES articles (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Comments on an article. The real comment is the reply in a Discord thread, and this table is a mirror of it.
-- Since the design deliberately builds no comment intake, no spam defence and no reporting of its own, writes always happen on the Discord side and this table never accepts one. It exists solely so the frontend can read comments.
--
-- The source of truth is Discord: the bot mirrors thread post, edit and delete events immediately. A missed event, for instance while the bot was stopped, is corrected by the bot's reconcile re-reading the thread history.
--
-- author_discord_user_id has no foreign key to users. The person behind a comment is not necessarily a guild member (another bot, a webhook, a user who left), and with a foreign key the whole comment thread would go missing the moment a post that cannot be mirrored appears. The display name and avatar are resolved by LEFT JOINing users, and if there is no match only the Discord user ID is shown.
--
-- body holds Discord's message content verbatim. Reading it requires the message_content privileged intent.
-- An article's visibility extends to its comments: only comments on articles the viewer can read are returned, and the API runs that check.
CREATE TABLE article_comments (
    id                     BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    post_id                BIGINT UNSIGNED NOT NULL,            -- Which notification post's thread the comment was written in.
    article_id             BIGINT UNSIGNED NOT NULL,            -- Reachable through post_id, but copied here because every listing needs the per-article tally. Neither the post nor the article is ever re-pointed afterwards, so the two cannot disagree.
    message_id             BIGINT UNSIGNED NOT NULL,            -- The message within the thread.
    author_discord_user_id BIGINT UNSIGNED NOT NULL,            -- The commenter's Discord user. May be absent from users (see above).
    body                   TEXT            NOT NULL,            -- The body, in Discord's raw markup as posted.
    up_count               INT UNSIGNED    NOT NULL DEFAULT 0,  -- Count of :arrow_up: on this comment, excluding the bot's own one.
    down_count             INT UNSIGNED    NOT NULL DEFAULT 0,  -- Count of :arrow_down: on this comment, excluding the bot's own one.
    posted_at              DATETIME        NOT NULL,            -- When it was posted on Discord.
    edited_at              DATETIME        NULL,                -- When it was last edited (NULL if never edited).
    reactions_seeded_at    DATETIME        NULL,                -- When the bot finished adding the vote emoji (NULL until done).
    last_synced_at         DATETIME        NULL,                -- When the votes were last recounted.
    PRIMARY KEY (id),
    UNIQUE KEY uq_ac_message_id (message_id),                   -- Keeps the same post from being mirrored twice. Reaction events look a row up by this column, so without an index every unrelated reaction anywhere in the guild costs a full table scan.
    KEY idx_ac_article_id_posted_at (article_id, posted_at),    -- Reads an article's comments in time order (the listing's main query path). The primary key (id) is implicitly appended, so comments sharing a posted_at still come out in ascending id order.
    -- When the bot re-reads a thread's history it deletes the rows under this post_id that the pass did not find, narrowed by a message_id range. Taking message_id into the index decides that range from the index alone. post_id stays leftmost, so this doubles as the index the foreign key requires.
    KEY idx_ac_post_id_message_id (post_id, message_id),
    CONSTRAINT fk_ac_post    FOREIGN KEY (post_id)    REFERENCES article_discord_posts (id) ON DELETE CASCADE,
    CONSTRAINT fk_ac_article FOREIGN KEY (article_id) REFERENCES articles (id)              ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- The queue of DMs to tag subscribers. Same outbox design as article_discord_posts: at publication the server enqueues a row per subscriber, and the bot sends it and fills sent_at in.
-- The primary key (article_id, user_id) prevents double sending, just as (article_id, channel_id) does for channel notifications.
-- An article with no destination channel still gets its DMs to subscribers, so this queue is enqueued independently of article_discord_posts.
CREATE TABLE article_subscription_notices (
    article_id   BIGINT UNSIGNED NOT NULL,
    user_id      BIGINT UNSIGNED NOT NULL,
    enqueued_at  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    sent_at      DATETIME        NULL,                          -- When it was sent (NULL while unsent).
    PRIMARY KEY (article_id, user_id),
    KEY idx_article_subscription_notices_user_id (user_id),
    KEY idx_asn_sent_at (sent_at),
    CONSTRAINT fk_asn_article FOREIGN KEY (article_id) REFERENCES articles (id) ON DELETE CASCADE,
    CONSTRAINT fk_asn_user    FOREIGN KEY (user_id)    REFERENCES users (id)    ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Ads

-- Ad slots, referenced by slug from the frontend templates.
-- The recommended dimensions are held so an external management app can reproduce the look itself before a creative is submitted; the core has no preview endpoint.
CREATE TABLE ad_slots (
    id                  BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    slug                VARCHAR(64)     NOT NULL,               -- The identifier the frontend references.
    name                VARCHAR(100)    NOT NULL,
    description         VARCHAR(500)    NULL,                   -- Notes for whoever submits the creative (that the default is a dark theme, and so on).
    recommended_width   INT UNSIGNED    NOT NULL,               -- Recommended creative dimensions.
    recommended_height  INT UNSIGNED    NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_ad_slots_slug (slug)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- The image ads that get served. The core decides the run period itself, so an external management app is not required to bring a scheduler.
-- Advertisers, run plans, billing and the management UI belong to the external application; the core does nothing but serve and track.
CREATE TABLE ads (
    id               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    slot_id          BIGINT UNSIGNED NOT NULL,
    advertiser_name  VARCHAR(100)    NOT NULL,                  -- For display and for reconciling performance stats. The external app manages advertisers.
    image_url        VARCHAR(1000)   NOT NULL,
    link_url         VARCHAR(1000)   NOT NULL,
    alt_text         VARCHAR(200)    NULL,                      -- Alt text, for screen readers and for when the image does not arrive.
    weight           INT UNSIGNED    NOT NULL DEFAULT 1,        -- Weight for the weighted random selection.
    starts_at        DATETIME        NULL,                      -- Start of the run (NULL means immediately).
    ends_at          DATETIME        NULL,                      -- End of the run (NULL means indefinite).
    is_active        TINYINT(1)      NOT NULL DEFAULT 1,
    created_at       DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at       DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_ads_serving (slot_id, is_active, starts_at, ends_at),
    CONSTRAINT fk_ads_slot FOREIGN KEY (slot_id) REFERENCES ad_slots (id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Daily rollup. Recording one row per event would add a row on every impression, so the counts are added up per day (INSERT ... ON DUPLICATE KEY UPDATE). Per-second analysis can be added at the point it turns out to be needed.
CREATE TABLE ad_stats_daily (
    ad_id        BIGINT UNSIGNED NOT NULL,
    stat_date    DATE            NOT NULL,
    impressions  BIGINT UNSIGNED NOT NULL DEFAULT 0,
    clicks       BIGINT UNSIGNED NOT NULL DEFAULT 0,
    PRIMARY KEY (ad_id, stat_date),
    CONSTRAINT fk_ad_stats_daily_ad FOREIGN KEY (ad_id) REFERENCES ads (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- The known ad slots. The frontend templates reference these slugs, so the migration seeds them.
INSERT INTO ad_slots (slug, name, description, recommended_width, recommended_height) VALUES
    ('header',         'ヘッダー',   'トップの見出し直上に出る横長のバナー。既定はダークテーマのため、白背景のロゴは背景に溶ける。', 728,  90),
    ('article_inline', '記事内',     '記事本文の途中に挟まる枠。本文と誤読されない境界と「広告」ラベルをコアが描画する。',       640, 200),
    ('sidebar',        'サイドバー', 'サイドバーの縦長の枠。既定はダークテーマのため、白背景のロゴは背景に溶ける。',             300, 250);
