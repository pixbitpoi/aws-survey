# AWS 構成調査環境の開発・運用

Claude Code / Codex 共通の指示。これは AWS を調査するための一式であり、対象インフラの説明書ではない。

## ホストと調査コンテナ

- **ホスト**（このリポジトリ）: 調査環境の開発。`aws-survey` が AWS 接続の準備と起動を持つ。
  `aws-survey run` でコンテナが立ち上がったら、ホスト側の仕事は無い。
- **調査コンテナ**: AWS を読み取り専用で調べる実行環境。中で動く **調査エージェント** が
  ユーザーと目的を決め、`out/` に記録する。
- 対象の概要・調査項目・フェーズの目的をホストから先渡ししない。調査エージェントが白紙の棚卸しから作る。
- 調査エージェントに渡す文書に、ホスト手順・隔離設計・ホスト側の判断を書かない。
  制約は環境から見える事実として書く。
- 接続設定と到達点は `environment.json`、調査の記憶は `out/` に分ける。
- この 3 語を使い分ける。「渡る・届く・起動する」はコンテナ、「調べる・書く・報告する」はエージェント、
  それを用意する側がホスト。

## 作業前に読むもの

必要な文書だけ読む。開発作業に対象アカウントの設定は不要。

| 依頼・対象 | 読むもの |
| --- | --- |
| 準備・立ち上げ・起動・一時キー | 手順書は置かない。`aws-survey`（引数なし）の出力に従う。段階の判定・次の 1 手・実行までコマンドが持つ |
| フェーズ運用・調査中の進め方 | ホストでは持たない。調査エージェント（`container/instructions/`・`container/method/`）が自分で持つ |
| エラー | 手順書は置かない。`aws-survey doctor`（ツールの有無と発行の切り分け）と各コマンドの出力から読む |
| リリース・Homebrew の formula 更新 | `docs/release.md`（配布物の範囲もここ） |
| `container/**`・`Dockerfile` | `.agents/rules/container.md` |
| `bin/aws-survey`・`libexec/**`・`templates/`・接続設定 | `.agents/rules/credentials.md` |
| `libexec/ec2/**`（EC2 に置く診断ゲートウェイと、導入・撤去スクリプトの雛形）・`libexec/commands/ssh.sh`（SSM 経由の導入と登録、`ssh verify`、`ssh rotate`、`ssh remove`。最後のホストでの `diag-ssh-<name>` の detach / delete も `remove` が持つ） | `docs/design-ec2-ssh.md` の第 2 節（名前）・第 4 節（セットアップと EC2 に作るもの。SSM の上限の実測値は第 4.3 節、`rotate` / `remove` とポリシーの片付けは第 4.6 節）・第 5 節（仕様）。実装の段階と未確認事項は第 10 節。偽の `aws` での検証は `tests/test_ssh_setup.py`（`setup` / `list` / `rotate` / `remove`。IAM の呼び出し順も）、`ssh verify` と `remove --print` は偽の `docker` で `tests/test_ssh_install.py` |
| `container/ec2`（調査コンテナの `ec2` ラッパー、二層の SSM セッションの後片付け、`--selftest`）・フックの `ec2` / `ssh` の判定・`Dockerfile` の session-manager-plugin・`container/method/06_EC2の中を調べる.md`・`survey-status` の登録済みホスト表示 | `.agents/rules/container.md` に加えて `docs/design-ec2-ssh.md` の第 7 節（自己診断の項目と SSM セッションの終わり方・起動時の後片付けの実測）・第 8 節（コンテナ側の部品とフックの判定）。通る例・落ちる例とラッパーの引数の渡し方・後片付けの呼び出し順は `tests/test_guards.py`（偽の `ssh` と `aws`）、配置は `tests/test_launcher.py`。`method/06` はゲートウェイの動詞（第 5 節）と食い違わせない |
| `diag-ssh-<name>` ポリシー（`role.sh` の作成・アタッチ、`credentials.sh` の `--policy-arns`、`verify.sh` の 6・7 項目目） | `.agents/rules/credentials.md` に加えて `docs/design-ec2-ssh.md` の第 6 節（ポリシーの内容と `PackedPolicySize` の実測）・第 7 節（検証）。偽の `aws` での検証は `tests/test_ec2_iam.py` |

上の規則は新規ファイルにも適用する。`.agents/rules/` はどのエージェントも自動では読み込まない。この表に従って読む。
`container/instructions/` は配布用の調査指示であり、開発中の自分の役割を切り替える指示ではない。
`method/` は編集・レビューするときだけ読む。

## 利用者との進め方

- 手順書を読むのはエージェント。ユーザーに節番号を案内して作業を委ねない。
- 必要な質問と、代われないホスト操作・ログイン・承認だけを依頼する。
- ホスト操作を依頼するときは、目的を 1 行添え、コマンドを 1 本渡し、結果を読んで次へ進む。ホストの入口は `aws-survey` で、引数なしの出力が次の 1 手を示す。利用者の端末では、その 1 手を「実行しますか？」と聞いてその場で実行し、起動まで進む（エージェントが叩いたときは端末ではないので案内だけ）。
- 実行環境のツール有無と権限を確認する。「aws / docker が無い」と決めつけない。
- `README.md` と `docs/security.md` はユーザー向けの入口。手順書は置かない。ホストの操作は `aws-survey` が案内から実行まで持ち、開発時の規則は `.agents/rules/` に置く。

## 変更の不変条件

守るべきことの一覧。理由・具体的な手順・触るときの注意は `.agents/rules/` にある。
同じ話が両方に出るのは重複ではなく、常に読む一覧と、そこを触るときだけ読む詳細の関係。
片方に寄せない（一覧を消すと常時の歯止めが無くなり、詳細を消すと理由が失われる）。

- 対象固有の値は `environment.json` に置き、スクリプトは `libexec/load-env.sh` から読む。
- `--policy-arns arn=...ReadOnlyAccess` を外さない。Deny のみのセッションポリシーと対で使う。
  `diag-ssh-<name>` はその後ろに並べる任意の追加で、許すのは `diag:ssh=<name>` タグ付きインスタンスへの `AWS-StartSSHSession` だけ。
  `ssm:SendCommand` を調査用ロールや一時キーに足さない。
- 長期 AWS キー・元プロファイル・資格情報の再発行機能を調査コンテナに渡さない。
- EC2 に残すもの（`libexec/ec2/`）の名前に「ai」「agent」「survey」を含めない。root 読み取り段は 5 動詞固定で、任意パスの読み取りを足さない。シェルを経由せず argv を list で渡す。
- ガードはイメージへ焼き込み、root 所有を保つ。プロンプトだけを安全性の根拠にしない。
- 調査の共通指示は `container/instructions/survey-agents.md`、詳細は `method/`。
- 対象固有の知識は `out/`、フェーズ固有の規則はその `00_目的と規則.md` に置く。
- `out/` を削除・整理しない。ノウハウの昇格では対象固有の例を落とし、元の記録は残す。
- 共通ルールはこのファイルと `.agents/rules/` が正本。`CLAUDE.md` に複製しない。
- **このリポジトリの追跡対象はすべて公開される。**実対象の構成・調査結果・そこから得た数字に触れる記述は、
  `out/`（Git 管理外）にだけ書く。`AGENTS.md`・`.agents/rules/`・`method/`・`docs/` は
  対象に依存しない書き方を保つ。

## 検証と引き継ぎ

- シェル変更: `bash -n`。ガード・起動配線の変更: `python3 -m unittest discover -s tests -v`。
- イメージ変更: 対象資格情報を渡さないコンテナでビルド・CLI 起動・権限を確認する。
- AWS ポリシー変更: ホストで `aws-survey verify` を再実行する（実 AWS が必要）。
- 実環境で未確認の事項を完了扱いしない。作業の区切りで、検証できたことと残っていることを区別してユーザーに伝える。

## コミットメッセージ

1 行目は Conventional Commits の型と、日本語の要約。本文は箇条書きで 3 つ程度、多くても 5 つ。

```
feat: 調査コンテナのログイン画面を整える

- ロケールを C.UTF-8 にして日本語ファイル名の文字化けを直す
- ログイン時の案内と survey-status の見た目を揃える
- シェル履歴をコンテナの作り直しをまたいで残す
```

- 型は `feat` / `fix` / `docs` / `refactor` / `test` / `chore`。スコープは付けない。
- 要約は「何をしたか」を 1 行で。なぜ・どうやっては本文に書く。
- 箇条書きは変更ごとに 1 行。変更が 1 つなら本文は省く。
- 5 つに収まらないなら、コミットを分ける。
