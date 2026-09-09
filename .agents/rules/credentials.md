# 資格情報とポリシーを触るとき

## 対象固有の値をスクリプトや文書に書かない

アカウント ID・ロール名・プロファイル名・リージョン・セッションの長さは
すべて `environment.json` が持つ。直書きすると、次の対象に向けたときに黙って壊れる。
`libexec/load-env.sh` 経由で読むこと。

| 値 | 置き場 | 直書きしてよい場所 |
| --- | --- | --- |
| アカウント ID・ロール名・プロファイル名 | `environment.json` | どこにもない |
| リージョン | `environment.json`（`claude-ro` プロファイルにも入る） | なし |
| セッションの長さ | `environment.json` の `auth.duration_seconds` | なし。調査エージェント向けの文書にも書かない |
| 元プロファイルの更新方法 | `environment.json` の `auth.refresh_command` | なし。環境ごとに変わる唯一の操作 |
| 現在のフェーズ | `environment.json` の `phase_dir` | なし |

セッション時間の絶対値を調査エージェント向けの文書に書かないこと。「1 時間で失効」「残り 15 分」と書くと、
12 時間のセッションでは無意味に細切れに動き、30 分のセッションでは常に着手禁止になる。
残り時間の判断は `container/survey-status` がセッションの長さから毎回計算して表示する。
調査エージェント向けの文書には「`survey-status` の表示に従う」とだけ書く。

対象フォルダ直下の `trust.json` は `aws-survey role --create` の生成物。手で書かない。本体側には置かない。

## パスは 3 つの変数で解決する

ホスト側スクリプトは `libexec/load-env.sh` を source し、次の変数だけでパスを組み立てる。

| 変数 | 指すもの | 既定 |
| --- | --- | --- |
| `AWS_SURVEY_HOME`（`LIBEXEC_DIR` = その `libexec/`） | 本体。`bin/`・`libexec/`・`container/`・`Dockerfile` | `load-env.sh` から見た `libexec/` の親 |
| `AWS_SURVEY_DIR` | 対象フォルダ。`environment.json`・`out/`・`trust.json` | カレントディレクトリ（絶対パスに正規化される） |
| `AWS_DIR` | 一時キー（`credentials` / `config` / `session.json`） | `~/.aws-survey/<name>/` |

`AWS_SURVEY_HOME` は環境変数が最優先。Homebrew 版の `bin/aws-survey` は env ラッパーで、バージョンを含まない
`opt` のパスを渡す（理由は `docs/release.md`）。実体パスからの解決に変えると、`status` が表示する
「本体」がバージョン入りになり、利用者がそれを控えるとアップグレードで切れる。

コンテナ側の名前（プロファイル `claude-ro`、マウント先 `~/.aws-claude`）は互換のために変えない。
ガード・`survey-status`・過去の `out/` の記録がこの名前を前提にしている。

利用者に見せる「次に打つコマンド」は `load-env.sh` が入れる `AWS_SURVEY_CMD`（`aws-survey` か `<本体>/aws-survey`）で
組み立てる。`./run.sh` のようなスクリプト名を案内文に書かない。入口は `aws-survey`（`role` / `credentials` / `verify` /
`run` / `status` / `doctor` / `init`）。実体は `role` / `credentials` / `verify` / `run` / `doctor` が
`libexec/commands/<名前>.sh`、`status` / `init` と段階の判定は `bin/aws-survey` 本体にある。
引数なしの `aws-survey` の判定は AWS を叩かずファイルだけで行う。
案内した 1 手を続けて実行するのは、利用者に `(Y/n)` で聞いて「はい」と答えたときだけ。読めなければ案内だけで終わる
（端末でない実行環境で黙って AWS を叩かないため）。
`init` が聞くのは AWS への繋ぎ方の 9 項目だけ（`AGENTS.md`「ホストと調査コンテナ」）。項目を足すときは `environment.json` の雛形・`load-env.sh`・
非対話モードの `AWS_SURVEY_INIT_*` を揃え、対象の概要や調査項目に踏み込まない。
全角括弧が変数の直後に来るときは `$VAR（` ではなく `${VAR}（` と波括弧で囲む。
本体の場所を `$PWD` で、対象フォルダの場所をスクリプトの位置で決めない。`cd` してから相対パスで
参照するのもやめる。対象を切り替えても一時キーが上書きされないよう、`AWS_DIR` は `name` ごとに分ける。
リポジトリ直下に `run.sh` などのラッパーを復活させない。開発中の動作確認は `./bin/aws-survey` を直接叩くか、
別フォルダから `AWS_SURVEY_DIR` / `--dir` で対象を指す。

## 利用者に見せる出力

表示は `libexec/ui.sh` の部品（`ui_title` / `ui_head` / `ui_kv` / `ui_ok` / `ui_warn` / `ui_err` / `ui_skip` / `ui_text` / `ui_raw` /
`ui_cmd` / `next_cmd` / `also_cmd` / `ui_die`）で組む。`aws-survey` と `load-env.sh` が読み込むので、`echo "==> ..."` や独自の記号を足さない。
`next_cmd` / `also_cmd` は `AWS_SURVEY_CHAIN=1` のとき黙る。引数なしの `aws-survey` が続けて実行している最中で、
次の案内は判定し直した側が出すため。案内を自前の `printf` で書くと、この抑止をすり抜けて二重に出る。
色と記号の装飾は標準出力が端末で `NO_COLOR` が無いときだけ付き、端末でなければ素の文字列になる（テストはこちらを見る）。
利用者向けの文に `own_role` / `route` / `setup.*` / `PackedPolicySize` のような内部の値名を書かない。
「ロールの用意のしかた（自分で作る / 既存のロールを借りる / 管理者に信頼してもらう）」のように言い換え、
値名は利用者が `environment.json` を手で直す場面でだけ添える。「次に打つコマンド」は `next_cmd` で、説明は「何をするか」を書く。

## AWS の API に渡す文字列

IAM の `--description` や `--role-session-name` に日本語を入れない。IAM は ASCII と Latin-1 しか受け付けず、
偽 `aws` のテストでは検出できない（2026-09-08 に実環境で `ValidationError`）。利用者に見せる文言は標準出力側に書く。

## `session-guard.json` に項目を足すとき

`PackedPolicySize` を確認する。上限は平文 2048 字ではなく圧縮後のサイズで、
平文 1,020 字でも 114% 超過した実績がある（2026-09-08 の実測は 71%）。
100% を超えると `PackedPolicyTooLarge` で発行できない。
`aws-survey credentials` が発行時に使用率を表示する。

変更したら `aws-survey verify` をやり直す。

## `libexec/commands/credentials.sh` を触るとき

`--policy-arns arn=...ReadOnlyAccess` を外さないこと。
`session-guard.json` は Deny しか書いていないため、外すと Allow がゼロになり全 API が拒否される。

## 資格情報の再発行を調査コンテナに移さない

移すには次の 4 つを同時に壊すことになり、ホストと調査コンテナの分担がどちらも意味を失う。

| 壊すもの | いま何をしているか |
| --- | --- |
| セッションポリシー | `sts:AssumeRole*` を Deny |
| フック | `assume-role` / `get-session-token` / `get-federation-token` を実行前に拒否 |
| 資格情報のマウント | ホストの `~/.aws-survey/<name>/` をコンテナの `~/.aws-claude` に読み取り専用でマウント |
| 強い権限のキー | コンテナに渡していない（`~/.aws` はマウントしない） |
