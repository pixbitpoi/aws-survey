# Lambda のコードを読む

AWS の API から分かるのは、関数の設定（ランタイム・ハンドラ・環境変数・ロール・トリガー）までです。
関数が実際に何をしているか（どのサービスのどのリソースを呼ぶか、どの環境変数を使うか、どこへ接続するか）は、コードを読むと分かります。

この環境では、取り出してある関数のコードが `code/` に読み取り専用で置かれています。

```
code/lambda/<リージョン>/<関数名>/
  _manifest.json        取り出したときの設定の控えと、何を置いて何を除いたか
  src/                  コード（秘密らしき値はマスク済み）
  <版>/                 エイリアスが指す公開版のうち、$LATEST とコードが違うもの（中の構成は同じ）
code/lambda-layers/<リージョン>/<レイヤー名>/<版>/
  _manifest.json
  src/
```

取り出してある関数の数は `survey-status` にも出ます。何も出なければ、この対象のコードはまだ取り出されていません。

## この環境でできないこと

- 関数とレイヤーのコードの URL は取得できません（`aws lambda get-function`・`get-layer-version`・`get-layer-version-by-arn`）。
  設定は `get-function-configuration`、タグは `list-tags --resource <関数の ARN>`、同時実行数は `get-function-concurrency` で読みます。
  コンテナイメージ形式の関数のイメージの場所は、`_manifest.json` の `ImageUri` にあります
- コードを取り出すことは、この環境ではできません。`code/` に無い関数が必要になったら、次の文をそのままユーザーに伝えてください
  （`<関数名>` は空白区切りで複数書けます。そのリージョンの全関数なら `--all`、依存ライブラリの中身も要るなら `--with-deps` を足します。
  主に見るリージョン以外なら `--region <リージョン>` を足します。読みたい理由を 1 行添えます）

  > Lambda 関数 `<関数名>` のコードを読みたいので、この作業環境の外で次を実行してください。
  > `aws-survey lambda pull <関数名>`
  >
  > この作業環境は止めなくて構いません。終わったら教えてください。`code/` に現れます。

- 関数を起動すること、取り出したコードを実行することはできません（`node` / `python3` は使えません）。読むだけです
- `code/` には書き込めません

## 読む前に: 取り出したコードが今のものか

`_manifest.json` の `CodeSha256` と、いまの設定の `CodeSha256` を比べます。

```
aws lambda get-function-configuration --function-name <関数名> --query CodeSha256 --output text
```

違っていれば、取り出したあとに更新されています。そのまま読むときは古いことを記録し、必要なら上の文で取り直しを依頼してください
（同じ `pull` で、変わった関数だけ落とし直します）。

## `_manifest.json` の読み方

| 項目 | 内容 |
| --- | --- |
| `Runtime` / `Handler` | 入口。`app.lambda_handler` なら `src/app.py` の `lambda_handler`、`index.handler` なら `src/index.js`（`.mjs` / `.cjs`）の `handler`、Java の `com.example.Handler::handleRequest` ならそのクラスとメソッド |
| `EnvironmentVariableNames` | 環境変数の名前だけ。値は入っていません（値は設定から、マスクの決まりに従って読みます） |
| `Layers` | 参照しているレイヤーの版。中身は `code/lambda-layers/` |
| `Aliases` | エイリアスと版。`code` が `$LATEST` なら `src/` と同じコード、`<版>/` ならそのディレクトリ |
| `contents.skipped` | 置かなかったファイルと理由（`name:*.pem` のような秘密らしき名前、`component:.git`、パスが不正なもの） |
| `contents.masked_only` | 名前は秘密らしいが、コードなので置いたもの（`secrets.py` など。中の値はマスク済み） |
| `contents.dependencies` / `excluded_dependencies` | 依存ライブラリの名前と版。中身は `src/` にありません |
| `contents.binaries` / `jars` / `classes` | 読めない形のもの（名前・大きさ・SHA-256、クラス名） |
| `contents.bundles` / `sourcemaps` | 1 本に固められた JavaScript と、ソースマップから戻した元のソース（`src/_sources/`） |
| `contents.contains_own_code` | レイヤーで `false` なら、依存ライブラリだけのレイヤー |

## 進め方: 入口から、呼んでいるものを拾う

いきなり全部を読まないでください。依存を除いても数百ファイルになることがあります。

1. **入口**: `Handler` のファイルと関数を読む。イベントのどの項目を使っているかを見る
2. **呼んでいる AWS**: SDK の呼び出しを検索する

   ```
   grep -rn boto3 code/lambda/<r>/<f>/src/
   rg -n -e '@aws-sdk/client-' -e 'aws-sdk' code/lambda/<r>/<f>/src/
   rg -n -e TableName -e QueueUrl -e Bucket -e TopicArn -e SecretId -e FunctionName code/lambda/<r>/<f>/src/
   ```

3. **環境変数**: `os.environ` / `process.env` / `ENV[` で、どの名前をどこで使うかを見る。`EnvironmentVariableNames` と突き合わせる
4. **外への接続**: `https://`・ホスト名・ポート
5. **失敗の扱い**: `try` / `except` / `catch`、再試行、どの条件で例外を外に投げるか（非同期の呼び出しなら、それが DLQ や失敗時の送り先に落ちる）
6. **設定と突き合わせる**: 下の「設定と突き合わせる」

`grep` / `rg` / `cat` / `head` / `wc` / `ls` は、単独で実行するなら `aws` や `boto3` を含むパターンも書けます。
パイプ・`;`・`&&`・`$()`・リダイレクトとは組み合わせられず、パターンの中の `|` も使えません。複数のパターンは `-e` を並べます。
ファイルを読むツールや検索のツールが使えるなら、それで `code/` を読んでも構いません。

## 設定と突き合わせる

コードだけ、設定だけでは見えないことがあります。**両方向で**突き合わせてください。
設定にあってコードが読まない環境変数、コードが呼ぶのに実行ロールが許していない操作、コードが名指しするのに存在しないリソースは、
片方向からでは見つかりません。

**設定は、秘密の値を落として `raw/` に保存します。** 環境変数は名前を全部、値は秘密でないもの（テーブル名・バケット名など）だけを選びます。
丸ごと保存すると、パスワードのような値が `out/` に落ちます。

```
aws lambda get-function-configuration --function-name <関数名> --query '{Runtime:Runtime,Handler:Handler,Role:Role,CodeSha256:CodeSha256,Layers:Layers,VpcConfig:VpcConfig,DeadLetterConfig:DeadLetterConfig,EnvNames:keys(Environment.Variables),EnvValues:{TABLE_NAME:Environment.Variables.TABLE_NAME}}' > out/<作業>/raw/raw-lambda-cfg-<関数名>.json
```

`EnvValues` には、名前を見てから秘密でないものだけを並べます。環境変数の無い関数では `keys()` がエラーになるので、`EnvNames` を外します
（`01_進め方.md` の「`--query`（JMESPath）の癖」）。

| 見ること | どう見るか |
| --- | --- |
| 実行ロールの権限 | `iam list-attached-role-policies` / `list-role-policies` / `get-role-policy`。コードが呼ぶ操作が許されているか |
| 呼び出し元 | 次の 5 つを見れば閉じる: `lambda list-event-source-mappings`、`lambda get-policy`（`ResourceNotFoundException` はリソースポリシーが無い＝他のサービスから呼ぶ許可が無い）、`lambda list-function-url-configs`、`events list-rule-names-by-target --target-arn <関数の ARN>`、API Gateway（`apigateway get-rest-apis` / `apigatewayv2 get-apis`） |
| 呼ばれているか | `cloudwatch get-metric-statistics --namespace AWS/Lambda --metric-name Invocations --dimensions Name=FunctionName,Value=<関数名>` |
| 相手のリソースがあるか | 各サービスの一覧・`describe` で、コードと設定が名指しする名前を探す |

## 読めない形のもの

| 形 | 置かれているもの | どうするか |
| --- | --- | --- |
| バンドルされた JavaScript | 固められたファイルと、ソースマップがあれば `src/_sources/` | `_sources/` を読み、固められたファイルと食い違わないかを確かめる（下の「読み違えやすいこと」）。`bundles[].map` が `none` / `no-sourcesContent` なら、固められたファイルから `grep` でサービス名・環境変数名だけを拾う |
| Java | 設定ファイル（`*.properties` など）と `META-INF/`。クラスと jar は一覧だけ | 設定ファイル・クラス名・依存の一覧から推す。逆コンパイルはできません |
| Go / Rust などのカスタムランタイム | `bootstrap` は一覧だけ | 設定・ロールの権限・環境変数名から推す |
| コンテナイメージ形式 | `_manifest.json` だけ | イメージの元のリポジトリを、ユーザーに聞く |
| 依存ライブラリ | 名前と版の一覧だけ | 中身が必要なら、上の文に `--with-deps` を足して取り直しを依頼する |

読めなかったものは「読めなかった」と記録してください（`01_進め方.md` 大原則 3）。

## 読み違えやすいこと

- **ソースマップから戻したソースを、動いているコードと思わない。** 実行されるのは固められたファイルで、別のビルドのソースマップが
  同梱されていることがあります。ハンドラの行（`exports.handler` など）を固められたファイルの側で読み、SDK の名前（`S3Client`・`.send(` など）も
  そちらで検索してから結論を出してください。食い違ったら、実行される側を正とし、食い違ったこと自体を記録します
- **コードに定数がある ≠ 使っている。** 接続文字列・トークン・設定ファイルが、定義だけで参照されていないことがあります。
  定数名で参照箇所を検索してから「接続先」「使っている設定」と書いてください
- **ロググループが無い ≠ 呼ばれていない。** 実行ロールにログの権限が無ければ、呼ばれてもロググループはできません。呼び出しの有無はメトリクスで見ます
- **自分のアカウントの一覧に無い ≠ どこにも無い。** たとえば `s3api list-buckets` は自分のアカウントのバケットしか出しません。
  コードが名指しするリソースが別のアカウントのものである可能性は残ります

## 記録のしかた

- 調査の記録からは `code/lambda/<r>/<f>/src/<パス>` の行と `CodeSha256` で参照する。コードを `out/` に丸ごと写さない
- 引用は要る行だけ。`***` はマスクされた値で、元の値を推測して書かない
- コードに直書きされた秘密らしき値が `***` になっていなかったら（エンコードされた値や、組み立てた文字列）、`out/` に写さず、ユーザーに伝える
- コードそのものが対象の資産です。統合版にはコードが何をしているかを書き、長い引用をしない

## いつ使うか

`code/` にその関数があって、コードを読めば決まる問いがあるときです。ユーザーに聞く前に使います。
基礎調査の非対話の回では設定とトリガーから役割を推し、コードで確かめたいことを `report/ユーザー確認事項.md` の「中を見れば分かること」に
取り出しの依頼文つきで挙げます（`method/04`）。取り出されたら、その回で読んで報告の本文に書き直します（`method/02`）。
