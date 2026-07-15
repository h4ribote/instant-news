# syntax=docker/dockerfile:1

# bot(Discord bot)のイメージ。
#
# ビルドコンテキストはリポジトリのルートであり、このファイルの置かれた bot/ ではない(docker-compose.yml の build.context を参照)。server と bot は common/ を共有しており、コンテキストを bot/ に絞ると common/ をコピーできないため。したがって COPY のパスはすべてリポジトリのルートからの相対で書く。
#
# bot は画像の実体を置くボリュームを持たない。画像を書くのも配信するのも server であり、bot は Discord へ送るだけであるため、画像の保存先を知る必要が無い。

FROM python:3.12-slim

# PYTHONDONTWRITEBYTECODE: 読み取り専用で動かすコードに .pyc を書かせない。PYTHONUNBUFFERED: 標準出力を溜めずに流す(溜めるとコンテナが落ちた際に直前のログを失う)。PYTHONPATH: common と bot を最上位のパッケージとして解決させる。
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

# 依存だけを先に入れる。requirements.txt が変わらない限り、ソースを変えてもこの層は再利用される。
COPY bot/requirements.txt ./bot/requirements.txt
RUN pip install --no-cache-dir --disable-pip-version-check -r bot/requirements.txt

COPY common/ ./common/
COPY bot/ ./bot/

# root で動かす理由が無い。ソースは root 所有のまま残すため、アプリケーション自身は自分のコードを書き換えられない。
RUN useradd --create-home --uid 10001 app
USER app

CMD ["python", "-m", "bot.main"]
