# 調査コンテナに渡るものを変えるとき

`container/` の中身だけが調査コンテナに届く。ここに置くものは、
コンテナの中から見える事実の形で書く。

## ホスト側の話を書かない

書かないもの: フックの内部仕様、隔離の設計、判断の経緯、ホストでの手順、
`libexec/` や `.agents/rules/` のパス、`environment.json`。

制約は「この環境では〜できません」と書く。「フックが拒否します」とは書かない。

## 秘密を書かない

`container/` の中身はコンテナに渡る。`.env` の値、接続文字列、鍵、顧客データを
絶対に書かない。秘密の値をマスクさせる方針は `container/instructions/survey-agents.md` にある
（環境変数・`userData`・アカウント ID / ARN / IP の扱い）。

## 対象システムのことを書かない

`method/` はどの AWS アカウントを調べるときでも同じでなければならない。
対象のことを書いた時点で、この一式は 1 つの環境専用になる。

対象システムの知識は調査エージェントが `out/01_システム概要.md` に書く。ホストは用意しない。
前提を先渡ししないこと。最初のフェーズは白紙で棚卸ししてからユーザーに聞く設計で、
先に教えると発見ではなく確認をするようになる。

## フェーズ固有の規則を書かない

`method/` と `container/instructions/survey-agents.md` に書くのは、どのフェーズでも変わらないことだけ。
「監査ではない」「評価を書かない」は基礎調査の規則であって、常時の禁止事項ではない。
フェーズ固有の「やる / やらない」は `out/<フェーズ>/00_目的と規則.md` に書く。

| 書きたいこと | 置き場 |
| --- | --- |
| 対象システムの知識 | ホストでは作らない（調査エージェントが `out/01_システム概要.md` に書く） |
| 調査項目・フェーズの目的と規則 | ホストでは作らない（調査エージェントが `out/<フェーズ>/` に書き、ユーザーが承認） |
| どのフェーズでも変わらない調査の作法 | `method/01_進め方.md` |
| 現場で見つかった、どの対象でも効くノウハウ | `method/01_進め方.md`（`out/02_調査ノウハウ.md` から昇格させる） |
| フェーズ運用そのものの決まり | `method/02_フェーズとは.md` |
| 常に意識させたいこと（読み取りのみ・出力先・マスク） | `container/instructions/survey-agents.md`。薄く保つ |

`container/instructions/survey-agents.md` は毎回すべて読まれる。厚くすると、そのフェーズに関係ない指示が
常に文脈を占める。詳細は `method/` に置く。

## 反映のされ方が 3 通りある

| ファイル | 渡し方 | 変更したら |
| --- | --- | --- |
| `container/method/` | ディレクトリのマウント | その場で効く |
| `container/instructions/survey-agents.md` と `survey-claude.md` | 単一ファイルのマウント | 再起動が要る。エディタがファイルを置き換えるとコンテナ側は古いまま |
| `container/settings.json` / `codex/` / `hooks/` / `bashrc` / `survey-status` / `survey-ui.sh` / `ec2` | イメージに焼き込み | 再ビルドが要る（`aws-survey scan` / `claude` / `codex` / `run` が毎回ビルドする） |

コンテナへ渡るものは `container/` だけではない。対象フォルダの `out/` はディレクトリのマウントなので
双方向にその場で効き（目的と規則・システム概要を直せば調査エージェントがすぐ読む）、`environment.json` の
`phase_dir` / `region` / `name` は起動時に読むので次回の起動（`aws-survey scan` / `claude` / `codex` / `run`）から効く。
同じ `out/` に対して 2 つのエージェントを同時に走らせない。台帳と監査ログの書き手が競合する。
`aws-survey scan` は起動前に `docker ps` で対話コンテナと自分の名前を見て、動いていれば止まる。

`aws-survey scan` は調査エージェントを非対話（`claude -p` / `codex exec`）で 1 回動かす。ホストが渡す指示は「ユーザーが応答できない回」と
「棚卸しだけ」であり、その回に何をするかは `method/00` の「ユーザーが応答できない回」が持つ。環境の確認と白紙の棚卸しを
承認前の前段とする位置づけは `survey-agents.md` と `method/00` / `04` にある。ホストのコマンド名はそこにも書かない。

ホストが同じイメージを `docker run --rm` で 1 コマンドだけ動かす経路がある（`aws-survey ls` / `ec2` / `lambda` の一覧と
`ssh verify` の `ec2 --selftest`。`libexec/container.sh`）。渡すのは一時キーの ro マウントだけで、`out/` も指示書も
エージェントのボリュームも付けない。ホスト側のスクリプト（`libexec/inventory.sh`）は `/x/` に ro マウントして借りるだけで、
`container/` には置かない。調査エージェントに使わせる道具ではなく、イメージにも残らない。

## 画面の見た目

ログイン時の案内（`bashrc`）と `survey-status` は続けて 1 画面に出る。記号・字下げ・色は
`container/survey-ui.sh` にまとめ、両方がそれを source する。ホストの `libexec/ui.sh` とは
記号の語彙（◆ ⚠ シアンのコマンド）だけを揃え、ファイルは複製しない。ビルド文脈の外なので
`COPY` できず、共有すると調査コンテナがホスト側の表示都合に引きずられる。

`survey-status` は人が読むだけではない。調査エージェントも実行するので、標準出力が端末でない
ときは装飾を落として素の文字列にする（`survey-ui.sh` が判定する）。

`bashrc` は対話シェルだけで動く。冒頭の `case $- in *i*)` を外さないこと。
エージェントのコマンド実行は非対話シェルで、案内が毎回の出力に混ざる。

## ガードの変更

監査ログの改変を防ぐ仕組みはエージェントごとに 1 系統ずつある。ここが正本。

| エージェント | 通常の編集・コマンド経由の監査ログ改変を防ぐもの |
| --- | --- |
| Claude Code | `settings.json` の `Edit(//home/node/aws-survey/out/_環境/aws-audit.log)` deny と、共通 Bash ガードの `has 'aws-audit'`。両方必要 |
| Codex | `codex/requirements.toml` が強制する管理対象 PreToolUse の `hooks/codex-guard.py`。コマンドとパッチ先を検査する |

Claude 側の 2 経路が揃っていることは `tests/test_guards.py` の `AuditLogPermissions` が確認する。

- 共通の AWS 許可・拒否規則は `hooks/aws-readonly-guard.sh` に置く。Codex 側へ複製しない。
  EC2 の中を調べる経路（`ec2` は単独コマンドだけ通す、`ssh` / `scp` / `sftp` / `session-manager-plugin` の直接実行は拒否）も同じ場所。
  `ec2` ラッパー（`container/ec2`）はガードと同じく root 所有でイメージへ焼き込む。仕様と自己診断は `docs/design-ec2-ssh.md` の第 7・8 節。
- Codex の対応範囲: 通常コマンドは単独実行で、引用符内の `jq` フィルタは使える。
  インタプリタや MCP ツールの追加は対応範囲外。`unified_exec` は無効のまま（理由は `codex/*.toml` のコメント）。
- 管理設定・フックはイメージへ root 所有で焼き込む。マウントに移していないことは
  `tests/test_launcher.py` の `BakedNotMounted` が確認する（理由もそちら）。
- Claude Code は `~/.local` のネイティブ版で、自分で更新する。npm prefix（`/usr/local`）は
  root 所有のまま。Codex は固定（ガードが Codex の機能契約に乗っているため）。
  配置と理由は `tests/test_launcher.py` の `CliInstallLayout`。
- コンテナ専用の Codex 設定は `sandbox_mode = "danger-full-access"`（隔離は Docker が担う）。
  ホストの Codex にコピーしない。Docker の privileged・追加 capability・seccomp 無効化も使わない。
- この仕組みで防げない範囲は `docs/security.md`「これで保証されないこと」が正本。
  フックを独立監査や完全な改ざん防止と説明しない。

恒久的な許可の変更は、対象エージェントのガードとそのテストを同時に直す。
`hooks/` や `settings.json` / `codex/` を変えたら、コンテナ内の環境の確認をやり直させる。
既存の `out/_環境/00_動作確認.md` は消さず、そのエージェントに再確認と追記を頼む。
`libexec/session-guard.json` やロールのポリシーを変えたときは、代わりにホストで `aws-survey verify`
（`.agents/rules/credentials.md`）。調査対象のアカウントを変えたときは両方。

## `_環境/` を成果物と別区画に置く理由

`out/_環境/` に入るのは `00_動作確認.md`（初回に調査エージェントが書く）と `aws-audit.log`（フックが書く）。
どちらもこの環境が期待どおり動いているかの記録で、調査の成果物ではない。だからフェーズのフォルダと分ける。

この 2 つは対で読む。離して置かないこと。監査ログはフックが `out/` 配下にしか書けないので、
動作確認だけ別の場所へ移すと証跡が対で読めなくなる。

環境の自己点検をフェーズにしないこと。フェーズはミッションの単位であり、
「自分を縛る仕組みを検証せよ」という指示は、調査エージェントに知らせない前提を持ち込む。

`Dockerfile` のビルド文脈は `container/`（`libexec/docker.sh` が `docker build -f <本体>/Dockerfile <本体>/container`）。
`COPY` のパスは `container/` からの相対で書く。文脈の外は `COPY` できない。

## フックを変えたら、通る例と落ちる例の両方を確かめる

`python3 -m unittest discover -s tests -v` で共通ガードと Codex アダプタを検証する。
次は共通 Bash ガードの手動確認例。監査ログの出力先は一時ファイルにする。

```bash
H=container/hooks/aws-readonly-guard.sh
export SURVEY_AUDIT_LOG=$(mktemp)

echo '{"tool_name":"Bash","tool_input":{"command":"aws ec2 describe-vpcs"}}' | bash $H; echo "exit=$?"
#   → exit=0

echo '{"tool_name":"Bash","tool_input":{"command":"aws ec2 terminate-instances --instance-ids i-0"}}' | bash $H; echo "exit=$?"
#   → exit=2

echo '{"tool_name":"Bash","tool_input":{"command":"aws ec2 describe-vpcs > out/<フェーズ>/raw/raw-x.json"}}' | bash $H; echo "exit=$?"
#   → exit=0

echo '{"tool_name":"Bash","tool_input":{"command":"aws ec2 describe-vpcs > out/../.claude/hooks/x.json"}}' | bash $H; echo "exit=$?"
#   → exit=2
```

保存先として通るのは `out/` 配下（サブフォルダ可・`..` と `//` は不可）で、
拡張子が `json` / `txt` / `csv` のものだけ。絶対パス・Markdown・`out/` の外は落ちる。

保存先の規則を変えるときは、次を同時に直す。片方だけだと食い違う。

| 直す場所 | 何を |
| --- | --- |
| `container/hooks/aws-readonly-guard.sh` | `> out/…` を通す正規表現（再ビルドが要る） |
| `container/method/01_進め方.md` | 成果物の置き場所・コマンドの作法・大きな出力の扱い |
| `container/instructions/survey-agents.md` | 出力の置き場所の図 |
| `libexec/launch.sh`（`run` と `scan` が共有） | 起動時に作るフォルダ |

`container/` にあるものは Dockerfile の `COPY` か `launch.sh` のマウントで必ず調査コンテナへ届く
（`tests/test_launcher.py` の `ContainerDelivery`）。

`instructions/` のファイル名を `AGENTS.md` / `CLAUDE.md` に戻さない。ホストの開発エージェントが自動読込し、
調査エージェントへの二人称の指示を自分への指示として受け取る。コンテナ内での名前はマウント先が決めるので、
ソース側の名前は何でもよい。

## 参照した公式仕様

- [Codex AGENTS.md](https://learn.chatgpt.com/docs/agent-configuration/agents-md)
- [Claude Code の AGENTS.md import と短い指示書](https://code.claude.com/docs/en/memory#agents-md)
- [Codex の管理対象フック・ツール対応範囲](https://learn.chatgpt.com/docs/hooks)
- [Codex の認証](https://learn.chatgpt.com/docs/auth)
