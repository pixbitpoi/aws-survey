# 経路 — EC2 の中と Lambda のコードを読む

AWS の API で見えるのはリソースの外側までです。EC2 の中（何が動いているか・ログ・設定）と Lambda のコード（何を呼び、どこへ繋ぐか）は、
経路があるときだけ読めます。どちらも `survey-status` に出ます。出ていれば、ユーザーに聞く前に読み、報告の「系統ごとの構成」に書きます。
ユーザーが応答できない回でも同じです。経路が無いものは、`report/ユーザー確認事項.md` の「中を見れば分かること」に、下の依頼文つきで挙げます。
経路を足すことは、この環境ではできません。

## EC2 の中: `ec2 <host> <動詞>`

```
ec2 --list                  この環境で調べられるホスト
ec2 <host> help             そのホストで使える動詞と引数の一覧（これが正）
ec2 <host> <動詞> [引数...]  1 つの動詞を送り、結果を受け取る
```

| 段 | 動詞 | 何が読めるか |
| --- | --- | --- |
| 一般読み取り | `ls` `find` `stat` `du` `cat` `head` `tail` `grep` | ログインユーザーの権限で読める任意のパス。絶対パスのみ |
| 固定診断 | `uptime` `os` `ps` `proc` `free` `vmstat` `df` `sar` `net` `services` `failed` `service` `unit` `timers` `journal` `logs` | OS とサービスの状態。引数は決まった形だけ |
| root 読み取り | `listeners` `dmesg` `cron` `journal` `log` | root でないと読めないもの。登録済みログと固定の診断だけ |

**進め方**: いきなり `cat` せず、場所を突き止めてから読みます。
`services` / `failed` / `ps --top 30` / `listeners` で何が動いているか → `unit <unit>` / `ls /var/log` / `find /var/log --name '*.log' --newer <日付>` で
どこに書いているか → `stat` で大きさ → `tail <path> --lines 200` / `grep <path> --pattern 'ERROR|Timeout' --context 2 --max 200` /
`journal <unit> --since 2h --priority err`。`net` は確立済みの TCP 接続（プロセス名は出ない）。
登録済みログ（`logs` に出るもの）は `log <名前> --tail N --grep <pat>` で読みます。`tail` で `Permission denied` になるファイルが `logs` にあれば、そちらが入口です。
`logs` に無く権限でも読めないものは「読めなかった」と記録します。

**できないこと**（回避しない）: `ssh` / `scp` / `sftp` の直接実行、シェル・任意コマンド・`sudo`、書き込み・再起動・プロセスの停止、
`tail -f` / `top` / `strace` / `tcpdump` / `curl`。秘密らしき名前（`.env`・`*.pem`・`*.key`・`id_*`・`*secret*`・`*credential*`・`*password*`・
`wp-config.php`・`database.yml`）と秘密の置き場（`/root`・`~/.ssh`・`~/.aws`・`/etc/ssh`・`/etc/shadow`・`.git/`・`/proc/*/environ`）は
`ls` に名前は出ますが読めません。DB の本体（`*.sqlite`・`*.db`）も読めません。`strict mode` のホストは固定診断と root 読み取り段だけです。
`ec2` は単独で実行し、保存は `> out/.survey/raw/raw-<host>-<何>.txt` の形だけ通ります。出力は 1 MiB で打ち切られ、同時に送れるのは 1 本です。
実行した要求は、通った・拒否されたにかかわらず EC2 側にも記録が残ります。

| 返り方 | 意味 | どうするか |
| --- | --- | --- |
| `denied: ...`（終了コード 2） | 動詞・引数・パスが決まりに合わない | 理由を読んで書き方を直す。決まりの外は諦めて記録する |
| `Permission denied` | OS の権限で読めない | `logs` にあれば `log` で。無ければ「読めなかった」と記録 |
| 終了コード 255 | 資格情報の期限切れか接続の不調 | `survey-status` を見る |
| `ec2: 接続が異常終了したため…` / `前回までの接続で残っていた…` | 後片付けの報せ | 何もしなくてよい。結果はそのまま使える |

AWS の API から見ると、登録済みのインスタンスにはタグ `diag:ssh=<名前>` が付き、OS には調査用のログインユーザー・sshd の設定・
`/usr/local/lib/diag/`・`/etc/diag/` があり、journal に `diag-gateway` の記録が残ります。これらは中を調べるための印で、対象の構成ではありません。
報告に書くなら「調査のために付いているもの」と一言添えます。

**経路が無いインスタンスの依頼文**（`<...>` はインスタンス ID か Name タグ。読みたい理由を 1 行添える）:

> EC2 `<インスタンス ID か Name タグ>` の中（ログ・サービスの状態）を調べたいので、この作業環境の外で次を実行してください。
> 1. `aws-survey ssh setup <インスタンス ID か Name タグ>`
> 2. `aws-survey`（引数なし。接続を許すポリシー → 一時キーの発行し直し → 接続の確認を、1 手ずつ聞きながら進めます）
>
> この作業環境は止めなくて構いません。終わったら教えてください。`survey-status` に出るようになります。

SSM の管理下に無いインスタンスは登録できません（そのときは `aws-survey ssh setup` がそう伝えます）。

読んだログを `out/` に写すときは、秘密らしき値（パスワード・トークン・鍵・接続文字列）を残さず、IP・ユーザー名・メールアドレスはユーザーと決めた扱いに従います。
ゲートウェイ側でも `***` に置き換えますが、すべては拾えません。ログはその場で伸びるので、同じ範囲を取り直さないでください。

## Lambda のコード: `code/`

```
code/lambda/<リージョン>/<関数名>/
  _manifest.json        取り出したときの設定の控えと、何を置いて何を除いたか
  src/                  コード（秘密らしき値はマスク済み）
  <版>/                 エイリアスが指す公開版のうち、$LATEST とコードが違うもの
code/lambda-layers/<リージョン>/<レイヤー名>/<版>/
```

**できないこと**: コードの URL の取得（`lambda get-function` / `get-layer-version` / `get-layer-version-by-arn`）、関数の起動、取り出したコードの実行
（`node` / `python3` は使えない）、`code/` への書き込み。設定は `get-function-configuration`、タグは `list-tags`、同時実行数は `get-function-concurrency` で読みます。

**経路が無い関数の依頼文**（関数名は空白区切りで複数可。全関数なら `--all`、依存の中身も要るなら `--with-deps`、主リージョン以外なら `--region <r>`）:

> Lambda 関数 `<関数名>` のコードを読みたいので、この作業環境の外で次を実行してください。
> `aws-survey lambda pull <関数名>`
>
> この作業環境は止めなくて構いません。終わったら教えてください。`code/` に現れます。

**読む前に**: `_manifest.json` の `CodeSha256` と、いまの `get-function-configuration --query CodeSha256` を比べます。違えば取り出し後に更新されています。

| `_manifest.json` の項目 | 内容 |
| --- | --- |
| `Runtime` / `Handler` | 入口。`app.lambda_handler` なら `src/app.py` の `lambda_handler`、`index.handler` なら `src/index.js`（`.mjs` / `.cjs`） |
| `EnvironmentVariableNames` | 環境変数の名前だけ（値は設定から、マスクの決まりに従って） |
| `Layers` / `Aliases` | 参照するレイヤーの版（中身は `code/lambda-layers/`）と、エイリアスが指す版（`code` が `$LATEST` なら `src/`） |
| `contents.skipped` / `masked_only` | 置かなかったファイルと理由 / 名前は秘密らしいがコードなので置いたもの（値はマスク済み） |
| `contents.dependencies` / `excluded_dependencies` | 依存ライブラリの名前と版。中身は `src/` に無い |
| `contents.binaries` / `jars` / `classes` / `bundles` / `sourcemaps` | 読めない形のもの、固められた JavaScript、ソースマップから戻した元のソース（`src/_sources/`） |
| `contents.contains_own_code` | レイヤーで `false` なら依存だけのレイヤー |
| `ImageUri` | コンテナイメージ形式の関数のイメージの場所（中身は取り出していない） |

**進め方**: 入口 → 呼んでいる AWS → 環境変数 → 外への接続 → 失敗の扱い → 設定と突き合わせる。全部は読まず、検索で拾います。

```
grep -rn boto3 code/lambda/<r>/<f>/src/
rg -n -e '@aws-sdk/client-' -e 'aws-sdk' code/lambda/<r>/<f>/src/
rg -n -e TableName -e QueueUrl -e Bucket -e TopicArn -e SecretId -e FunctionName code/lambda/<r>/<f>/src/
```

`grep` / `rg` / `cat` / `head` / `wc` / `ls` は単独で実行するなら `aws` や `boto3` を含むパターンも書けます。パターンの中の `|` は使えず、複数は `-e` を並べます。
ファイルを読むツールや検索のツールが使えるなら、それで `code/` を読んでも構いません。

**設定と突き合わせる**（両方向で）: 設定にあってコードが読まない環境変数、コードが呼ぶのに実行ロールが許していない操作、コードが名指しするのに存在しないリソース。
設定は秘密の値を落として保存します。

```
aws lambda get-function-configuration --function-name <関数名> --query '{Runtime:Runtime,Handler:Handler,Role:Role,CodeSha256:CodeSha256,Layers:Layers,VpcConfig:VpcConfig,DeadLetterConfig:DeadLetterConfig,EnvNames:keys(Environment.Variables),EnvValues:{TABLE_NAME:Environment.Variables.TABLE_NAME}}' > out/.survey/raw/raw-lambda-cfg-<関数名>.json
```

`EnvValues` には名前を見てから秘密でないものだけを並べます。環境変数の無い関数では `EnvNames` を外します。
呼び出し元は `lambda list-event-source-mappings`・`lambda get-policy`・`lambda list-function-url-configs`・`events list-rule-names-by-target`・API Gateway の 5 つで閉じ、
呼ばれているかは `cloudwatch get-metric-statistics --namespace AWS/Lambda --metric-name Invocations` で見ます。

| 読めない形 | どうするか |
| --- | --- |
| バンドルされた JavaScript | `src/_sources/` を読み、固められたファイルと食い違わないかを確かめる。ソースマップが無ければ固められたファイルから `grep` でサービス名・環境変数名だけ拾う |
| Java / Go / Rust | 設定ファイル・クラス名・依存の一覧・ロールの権限・環境変数名から推す。逆コンパイルはできない |
| コンテナイメージ形式 | `_manifest.json` だけ。イメージの元のリポジトリをユーザーに聞く |
| 依存ライブラリ | 名前と版だけ。中身が必要なら依頼文に `--with-deps` を足す |

**読み違えやすいこと**: ソースマップから戻したソースは、動いているコードとは限らない（ハンドラの行と SDK の名前を固められた側で確かめ、食い違えば実行される側を正とする）。
コードに定数がある ≠ 使っている（参照箇所を検索してから「接続先」と書く）。

**記録**: `code/lambda/<r>/<f>/src/<パス>` の行と `CodeSha256` で参照し、コードを `out/` に丸ごと写さない。引用は要る行だけ。`***` はマスクされた値で、元の値を推測しない。
マスクされていない秘密らしき値を見つけたら `out/` に写さず、ユーザーに伝える。報告にはコードが何をしているかを書き、長い引用をしない。
