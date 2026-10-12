#!/usr/bin/env bash
# Put the database back to a dump taken by scripts/update.sh: stop server and bot, drop the database, and load the dump, which recreates it together with its migration history.
#
#   scripts/restore-db.sh backups/<time>-<commit>.sql.gz
set -euo pipefail

fail() { printf '\nエラー: %s\n' "$*" >&2; exit 1; }

[[ $# -eq 1 ]] || fail '使い方: scripts/restore-db.sh <バックアップのファイル>'
backup=$(realpath "$1")
[[ -f $backup ]] || fail "ファイルがありません: $1"
gzip -t "$backup" || fail "gzip として読めません: $1"

cd "$(git rev-parse --show-toplevel)"

docker compose stop server bot
docker compose up -d --wait db
# Tables a failed migration created are not in the dump, so the database is dropped rather than overwritten. The application user's grants live in the mysql schema and survive the drop.
gunzip -c "$backup" | docker compose exec -T db sh -c 'export MYSQL_PWD="$MYSQL_ROOT_PASSWORD"; mysql -uroot -e "DROP DATABASE IF EXISTS \`$MYSQL_DATABASE\`" && exec mysql -uroot'

cat <<EOF

DB を $(basename "$backup") の時点に戻しました。server と bot は止めたままです。
バックアップ名の末尾は取得時のコミットです。コードもその時点に戻してから起動してください:
  git checkout <コミット> && docker compose up -d --build
EOF
