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
`scan` / `claude` / `codex` / `login` / `run` / `status` / `doctor` / `init` / `ls` / `ec2` / `lambda` / `ssh`）。実体は `role` / `credentials` / `verify` /
`run` / `scan` / `login` / `doctor` / `ls` / `ec2` / `ssh` / `lambda` が `libexec/commands/<名前>.sh`、`claude` / `codex` は `libexec/commands/agent.sh`
（第 1 引数がエージェント名）、`status` / `init` と段階の判定は `bin/aws-survey` 本体にある。
イメージのビルドは `libexec/docker.sh`（`run` と `lambda pull` と `libexec/container.sh` が共有する）。
調査コンテナ一式（一時キー・指示書・`method/`・`out/`・`code/`・Claude / Codex のボリューム）のマウント列は `libexec/launch.sh`
（`run` と `scan` が共有する）。`container.sh` とは分ける。あちらは調査エージェントに渡さない道具を一時キーだけで動かす経路で、
`out/` も指示書もボリュームも付けてはいけない。

引数なしの `aws-survey` は段階 4（一時キー）までを止まらずに進め、段階 5 以降は `guide_ready` で「準備完了」と次のコマンドを順に
案内して止まる（`ls` → `scan` → `claude` / `codex`。`run` は出さない）。`scan` は時間がかかり、`claude` / `codex` は端末を渡すので、
自動では実行しない。段階 5「初期調査」は `setup.scanned`（`scan` が成功時に分までの日時で書く）で済・未済を見る。
`out/00_進捗.md` があれば対話で始めた扱いで段階 6 に進める。

### 非対話の棚卸し（`scan`）

`aws-survey scan` は `launch.sh` のマウント列で `-it` を付けずに（stdin は `/dev/null`）`claude -p` か `codex exec` を 1 回走らせ、
出力をそのまま流す。ホストから渡す指示は「棚卸しだけ・ユーザーに聞かずに終える」に限り、対象の概要・調査項目・サービス名を含めない
（`tests/test_scan.py` が字面で見る）。棚卸しを承認前の前段と位置づける文言はコンテナ側（`survey-agents.md`・`method/00`・`04`）にあり、
Cost Explorer を入口にする話もそちらに書く。
Claude / Codex の認証はコンテナ内のボリュームに残る。判定は状態のボリューム 3 本だけを付けて各 CLI 自身に聞く
（`launch_agent_authenticated`: `claude auth status --json` / `codex login status`。未認証なら終了コード 1。2026-09-12 に実測）。
ファイル名を推測せず、トークンを環境変数で渡さない。
未認証なら端末で `-it` のログインだけ先に行う（`claude auth login` / `codex login --device-auth`）。
Claude はさらに `-p` の前にワークスペースの信頼を `.claude.json`（ボリュームの中）に記録する（`launch_trust_workspace`）。
未信頼だと非対話では信頼の確認画面が出ず、作業ディレクトリの `settings.json` の allow が無視される（2026-09-12 に実測）。
この「イメージの用意 → 認証の確認 → ログイン → 信頼の記録」は `launch.sh` の `launch_agent_login` が 1 つで持ち、`init`（聞き取りの最後。
端末で docker があるとき）・`login`（`libexec/commands/login.sh`）・`scan` が呼ぶ。`init` が済ませておくので、`scan` では通常「済んでいます」で通る。
使うエージェントは `environment.json` の `agent`（`{name, model, effort}`。`libexec/agents.sh`）に記録する。`init` が聞いて書き、
`scan --agent` も `aws-survey claude` / `codex` も `name` を「最後に使ったもの」として上書きする（別のエージェントに切り替えたときは
`model` / `effort` もそのエージェントの既定に戻る。前のものの値は渡せないため）。古い形（`"agent": "claude"`）も名前だけとして読む。
`model` / `effort` は起動時に CLI のフラグにして渡す（Claude Code は `--model` / `--effort`、Codex は `-m` / `-c model_reasoning_effort=`。
対話も非対話も同じ。`launch_agent_flags`）。既定は Claude Code が `opus` / `medium`、Codex が `gpt-5.6-sol` / `low`。モデルの候補は
`agents.sh` に並べるだけで、候補に無ければ手で入れられる（名前は各 CLI の都合で増減する）。イメージや設定ファイルには焼かない。
利用者に見せる `scan` の説明は「対象アカウントの初期調査を <エージェント> が行います（…数十分かかります）」（`scan_desc`）で、
「白紙の棚卸し」は開発側の言葉として文書とコメントに留める。
起動前に `docker ps` で `<name>`（対話コンテナ）と `<name>-scan` を見て、動いていれば止まる（同じ `out/` に 2 つのエージェントを走らせない）。
一時キーの残りが短ければ（総時間の半分、上限 30 分。この絶対値はホスト側だけに置く）発行し直してから始める。

利用者に見せる入口はリソース名（`ls` / `ec2` / `lambda`）で、ホストで動くかコンテナで動くかは必要な資格情報で内部的に決め、
利用者には見せない。元プロファイル（強い権限）が要るもの（`ssh setup` / `rotate` / `remove`、`lambda pull`）はホストで動き、
読み取り専用の一時キーで足りるもの（リソースの列挙、`ec2 --selftest`）は調査コンテナと同じイメージで `docker run --rm` の
1 コマンドとして動く（`libexec/container.sh` の `container_run`。渡すのは `$AWS_DIR` の ro マウントとリージョンだけで、`out/` も
`code/` も Claude / Codex のボリュームも付けない）。列挙は `libexec/inventory.sh` を `/x/` に ro マウントして貸す
（`container_inventory`。`lambda pull` が抽出器を貸すのと同じ型。イメージには焼かず、調査エージェントには届かない）。
出力は 1 行 1 JSON で、読めないサービスも `denied` の行を落とさない。画面に出すだけで `out/` には書かない
（対象の棚卸しを調査エージェントに先渡ししない規則は、ファイルに残さないことで守る）。
`ec2` / `lambda`（引数なし）は一覧から矢印キーで選ばせ（`libexec/menu.sh` の `choose_menu`。`init` と共有。`lambda` は Space で複数選べる
`choose_multi`）、選んだものを既存の `ssh.sh setup|verify|remove <対象>` に `exec`、`lambda.sh` の `cmd_pull` / `cmd_remove` に in-process で
渡す。端末でなければ一覧と `next_cmd` の案内だけで終わる。`choose_menu` / `choose_multi` は EXIT トラップを張って外すので、`trap ... EXIT` を張る前に呼ぶ。
準備が済んだ状態で終わるコマンド（`ls` / `lambda pull` / `credentials` / `verify`）の末尾は `load-env.sh` の `survey_next_cmd` で
「棚卸しがまだなら `scan`、済んでいれば記録してあるエージェント」を案内する。`run`（素のシェル）は案内しない。
一時キーの状態の判定（`key_state`）は `libexec/keys.sh` にあり、`bin/aws-survey` と `container.sh` が共有する。
`ssh` と `lambda` は任意の追加機能。段階 0〜4 の判定には組み込まず、段階 5（調査）に着いてから `show_extras` で現状と足し方を出す。
EC2 だけは登録のあとに 3 手（`role --create` → `credentials` → `ssh verify <host>`）が要るので、`ec2_pending` が
ファイルだけで途中かどうかを判定し、案内ループに乗せる。判定に使う記録は 3 つ: `setup.ssh_policy_attached`（`role --create` が書く。
`ssh remove` が最後のホストで消す）、`session.json` の `ssh_hosts`（`credentials` が発行時の登録済みホストを書く）、
`ssh.hosts.<host>.verified_at`（`ssh verify` が成功時に書く。`installed_at` より古ければやり直し）。
借りたロールでは `role --create` が管理者に頼む内容を出すだけなので、ループは状態が変わらないことを見て止まる。
管理者が付けたあとの `role --create` は「付いているポリシー」から記録して先へ進む。
`launch.sh`（`run` / `scan`）は `code/` を空でも作って常に読み取り専用でマウントする。調査中に `lambda pull` したものが起動し直さずに見えるようにするため
（`method/07` の依頼文がそれを前提にしている）。

`lambda pull` の取り出し専用の一時キー（調査用ロールを Lambda の読み取り 4 つだけのインラインポリシーで借りる。`--policy-arns` は渡さない）は、
ファイルに書かず、コマンドの引数（`ps` に出る）にも here-string（bash 3.2 では一時ファイルになる）にも載せない。`get-function` の応答には
環境変数の値とコードの署名付き URL が入るので、`--query` で要る項目だけ取り、URL は `curl -K -` の標準入力で渡す。内容は `docs/design-lambda-code.md` の第 4・6.2 節。
引数なしの `aws-survey` の判定は AWS を叩かずファイルだけで行う。
端末（標準入力と標準出力の両方）なら案内した 1 手を何も聞かずに実行する（`guide_needs_confirm`。`role --create` も準備に必ず要る 1 手なので
聞かない。段階 2 は `role`（判定だけ）を挟まず `role --create` に直行し、作れない権限なら `role.sh` の `show_cannot_create` が手順を出して止まる）。
端末でなければ 1 手ごとに `(Y/n)` で聞き、読めなければ案内だけで終わる（端末でない実行環境で黙って AWS を叩かないため）。
`ssh setup` の末尾も同じで、標準入力と標準出力の両方が端末なら聞かずに引数なしの `aws-survey` に exec して残りの 3 手へ進み、
端末でなければ `aws-survey` 1 本を案内する（`setup_continue`）。`role --create` → `credentials` → `ssh verify` は「手で 1 手ずつ進めるなら」の補足に留める。
`init` が聞くのは AWS への繋ぎ方と、調査に使うエージェントだけ（`AGENTS.md`「ホストと調査コンテナ」）。`name` / `source_profile` / `region` /
`role_name` の 4 つと、IAM ユーザーのログインでは `mfa_required` / `duration_seconds`、そのあとエージェント（`claude` / `codex`）とモデル・effort
（矢印キーの候補。候補に無ければ入力）。`account_id` は `source_profile` でログインしたアカウント（`sts get-caller-identity` の `Account`）で聞かない。
ロールはそのログインのアカウントに作られるので、別のアカウントを `account_id` にしても自分で作る経路では意味が無く、`role` はログインの
アカウント（Arn の 5 番目）と `account_id` が違えば自分で作る経路では止まり、借りる経路では ⚠ を出す。AWS Organizations で複数のアカウントが
あるときは「調べたいアカウントに入るプロファイルを選ぶ」が入口で、プロファイルの候補には Identity Center の `sso_account_id`（`aws configure get`。
AWS は叩かない）を添える。Organizations の一覧（`organizations list-accounts`）は管理アカウントでしか通らず、選んでもロールを作れないので使わない。
貸す相手（`principal_arn`）はいまのログインの自分だけ、`refresh_command` は aws-login に固定して聞かない（非対話の `--from` / `AWS_SURVEY_INIT_*` では
`account_id` も含めてどれも渡せる。エージェントは `AWS_SURVEY_INIT_AGENT` / `_AGENT_MODEL` / `_AGENT_EFFORT`、`--from` では `.agent.{name,model,effort}`。
未指定なら未定のまま `scan` が聞く）。項目を足すときは `environment.json` の雛形・`load-env.sh`・非対話モードの `AWS_SURVEY_INIT_*` を揃え、対象の
概要や調査項目に踏み込まない。`init` は `.gitignore` に `environment.json` / `trust.json` を入れる（無ければ作り、あれば足りない行だけ足す）。
書き出しと器の用意（`out/`・`AGENTS.md`・`CLAUDE.md`・`.gitignore`）は黙って行い、出すのは失敗と置き換えなかったものだけ。「対象」のまとめも
出さず、準備完了の案内（`guide_ready`）が対象・ロール・エージェントを出す。
画面は、決まった項目を「◆ 項目: 値」の 1 行に畳む（`init_begin` で見出しと補足を出し、`ask` / メニューのあと `init_done` が端末なら
そこまでを消して 1 行にする。聞かずに決まる項目は `init_fixed`）。端末でなければ消さず、見出しと補足のあとに 1 行が足される（テストはこちらを見る）。画面に出すパスはホームの下なら `~` で省略する（`ui_path`。`ui_kv` は値に自動で掛ける。
ファイルやコマンドに書く値には使わない）。
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
引数なしの `aws-survey` が端末で続けて実行するときは簡潔表示（`ui.sh` の「簡潔表示」の節。`AWS_SURVEY_COMPACT=1` と
`AWS_SURVEY_SPIN_DIR`）で、各コマンドは画面に回転する印付きの 1 行だけを持つ。`ui_head` と `ui_ok` はその行の文言を入れ替え、
`ui_text` / `ui_kv` / `ui_raw` / `ui_skip` は記録にだけ残り、`ui_warn` / `ui_err` / `ui_die` はその行の上に残る。終わったら親が
最後の `ui_ok` の文言で ✔ の 1 行にし、失敗したら記録の全部を見せて止まる。だから各コマンドの最後の `ui_ok` は、1 行で結果が
分かる文にする（「発行しました」ではなく「一時キーを発行しました（60 分）」）。子の標準出力は記録へ向くので、利用者に聞く・
ログインのコマンドを走らせるところは `ui_pause` → `ui_tty` / `/dev/tty` → `ui_resume` で囲む（`credentials` の 2 か所が例）。
`run` は端末を渡すので畳まず `AWS_SURVEY_COMPACT=1` だけ（見出し・補足を出さず ✔ ⚠ ✗ だけ）。`init` は質問なので親は畳まず、自分で 1 項目 1 行に畳む。
利用者向けの文に `own_role` / `route` / `setup.*` / `PackedPolicySize` のような内部の値名を書かない。
「ロールの用意のしかた（自分で作る / 既存のロールを借りる / 管理者に信頼してもらう）」のように言い換え、
値名は利用者が `environment.json` を手で直す場面でだけ添える。「次に打つコマンド」は `next_cmd` で、説明は「何をするか」を書く。

## 信頼ポリシーの貸す相手（`principal_arn`）

`init` はいまのログイン（`sts get-caller-identity` の Arn）を「自分だけ」として書き、選ばせない。ログインを読めなければ手で入れさせる。
「同じロールでログインした人なら誰でも」（パス付きのロール ARN）にしたいときは非対話（`--from` / `AWS_SURVEY_INIT_PRINCIPAL_ARN`）で渡すか
`environment.json` を直す。そのときセッション ARN から `arn:aws:iam::<ID>:role/<名前>` と組み立てるとパス（Identity Center なら
`aws-reserved/sso.amazonaws.com/<region>/`）が落ち、信頼ポリシーが `MalformedPolicyDocument` で拒否される（2026-09-10 に実環境で発生）。
`iam get-role` で取ること。
`role` の判定は文字の一致ではなく「`principal_arn` の相手が貸す相手に含まれるか」で見る（`principal_coverage`）。
含まれるが書き方が違うだけなら不足にしない。信頼ポリシーのほうが広い（ロール ARN）場合も、狭い（いまのログインのセッション ARN だけ）場合も同じ。
含まれないときと、MFA 必須なのに条件が無いときだけ不足にする。1 段目の「いまのログインと `principal_arn` が一致するか」も、
`principal_arn` がいまのログインの借りているロール ARN なら一致とみなす（`session_in_role`）。
Identity Center でログインしたセッションには、MFA を通っていても `aws:MultiFactorAuthPresent` が付かない。信頼ポリシーに MFA の条件を
付けると AssumeRole が AccessDenied になる（2026-09-11 に実測。条件ごとに別のロールを作り、反映を待ってから約 1 分試して確かめた。
1 つのロールの信頼ポリシーを書き換えた直後の 1 回目で判定すると、古いポリシーで評価されて逆の結論になる。実際に一度そう誤った）。
そのため Identity Center のログイン（`is_sso_arn`。ロール名が `AWSReservedSSO_` で始まる）では、`init` は MFA を聞かず（見出しも出さず）false にし、
非対話で true を渡されたら止める。`role` は true なら false にするよう、信頼ポリシーに条件があれば借りられないと不足に挙げ、
`role --create` は true のままでは条件を書き込まずに止める。Identity Center 以外で借りたロールからの MFA の扱いは未確認なので、今までどおり聞く。

## 一時キーの長さ（`duration_seconds`）とロールチェーン

ロールを借りた状態のログイン（Identity Center は常にこれ。`sts get-caller-identity` の Arn が `assumed-role/`）から調査用ロールを
借りると、AWS の決まり（ロールチェーン）で一時キーは 1 時間が上限になる。ロール側の `MaxSessionDuration` を延ばしても変わらず、
`AssumeRole` が `ValidationError ... 1 hour session limit for roles assumed by role chaining` で落ちる（2026-09-12 に実環境で発生。
`role --create` が上限を 3 時間にしていても同じ）。1 時間を超えるには IAM ユーザーの長期キー（MFA 付き）でログインする経路が要る。
判定は `ui.sh` の `is_chained_arn`（ログインの Arn）と `is_chained_principal`（`principal_arn`。セッション ARN でもロール ARN でも
ロールを借りてのログイン）で行い、`init` は `duration_seconds` を聞かずに 3600 に固定してまとめに「セッションの制限時間」として出し
（非対話で超える値は拒否）、`role` は ⚠ を出し、
`credentials` は発行前に判定して「`environment.json` の `auth.duration_seconds` を 3600 に直して、そのまま発行しますか？」と聞く
（`read_line`。読めなければ案内だけで止まる）。AWS 側で断られたときも同じ案内で直して発行し直す。`update-role` を勧めない。
既定の 3 時間（`DURATION_DEFAULT`）は IAM ユーザーのログイン向けで、変えない。

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
