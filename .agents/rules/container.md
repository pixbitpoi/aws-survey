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

対象システムの知識は調査エージェントが `out/report/前提.md` に書く。ホストは用意しない。
前提を先渡ししないこと。基礎調査は白紙で調べて報告を書き、それを手にユーザーと話す設計で、
先に教えると発見ではなく確認をするようになる。

## 承認や計画のゲートを置かない

調査エージェントが「目的と規則」をユーザーに承認させてから動く運用は、うまく働かなかった（計画に無い質問に答えず、
1 つ聞くにも計画と承認が要った）。`method/` と `survey-agents.md` に、事前の草案・承認・規則ファイルを前提にする手順を戻さない。
何を書いてよいか（推測は推測と分けて書く、「気になる点」は根拠つきで書く、「不要」とは断定しない）は `method/report.md` に直接書く。

`method/` は 3 本だけ。番号は付けない（読む順・仕事の順・時系列が混ざり、`02` が「あと」で `04` が「型」のようなずれを生む）。

| 書きたいこと | 置き場 |
| --- | --- |
| 対象システムの知識 | ホストでは作らない（調査エージェントが `out/report/前提.md` に書く） |
| 報告に何を書けば役に立つか（骨子・見本・完了の基準・検証） | `method/report.md` |
| 基礎調査の進め方・環境の確認・コマンドの作法・読み違い・ユーザーとの進め方 | `method/survey.md` |
| EC2 の中と Lambda のコードの読み方と、経路が無いときの依頼文 | `method/routes.md` |
| 現場で見つかった、どの対象でも効くノウハウ | `method/survey.md`「読み違えやすいこと」（`out/.survey/notes.md` から昇格させる） |
| 常に意識させたいこと（読み取りのみ・出力先・マスク） | `container/instructions/survey-agents.md`。薄く保つ |

量の配分を「守り」に寄せない。以前の `method/` は約 1,200 行のうち「良い報告とは何か」が 40 行ほどで、同じ規則が 5〜6 か所に繰り返されていた。
その結果、報告が「IAM ロールは 9 個」のような件数の羅列になった。同じ規則を 2 か所以上に書かない。
情報の信頼性のために守ること（事実と推測の分離・出典・読めなかったの明記）は残し、それ以外は「役に立つものを出すため」の指示にする。

`container/instructions/survey-agents.md` は毎回すべて読まれる。厚くすると、その回に関係ない指示が
常に文脈を占める。詳細は `method/` に置く。

## 反映のされ方が 3 通りある

| ファイル | 渡し方 | 変更したら |
| --- | --- | --- |
| `container/method/` | ディレクトリのマウント | その場で効く |
| `container/instructions/survey-agents.md` と `survey-claude.md` | 単一ファイルのマウント | 再起動が要る。エディタがファイルを置き換えるとコンテナ側は古いまま |
| `container/settings.json` / `codex/` / `hooks/` / `bashrc` / `survey-status` / `survey-ui.sh` / `ec2` | イメージに焼き込み | 再ビルドが要る（`aws-survey scan` / `claude` / `codex` / `run` が毎回ビルドする） |

コンテナへ渡るものは `container/` だけではない。対象フォルダの `out/` はディレクトリのマウントなので
双方向にその場で効き（報告・前提・確認事項の回答欄を直せば調査エージェントがすぐ読む）、`environment.json` の
`region` / `name` は起動時に読むので次回の起動（`aws-survey scan` / `claude` / `codex` / `run`）から効く。
同じ `out/` に対して 2 つのエージェントを同時に走らせない。台帳と監査ログの書き手が競合する。
`aws-survey scan` は起動前に `docker ps` で対話コンテナと自分の名前を見て、動いていれば止まる。

`aws-survey scan` は調査エージェントを非対話（`claude -p` / `codex exec`）で 1 回動かす。ホストが渡す指示は「ユーザーが応答できない回」と
「基礎調査を報告まで」であり、その回に何をするかは `method/survey.md` の「ユーザーが応答できない回」が持つ。その回に一時キーが入れ替わることは
「コンテナから見える事実」として `method/survey.md` と `survey-agents.md` に書く（入れ替える仕組みは `.agents/rules/credentials.md`）。
ホストのコマンド名はそこにも書かない。

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
| Claude Code | `settings.json` の `Edit(//home/node/aws-survey/out/.survey/env/aws-audit.log)` deny と、共通 Bash ガードの `has 'aws-audit'`。両方必要 |
| Codex | `codex/requirements.toml` が強制する管理対象 PreToolUse の `hooks/codex-guard.py`。コマンドとパッチ先を検査する |

Claude 側の 2 経路が揃っていることは `tests/test_guards.py` の `AuditLogPermissions` が確認する。

- 共通の AWS 許可・拒否規則は `hooks/aws-readonly-guard.sh` に置く。Codex 側へ複製しない。
  EC2 の中を調べる経路（`ec2` は単独コマンドだけ通す、`ssh` / `scp` / `sftp` / `session-manager-plugin` の直接実行は拒否）も同じ場所。
  `ec2` ラッパー（`container/ec2`）はガードと同じく root 所有でイメージへ焼き込む。仕様と自己診断は `docs/design-ec2-ssh.md` の第 7・8 節。
- Codex の対応範囲: 通常コマンドは単独実行で、引用符内の `jq` フィルタは使える。
  インタプリタや MCP ツールの追加は対応範囲外。`unified_exec` は無効のまま（理由は `codex/*.toml` のコメント）。
- 管理設定・フックはイメージへ root 所有で焼き込む。マウントに移していないことは
  `tests/test_launcher.py` の `BakedNotMounted` が確認する（理由もそちら）。
- Claude Code は `~/.local` のネイティブ版、Codex は npm prefix を `~/.npm-global` に向けた npm 版で、
  どちらも node 所有の名前付きボリュームに置き、自分で更新する。`/usr/local` は root 所有のまま。
  ガードは Codex の機能契約に乗っているため、`Dockerfile` の `CODEX_VERSION` を上げたら
  `tests/container_smoke.py` を通す。配置と理由は `tests/test_launcher.py` の `CliInstallLayout`。
- コンテナ専用の Codex 設定は `sandbox_mode = "danger-full-access"`（隔離は Docker が担う）。
  ホストの Codex にコピーしない。Docker の privileged・追加 capability・seccomp 無効化も使わない。
- この仕組みで防げない範囲は `docs/security.md`「これで保証されないこと」が正本。
  フックを独立監査や完全な改ざん防止と説明しない。

恒久的な許可の変更は、対象エージェントのガードとそのテストを同時に直す。
`hooks/` や `settings.json` / `codex/` を変えたら、コンテナ内の環境の確認をやり直させる。
既存の `out/.survey/env/check.md` は消さず、そのエージェントに再確認と追記を頼む。
`libexec/session-guard.json` やロールのポリシーを変えたときは、代わりにホストで `aws-survey verify`
（`.agents/rules/credentials.md`）。調査対象のアカウントを変えたときは両方。

## `out/` の 2 区画と、`.survey/` を隠す理由

`out/` は `report/`（人が読む成果物）と `.survey/`（生データ・台帳・ノウハウ・調査ログ・環境の記録）の 2 つだけ。
仕事ごとのフォルダは作らない。別の種類の仕事の成果物は `report/<仕事の名前>.md` として増え、生データは同じ `.survey/raw/` に足す
（基礎調査の `raw/` を土台にするので、分ける理由がない）。台帳は `.survey/state.md` の 1 本で、全体と作業の 2 段にしない
（仕事が 1 つのうちは同じ 1 行を 2 か所に書くだけだった）。番号付きのファイル名も使わない（読む順・仕事の番号・時系列が混ざる）。

`.survey/` をドットで始めるのは、利用者が `out/` を開いたとき `report/` だけが見えるようにするため。生データは人が読むものではないので `.survey/raw/`。
エージェントには `ls -A` で見せる（`survey-agents.md`）。Claude Code の権限の glob（`out/**`）がドットで始まるフォルダを
含まない実装があるので、`settings.json` は `out/.survey/**` を Read / Edit の allow に明示している。外さないこと。

調査ログ（`.survey/log/`）に叩いたコマンドの列挙を書かせない。監査ログ（`.survey/env/aws-audit.log`）が全件を持つので重複になる。
ログに残すのは判断と訂正の経緯だけ。`.survey/` の中のファイル名は英語（`NN-<topic>.md`）。人が読まない区画なので日本語名にしない。

`out/.survey/env/` に入るのは `check.md`（初回に調査エージェントが書く）と `aws-audit.log`（フックが書く）。
どちらもこの環境が期待どおり動いているかの記録で、調査の成果物ではない。だから `report/` と分ける。

この 2 つは対で読む。離して置かないこと。監査ログはフックが `out/` 配下にしか書けないので、
動作確認だけ別の場所へ移すと証跡が対で読めなくなる。

環境の自己点検を調査の仕事にしないこと。`report/` は調査の成果物の置き場であり、
「自分を縛る仕組みを検証せよ」という指示は、調査エージェントに知らせない前提を持ち込む。

## 報告は「何をしていて、どう組まれていて、どこが気になるか」に答えさせる

`method/report.md` の骨子は「このアカウントは何か → 構成図（Mermaid）→ 系統ごとの構成 → 系統に入らないもの → 権限とアクセス → 気になる点 →
外からは分からないこと → 付録: 網羅性」。本文をサービスのカテゴリ順にすると、権限・ネットワーク・稼働の事実が別々の節に散り、
繋げれば言えること（設定が指す先が無く、権限も無く、動いた形跡も無い）が書かれなくなる。カテゴリ順の節を本文に戻さない。
件数だけの文（「〜は N 個」）を本文に書かせず、主要なリソースは 1 行 1 リソースの表（名前・役割・稼働・根拠）で書かせる。
付録の表も領域ごとの 1 行に「N 台」と書くだけの見本にしない（以前の見本 `| C | EC2 | 3 台 |` がそのまま報告の書き方になった）。
「気になる点」は書かせる。「基礎調査では評価しない」の規則は、役に立つ観察まで止めていたので外した。
残す歯止めは「推測は推測と分かる書き方で根拠を添える」「不要・消してよいと断定しない」の 2 つだけ。
検証（別の回で `raw/` と突き合わせる）には「正しいか」に加えて「役に立つか」（完了の基準）を見させる。

`ユーザー確認事項.md` は「質問（見出し）→ 背景 2〜3 文 → 回答欄」の 3 つで書かせる。問いを吟味する観点（なぜ聞くのか・
なぜ AWS から分からないのか・答えの使い道）を項目として並べると、質問が 5 つに見える。回答欄はユーザーが書く入口で、
エージェントは回の始めに読み、書かれていれば答えとして扱う（`survey-agents.md`・`method/survey.md`）。

## `scan` は入口で経路を足し、2 回目は続きであって、やり直しではない

「ユーザーが応答できない回」でも、`survey-status` に出ている経路（登録済みホスト・取り出してあるコード）は読ませる。
端末で動く `scan` は、起動前の量の見積もりで EC2（SSM 管理下）と Lambda を知るので、そこで経路を足すかを聞き、
`ssh setup` / `lambda pull` を通してからエージェントを 1 回起動する（`.agents/rules/credentials.md`「非対話の棚卸し」）。
初回の報告に中まで入る方が、利用者にとって価値がある。エージェントは `survey-status` から経路を知るだけで、ホストから対象の概要は渡さない。
報告が既にある回は `method/report.md` の検証から始め、経路と回答欄を読んで報告を更新する（`method/survey.md`）。

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

echo '{"tool_name":"Bash","tool_input":{"command":"aws ec2 describe-vpcs > out/.survey/raw/raw-x.json"}}' | bash $H; echo "exit=$?"
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
| `container/method/survey.md` | コマンドの作法（保存の形） |
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
