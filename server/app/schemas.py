"""HTTP API の要求・応答の pydantic モデル。

ID は必ず文字列で受け渡す。DB の BIGINT も Discord のスノーフレークも 64bit であり、JavaScript の Number は 2^53 までしか整数を正確に表せない。JSON に数値として載せると下位の桁が黙って書き換わり、壊れた ID はエラーにならず「存在しない記事」「別のユーザー」として振る舞う。応答に載る ID は IdOut、要求で受け取る ID は IdIn を使う。IdIn が数値を受け付けないのは、クライアント側で既に精度を失った値を、こちら側では検出できないためである。

日時は DB の DATETIME に合わせて UTC の naive として扱い、JSON では ISO 8601 の Z 付きで出す(UtcDateTime)。

*Write のモデルは作成(POST)と部分更新(PATCH)の両方で使うため、全ての項目を省略可能にしてある。「省略された」と「明示的に null を指定した」の区別は pydantic の model_fields_set で行う(payload.model_fields_set に項目名があれば指定されたということ)。作成時に必須となる項目の検査はサービス層が行う。
"""

from __future__ import annotations

import unicodedata
from datetime import date, datetime, timezone
from typing import Annotated, Any, Final, Generic, Literal, TypeAlias, TypeVar

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, PlainSerializer, field_validator

from common.settings import SettingKind
from common.snowflake import to_snowflake

# --------------------------------------------------------------------------
# 共通の値の型
# --------------------------------------------------------------------------


def _validate_id_text(value: str) -> str:
    """ID の文字列を検証し、10 進表記へ正規化する。"""
    return str(to_snowflake(value))


def _coerce_id_text(value: Any) -> Any:
    """応答へ載せる ID を文字列へ寄せる。

    DB の行は ID を int で返すため、そのまま渡されても JSON には必ず文字列で出るようにする。文字列であることを型として強制するだけでは、int を渡した箇所が検証エラーになるだけで、数値が漏れる余地は残らない。ここで受け止めておけば「応答に数値の ID が載る」ことが構造的に起こらない。
    """
    if isinstance(value, bool):
        raise ValueError('ID に真偽値は使えません。')
    if isinstance(value, int):
        return str(to_snowflake(value))
    return value


# 要求で受け取る ID。文字列のみを受ける。
IdIn = Annotated[str, AfterValidator(_validate_id_text)]
# 応答へ出す ID。int も受けて必ず文字列へ正規化する。
IdOut = Annotated[str, BeforeValidator(_coerce_id_text), AfterValidator(_validate_id_text)]

# 画面に何も描かない文字の Unicode カテゴリ。Cc は制御、Cf は書式(U+200B のゼロ幅空白・U+FEFF・双方向制御など)、Co は私用領域、Cs はサロゲートである。
_INVISIBLE_CATEGORIES: Final[frozenset[str]] = frozenset({'Cc', 'Cf', 'Co', 'Cs'})


def visible_text(value: str) -> str:
    """表示可能な文字だけを残した形を返す。値が実質的に空かどうかの判定に使う。

    str.strip() では足りない。strip が落とすのは str.isspace() が真の文字だけであり、U+200B(ゼロ幅空白)や U+FEFF は isspace() が偽であるため、strip を通しても残って「空でない文字列」として振る舞う。一方これらは画面上に何も描かないため、開示や識別のために名前を要求する項目では、空白のみの値と区別する意味がない。

    NFKC で互換文字を畳んでから不可視のカテゴリを落とし、前後の空白を除いた結果を返す。空文字列が返るなら、その値は読み手にとって何も表示しない。
    """
    normalized = unicodedata.normalize('NFKC', value)
    return ''.join(char for char in normalized if unicodedata.category(char) not in _INVISIBLE_CATEGORIES).strip()


def _require_visible_text(value: str) -> str:
    """表示可能な文字を 1 文字以上含むことを要求する。値そのものは書き換えない(表記は書いた側のものである)。"""
    if not visible_text(value):
        raise ValueError('表示できる文字を 1 文字以上含めてください。空白やゼロ幅文字だけの値は指定できません。')
    return value


# 表示可能な文字を 1 文字以上含む文字列。読み手に何かを示すことが要件である項目に使う。
VisibleStr = Annotated[str, AfterValidator(_require_visible_text)]


def _as_naive_utc(value: datetime) -> datetime:
    """タイムゾーン付きの値を UTC へ寄せ、tzinfo を落とす。

    MySQL の DATETIME はタイムゾーンを持たない。tz 付きの値をそのまま渡すと、ドライバが tzinfo を無視して壁時計の値を書き込み、時刻が静かにずれる。tzinfo が無い値は既に UTC とみなす。
    """
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _isoformat_z(value: datetime) -> str:
    """UTC の naive な値を ISO 8601(Z 付き)で表記する。"""
    aware = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    return aware.isoformat(timespec='seconds').replace('+00:00', 'Z')


# 日時。入力は UTC の naive へ寄せ、出力は必ず Z 付きの ISO 8601 にする。
UtcDateTime = Annotated[
    datetime,
    AfterValidator(_as_naive_utc),
    PlainSerializer(_isoformat_z, return_type=str, when_used='unless-none'),
]

# 以下の Literal は DB の ENUM を写したもの。値の集合の真実は migrations/0001_initial.sql にある。
ArticleStatus = Literal['draft', 'in_review', 'scheduled', 'published', 'archived']
Visibility = Literal['public', 'members', 'restricted']
BylineMode = Literal['author', 'publisher']
PublisherStance = Literal['independent', 'state']
Scope = Literal['articles:write', 'articles:write:any', 'ads:admin', 'taxonomy:admin', 'settings:admin']

# slug は URL のパス断片としてそのまま出る。空白やパス・クエリの区切りに化ける文字を含めない。
SLUG_PATTERN = r'^[^\s/?#&=%]+$'


class _OutModel(BaseModel):
    """応答本文の基底。DB の行(dict)や dataclass からそのまま組み立てられるようにする。"""

    model_config = ConfigDict(from_attributes=True)


class _WriteModel(BaseModel):
    """要求本文の基底。

    未知のキーは黙って捨てず 422 で弾く。項目名を書き間違えた指定が「無視されて反映されない」形で通ると、送った側は成功したと解釈するため、誤りが発覚しない。
    """

    model_config = ConfigDict(extra='forbid')


ItemT = TypeVar('ItemT')


class Page(_OutModel, Generic[ItemT]):
    """一覧の応答。total は絞り込み後の総件数。"""

    items: list[ItemT]
    page: int
    per_page: int
    total: int
    has_next: bool


# --------------------------------------------------------------------------
# 利用者・ロール
# --------------------------------------------------------------------------


class UserBrief(_OutModel):
    """記事の署名や所属記者の一覧に出す、利用者の最小限の表現。"""

    id: IdOut
    discord_user_id: IdOut
    username: str
    global_name: str | None = None
    avatar_hash: str | None = None


class UserOut(UserBrief):
    """自分自身の情報。guild メンバーシップとオーナーかどうかは閲覧範囲と権限に直結するため見せる。"""

    is_guild_member: bool
    is_guild_owner: bool


class RoleOut(_OutModel):
    """Discord のロール。権限ビットフィールドは出さない(管理者かどうかは MeOut.is_admin として解決済みの形で渡す)。"""

    id: IdOut
    name: str
    color: int
    position: int


# --------------------------------------------------------------------------
# 分類(カテゴリ・タグ・報道社・連載)
# --------------------------------------------------------------------------


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
    """タグページに出す情報。"""

    article_count: int


class TagWrite(_WriteModel):
    slug: str | None = Field(default=None, min_length=1, max_length=64, pattern=SLUG_PATTERN)
    name: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=1000)
    icon_url: str | None = Field(default=None, max_length=1000)
    notify_channel_id: IdIn | None = None


class PublisherOut(_OutModel):
    """報道社。role_id は所属を表す Discord ロール。解散した報道社は role_id が NULL になる。"""

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
    """報道社の実績。score はランキングの並び順そのものであり、算出の規則は PublisherRankingItem に述べたとおりである。"""

    article_count: int
    up_count: int
    down_count: int
    comment_count: int
    score: int


class PublisherDetailOut(PublisherOut):
    """報道社のプロフィール・所属記者・実績。所属は role_id を保有する利用者から引く。"""

    members: list[UserBrief] = Field(default_factory=list)
    stats: PublisherStats


class PublisherRankingItem(_OutModel):
    """報道社ランキングの 1 行。

    score は記事ごとに次の規則で求めた値の合計である(賛成 up、反対 down、コメント数 c として)。

      ・2 × down ≥ up … その記事は叩かれている。score = up + c(反対は加えない)
      ・2 × down < up  … 叩かれてはいない。score = up + (down ÷ 2 の切り捨て) + c(反対も関心の証として半分だけ加える)

    反対が点数を引くことはない。反対は「読まれなかった」ことではなく「読まれたうえで反対された」ことの印であり、賛成に対して十分小さい限りは注目の大きさとして数える。一方で反対が賛成に迫るほど付いた記事は、その注目を実績として数えない。

    判定を記事ごとに行うのは、叩かれているかどうかが記事の性質だからである。報道社の合計に対して判定すると、1 本の炎上がその報道社の他の記事の評価まで一斉に変える。

    up_count / down_count / comment_count は期間内の素の合計であり、score の内訳ではない(score は記事ごとの判定を経ているため、この 3 つから再計算できない)。
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


# --------------------------------------------------------------------------
# 改版履歴
# --------------------------------------------------------------------------


class RevisionListItem(_OutModel):
    """改版の一覧に出す項目。

    editor は「誰の名前で書かれたか」、via_token_id は「どのキーが実際に投稿したか」であり、両者は別物である。articles:write:any が他人の署名で記事を出せる以上、後者が無いと説明責任が追えない。
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
    """過去版そのもの。"""

    body_md: str


class RevisionDiffOut(_OutModel):
    """直前版との差分。diff は unified diff を行ごとに分けたもの。"""

    revision_number: int
    previous_revision_number: int | None = None
    diff: list[str]


# --------------------------------------------------------------------------
# 記事
# --------------------------------------------------------------------------


class ArticleListItem(_OutModel):
    """一覧に出す記事。

    up_count / down_count / comment_count は当該記事の通知メッセージ全ての合計である(1 記事が複数のチャンネルへ通知されうるため)。score は up_count − down_count であり、負になりうる。読者へ見せる点数はこの差し引きであって、報道社ランキングの score とは別物である(そちらの規則は PublisherRankingItem を参照)。
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
    # 読者へ見せる点数。up_count − down_count であり、負になりうる。
    score: int
    comment_count: int
    published_at: UtcDateTime | None = None
    created_at: UtcDateTime
    updated_at: UtcDateTime


class DiscordPostOut(_OutModel):
    """記事の通知メッセージ 1 件。1 記事が複数のチャンネルへ通知されうるため、記事詳細はこれを配列で持つ。

    thread_url を組み立てるのはサーバーである。Discord のスレッドの URL には対象 guild の ID が要り、それは設定(discord.guild_id)にしか無い。画面がこれを知る手段は無いため、URL を組み立てる責務を画面へ渡すことはできない。未送信(message_id が NULL)・スレッド未作成(thread_id が NULL)の行では null になる。
    """

    channel_id: IdOut
    message_id: IdOut | None = None
    thread_id: IdOut | None = None
    # スレッドを開くための URL。thread_id が無ければ null(bot がまだスレッドを開いていない)。
    thread_url: str | None = None
    up_count: int
    down_count: int
    # この通知だけの点数(up_count − down_count)。負になりうる。
    score: int
    comment_count: int
    posted_at: UtcDateTime | None = None


class CommentAuthorOut(_OutModel):
    """コメントの投稿者。

    users に居ない相手もいる(guild を抜けた利用者・他の bot・webhook)。その場合に返せるのは Discord のユーザー ID だけであり、username も avatar_hash も null になる。画面はその前提で組む必要がある。
    """

    discord_user_id: IdOut
    username: str | None = None
    global_name: str | None = None
    avatar_hash: str | None = None


class CommentOut(_OutModel):
    """記事のコメント 1 件。実体は Discord スレッドの返信であり、これはその写しである。

    body は Discord の本文をそのままの記法で持つ。サーバーは整形しない(Discord の記法をここで解釈し始めると、Discord 側の表示と食い違う二つ目の実装を抱えることになる)。画面は必ずエスケープして出す。

    score は up_count − down_count であり、負になりうる。
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
    """記事のコメントの一覧。

    total は返した件数ではなく、その記事のコメントの総数である(画面が「もっと読む」を出すかどうかを決められるようにするため)。thread_url は Discord のスレッドへの入口であり、コメントを書く場所はそこしかない(この API は読み出しのみで、書き込みは受け付けない)。
    """

    items: list[CommentOut]
    total: int
    thread_url: str | None = None


class ArticleOut(ArticleListItem):
    """記事詳細。corrections は訂正として記録された改版の一覧で、記事面の冒頭に出す。"""

    body_md: str
    body_html: str | None = None
    visibility_role_ids: list[IdOut] = Field(default_factory=list)
    scheduled_at: UtcDateTime | None = None
    corrections: list[RevisionListItem] = Field(default_factory=list)
    # 通知メッセージごとの参照。記事面が「スレッドを開く」導線と通知先の数を出すために使う。合計値である up_count / down_count / comment_count とは別に、内訳としてここに持つ。
    discord_posts: list[DiscordPostOut] = Field(default_factory=list)


class SeriesDetailOut(SeriesOut):
    articles: list[ArticleListItem] = Field(default_factory=list)


class ArticleWrite(_WriteModel):
    """記事の作成・更新の要求本文。

    全ての項目が省略可能である。作成(POST)で必須となる title / body_md / category_id の検査、および指定してよいかどうかの判定(速報の権限、名乗れる報道社、他人を著者にできるか)はサービス層が行う。ここで表現できるのは形式だけであり、資格に依る規則は要求元の主体を知らないと判定できない。

    tags はタグの slug の配列。ID ではなく slug で受けるのは、自動投稿するクライアントにとって安定して書ける識別子であるため。存在しない slug はサービス層がエラーにする(タグの新設は taxonomy:admin の管轄であり、記事の投稿で勝手に増やさない)。
    """

    title: str | None = Field(default=None, min_length=1, max_length=300)
    body_md: str | None = None
    excerpt: str | None = Field(default=None, max_length=500)
    category_id: IdIn | None = None
    publisher_id: IdIn | None = None
    series_id: IdIn | None = None
    series_position: int | None = None
    tags: list[str] | None = None
    visibility: Visibility | None = None
    visibility_role_ids: list[IdIn] | None = None
    cover_image_url: str | None = Field(default=None, max_length=1000)
    byline_mode: BylineMode | None = None
    is_breaking: bool | None = None
    is_sponsored: bool | None = None
    # 誰の広告かを読み手に示す値であるため、表示可能な文字を要求する。ゼロ幅文字だけの名前は「広告」ラベルだけを出して広告主を伏せることになり、開示の要件(is_sponsored=1 なら sponsor_name 必須)を形の上でだけ満たす。
    sponsor_name: VisibleStr | None = Field(default=None, max_length=100)
    slug: str | None = Field(default=None, min_length=1, max_length=200, pattern=SLUG_PATTERN)
    status: ArticleStatus | None = None
    scheduled_at: UtcDateTime | None = None
    author_id: IdIn | None = None
    # 改版に残すメモ。訂正の場合は何を訂正したかを書く。
    revision_note: str | None = Field(default=None, max_length=500)
    is_correction: bool | None = None

    @field_validator('tags', 'visibility_role_ids')
    @classmethod
    def _unique_items(cls, value: list[str] | None) -> list[str] | None:
        """重複を畳む。article_tags / article_visibility_roles の主キーが (記事, 対象) であり、重複したまま挿入すると重複キーになる。"""
        if value is None:
            return None
        return list(dict.fromkeys(value))


class MediaFailure(_OutModel):
    """記事の保存時に取り込めなかった画像。

    取り込めなかった URL は本文にそのまま残る(外部ホストの一時的な不調で記事が出せなくなってはならない)。残った以上、その URL はいずれ失効して画像が消えるため、黙って成功と扱わず必ず執筆者へ伝える。
    """

    source_url: str
    reason: str


class ArticleSaveOut(_OutModel):
    """記事の保存の応答。"""

    article: ArticleOut
    media_failures: list[MediaFailure] = Field(default_factory=list)


ArticleListOut: TypeAlias = Page[ArticleListItem]


# --------------------------------------------------------------------------
# 自分・API キー・購読
# --------------------------------------------------------------------------


class MeOut(_OutModel):
    """自分の情報。publishers は保有ロールから引ける所属報道社。"""

    user: UserOut
    roles: list[RoleOut] = Field(default_factory=list)
    publishers: list[PublisherOut] = Field(default_factory=list)
    is_admin: bool
    subscriptions: list[TagOut] = Field(default_factory=list)


class TokenOut(_OutModel):
    """API キーの一覧に出す表現。生値は DB に無い(SHA-256 のみを保存する)ため返せない。"""

    id: IdOut
    token_prefix: str
    name: str
    scopes: list[Scope] = Field(default_factory=list)
    created_at: UtcDateTime
    last_used_at: UtcDateTime | None = None
    expires_at: UtcDateTime | None = None
    revoked_at: UtcDateTime | None = None


class TokenCreated(TokenOut):
    """発行直後の応答。生値をこの応答でだけ返す。以後は二度と取り出せない。"""

    token: str


class TokenCreate(_WriteModel):
    """API キーの発行要求。

    管理者スコープ(articles:write:any / ads:admin / taxonomy:admin / settings:admin)を発行できるのは、要求の時点で管理者資格を満たす者だけである。その検査はサービス層が行う。

    settings:admin は管理ロール(admin.role_ids)を書き換えられる、すなわち管理者の資格そのものを配れるため、他のどのスコープよりも強い。これを与えたキーは実質的に管理者権限の委譲であり、他の管理者スコープのように「広告だけ」「分類だけ」と対象を限れない。
    """

    name: str = Field(min_length=1, max_length=100)
    scopes: list[Scope] = Field(min_length=1)
    # 期限は DATETIME の範囲を超える指定を弾くために上限を設ける。省略すると無期限。
    expires_days: int | None = Field(default=None, ge=1, le=3650)

    @field_validator('scopes')
    @classmethod
    def _unique_scopes(cls, value: list[Scope]) -> list[Scope]:
        """重複を畳む。api_token_scopes の主キーが (token_id, scope) であり、重複したまま挿入すると重複キーになる。"""
        return list(dict.fromkeys(value))


# --------------------------------------------------------------------------
# 画像
# --------------------------------------------------------------------------


class MediaOut(_OutModel):
    """取り込んだ画像。url は配信 URL(/media/{hash}/{variant}.webp)。"""

    id: IdOut
    content_hash: str
    url: str


# --------------------------------------------------------------------------
# 広告
# --------------------------------------------------------------------------


class AdSlotOut(_OutModel):
    """広告枠。推奨寸法を返すのは、外部の管理アプリが入稿前に見え方を自前で再現できるようにするため。"""

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
    """広告の入稿・更新。掲載を止めるのは is_active=false であり、行を消さない(実績を残すため)。"""

    slot_id: IdIn | None = None
    advertiser_name: str | None = Field(default=None, min_length=1, max_length=100)
    image_url: str | None = Field(default=None, min_length=1, max_length=1000)
    link_url: str | None = Field(default=None, min_length=1, max_length=1000)
    alt_text: str | None = Field(default=None, max_length=200)
    # 重み付き抽選の重み。0 を許すと候補があるのに合計が 0 になる状態を作れてしまうため 1 以上とする(掲載を止めるなら is_active=false を使う)。
    weight: int | None = Field(default=None, ge=1)
    starts_at: UtcDateTime | None = None
    ends_at: UtcDateTime | None = None
    is_active: bool | None = None


class AdServed(_OutModel):
    """配信された 1 件の広告。link_url ではなく click_url を返すのは、計測を挟むため。"""

    id: IdOut
    slot: str
    advertiser_name: str
    image_url: str
    click_url: str
    alt_text: str | None = None


class AdServeOut(_OutModel):
    """枠に対する配信の応答。候補が無ければ ad は null。フロントはその場合に枠自体を消す。"""

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


# --------------------------------------------------------------------------
# プラットフォームの設定
# --------------------------------------------------------------------------


class SettingOut(_OutModel):
    """設定 1 つの定義と現在値。組み立てるのは common.settings の SettingsStore.describe である。

    画面はこの応答から入力欄を生成する。定義(型・ラベル・説明・変更の可否)を値と一緒に返すのは、画面にキーを直書きさせないためである。直書きすると、設定を 1 つ足すたびに登録簿と画面の両方を直すことになり、片方を忘れた分だけが「設定できるのに画面に出ない」あるいは「画面に出るのに保存できない」状態で残る。

    value は kind に応じた JSON の値であり、id は文字列、id_list は文字列の配列である。**secret が真の設定は value を必ず null にし、設定済みかどうかを is_set だけで示す。**生値を返した時点で、画面を開ける者すべてに bot のトークンが渡る。設定を読める資格と、秘密そのものを持ち出せることは別である。
    """

    key: str
    kind: SettingKind
    label: str
    description: str
    # 変更できない設定。画面は読み取り専用で表示し、変更できない理由は description が示す。
    editable: bool
    # 変更してもプロセスが読み直さない設定。反映には再起動が要る。
    restart_required: bool
    secret: bool
    # kind に応じた値。secret の設定では常に null。
    value: Any = None
    # 値が入っているか。secret の設定について、生値を返さずに設定済みかどうかだけを伝えるためにある。
    is_set: bool
    updated_at: UtcDateTime | None = None
    # 最後に変更した利用者。初期値の投入と CLI からの変更では null(どちらもプラットフォームの利用者による変更ではない)。
    updated_by: IdOut | None = None


class SettingsOut(_OutModel):
    """設定の全件。並びは登録簿の順で、画面での並びが要求ごとに変わることはない。"""

    settings: list[SettingOut] = Field(default_factory=list)


class SettingsUpdate(_WriteModel):
    """設定の部分更新の要求本文。

    updates に載せたキーだけを変更し、省略したキーは現在の値のまま残す。**部分更新であることは、秘密の設定を「変更しない」と表現する唯一の手段でもある。**画面は秘密の生値を読めない以上、変更しない秘密を再送できず、全件を送る方式では秘密が必ず壊れる。

    値の型・値域・そのキーを変更してよいかの検査はここでは行わない。キーごとに型が異なり、その定義の真実は設定の登録簿(common.settings)にあるためである。ここに型を書くと定義が 2 箇所に分かれ、登録簿へ設定を足したときにここを直し忘れた分だけが、登録簿の検査に届く前に 422 で弾かれる。検査は SettingsStore.set_many が行い、その ValueError を 400 へ写す。
    """

    updates: dict[str, Any]


class SettingsUpdateOut(SettingsOut):
    """設定の更新の応答。

    updated_keys は実際に変更したキー。settings には更新後の全件を返す。更新した値だけを返すと、画面は自分が送った値を信じて描き直すことになり、正規化(ID の重複の除去・URL の末尾の / の削除など)の結果が次の再読み込みまで画面に現れない。
    """

    updated_keys: list[str] = Field(default_factory=list)
