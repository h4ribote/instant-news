#!/usr/bin/env bash
# Take in the latest code and its schema migrations: fast-forward the checkout, build, and when migrations are pending back the database up, stop server and bot, migrate, then start everything again.
# docs/migrations.md describes each step and how to recover when one fails.
#
#   scripts/update.sh            fetch and fast-forward to the upstream branch first
#   scripts/update.sh --no-pull  deploy the checkout exactly as it is
set -euo pipefail

PENDING_EXIT_CODE=3
BACKUP_DIR=backups

step() { printf '\n==> %s\n' "$*"; }
fail() { printf '\nエラー: %s\n' "$*" >&2; exit 1; }

pull=1
case "${1:-}" in
  '') ;;
  --no-pull) pull=0 ;;
  *) fail "未知の引数です: $1(指定できるのは --no-pull だけです)" ;;
esac

cd "$(git rev-parse --show-toplevel)"
[[ -f config.yaml ]] || fail 'config.yaml がありません。README の手順で用意してください。'
[[ -f .env ]] || fail '.env がありません。README の手順で用意してください。'

before=$(git rev-parse HEAD)

if (( pull )); then
  step 'コードを取得します'
  git diff --quiet HEAD -- || fail '追跡中のファイルに未コミットの変更があります。コミットするか戻してから実行してください。'
  git rev-parse --abbrev-ref --symbolic-full-name '@{u}' > /dev/null 2>&1 || fail '現在のブランチに upstream がありません(git branch --set-upstream-to で設定してください)。'
  git fetch --quiet
  if [[ $(git rev-parse '@{u}') == "$before" ]]; then
    echo 'コードは最新です。'
  else
    echo '取り込むコミット:'
    git log --oneline "HEAD..@{u}"
    added=$(git diff --name-only --diff-filter=A HEAD '@{u}' -- migrations/)
    if [[ -n $added ]]; then
      echo '新しいマイグレーション:'
      printf '  %s\n' $added
    fi
    git merge --ff-only --quiet '@{u}' || fail "fast-forward できません。ローカルのコミットが upstream と分岐しています(更新前のコミットは $before のままです)。"
  fi
fi

step 'イメージをビルドします(稼働中のサービスはまだ止めません)'
docker compose build || fail "ビルドに失敗しました。稼働中のサービスは止めていません。コードを戻すには git checkout $before を実行してください。"

step 'マイグレーションの状態を確認します'
set +e
docker compose run --rm -T migrate python -m server.cli migration-status --check
status=$?
set -e

if (( status == PENDING_EXIT_CODE )); then
  step 'DB をバックアップします'
  mkdir -p "$BACKUP_DIR"
  backup="$BACKUP_DIR/$(date -u +%Y%m%dT%H%M%SZ)-${before:0:12}.sql.gz"
  # The dump holds every credential in the settings table, so only the owner may read it.
  (
    umask 077
    docker compose exec -T db sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysqldump -uroot --single-transaction --routines --triggers --events --no-tablespaces --databases "$MYSQL_DATABASE"' | gzip > "$backup.partial"
  ) || { rm -f "$backup.partial"; fail 'バックアップに失敗しました。サービスは止めていません。'; }
  mv "$backup.partial" "$backup"
  echo "バックアップ: $backup"

  step 'server と bot を止めてマイグレーションを適用します'
  docker compose stop server bot
  if ! docker compose run --rm -T migrate; then
    cat >&2 <<EOF

マイグレーションに失敗しました。server と bot は止めたままです。
  バックアップ:       $backup
  更新前のコミット:   $before
状態の確認:   docker compose run --rm migrate python -m server.cli migration-status
DB を戻す:    scripts/restore-db.sh $backup
コードを戻す: git checkout $before && docker compose up -d --build
詳しくは docs/migrations.md の「失敗したとき」を参照してください。
EOF
    exit 1
  fi
elif (( status != 0 )); then
  fail "マイグレーションを適用できない状態です(上の「問題:」を参照してください)。サービスは止めていません。コードを戻すには git checkout $before && docker compose build を実行してください。"
fi

step 'サービスを起動します'
docker compose up -d --wait || fail 'サービスが正常に起動しませんでした。docker compose ps と docker compose logs server bot で原因を確認してください。'

echo
echo "更新が完了しました: $(git rev-parse --short HEAD)"
