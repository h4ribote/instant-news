# syntax=docker/dockerfile:1

# server(FastAPI)のイメージ。
#
# ビルドコンテキストはリポジトリのルートであり、このファイルの置かれた server/ ではない(docker-compose.yml の build.context を参照)。server と bot は common/ を共有しており、コンテキストを server/ に絞ると common/ をコピーできないため。したがって COPY のパスはすべてリポジトリのルートからの相対で書く。

FROM python:3.12-slim

# PYTHONDONTWRITEBYTECODE: 読み取り専用で動かすコードに .pyc を書かせない。PYTHONUNBUFFERED: 標準出力を溜めずに流す(溜めるとコンテナが落ちた際に直前のログを失う)。PYTHONPATH: common と server を最上位のパッケージとして解決させる。
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

# 依存だけを先に入れる。requirements.txt が変わらない限り、ソースを変えてもこの層は再利用される。
COPY server/requirements.txt ./server/requirements.txt
RUN pip install --no-cache-dir --disable-pip-version-check -r server/requirements.txt

# 取り込んだ画像は必ずデコードして WebP へ再エンコードするため、Pillow が WebP を扱えないとこのサービスは画像を 1 枚も受け付けられない。Pillow の wheel は libwebp を同梱するので slim イメージに追加の OS パッケージは要らないが、wheel が用意されていない環境ではソースからビルドされ、libwebp を欠いたまま「import はできるが WebP だけ使えない」Pillow が入りうる。それを利用者が画像を投稿した瞬間ではなくビルドの時点で失敗させる。
RUN python -c "from PIL import features; assert features.check('webp'), 'Pillow が WebP を扱えません(libwebp を欠いた状態でビルドされています)'"

COPY common/ ./common/
COPY server/ ./server/

# 画像の実体を置くディレクトリ。config.yaml の media.storage_dir の既定値と一致させてある。実行時にはここへ名前付きボリュームを重ねる(docker-compose.yml)。イメージ側に所有者を設定したディレクトリを用意しておくと、Docker が初回にボリュームを作る際にその所有者を引き継ぐため、非 root のままボリュームへ書き込める。
RUN useradd --create-home --uid 10001 app \
 && mkdir -p /var/lib/instant-news/media \
 && chown -R app:app /var/lib/instant-news

# root で動かす理由が無い。ソースは root 所有のまま残すため、アプリケーション自身は自分のコードを書き換えられない。
USER app

# EXPOSE は置かない。待ち受けポートは config.yaml の server.port が決めるため、ここに数値を書くと設定を変えた瞬間に嘘になる。公開は docker-compose.yml の ports が行う。
# 待ち受けアドレスとポートも同様に config.yaml が唯一の真実であるため、起動コマンドで固定しない。server/main.py が設定を読んで uvicorn を起動する。
CMD ["python", "-m", "server.main"]
