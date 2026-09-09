# リリース手順（Homebrew）

配布は個人用 tap [pixbitpoi/homebrew-tap](https://github.com/pixbitpoi/homebrew-tap) の
`Formula/aws-survey.rb` で行う。formula はこのリポジトリのタグの tarball を参照する。
利用者向けの入れ方は [README.md](../README.md)「入れる」にある。

## 配布物の範囲

formula は `docs/` と `tests/` を除いた一式を **keg 直下**（`prefix`）に置く。リポジトリが `bin/` + `libexec/` +
`container/` の形をしているので、そのまま keg の構造になる。

```
<keg>/bin/aws-survey        env ラッパー。brew が /opt/homebrew/bin へリンクする
<keg>/libexec/              aws-survey の実体と、ホスト側スクリプト
<keg>/container/  templates/  .agents/  Dockerfile  AGENTS.md …
```

prefix へリンクされるのは `bin` / `sbin` / `etc` / `include` / `share` / `lib` だけ（`Library/Homebrew/keg.rb` の
`Keg#link`）なので、`container/` や `templates/` は keg の中に留まり、`/opt/homebrew/` を汚さない。

`bin/aws-survey` は **env ラッパー**で、実体は `libexec/aws-survey` へ移してある。keg のパスはバージョンを含み
（`Cellar/aws-survey/0.1.0/`）、`aws-survey` の実体パス解決は `pwd -P` ですべてのリンクを解決するため、
実体を `bin/` に置いたままだとバージョン入りのパスが `AWS_SURVEY_HOME` になる。このパスは `aws-survey status`
が「本体」として表示し、利用者が `AWS_SURVEY_HOME` に手で設定することもある。keg のパスを渡すと、次の
アップグレードでそれが切れる。formula は `write_env_script` で `opt_prefix`
（`/opt/homebrew/opt/aws-survey`。バージョンを含まない）を渡してこれを防ぐ。
`tests/test_distribution.py` が `opt` 経由のツリーを組んで、表示されるパスが keg を指さないことを見る。
なお `init` は対象フォルダに絶対パスを焼き込まない（`aws-survey run` から先はコンテナの仕事なので、
生成する `AGENTS.md` が配布物を参照しない）。

実体を `libexec/` に置くのは `libexec` の定義どおり（ユーザーが直接叩かない実行ファイル）で、リポジトリ側で
`bin/aws-survey` なのは clone した状態で `./bin/aws-survey` を叩けるようにするため。

配布するのはランタイムだけ。`Dir["*"]` で足り、ドット始まりを拾う glob は要らない
（Ruby の `Dir["*"]` はドットで始まるものを拾わない）。ドット始まりを配布物に入れる必要が出たときは、
formula と `tests/test_distribution.py` の `INCLUDED_HIDDEN` を揃えること。
忘れると、エラーも警告も出ないまま配布物から落ちる。

| 入るもの | 理由 |
| --- | --- |
| `bin/aws-survey` | 入口。keg では `libexec/aws-survey` へ移され、`bin` には env ラッパーが置かれる |
| `libexec/` | ホスト側の実装。各スクリプトと `session-guard.json`、EC2 へ導入する診断ゲートウェイと導入スクリプトの雛形（`ec2/`。`aws-survey ssh setup --print` が組み立てる） |
| `container/`・`Dockerfile` | 調査コンテナへ渡る資材と、そのビルド定義。ビルド文脈は `container/` |
| `templates/` | `init` が使う `environment.json` の雛形 |

| 入らないもの | 理由 |
| --- | --- |
| `README.md`・`docs/` | **利用者向け文書の正本は GitHub**（`brew home aws-survey`）。配布物の中には読む手段が無く、参照する側も無い |
| `AGENTS.md`・`CLAUDE.md`・`.agents/` | 開発時の入口と規則。`aws-survey run` から先はコンテナの仕事なので、インストール済みのツリーの中で作業するエージェントはいない。開発は git clone で行う |
| `tests/` | 開発時にリポジトリで走らせる（`ec2_install_smoke.sh` は Docker が要るので手で実行する） |
| `environment.json`・`out/`・`trust.json`・一時キー | 対象フォルダとホームに属する。配布物には無い |

`README.md` を formula の除外リストに入れても、**keg 直下には現れます。**Homebrew が展開した tarball から
metafile（README・LICENSE など）を prefix へ複製するためです（`Library/Homebrew/build.rb:227` の
`install_metafiles`）。これは Homebrew の作法なので止めません。配布物として扱わない、という意味は
**配布物の中の誰もそれを参照しない**ことです。`tests/test_distribution.py` の `NOT_INSTALLED` が
コピー対象から外し、`REQUIRED` からも抜いてあります。

利用者向け文書（`README.md`・`docs/security.md`）を配布しない理由は、読む手段が無いからです。`brew` に
README を表示するコマンドは無く、`brew home` は formula の `homepage`（GitHub）を開きます。
`AGENTS.md`「利用者との進め方」も「手順書を読むのはエージェント。ユーザーに節番号を案内して作業を委ねない」
と定めています。**利用者向けの説明を足すときは GitHub 側だけを直せばよく、リリースは要りません。**

配布物だけで動くことは `tests/test_distribution.py` が確認する。ただし metafile の移動は Homebrew 側の挙動なので、
このテストでは再現しない。ファイルを増やしたときは、この表と formula の除外リストを合わせ、
`brew install` 後に `libexec` の中身を目で見る。

## 依存

- `jq` は `depends_on`。
- [aws-login](https://github.com/pixbitpoi/aws-login) は `depends_on`（同じ tap の `pixbitpoi/tap/aws-login`）。
  `init` が `auth.refresh_command` の既定に `aws-login --profile <元プロファイル>` を書き、
  `aws-survey credentials` が元プロファイルの期限切れでそれを実行するため、入っている前提にする。
- AWS CLI v2 は `depends_on` にしない。公式インストーラーで入れている環境と二重になるため、caveats で案内する
  （[aws-login](https://github.com/pixbitpoi/aws-login) の formula と同じ扱い）。
- Docker は cask なので `depends_on` にしない。有無は `aws-survey doctor` と引数なし実行が見る。

## 手順

1. `main` を push した状態で、`bash -n` と `python3 -m unittest discover -s tests -v` を通す。

2. タグを打って push する。タグは `vX.Y.Z` の形式。formula の `version` はタグから決まるので書かない。

   ```bash
   git tag -a v0.1.0 -m "v0.1.0"
   git push origin v0.1.0
   ```

3. GitHub が生成する tarball の sha256 を取る。

   ```bash
   curl -sL https://github.com/pixbitpoi/aws-survey/archive/refs/tags/v0.1.0.tar.gz | shasum -a 256
   ```

4. tap の `Formula/aws-survey.rb` で `url` のタグと `sha256` を更新する。`head` は `main` を指すので変更しない。

5. tap で確認してからコミット・push する。コミット件名は `aws-survey 0.1.0` のように formula 名とバージョンを書く。

   ```bash
   brew style Formula/aws-survey.rb
   brew audit --strict pixbitpoi/tap/aws-survey
   brew install pixbitpoi/tap/aws-survey
   brew test aws-survey
   ```

   短い名前で `brew test` するには、事前に `brew trust pixbitpoi/tap` で tap を信頼しておく。

6. 入れた実行ファイルで通しを確認する（実 AWS と Docker が要る）。空のフォルダで
   `aws-survey` → `init` → `role --create` → `credentials` → `verify` → `run` まで進むこと、
   `aws-survey status` の「本体」が `libexec` を指すことを見る。

開発版は `brew install --HEAD pixbitpoi/tap/aws-survey` で入る。formula の確認に使う。

## 開発中の実行

インストールせずに、リポジトリの `./bin/aws-survey` を直接実行するか、`~/bin/aws-survey` などへシンボリックリンクを張る。
リンクをたどって実体パスから `AWS_SURVEY_HOME` を解決するので、本体はリポジトリのままになる。
`brew` で入れたものと同時に PATH にあると順序で決まるため、どちらを見ているかは `aws-survey status` の「本体」で確かめる。
