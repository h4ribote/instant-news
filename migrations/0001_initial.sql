-- instant-news 初期スキーマ。
-- 対象は単一 Discord コミュニティ(1 デプロイ = 1 guild)。settings 表の discord.guild_id(初期値は
-- config.yaml)が示すただ 1 つのサーバーを前提とするため、guild を跨ぐカラムは持たない。
-- 文字コードは絵文字・多言語を扱うため utf8mb4 に統一する。Discord のスノーフレーク ID は
-- 符号なし 64bit に収まるため BIGINT UNSIGNED で保持する。
--
-- Discord のロールのスナップショット(discord_roles / user_roles)は 2 つの用途に使われ、
-- 扱いが正反対である点に注意する。
--   署名(articles.publisher_id) … 公開時に凍結する。記者が移籍しても過去記事の署名は動かさない。
--   権限判定(閲覧範囲・API キーの資格) … 要求のたびに現在のスナップショットを参照する。
--                                        管理者を外れたユーザーのキーはその時点で通らなくなる。

SET NAMES utf8mb4;

-- --------------------------------------------------------------------------
-- 認証・ユーザー
-- --------------------------------------------------------------------------

-- 執筆者・閲覧者。独自パスワードは持たず、Discord アカウントを一意な識別子とする。
-- guild メンバーシップとロールはログイン時に取得してスナップショットし、閲覧権限の判定に用いる。
-- bot は照合で guild の全メンバーを洗い替えし、以後は参加・離脱・更新のイベントで追随するため、
-- 未ログインのメンバーもここに現れる
-- (articles:write:any で他人を著者に指定する際、その相手が実在する必要があるため)。
CREATE TABLE users (
    id                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    discord_user_id   BIGINT UNSIGNED NOT NULL,                 -- Discord のユーザー スノーフレーク。
    username          VARCHAR(32)     NOT NULL,                 -- Discord ユーザー名(例 someone)。
    global_name       VARCHAR(32)     NULL,                     -- Discord の表示名(未設定なら NULL)。
    avatar_hash       VARCHAR(64)     NULL,                     -- アバター URL の組み立てに用いるハッシュ。
    is_guild_member   TINYINT(1)      NOT NULL DEFAULT 0,       -- 対象 guild に現在参加しているか。members 公開の判定に使う。bot が参加・離脱のイベントで即時に更新し、照合で補正する。
    is_guild_owner    TINYINT(1)      NOT NULL DEFAULT 0,       -- guild のオーナーか。オーナーはロールに依らず全権を持つため管理者判定に要る。bot が guild の更新イベント(オーナーの移譲)で写し、照合で洗い替えする。
    created_at        DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at        DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    last_login_at     DATETIME        NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_users_discord_user_id (discord_user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 対象 guild のロール一覧のキャッシュ。閲覧制限のロール選択 UI や表示名の解決に用いる。
-- 真実のソースは Discord 側であり、bot がロールの作成・更新・削除のイベントを受けて即時に写す。
-- イベントを取りこぼした場合に備え、bot は全ロールを定期的に照合して補正する。
-- permissions は Discord の権限ビットフィールド。API キーの発行資格を Administrator 権限で
-- 判定するために要る(これが無いと「このロールは管理者か」を知る手段が無い)。
CREATE TABLE discord_roles (
    id          BIGINT UNSIGNED NOT NULL,                       -- Discord のロール スノーフレーク。
    name        VARCHAR(100)    NOT NULL,
    color       INT UNSIGNED    NOT NULL DEFAULT 0,             -- Discord のロールカラー(24bit RGB)。
    position    INT             NOT NULL DEFAULT 0,             -- Discord 上の並び順。
    permissions BIGINT UNSIGNED NOT NULL DEFAULT 0,             -- Discord の権限ビットフィールド。Administrator は 1 << 3。
    synced_at   DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP, -- この行をイベントまたは照合で最後に書いた時刻。
    PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ユーザーが保有する guild ロールのスナップショット。ログイン時に洗い替えし、以後は bot が
-- メンバーの更新イベントで追随させる。イベントを取りこぼした場合は bot の照合が補正する。
CREATE TABLE user_roles (
    user_id   BIGINT UNSIGNED NOT NULL,
    role_id   BIGINT UNSIGNED NOT NULL,
    PRIMARY KEY (user_id, role_id),
    KEY idx_user_roles_role_id (role_id),
    CONSTRAINT fk_user_roles_user FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE,
    CONSTRAINT fk_user_roles_role FOREIGN KEY (role_id) REFERENCES discord_roles (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- サーバー側セッション。Cookie にはランダムな不透明トークンを載せ、DB にはその SHA-256 を保存する
-- (漏洩時にセッションを直接復元できないようにするため生値は保持しない)。
CREATE TABLE sessions (
    token_hash    CHAR(64)        NOT NULL,                     -- セッショントークンの SHA-256(16 進 64 桁)。
    user_id       BIGINT UNSIGNED NOT NULL,
    created_at    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at    DATETIME        NOT NULL,
    PRIMARY KEY (token_hash),
    -- セッションの照合は token_hash(主キー)で引き、expires_at はその行で確かめる。期限切れの掃除は、その利用者が次にログインしたときに user_id で自分の行だけを消す形で行う。したがって expires_at だけを条件に全利用者の行を横断して引く経路は無く、その索引は持たない(全体を掃く処理を設ける時が来たら、そのときに索引も併せて足す)。
    KEY idx_sessions_user_id (user_id),
    CONSTRAINT fk_sessions_user FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- OAuth2 認可フロー中の一時状態。CSRF 対策の state と PKCE の code_verifier を短命に保持する。
CREATE TABLE oauth_states (
    state          CHAR(64)      NOT NULL,                      -- CSRF 対策のランダム state。
    code_verifier  VARCHAR(128)  NULL,                          -- PKCE の code_verifier。
    redirect_to    VARCHAR(512)  NULL,                          -- 認証後に戻す遷移先パス。
    created_at     DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at     DATETIME      NOT NULL,
    PRIMARY KEY (state),
    KEY idx_oauth_states_expires_at (expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- --------------------------------------------------------------------------
-- プラットフォームの設定
-- --------------------------------------------------------------------------

-- 設定値の真実。config.yaml が持つのは、設定を DB から読むために先に要る値(server・database)と、
-- 実行環境と一体でありコンテナの外で決まる値(画像の保存先。名前付きボリュームのマウント先と一致していなければ
-- 意味を成さない)だけであり、それ以外の設定はこの表が持つ。管理者はフロントエンドから変更できる。
--
-- config.yaml の対応する値は初回起動時の初期値としてのみ読まれ、以後は参照されない。起動のたびに設定ファイルで
-- 上書きすると、フロントエンドから変更した値が次の再起動で黙って元へ戻る。どちらが真実かを一方に決める必要がある。
--
-- 値を JSON で持つのは、設定が整数・文字列・ロール ID の配列と一様でないため。型・既定値・検証・
-- 秘密かどうか・変更に再起動を要するかは、コード側の登録簿(common/settings.py)が定義する。表は値だけを持つ。
-- 登録簿に無いキーは読み書きしないため、この表に古い行が残っていても無視される。
CREATE TABLE settings (
    setting_key  VARCHAR(64)     NOT NULL,                     -- 設定の識別子(例 discord.bot.token)。
    value        JSON            NOT NULL,                     -- 値。型は登録簿の定義が決め、書き込み時に検証する。
    updated_at   DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    updated_by   BIGINT UNSIGNED NULL,                         -- 最後に変更した利用者(初期値の投入なら NULL)。
    PRIMARY KEY (setting_key),
    KEY idx_settings_updated_by (updated_by),
    CONSTRAINT fk_settings_updated_by FOREIGN KEY (updated_by) REFERENCES users (id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- --------------------------------------------------------------------------
-- API キー(機械クライアントの認証)
-- --------------------------------------------------------------------------

-- 記事の自動投稿とプラットフォーム管理を行う機械クライアントの資格情報。
-- キーは発行者ユーザーに紐づく。独自アカウントを増やすのではなく、既存ユーザーの権限を
-- 機械が借りる形にすることで、責任の所在が必ず人間に残る。
-- 生値は保持せず SHA-256 のみを保存する(sessions と同じ設計を踏襲する)。
-- 発行時の資格を凍結してはならない。権限は要求のたびに user_roles / users を参照して判定する。
CREATE TABLE api_tokens (
    id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    user_id       BIGINT UNSIGNED NOT NULL,                     -- 発行者。権限判定と署名の既定値の両方に使う。
    token_hash    CHAR(64)        NOT NULL,                     -- キーの SHA-256(16 進 64 桁)。
    token_prefix  VARCHAR(12)     NOT NULL,                     -- 一覧でキーを識別するための先頭数文字(平文)。
    name          VARCHAR(100)    NOT NULL,                     -- 用途が分かる名前(例 「相場自動投稿」「広告管理アプリ」)。
    created_at    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_used_at  DATETIME        NULL,
    expires_at    DATETIME        NULL,
    revoked_at    DATETIME        NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_api_tokens_token_hash (token_hash),
    KEY idx_api_tokens_user_id (user_id),
    CONSTRAINT fk_api_tokens_user FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- キーに与えたスコープ。発行資格(誰が発行できるか)とスコープ(何ができるか)は別の軸として扱う。
--   articles:write      … 記事の作成・更新。著者はキーの所有者に固定。投稿者が自分で発行できる。
--   articles:write:any  … 著者・報道社を自由に指定した記事の作成・更新。管理者のみが発行できる。
--   ads:admin           … 広告の登録・更新・停止・実績取得。管理者のみが発行できる。
--   taxonomy:admin      … カテゴリ・タグ・報道社の管理。管理者のみが発行できる。
--   settings:admin      … プラットフォームの設定(settings 表)の読み取りと変更。管理者のみが発行できる。
-- 管理者が発行できるスコープを 1 つに束ねてはならない。広告管理アプリに与えるのは ads:admin だけであり、
-- 記事の署名を偽造する力(articles:write:any)を混ぜて渡してはならない。
-- settings:admin は admin.role_ids を書き換えられる、すなわち管理者の資格そのものを配れるため、
-- 他のどのスコープよりも強い。実質的に管理者権限の委譲であることを踏まえて渡すこと。
CREATE TABLE api_token_scopes (
    token_id  BIGINT UNSIGNED NOT NULL,
    scope     ENUM('articles:write','articles:write:any','ads:admin','taxonomy:admin','settings:admin') NOT NULL,
    PRIMARY KEY (token_id, scope),
    CONSTRAINT fk_api_token_scopes_token FOREIGN KEY (token_id) REFERENCES api_tokens (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- --------------------------------------------------------------------------
-- 画像
-- --------------------------------------------------------------------------

-- 取り込んだ画像の登録簿。実体はファイルシステム上に内容アドレスで置く
-- (media/{hash[0:2]}/{hash[2:4]}/{hash}.{variant}.webp)。
-- 受け取ったバイト列をそのまま保存せず、デコードして WebP に再エンコードしたものだけを保存する。
-- これによりポリグロットファイルが破壊され、EXIF が落ち、配信時の Content-Type を自分で決められる。
-- content_hash は変換後 WebP の SHA-256 であり、変換前の原本のハッシュではない(配信するものと一致させる)。
-- 公開・非公開は配信時の判断であり保存は分けない。同じバイト列が公開のカバーにも本文中の画像にも
-- なりうるため、保存を分けると実体が二重化する。
CREATE TABLE media (
    id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    content_hash  CHAR(64)        NOT NULL,                     -- 変換後 WebP(原寸版)の SHA-256。保存先と URL の決定に使う。
    width         INT UNSIGNED    NOT NULL,
    height        INT UNSIGNED    NOT NULL,
    byte_size     BIGINT UNSIGNED NOT NULL,                     -- 原寸版のバイト数。利用量の集計に使う。
    is_public     TINYINT(1)      NOT NULL DEFAULT 0,           -- 1 なら認証なしで配信する(カバー・広告・ロゴ・アイコン)。
    is_animated   TINYINT(1)      NOT NULL DEFAULT 0,           -- アニメーション WebP か。変種を生成しないため配信時に区別する。
    is_lossless   TINYINT(1)      NOT NULL DEFAULT 0,           -- 可逆で符号化したか(ロゴ・アイコンなどの線画)。
    source_url    VARCHAR(1000)   NULL,                         -- 取り込み元(保存時に取得した場合)。追跡用。
    uploaded_by   BIGINT UNSIGNED NOT NULL,
    via_token_id  BIGINT UNSIGNED NULL,                         -- API 経由の場合の発行元キー(Web UI からなら NULL)。
    created_at    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_media_content_hash (content_hash),
    KEY idx_media_uploaded_by (uploaded_by),
    -- source_url には索引を張らない。重複排除は取り込み元の URL ではなく変換後の content_hash で行うため(同じ画像が別々の URL から来ても実体は 1 つに畳む)、この列を条件に行を引く経路が無い。取り込み元を知りたい場面は、既に引き当てた行の列を読むだけで足りる。VARCHAR(1000) の前方 255 文字は utf8mb4 では 1 項目あたり最大 1020 バイトに及び、読む者の無い索引に画像を取り込むたびその費用を払うことになる。
    CONSTRAINT fk_media_uploader FOREIGN KEY (uploaded_by)  REFERENCES users (id)      ON DELETE RESTRICT,
    CONSTRAINT fk_media_token    FOREIGN KEY (via_token_id) REFERENCES api_tokens (id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- --------------------------------------------------------------------------
-- 分類(報道社・カテゴリ・タグ・連載)
-- --------------------------------------------------------------------------

-- 記事を発行する報道社。所属は Discord のロールで表すため role_id で参照する。
-- ロールから導けるのは所属者・名前・色・並び順だけであり、ロゴ・slug・紹介文・立場は導けないため
-- テーブルが要る。ロールを主キーにはせず、参照する形にする。
-- role_id を ON DELETE CASCADE にしてはならない。Discord でロールを削除すると bot がロールの削除
-- イベントを受けて discord_roles の行を消し、CASCADE なら報道社が消滅して過去記事の署名が全て失われる。
-- 解散した報道社の過去記事は残るのが正しい。
CREATE TABLE publishers (
    id           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    role_id      BIGINT UNSIGNED NULL,                          -- 所属を表す Discord ロール。解散後も報道社は残すため NULL 許容。
    slug         VARCHAR(64)     NOT NULL,
    name         VARCHAR(100)    NOT NULL,                      -- 表示名。ロール名とは独立に持つ。
    description  VARCHAR(1000)   NULL,
    logo_url     VARCHAR(1000)   NULL,
    stance       ENUM('independent','state') NOT NULL DEFAULT 'independent',  -- 立場。state は国営メディア等、記事面にラベルを添える。
    created_at   DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_publishers_slug (slug),
    UNIQUE KEY uq_publishers_role_id (role_id),
    CONSTRAINT fk_publishers_role FOREIGN KEY (role_id) REFERENCES discord_roles (id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 記事の主分類。記事は単一のカテゴリを必ず持つ(articles.category_id は NOT NULL)。
-- 「この記事は何欄の記事か」が一意に定まり、通知先の振り分けが多重化しない。
-- カテゴリ集合はデプロイごとに異なるため、コアは固定せず taxonomy:admin スコープの管理用 API で編集する。
-- 設定ファイルではなく DB に置くのは、articles.category_id が外部キーで参照する実体であり、
-- 設定ファイルに置くと参照整合性を保てないため。
CREATE TABLE categories (
    id                 BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    slug               VARCHAR(64)     NOT NULL,
    name               VARCHAR(64)     NOT NULL,
    position           INT             NOT NULL DEFAULT 0,      -- ナビゲーションでの並び順。
    notify_channel_id  BIGINT UNSIGNED NULL,                    -- この欄の記事を流す主チャンネル(未設定なら、この欄は通知しない)。
    include_in_latest  TINYINT(1)      NOT NULL DEFAULT 1,      -- 0 ならトップの新着一覧に載せない。相場のような定期記事が横断的な流れを埋めるのを防ぐ。
    PRIMARY KEY (id),
    UNIQUE KEY uq_categories_slug (slug),
    KEY idx_categories_position (position)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 主分類以外の分類軸。記事は複数のタグを持てる(article_tags)。
-- notify_channel_id を設定したタグは、カテゴリが決める主チャンネルに加えて追加の宛先になる。
-- 宛先はカテゴリとタグの notify_channel_id を重複排除した集合とし、1 チャンネルにつき 1 通だけ送る。
-- description と icon_url はタグページ(話題の特設ページ)に出す。
CREATE TABLE tags (
    id                 BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    slug               VARCHAR(64)     NOT NULL,
    name               VARCHAR(64)     NOT NULL,
    description        VARCHAR(1000)   NULL,                    -- タグページに出す説明文。
    icon_url           VARCHAR(1000)   NULL,                    -- タグページ・一覧に出すアイコン。
    notify_channel_id  BIGINT UNSIGNED NULL,                    -- このタグが付いた記事の新着を送る追加の Discord チャンネル(未設定なら振り分けに関与しない)。
    PRIMARY KEY (id),
    UNIQUE KEY uq_tags_slug (slug)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- タグの購読。チャンネル単位のプッシュ(tags.notify_channel_id)に対し、こちらは個人単位のプッシュ。
-- 購読したタグの記事が公開されると bot が DM を送る。
-- チャンネルを持たない小さなタグに対しても成立するため、両者は排他ではない。
CREATE TABLE tag_subscriptions (
    user_id     BIGINT UNSIGNED NOT NULL,
    tag_id      BIGINT UNSIGNED NOT NULL,
    created_at  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, tag_id),
    KEY idx_tag_subscriptions_tag_id (tag_id),
    CONSTRAINT fk_tag_subscriptions_user FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE,
    CONSTRAINT fk_tag_subscriptions_tag  FOREIGN KEY (tag_id)  REFERENCES tags (id)  ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 連載(マガジン)。順序を持つ記事のまとまりを表す軸で、カテゴリ・タグとは別に併存する。
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

-- --------------------------------------------------------------------------
-- 記事
-- --------------------------------------------------------------------------

-- 記事本体。本文は GitHub Flavored Markdown のソースを保持し、表示用 HTML はレンダリング結果の
-- キャッシュとして持つ(再レンダリング可能なため権威は body_md 側)。
-- status は 下書き→レビュー→(予約)→公開→アーカイブ の状態遷移を表す。
-- visibility は Discord ロールに基づく閲覧範囲を表す:
--   public     … 未ログインを含む誰でも閲覧可
--   members    … 対象 guild に参加しているログイン済みユーザーのみ
--   restricted … article_visibility_roles に列挙したロールのいずれかを持つユーザーのみ
-- ただしカバー画像(cover_image_url)は Discord embed に出すため認証なしで配信される。
-- restricted 記事であってもカバーは URL を知る者に見えるため、機密記事のカバーに中身が分かる画像を
-- 選んではならない。本文中の画像は article_media を介して本文と同じ判定を受ける。
-- publisher_id は公開時に凍結する。著者の現在のロールから報道社を引く実装にすると、記者が移籍した
-- 瞬間に過去記事の署名が遡って書き換わる。
CREATE TABLE articles (
    id               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    slug             VARCHAR(200)    NOT NULL,                  -- パーマリンク用の一意な slug。
    author_id        BIGINT UNSIGNED NOT NULL,                  -- 責任の所在。機械が投稿した記事でも必ず人間に紐づく。
    publisher_id     BIGINT UNSIGNED NULL,                      -- 発行した報道社。公開時に凍結する。
    category_id      BIGINT UNSIGNED NOT NULL,                  -- 主分類。単一必須。
    series_id        BIGINT UNSIGNED NULL,
    series_position  INT             NULL,                      -- 連載内での並び順。
    title            VARCHAR(300)    NOT NULL,
    excerpt          VARCHAR(500)    NULL,                      -- 要約。一覧・Discord embed・記事面のリード文に共通で使う。
    body_md          LONGTEXT        NOT NULL,                  -- GFM ソース(権威)。
    body_html        LONGTEXT        NULL,                      -- レンダリング済み HTML のキャッシュ。
    cover_image_url  VARCHAR(1000)   NULL,
    status           ENUM('draft','in_review','scheduled','published','archived') NOT NULL DEFAULT 'draft',
    visibility       ENUM('public','members','restricted')     NOT NULL DEFAULT 'public',
    byline_mode      ENUM('author','publisher') NOT NULL DEFAULT 'author',  -- publisher なら表示は報道社名のみ。author_id は保持され監査では追える。
    is_breaking      TINYINT(1)      NOT NULL DEFAULT 0,        -- 速報。通知時に設定したロールを mention する。
    is_sponsored     TINYINT(1)      NOT NULL DEFAULT 0,        -- 記事広告(タイアップ記事)。コアが「広告」表示を描画し消せないようにする。
    sponsor_name     VARCHAR(100)    NULL,                      -- 誰の広告か。is_sponsored=1 なら必須(「広告」とだけ書いても開示にならない)。
    view_count       BIGINT UNSIGNED NOT NULL DEFAULT 0,
    scheduled_at     DATETIME        NULL,                      -- status=scheduled のときの公開予定時刻。
    published_at     DATETIME        NULL,                      -- 最初に公開された時刻。
    created_at       DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at       DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_articles_slug (slug),
    -- 著者・報道社で絞る一覧は、いずれも status='published' を伴い published_at の降順に並ぶ。status と published_at まで含めると、絞り込みと並び替えの両方が索引だけで済む(status で等値、published_at で整列)。先頭が author_id / publisher_id であることは変わらないため、外部キーが要求する索引も兼ねる。
    KEY idx_articles_author_status_published_at (author_id, status, published_at),
    KEY idx_articles_publisher_status_published_at (publisher_id, status, published_at),
    -- 連載の一覧は series_position の NULL を末尾へ送ってから並べるため、索引の並び(NULL が先頭)と一致しない。series_position を足しても整列には使えないので、外部キーのための索引に留める。
    KEY idx_articles_series_id (series_id),
    -- 新着一覧の主経路。末尾には主キー(id)が暗黙に付くため、この索引を逆向きに走査すると published_at の降順・同時刻なら id の降順が得られ、一覧の ORDER BY published_at DESC, id DESC と厳密に一致する。降順索引にすると id が昇順のままになり、同時刻の並びが崩れるため昇順で持つ。
    KEY idx_articles_status_published_at (status, published_at),
    -- ナビゲーションが欄ごとの記事数を数える経路(category_id と status の等値)。?category= の一覧では published_at の整列も担う。
    KEY idx_articles_category_status_published_at (category_id, status, published_at),
    -- 公開予約の取り込みは status='scheduled' かつ scheduled_at が現在以前の行を、scheduled_at の順に拾う。scheduled_at だけの索引では、公開後も scheduled_at が残る(公開時に消さない)以上、範囲に過去の予約が全て入って絞り込めず、実際に選ばれるのは status を先頭に持つ別の索引の方であり、この索引は使われないまま書き込みを重くするだけになる。status を先頭に置くと範囲と整列が索引だけで完結し、id しか読まないこの問い合わせは行本体に触れずに済む。
    KEY idx_articles_status_scheduled_at (status, scheduled_at),
    FULLTEXT KEY ftx_articles_title_body (title, body_md) WITH PARSER ngram,  -- 日本語を含む全文検索用。
    CONSTRAINT fk_articles_author    FOREIGN KEY (author_id)    REFERENCES users (id)      ON DELETE RESTRICT,
    CONSTRAINT fk_articles_publisher FOREIGN KEY (publisher_id) REFERENCES publishers (id) ON DELETE RESTRICT,
    CONSTRAINT fk_articles_category  FOREIGN KEY (category_id)  REFERENCES categories (id) ON DELETE RESTRICT,
    CONSTRAINT fk_articles_series    FOREIGN KEY (series_id)    REFERENCES series (id)     ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 記事とタグの多対多。
CREATE TABLE article_tags (
    article_id  BIGINT UNSIGNED NOT NULL,
    tag_id      BIGINT UNSIGNED NOT NULL,
    PRIMARY KEY (article_id, tag_id),
    KEY idx_article_tags_tag_id (tag_id),
    CONSTRAINT fk_article_tags_article FOREIGN KEY (article_id) REFERENCES articles (id) ON DELETE CASCADE,
    CONSTRAINT fk_article_tags_tag     FOREIGN KEY (tag_id)     REFERENCES tags (id)     ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- visibility=restricted の記事について、閲覧を許可する Discord ロールの集合。
-- ここに列挙したロールのいずれかを持つユーザーだけが閲覧できる。
-- 報道社は Discord ロールであるため、その報道社ロールをここに指定すれば社内限定記事が成立する。
CREATE TABLE article_visibility_roles (
    article_id  BIGINT UNSIGNED NOT NULL,
    role_id     BIGINT UNSIGNED NOT NULL,
    PRIMARY KEY (article_id, role_id),
    KEY idx_article_visibility_roles_role_id (role_id),
    CONSTRAINT fk_avr_article FOREIGN KEY (article_id) REFERENCES articles (id)      ON DELETE CASCADE,
    CONSTRAINT fk_avr_role    FOREIGN KEY (role_id)    REFERENCES discord_roles (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 記事の改版履歴。編集のたびにタイトル・本文・抜粋のスナップショットを追記し、復元と差分表示を可能にする。
-- via_token_id は「誰が実際に投稿したか」の監査証跡。editor_id は「誰の名前で書かれたか」であって
-- 両者は別物であり、articles:write:any が他人の署名で記事を出せる以上、これが無いと説明責任が崩れる。
-- is_correction は単なる更新と訂正を区別する。訂正された記事は記事面の冒頭で明示する。
CREATE TABLE article_revisions (
    id               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    article_id       BIGINT UNSIGNED NOT NULL,
    revision_number  INT             NOT NULL,                  -- 記事内で 1 から連番。
    editor_id        BIGINT UNSIGNED NOT NULL,
    via_token_id     BIGINT UNSIGNED NULL,                      -- API 経由の場合の発行元キー(Web UI からの編集なら NULL)。
    title            VARCHAR(300)    NOT NULL,
    body_md          LONGTEXT        NOT NULL,
    excerpt          VARCHAR(500)    NULL,
    note             VARCHAR(500)    NULL,                      -- 編集内容のメモ(任意)。訂正の場合は何を訂正したかを書く。
    is_correction    TINYINT(1)      NOT NULL DEFAULT 0,        -- 訂正か。単なる更新と区別し、記事面の冒頭に出す。
    created_at       DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_article_revisions_article_number (article_id, revision_number),
    KEY idx_article_revisions_editor_id (editor_id),
    -- API キーごとの投稿数の制限は、そのキーが直近 1 時間に作った改版を数え、最も古いものの時刻を求める。created_at まで含めると、期間の絞り込みも数え上げも索引だけで完結し、行本体を読まずに済む。先頭が via_token_id であることは変わらないため、外部キーが要求する索引も兼ねる。
    KEY idx_article_revisions_via_token_id_created_at (via_token_id, created_at),
    CONSTRAINT fk_article_revisions_article FOREIGN KEY (article_id)   REFERENCES articles (id)   ON DELETE CASCADE,
    CONSTRAINT fk_article_revisions_editor  FOREIGN KEY (editor_id)    REFERENCES users (id)      ON DELETE RESTRICT,
    CONSTRAINT fk_article_revisions_token   FOREIGN KEY (via_token_id) REFERENCES api_tokens (id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 記事が参照する画像。配信時の権限判定に使うため、推測ではなく事実として保持する。
-- 本文の保存時に body_md を走査して積む。行を消してはならない。編集で本文から画像を外しても、
-- 過去の改版はその画像を参照し続けており、削除すると改版履歴を開いた瞬間に画像が 404 になる。
CREATE TABLE article_media (
    article_id  BIGINT UNSIGNED NOT NULL,
    media_id    BIGINT UNSIGNED NOT NULL,
    PRIMARY KEY (article_id, media_id),
    KEY idx_article_media_media_id (media_id),
    CONSTRAINT fk_article_media_article FOREIGN KEY (article_id) REFERENCES articles (id) ON DELETE CASCADE,
    CONSTRAINT fk_article_media_media   FOREIGN KEY (media_id)   REFERENCES media (id)    ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- --------------------------------------------------------------------------
-- Discord 連携(通知・スレッド・リアクション)
-- --------------------------------------------------------------------------

-- 記事公開時の通知の宛先と、bot が送った通知メッセージ・スレッドの対応。
-- コメントは専用テーブルを持たず Discord スレッドに委譲するため、その参照点をここで保持する。
--
-- この表は送信の待ち行列(アウトボックス)を兼ねる。server は公開時に宛先を解決して
-- message_id = NULL の行を積むだけで、実際の送信は bot が行い message_id を埋める。
-- server から bot へ直接イベントを送る方式は、bot が落ちている間のイベントを取りこぼす。
-- DB を待ち行列にすれば、bot は復帰後に未送信の行を拾って送れる。
--   message_id IS NULL … 未送信。bot が拾う対象。
--   message_id IS NOT NULL … 送信済み。
--
-- 宛先は記事のカテゴリとタグの notify_channel_id を重複排除した集合であり、1 記事が複数チャンネルへ
-- 通知されうる。その場合この表は記事につき複数行を持ち、通知ごとにスレッドと投票を独立して保持する。
-- 記事としての賛否は、当該記事の全行を合計して求める。
-- 宛先が 0 件の記事はどこにも通知しない(既定チャンネルへのフォールバックは持たない)。
--
-- 投票は通知メッセージに付いた :arrow_up:(賛成)と :arrow_down:(反対)のリアクションであり、
-- それ以外の絵文字は数えない。bot が Discord のイベント(リアクションの増減)を受けて数え直し、
-- その時点の実際の数で上書きする。イベントは差分を運ばないものがあるため加減算はしない。
-- イベントを取りこぼした行は、公開から一定期間内であれば bot の照合が同じ経路で数え直して補正する。
-- bot は投票を促すために自分でも両方の絵文字を最初に付けるが、その 1 件はここには数えない。
--
-- up_count と down_count は個数であるため符号なしで持つ。差し引き(賛成 − 反対)は負になりうるため、
-- 列としては持たず、読み出し側が SIGNED へ寄せて求める(符号なし同士の引き算は MySQL では
-- 値が丸まるのではなく文が失敗する)。
--
-- コメント数はここに持たない。コメントは 1 件ずつ article_comments に写しており、その行数が唯一の真実である。
-- ここに写しの数を別に持つと、画面に並ぶコメントの数と食い違いうる。
CREATE TABLE article_discord_posts (
    id                 BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    article_id         BIGINT UNSIGNED NOT NULL,
    channel_id         BIGINT UNSIGNED NOT NULL,                -- 通知の宛先 Discord チャンネル。
    message_id         BIGINT UNSIGNED NULL,                    -- bot が送った通知メッセージ(未送信なら NULL)。
    thread_id          BIGINT UNSIGNED NULL,                    -- コメント用に開いたスレッド(未作成なら NULL)。
    up_count           INT UNSIGNED    NOT NULL DEFAULT 0,      -- 通知メッセージへの :arrow_up: の数(bot 自身の 1 件を除く)。
    down_count         INT UNSIGNED    NOT NULL DEFAULT 0,      -- 通知メッセージへの :arrow_down: の数(bot 自身の 1 件を除く)。
    enqueued_at        DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    posted_at          DATETIME        NULL,                    -- 送信した時刻(未送信なら NULL)。
    reactions_seeded_at DATETIME       NULL,                    -- bot が投票用の絵文字を付け終えた時刻(未実施なら NULL)。
    last_synced_at     DATETIME        NULL,                    -- 投票を最後に数え直した時刻。
    PRIMARY KEY (id),
    UNIQUE KEY uq_adp_article_channel (article_id, channel_id), -- 二重送信を DB 側でも防ぐ。
    UNIQUE KEY uq_adp_message_id (message_id),                  -- MySQL は UNIQUE 索引に複数の NULL を許すため、未送信の行と両立する。
    -- スレッドへの投稿は guild 中のどのスレッドでもイベントとして届き、bot はその都度この列で行を引く。
    -- 索引が無いと、記事に無関係なスレッドの発言 1 つごとに全表走査になる。UNIQUE なのは、1 つのスレッドが
    -- 2 つの通知のコメント欄を兼ねることがないため(NULL は複数許されるので未作成の行と両立する)。
    UNIQUE KEY uq_adp_thread_id (thread_id),
    KEY idx_adp_posted_at (posted_at),
    -- 絵文字を付け損ねた行を拾い直す待ち行列として引く。bot は reactions_seeded_at が NULL の行を、posted_at が一定期間内のものに限り posted_at の順で拾うため、posted_at まで含めて絞り込みと整列を索引だけで済ませる。
    KEY idx_adp_reactions_seeded_at_posted_at (reactions_seeded_at, posted_at),
    CONSTRAINT fk_adp_article FOREIGN KEY (article_id) REFERENCES articles (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 記事のコメント。実体は Discord スレッドの返信であり、この表はその写しである。
-- コメントの受け口・スパム対策・通報を自前で作らないという方針の帰結として、書き込みは常に Discord 側で行われ、
-- この表が書き込みを受け付けることはない。フロントエンドがコメントを読めるようにするためだけに存在する。
--
-- 真実のソースは Discord であり、bot がスレッドへの投稿・編集・削除のイベントで即時に写す。
-- イベントを取りこぼした場合(bot の停止中など)は、bot の照合がスレッドの履歴を読み直して補正する。
--
-- author_discord_user_id に users への外部キーを張らない。コメントの主は guild のメンバーとは限らず
-- (他の bot・webhook・退会した利用者)、外部キーにすると、写せない投稿が現れた瞬間にコメント欄全体が欠ける。
-- 表示名とアバターは users を LEFT JOIN して解決し、見つからなければ Discord のユーザー ID だけを見せる。
--
-- body は Discord の本文をそのまま持つ。取得には message_content 特権 intent が要る。
-- 記事の閲覧範囲はコメントにも及ぶ(読める記事のコメントだけを返す)。API 側がその判定を行う。
CREATE TABLE article_comments (
    id                     BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    post_id                BIGINT UNSIGNED NOT NULL,            -- どの通知のスレッドに付いたコメントか。
    article_id             BIGINT UNSIGNED NOT NULL,            -- post_id から辿れるが、記事単位の集計が全ての一覧で要るため写しておく。post も記事も後から繋ぎ替わらないため食い違いようがない。
    message_id             BIGINT UNSIGNED NOT NULL,            -- スレッド内のメッセージ。
    author_discord_user_id BIGINT UNSIGNED NOT NULL,            -- 投稿者の Discord ユーザー。users に無いこともある(上記)。
    body                   TEXT            NOT NULL,            -- 本文(Discord の生の記法のまま)。
    up_count               INT UNSIGNED    NOT NULL DEFAULT 0,  -- このコメントへの :arrow_up: の数(bot 自身の 1 件を除く)。
    down_count             INT UNSIGNED    NOT NULL DEFAULT 0,  -- このコメントへの :arrow_down: の数(bot 自身の 1 件を除く)。
    posted_at              DATETIME        NOT NULL,            -- Discord 上の投稿時刻。
    edited_at              DATETIME        NULL,                -- 最後に編集された時刻(未編集なら NULL)。
    reactions_seeded_at    DATETIME        NULL,                -- bot が投票用の絵文字を付け終えた時刻(未実施なら NULL)。
    last_synced_at         DATETIME        NULL,                -- 投票を最後に数え直した時刻。
    PRIMARY KEY (id),
    UNIQUE KEY uq_ac_message_id (message_id),                   -- 同じ投稿を二重に写さない。リアクションのイベントはこの列で行を引くため、索引が無いと guild 中の無関係なリアクション 1 件ごとに全表走査になる。
    KEY idx_ac_article_id_posted_at (article_id, posted_at),    -- 記事のコメントを時系列で引く(一覧の主経路)。末尾に主キー(id)が暗黙に付くため、posted_at が同時刻のコメントも id の昇順に定まる。
    -- bot はスレッドの履歴を読み直す際、この post_id の行のうち今回見つからなかったものを message_id の範囲で絞って消す。message_id まで含めると、その範囲が索引だけで決まる。先頭が post_id であることは変わらないため、外部キーが要求する索引も兼ねる。
    KEY idx_ac_post_id_message_id (post_id, message_id),
    CONSTRAINT fk_ac_post    FOREIGN KEY (post_id)    REFERENCES article_discord_posts (id) ON DELETE CASCADE,
    CONSTRAINT fk_ac_article FOREIGN KEY (article_id) REFERENCES articles (id)              ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- タグ購読者への DM の待ち行列。article_discord_posts と同じアウトボックス方式で、
-- server が公開時に購読者の行を積み、bot が送信して sent_at を埋める。
-- 主キー (article_id, user_id) が、チャンネルへの通知における (article_id, channel_id) と同じく
-- 二重送信を防ぐ。宛先チャンネルを持たない記事でも購読者への DM は成立するため、
-- この待ち行列は article_discord_posts とは独立に積む。
CREATE TABLE article_subscription_notices (
    article_id   BIGINT UNSIGNED NOT NULL,
    user_id      BIGINT UNSIGNED NOT NULL,
    enqueued_at  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    sent_at      DATETIME        NULL,                          -- 送信した時刻(未送信なら NULL)。
    PRIMARY KEY (article_id, user_id),
    KEY idx_article_subscription_notices_user_id (user_id),
    KEY idx_asn_sent_at (sent_at),
    CONSTRAINT fk_asn_article FOREIGN KEY (article_id) REFERENCES articles (id) ON DELETE CASCADE,
    CONSTRAINT fk_asn_user    FOREIGN KEY (user_id)    REFERENCES users (id)    ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- --------------------------------------------------------------------------
-- 広告
-- --------------------------------------------------------------------------

-- 広告枠。フロントエンドのテンプレートが slug で参照する。
-- 推奨寸法を持つのは、外部の管理アプリが入稿前に見え方を自前で再現できるようにするため
-- (コアはプレビュー用エンドポイントを持たない)。
CREATE TABLE ad_slots (
    id                  BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    slug                VARCHAR(64)     NOT NULL,               -- フロントエンドが参照する識別子。
    name                VARCHAR(100)    NOT NULL,
    description         VARCHAR(500)    NULL,                   -- 入稿側へ伝える注意事項(既定がダークテーマである等)。
    recommended_width   INT UNSIGNED    NOT NULL,               -- 推奨するクリエイティブの寸法。
    recommended_height  INT UNSIGNED    NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_ad_slots_slug (slug)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 配信する画像広告。掲載期間はコアが判定するため、外部の管理アプリ側にスケジューラを要求しない。
-- 広告主・掲載プラン・課金・管理 UI は外部アプリケーションの担当であり、コアは配信と計測に徹する。
CREATE TABLE ads (
    id               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    slot_id          BIGINT UNSIGNED NOT NULL,
    advertiser_name  VARCHAR(100)    NOT NULL,                  -- 表示・実績の突き合わせ用。広告主の管理は外部アプリが持つ。
    image_url        VARCHAR(1000)   NOT NULL,
    link_url         VARCHAR(1000)   NOT NULL,
    alt_text         VARCHAR(200)    NULL,                      -- 代替テキスト。読み上げと画像不達時のため。
    weight           INT UNSIGNED    NOT NULL DEFAULT 1,        -- 重み付き抽選の重み。
    starts_at        DATETIME        NULL,                      -- 掲載開始(NULL なら即時)。
    ends_at          DATETIME        NULL,                      -- 掲載終了(NULL なら無期限)。
    is_active        TINYINT(1)      NOT NULL DEFAULT 1,
    created_at       DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at       DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_ads_serving (slot_id, is_active, starts_at, ends_at),
    CONSTRAINT fk_ads_slot FOREIGN KEY (slot_id) REFERENCES ad_slots (id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 日次ロールアップ。イベント 1 行ずつ記録すると表示のたびに行が増えるため、日次で加算する
-- (INSERT ... ON DUPLICATE KEY UPDATE)。秒単位の分析が要ると分かった時点で足せばよい。
CREATE TABLE ad_stats_daily (
    ad_id        BIGINT UNSIGNED NOT NULL,
    stat_date    DATE            NOT NULL,
    impressions  BIGINT UNSIGNED NOT NULL DEFAULT 0,
    clicks       BIGINT UNSIGNED NOT NULL DEFAULT 0,
    PRIMARY KEY (ad_id, stat_date),
    CONSTRAINT fk_ad_stats_daily_ad FOREIGN KEY (ad_id) REFERENCES ads (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 既知の広告枠。フロントエンドのテンプレートがこの slug を参照するため、移行で投入する。
INSERT INTO ad_slots (slug, name, description, recommended_width, recommended_height) VALUES
    ('header',         'ヘッダー',   'トップの見出し直上に出る横長のバナー。既定はダークテーマのため、白背景のロゴは背景に溶ける。', 728,  90),
    ('article_inline', '記事内',     '記事本文の途中に挟まる枠。本文と誤読されない境界と「広告」ラベルをコアが描画する。',       640, 200),
    ('sidebar',        'サイドバー', 'サイドバーの縦長の枠。既定はダークテーマのため、白背景のロゴは背景に溶ける。',             300, 250);
