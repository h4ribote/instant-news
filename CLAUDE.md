# CLAUDE.md

このファイルは、本リポジトリで作業する際に Claude Code が従うべき方針を定めるものです。

## 作業方針

- 常に最適なロジックを組み込むことを最優先とする。工数や後方互換性などのコストは考慮せず、最善の実装を選択して進めること。

## ドキュメントと実装の整合性

- ドキュメントと実装は、常に差異がない状態を維持すること。
- 矛盾・衝突、または「未実装であるにも関わらずドキュメントに記載されている」といった不整合を確認した場合は、速やかに問題を解消すること。
- 未実装箇所は、ドキュメントの記載に合わせて完全に実装すること(ドキュメント側を削って辻褄を合わせるのではなく、実装を完成させること)。

## Documentation style

- No formatting line breaks: never hard-wrap a sentence or a list item across physical lines just to limit width (same rule as the `/commit` command). Keep each paragraph and each bullet on one physical line, however long; if a bullet grows unwieldy, split it into separate bullets rather than wrapping it.
- Markdown tables: do not pad cells with spaces to align columns. Use the minimal `| a | b |` form with a `| --- | --- |` separator row.
- Diagrams: render figures as Mermaid (```` ```mermaid ```` fenced blocks) — sequence, flow/dependency, state, directory trees, and packet/byte layouts (`packet-beta`). Keep plain code fences only for literal code, shell commands, and serialization pseudo-code (listings, not figures).

## コミット

- コミット時には、`/commit` に沿った手順・フォーマットでコミットを実行すること。
- コミットメッセージは git 履歴のみを見る第三者にとって自己完結させること。実質ローカルにのみ存在するファイルや知識(`tmp/` 配下のメモ・ロードマップの採番/行番号/見出し、未コミットの計画書、チャット限定の符牒、外部に存在しないチケット番号など)を参照しない。指し示したい事項は内容そのものを言葉で書き下す。リポジトリにコミット済みで誰でも辿れるパスや、ドキュメントの章番号(例 `doc 12 12.9`)の参照は可。
- 併せて `#1`/`#2` のような `#<数字>` 表記もコミットメッセージに書かない。GitHub が無関係な Issue/PR へ自動リンクするうえ、参照先(例 `tmp/` のローカル採番)も第三者には辿れないため、所見は番号でなく内容を言葉で記述する(`good.food#drink` のように `#` の直後が数字でない識別子は可)。

## コードコメント

- コードコメント(行コメント・docstring 等)は、将来コードだけを読む者にとって自己完結させること。コミットメッセージと同じく、実質ローカルにのみ存在するファイルや知識(`tmp/` 配下のメモ・ロードマップやその採番、監査の所見番号、未コミットの計画書、チャット限定の符牒など)を参照しない。意図・背景は番号でなく内容そのものを言葉で説明する。
- リポジトリにコミット済みで誰でも辿れる参照は可: ドキュメントの章番号(例 `doc 05 5.2.5`)、コードのシンボル名・パス、コミット済みファイル。`#1`/`#2` のような採番だけの参照や、共有されない計画書への参照は残さない(第三者が辿れず意味が失われるため)。
- ドキュメントの記述スタイルと同様に、整形のための文中での改行を行わないこと。
