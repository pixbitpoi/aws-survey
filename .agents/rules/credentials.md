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
| 元プロファイルの更新方法 | `environment.json` の `auth.refresh_command`（既定は aws-login） | なし。環境ごとに変わる唯一の操作 |
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
`run` / `status` / `doctor` / `init` / `ls` / `ec2` / `lambda` / `ssh`）。実体は `role` / `credentials` / `verify` / `run` / `doctor` /
`ls` / `ec2` / `ssh` / `lambda` が `libexec/commands/<名前>.sh`、`status` / `init` と段階の判定は `bin/aws-survey` 本体にある。
イメージのビルドは `libexec/docker.sh`（`run` と `lambda pull` と `libexec/container.sh` が共有する）。

利用者に見せる入口はリソース名（`ls` / `ec2` / `lambda`）で、ホストで動くかコンテナで動くかは必要な資格情報で内部的に決め、
利用者には見せない。元プロファイル（強い権限）が要るもの（`ssh setup` / `rotate` / `remove`、`lambda pull`）はホストで動き、
読み取り専用の一時キーで足りるもの（リソースの列挙、`ec2 --selftest`）は調査コンテナと同じイメージで `docker run --rm` の
1 コマンドとして動く（`libexec/container.sh` の `container_run`。渡すのは `$AWS_DIR` の ro マウントとリージョンだけで、`out/` も
`code/` も Claude / Codex のボリュームも付けない）。列挙は `libexec/inventory.sh` を `/x/` に ro マウントして貸す
（`container_inventory`。`lambda pull` が抽出器を貸すのと同じ型。イメージには焼かず、調査エージェントには届かない）。
出力は 1 行 1 JSON で、読めないサービスも `denied` の行を落とさない。画面に出すだけで `out/` には書かない
（対象の棚卸しを調査エージェントに先渡ししない規則は、ファイルに残さないことで守る）。
`ec2` / `lambda`（引数なし）は一覧から矢印キーで選ばせ（`libexec/menu.sh` の `choose_menu`。`init` と共有）、選んだ 1 つを既存の
`ssh.sh setup|verify|remove <対象>` に `exec`、`lambda.sh` の `cmd_pull` / `cmd_remove` に in-process で渡す。端末でなければ一覧と
`next_cmd` の案内だけで終わる。`choose_menu` は EXIT トラップを張って外すので、`trap ... EXIT` を張る前に呼ぶ。
一時キーの状態の判定（`key_state`）は `libexec/keys.sh` にあり、`bin/aws-survey` と `container.sh` が共有する。
`ssh` と `lambda` は任意の追加機能。段階 0〜4 の判定には組み込まず、段階 5（調査）に着いてから `show_extras` で現状と足し方を出す。
EC2 だけは登録のあとに 3 手（`role --create` → `credentials` → `ssh verify <host>`）が要るので、`ec2_pending` が
ファイルだけで途中かどうかを判定し、案内ループに乗せる。判定に使う記録は 3 つ: `setup.ssh_policy_attached`（`role --create` が書く。
`ssh remove` が最後のホストで消す）、`session.json` の `ssh_hosts`（`credentials` が発行時の登録済みホストを書く）、
`ssh.hosts.<host>.verified_at`（`ssh verify` が成功時に書く。`installed_at` より古ければやり直し）。
借りたロールでは `role --create` が管理者に頼む内容を出すだけなので、ループは状態が変わらないことを見て止まる。
管理者が付けたあとの `role --create` は「付いているポリシー」から記録して先へ進む。
`run.sh` は `code/` を空でも作って常に読み取り専用でマウントする。調査中に `lambda pull` したものが起動し直さずに見えるようにするため
（`method/07` の依頼文がそれを前提にしている）。

`lambda pull` の取り出し専用の一時キー（調査用ロールを Lambda の読み取り 4 つだけのインラインポリシーで借りる。`--policy-arns` は渡さない）は、
ファイルに書かず、コマンドの引数（`ps` に出る）にも here-string（bash 3.2 では一時ファイルになる）にも載せない。`get-function` の応答には
環境変数の値とコードの署名付き URL が入るので、`--query` で要る項目だけ取り、URL は `curl -K -` の標準入力で渡す。内容は `docs/design-lambda-code.md` の第 4・6.2 節。
引数なしの `aws-survey` の判定は AWS を叩かずファイルだけで行う。
案内した 1 手を続けて実行するのは、利用者に `(Y/n)` で聞いて「はい」と答えたときだけ。読めなければ案内だけで終わる
（端末でない実行環境で黙って AWS を叩かないため）。
`ssh setup` の末尾も同じで、標準入力と標準出力の両方が端末のときだけ「続けて `aws-survey` を実行しますか？」と聞き、
「はい」なら引数なしの `aws-survey` に exec して残りの 3 手へ進む（`setup_offer_continue`）。案内文では `aws-survey` 1 本を主にし、
`role --create` → `credentials` → `ssh verify` は「手で 1 手ずつ進めるなら」の補足に留める。
`init` が聞くのは AWS への繋ぎ方の 9 項目だけ（`AGENTS.md`「ホストと調査コンテナ」）。項目を足すときは `environment.json` の雛形・`load-env.sh`・
非対話モードの `AWS_SURVEY_INIT_*` を揃え、対象の概要や調査項目に踏み込まない。
`refresh_command` の既定は `aws-login --profile <元プロファイル>`（`refresh_default`）。`<名>-mfa` は aws-login が作る
一時キーの保存先なので、渡すのは `-mfa` を外した元の名前。aws-login は formula の依存なので、在る前提で既定に出す。
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

## 信頼ポリシーの貸す相手（`principal_arn`）

`init` の 3 問目は、いまのログイン（`sts get-caller-identity`）から「自分だけ」と、ロールを借りてログインしているときの
「同じロールでログインした人なら誰でも」を番号の選択肢に出す（`choose_principal`）。後者のパス付きロール ARN は `iam get-role` で取る。
セッション ARN から `arn:aws:iam::<ID>:role/<名前>` と組み立てるとパス（Identity Center なら `aws-reserved/sso.amazonaws.com/<region>/`）が落ち、
信頼ポリシーが `MalformedPolicyDocument` で拒否される（2026-09-10 に実環境で発生）。
`role` の判定は文字の一致ではなく「`principal_arn` の相手が貸す相手に含まれるか」で見る（`principal_coverage`）。
含まれるが書き方が違うだけなら不足にしない。信頼ポリシーのほうが広い（ロール ARN）場合も、狭い（いまのログインのセッション ARN だけ）場合も同じ。
含まれないときと、MFA 必須なのに条件が無いときだけ不足にする。1 段目の「いまのログインと `principal_arn` が一致するか」も、
`principal_arn` がいまのログインの借りているロール ARN なら一致とみなす（`session_in_role`）。
Identity Center でログインしたセッションには、MFA を通っていても `aws:MultiFactorAuthPresent` が付かない。信頼ポリシーに MFA の条件を
付けると AssumeRole が AccessDenied になる（2026-09-11 に実測。条件ごとに別のロールを作り、反映を待ってから約 1 分試して確かめた。
1 つのロールの信頼ポリシーを書き換えた直後の 1 回目で判定すると、古いポリシーで評価されて逆の結論になる。実際に一度そう誤った）。
そのため Identity Center のログイン（`is_sso_arn`。ロール名が `AWSReservedSSO_` で始まる）では、`init` は MFA を聞かずに false にし、
非対話で true を渡されたら止める。`role` は true なら false にするよう、信頼ポリシーに条件があれば借りられないと不足に挙げ、
`role --create` は true のままでは条件を書き込まずに止める。Identity Center 以外で借りたロールからの MFA の扱いは未確認なので、今までどおり聞く。

## AWS の API に渡す文字列

IAM の `--description` や `--role-session-name` に日本語を入れない。IAM は ASCII と Latin-1 しか受け付けず、
偽 `aws` のテストでは検出できない（2026-09-08 に実環境で `ValidationError`）。利用者に見せる文言は標準出力側に書く。

## `session-guard.json` に項目を足すとき

`PackedPolicySize` を確認する。上限は平文 2048 字ではなく圧縮後のサイズで、
平文 1,020 字でも 114% 超過した実績がある（2026-09-08 の実測は 71%。2026-09-10 は `ReadOnlyAccess` と対で 31%、
`diag-ssh-<name>` を並べて 32〜33%。2026-09-11 に `lambda:GetFunction` / `lambda:GetLayerVersion` を足して、`diag-ssh-<name>` と並べて 33%）。
100% を超えると `PackedPolicyTooLarge` で発行できない。
`aws-survey credentials` が発行時に使用率を表示する。

変更したら `aws-survey verify` をやり直す。

## `libexec/commands/credentials.sh` を触るとき

`--policy-arns arn=...ReadOnlyAccess` を外さないこと。
`session-guard.json` は Deny しか書いていないため、外すと Allow がゼロになり全 API が拒否される。

`environment.json` に `ssh.hosts` があるときは、その後ろに `diag-ssh-<name>`（`role --create` が作る顧客管理ポリシー。
`DIAG_POLICY_ARN`）を並べる。空のときは渡さない（ポリシーが無くても発行できること）。`--policy-arns` で渡したポリシーを
あとから消すと（`ssh remove` が最後のホストで消す）、その一時キーは読み取りも含めて全部拒否される。消したら発行し直す案内を出す。ロールセッション名の接頭辞
`SESSION_NAME_PREFIX` は `load-env.sh` が持ち、`diag-ssh-<name>` の `ssm:TerminateSession` / `ResumeSession` の資源
（`session/<接頭辞>-*`）と一致していなければならない。片方だけ変えない。内容は `docs/design-ec2-ssh.md` の第 6 節。

## 資格情報の再発行を調査コンテナに移さない

移すには次の 4 つを同時に壊すことになり、ホストと調査コンテナの分担がどちらも意味を失う。

| 壊すもの | いま何をしているか |
| --- | --- |
| セッションポリシー | `sts:AssumeRole*` を Deny |
| フック | `assume-role` / `get-session-token` / `get-federation-token` を実行前に拒否 |
| 資格情報のマウント | ホストの `~/.aws-survey/<name>/` をコンテナの `~/.aws-claude` に読み取り専用でマウント |
| 強い権限のキー | コンテナに渡していない（`~/.aws` はマウントしない） |
