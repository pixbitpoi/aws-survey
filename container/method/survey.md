# 調べ方 — 基礎調査の進め方と、この環境の作法

基礎調査の目的は、`method/report.md` の骨子を埋められるだけの事実を集めることです。
どの AWS アカウントでも同じ型で進めます。ユーザーに目的を聞いてから始めるのではなく、白紙で調べます。
先に「何があるはずか」を聞くと、発見ではなく確認になり、想定外のものを見落とします。

## 最初の 1 回だけ: この環境で何ができるか

`out/.survey/env/check.md` が無いか、使用エージェント（Claude Code / Codex）の確認がまだなら、先に確かめて記録します。
拒否されるものは拒否されるのが期待どおりで、回避策を探さないでください。

| 番号 | 実行すること | 期待 |
| --- | --- | --- |
| 1 | `aws sts get-caller-identity` | Arn が `assumed-role/…`（借りたロール） |
| 2 | `aws ec2 describe-vpcs --max-items 1 > out/.survey/env/raw-verify.json` | 保存できる |
| 3 | `aws ec2 create-tags --resources <2 の VpcId> --tags Key=canary,Value=1 --dry-run` | 拒否される（`DryRunOperation` が返ったら書き込めるという意味。調査を止めてユーザーに伝える） |
| 4 | `aws ec2 describe-vpcs \| head -3` と `bash -c "aws sts get-caller-identity"` | どちらも実行できない |
| 5 | ファイル編集ツールで `method/report.md` と `out/.survey/env/aws-audit.log` の末尾に 1 行足す | どちらも書き込めない（監査ログに書けてしまったら調査を止めてユーザーに伝える） |

結果を `check.md` に「番号・実行したこと・結果・何に拒否されたか・期待どおりか」の表で残します。期待と違うものがあれば冒頭に大きく書き、
ユーザーが応答できない回はそこで止めます。エージェントを切り替えたときは既存の記録を残して追記します。

## 工程

1. **台帳と前提の見出しを作る**（無ければ）。`out/.survey/state.md` は「作業・現在地・次にやること・未解決・決定事項」の 5 見出し。
   `out/report/前提.md` は見出しと、用途を示す 1 行だけ: 「ユーザーから聞いて分かったことを、出どころを添えてエージェントがここに書きます。
   回答は `ユーザー確認事項.md` の回答欄にお願いします」。中身はユーザーから聞いたときに書きます。
2. **当たりを付ける。** 全リージョン・全サービスの総当たりはしません。

   ```
   aws ce get-cost-and-usage --time-period Start=<先月の1日>,End=<今月の1日> --granularity MONTHLY --metrics UnblendedCost --group-by Type=DIMENSION,Key=SERVICE --region us-east-1
   aws resourcegroupstaggingapi get-resources --resources-per-page 100 > out/.survey/raw/raw-tagged.json
   ```

   課金のあるサービスとリージョン（`Key=REGION`）が実際に動いているものです。Cost Explorer が `DataUnavailableException` /
   `AccessDeniedException` なら、アカウント側で有効になっていないだけで、この環境の制約ではありません。有効化の依頼を確認事項に書き
   （コンソールの「請求とコスト管理」→「Cost Explorer」を開くだけ。翌日から使える）、待たずにタグ検索と `ec2 describe-regions` からの
   リージョンごとの `describe-*` で続けます。Resource Explorer のインデックスがあれば、それも入口になります。
3. **系統を辿る。** 入口（CloudFront・ALB・API Gateway・関数 URL・Route 53・イベント）から、コンピュート、データストアへ。
   繋がりは、トリガー・環境変数の名前・実行ロールの許可先・SG の参照・ターゲットグループから辿ります。
   入口が多ければ、入口の数だけ辿ります。35 の CloudFront は 35 の経路で、それぞれの別名・オリジン・WAF・ログ先を取り、
   ALB のルールはホスト名 → ターゲットグループ → 登録ターゲットとその health まで引きます（`method/report.md` 3 節の経路の表の材料）。
   `survey-status` に経路が出ていれば、EC2 の中と Lambda のコードも読みます（`method/routes.md`）。
4. **網羅性を担保する。** `method/report.md` の付録の領域（リージョン / コンピュート / データ / 配信 / 非同期 / 監視 / 権限 / 稼働）を
   1 つずつ見て、無かったものも空の生データを残します。「無かった」と「見ていない」を区別するためです。
   ロールは 1 つずつ信頼関係と主なポリシーを読みます。SG は ENI から実際に付いているリソースを引き直します。
   ロググループは削除済みリソースの痕跡が残るので、構成の履歴を読む材料になります。

   **代表確認で済ませないもの。** 一覧に出る名前だけでは役割も状態も分からない種類は、1 つずつ取ります。数が多いほど、
   読み手はコンソールで追えないので、報告の価値はここで決まります。独立したコマンドは 1 回の応答にまとめて投げれば時間はかかりません。

   | 種類 | 全件で取るもの | 取り方の例 |
   | --- | --- | --- |
   | S3 バケット | 公開ブロックとポリシーの公開判定、容量とオブジェクト数、ライフサイクル、バージョニング、誰が参照するか（CloudFront のオリジン・ALB/CloudFront のログ先・ロールの許可先） | `get-public-access-block` / `get-bucket-policy-status` / `get-bucket-lifecycle-configuration` を 1 つずつ。容量は `cloudwatch get-metric-data` に全バケットの `BucketSizeBytes` / `NumberOfObjects` を `file://` で 1 回 |
   | IAM ユーザー | 全員のコンソールログイン・MFA・アクセスキーの最終使用、所属グループと直付けポリシー | `get-account-authorization-details` に大半がある。最終使用は `list-access-keys` → `get-access-key-last-used` を 1 人ずつ |
   | IAM ロール | 信頼関係・主なポリシー・最終使用 | 同上と `RoleLastUsed` |
   | CloudFront | 別名・オリジン（種類と先）・WAF・ログ先・有効か | `list-distributions` の `--query` で全項目 |
   | ALB / NLB | リスナー → ルール（ホスト・パス）→ ターゲットグループ → ターゲットと health | `describe-rules` はリスナーごと、`describe-target-health` はターゲットグループごと |
   | Route 53 | ゾーンごとのレコードのうち、AWS のリソース（CloudFront・ALB・EIP・S3）を指すもの | `list-resource-record-sets` をゾーンごと。経路の表の「入口」の裏付け |
   | EC2 | 稼働中も停止中も、名前・タイプ・サブネット・公開 IP・SG・インスタンスプロファイル・起動日 | `describe-instances` を状態で絞らない |
   | セキュリティグループ | 実際に付いている ENI と、0.0.0.0/0 からの受信 | ENI から引き直す |
   | Lambda | ランタイム・トリガー・実行ロール・関数 URL・直近 30 日の呼び出し | `method/routes.md` の絞った `--query` |
   | 証明書・ドメイン | 期限・使用先・自動更新 | `acm describe-certificate` を 1 つずつ、`route53domains list-domains` |
5. **報告を書く。** 領域ごとに集めて書いても構いません。全部集めてからでなくてよいですが、報告まで書き切ってから回を終えます。
   生データだけで終わった回は、ユーザーには何も伝わりません。

### ユーザーが応答できない回

最初の指示にそうあれば、聞く工程を飛ばし、聞きたいことは `report/ユーザー確認事項.md` に書いて、報告まで書き切ります。
報告が既にあれば、まず検証（`method/report.md`「検証」）をし、`survey-status` の経路と確認事項の回答欄を読んで報告を更新します。
この回は一時キーが自動で入れ替わるので、残り時間で切り上げません。`ExpiredToken` が実際に出たときだけ、書きかけを保存して終えます。

### 対話の回

ユーザーの言うことに従います。指示が来たらそれをやり、質問が来たら答えます。台帳の「次にやること」は、ユーザーが何も言わないときの既定です。
「調査を進めて」のように決まっていなければ、確認事項を手に、経路があるものを読み、無いものは依頼文を伝え、外のことを答えやすい順に聞きます。
答えは `report/前提.md` に（出どころを添えて）、分かったことは報告の本文に、その場で反映します。

聞くのは AWS の外のことです。「このシステムは何をするものですか」「利用者は誰ですか」「`prod` / `stg` はどれですか」
「これは意図してこうなっていますか」。AWS の中で調べれば決まることは、聞く前に調べます。
「Lambda が参照する DB がこのアカウントに無い」は、全リージョンと Secrets の名前まで確かめてから聞く問いです。

基礎調査と性質の違う仕事（監査・コスト削減・影響範囲・構成図の清書）を頼まれたら、`out/report/<仕事の名前>.md` に成果物を足し、
台帳の「作業」に 1 行足します。監査なら評価が成果物です。その途中で構成の事実が新しく分かったら、`構成報告.md` の本文にも足します。

## 回の始めと終わり

**始め**: `survey-status`（残り時間・経路）、台帳、確認事項の回答欄、報告、前提、`notes.md` の順に読み、`aws sts get-caller-identity` で身元を確かめます。
`survey-status` が「着手中の領域を仕上げてください」「新しい作業に着手しないでください」と出したら、それに従います（応答できない回を除く）。

**終わり**: 台帳を更新し、分かったことを報告の本文に書き直し（末尾に追記しない）、聞けなかったことを確認事項に移し、
その回の判断と訂正を `out/.survey/log/NN-<topic>.md` に、次も効く調べ方と読み違いを `out/.survey/notes.md` に書きます。
`notes.md` の教訓は、別のアカウントでも通じる形で書きます。この文書（`method/`）と違う挙動を見つけたら「違った」と書いてください。

## コマンドの作法

| 決まり | 内容 |
| --- | --- |
| `aws` は単独で実行する | パイプ・`;`・`&&`・`$()`・`bash -c`・改行や `\` の継続はこの環境では実行できません |
| 保存は `> out/.survey/raw/raw-名前.json` の形だけ | 相対パスのみ。拡張子は `json` / `txt` / `csv`。位置引数で出力先を取る API も同じ形 |
| 絞り込みは `--query` と `--output`、必ず `--max-items` | バックティック・`\|\|`・パイプ演算子は使えない。`keys()` は対象が `null` だとエラー（`[?Environment.Variables!=null]` で先に絞る） |
| `--max-items` が効かない API がある | `--max-results` / `--limit` を使う（App Runner・WAF・Synthetics・Budgets・Image Builder など）。弾かれたら `help` で確かめる。`resourcegroupstaggingapi get-resources` は `--resources-per-page` と併用不可 |
| 多数の対象を 1 回で問い合わせる | `for` ループは使えない。クエリを JSON に書いて `file://out/.survey/raw/名前.json` で渡す（例: `cloudwatch get-metric-data --metric-data-queries file://…`）。独立したコマンドは 1 回の応答でまとめて投げると速い |
| 文字列 `aws` を含むパターン | `grep` / `rg` / `cat` / `head` / `wc` / `ls` をファイルに単独で使うなら書ける。`sed` / `jq` のフィルタでは `^/.ws/lambda/` のようにワイルドカードで字面を避ける |
| 大きな出力は読まずに保存する | `> raw/` に落としてから `jq` で必要な部分だけ読む。保存した JSON は何度でも別の観点で引き直せる。同じ問いに API を叩き直さない |
| リージョンは指定しなくてよい | 既定が設定済み。他を見るときは `--region` を明示する |
| 秘密の値を生データに入れない | `lambda list-functions` は環境変数の値を返す。設定は `method/routes.md` の絞った `--query` で保存する。`userData` も同じ |
| `sqs receive-message` は使わない | メッセージを消費し本番に影響する。滞留は属性で見る |
| 報告（Markdown）はファイル編集ツールで書く | `out/` 配下だけ書ける |

## 読み違えやすいこと

- **名前 ≠ 用途。** 名前から推した用途は推測と書き、コード・設定・トリガーで確かめる
- **一覧に無い ≠ どこにも無い。** 他のリージョン、別のアカウント、別の名前を確かめてから「無い」と書く
- **ロググループが無い ≠ 呼ばれていない。** 実行ロールにログの権限が無ければロググループはできない。呼び出しはメトリクスで見る
- **`ResourceNotFoundException` ≠ リソースが無い。** `lambda get-policy` / `get-function-url-config` は、その設定が無いだけでも返す
- **既定オブジェクト ≠ 使っている。** EventBridge の default バス、Athena の primary ワークグループ、既定の VPC などは実体の一覧と突き合わせる
- **使われていないように見える ≠ 不要。** 並行する世代が現役のことがある。最終更新・メトリクス・紐づけを添えて書き、判断は読み手に残す
