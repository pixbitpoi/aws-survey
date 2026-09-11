# 実 AWS で手で回すテスト

`tests/` の unittest は偽の `aws` / `docker` で動く。ここにあるのは、それでは確かめられないことを
**実 AWS に対象を作って** 確かめるための一式で、`python3 -m unittest discover` には入らない。手で実行する。
配布物（Homebrew）にも入らない（`docs/release.md`）。

前提と注意:

- 管理者権限のプロファイル（`AWS_PROFILE`）と、壊してよい検証用のアカウント。本番相当のアカウントでは使わない。
- 作るものは課金対象。`launch.sh` は無料枠の範囲に収まる既定値を選ぶが、片付け（`cleanup.sh`）を忘れると課金が続く。
- 作ったものは `state/<名前>.env` に記録し、`cleanup.sh` はそこに書かれたものだけを消す。`state/` は Git 管理外
  （インスタンス ID などを含む）。途中で失敗したら、もう一度 `cleanup.sh` を実行すれば残りを片付ける。
- **対象に置くものに、テストの意図が分かる文を残さない。**調査コンテナのエージェントは対象の中（コード・user-data・タグ・設定）を読む。
  仕込む値は `LEAKCHECK-` のように、漏れたときにしか目に付かない字面にする。答案用紙（`check.sh` の項目）はホストにだけ置く。
- 対象フォルダ（`aws-survey init` で作ったもの）は別に用意する。ここのスクリプトは対象フォルダに触らない。

## ec2/ — 診断ゲートウェイと `ec2` ラッパー

Amazon Linux 2023 を 1 台起動し、user-data で nginx → Python API → Redis / PostgreSQL、systemd timer（1 つは失敗し続ける）、
cron、logrotate、rsyslog を構成する。`aws-survey ssh setup` の導入先と、調査コンテナから `ec2 <host>` で読む相手になる。

```
cd tests/live/ec2
AWS_PROFILE=<管理者> ./launch.sh                 # web2 を起動（--bare で中身を構成せずに起動）
./configure.sh --check web2                     # 起動時の構成の完了を待ち、サービス・待ち受け・応答を見る
cd <対象フォルダ> && aws-survey ssh setup web2 --log shop=/var/log/shop/*.log --log messages=/var/log/messages*
aws-survey ssh verify web2 && aws-survey run    # 調査コンテナで method/06 に沿って読ませる
aws-survey ssh remove web2                      # 対象フォルダで。ゲートウェイとタグ・ポリシーを外す
cd tests/live/ec2 && ./cleanup.sh web2          # インスタンス・SG・（作っていれば）インスタンスプロファイル
```

EC2 に渡るのは `configure.sh --payload` の出力（「EC2 側」から下。コメント行なし）だけ。user-data は
`describe-instance-attribute` や IMDS で読めるので、ホスト側の使い方やコメントは渡さない。

## lambda/ — `aws-survey lambda pull` と `method/07`

Python（版 1 に alias `live`、`$LATEST` は別のコード、レイヤー参照、直書きの秘密・`.env`・同梱の依存）と
Node.js（100 KiB 超のバンドルとソースマップ、`node_modules`）の関数を作る。関数は起動せず、実行ロールに権限は付けない。

```
cd tests/live/lambda
AWS_PROFILE=<管理者> ./launch.sh                 # shop-order-api / shop-thumbnail / shop-common
cd <対象フォルダ> && aws-survey lambda pull --all
<リポジトリ>/tests/live/lambda/check.sh <対象フォルダ>   # 取り出した code/ を 32 項目で見る（AWS は叩かない）
aws-survey run                                  # 調査コンテナで method/07 に沿って読ませる
aws-survey lambda remove --all                  # 対象フォルダの code/
cd tests/live/lambda && ./cleanup.sh            # 関数・レイヤーの版・実行ロール
```

`./launch.sh --build-only <フォルダ>` は zip を組み立てるだけで、抽出器（`libexec/lambda/extract.py`）を手元で試すのに使う。
仕込んだものと `check.sh` の項目は対にして直す（片方だけ変えると、項目が何も見ていないことになる）。
