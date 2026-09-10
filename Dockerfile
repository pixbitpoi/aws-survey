# AWS インフラ構成調査専用コンテナ
#
# 必要なのは AWS CLI・jq・Claude Code・Codex CLI。EC2 の中を調べる機能のために
# openssh-client と session-manager-plugin（aws ssm start-session の実体）も入れる。
#   - 非 root ユーザーで動作し、sudo は入れない
#   - 設定とフックは root 所有にして、コンテナ内から書き換えられないようにする
#
# ⚠️ ビルド文脈は container/（run.sh が `-f Dockerfile <本体>/container` で叩く）。
#    COPY のパスは container/ からの相対で書くこと。文脈の外は COPY できないので、
#    ホスト側の開発文書がイメージに入ることは構造上ありえない。
FROM node:22-bookworm-slim

ARG AWSCLI_ARCH=aarch64

# session-manager-plugin は AWS が deb で配る。アーキテクチャの名前が AWS CLI と違うので、ここで読み替える。
# deb は依存パッケージを持たないので dpkg -i で入る。arm64 の deb は 2026-09-10 に arm64 の Mac で動作を確認した。
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl unzip jq git less python3 ripgrep openssh-client \
 && curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-${AWSCLI_ARCH}.zip" -o /tmp/awscliv2.zip \
 && unzip -q /tmp/awscliv2.zip -d /tmp \
 && /tmp/aws/install \
 && rm -rf /tmp/aws /tmp/awscliv2.zip \
 && case "$AWSCLI_ARCH" in aarch64) smp=ubuntu_arm64 ;; x86_64) smp=ubuntu_64bit ;; *) echo "unknown arch $AWSCLI_ARCH" >&2; exit 1 ;; esac \
 && curl -fsSL "https://s3.amazonaws.com/session-manager-downloads/plugin/latest/${smp}/session-manager-plugin.deb" -o /tmp/smp.deb \
 && dpkg -i /tmp/smp.deb \
 && rm -f /tmp/smp.deb \
 && apt-get purge -y --auto-remove unzip \
 && rm -rf /var/lib/apt/lists/* \
 && aws --version \
 && session-manager-plugin --version \
 && ssh -V

# Codex はバージョンを固定する。ガードは Codex 固有の機能契約（allow_managed_hooks_only・
# managed_dir・unified_exec）に乗っているため、勝手に上がるとフックが静かに外れうる。
# 上げるときは既定値を変え、tests/container_smoke.py でガードが効くことを確かめる。
ARG CODEX_VERSION=0.153.4
# claude-code は次段でネイティブ版に入れ替えるための踏み台。npm 版は最後に消す。
RUN npm install -g @anthropic-ai/claude-code @openai/codex@${CODEX_VERSION} \
 && npm cache clean --force

# Claude Code は npm ではなくネイティブ導入にする。npm prefix（/usr/local）は root 所有のまま
# 保ちたい一方、自動更新には書き込み先が要る。ネイティブ版は ~/.local に入るので、
# node ユーザーが自分で更新でき、ガード設定（root 所有の settings.json・hooks）には触れない。
# ~/.local は run.sh が名前付きボリュームにするので、更新はコンテナを作り直しても残る。
ENV PATH=/home/node/.local/bin:$PATH
# Claude Code の設定と認証情報を 1 か所にまとめる。
# 既定では ~/.claude/.credentials.json と ~/.claude.json に分かれて置かれ、
# 後者がボリュームの外になるため、コンテナを作り直すたびに再認証が必要になる。
# 導入より前に置くこと。あとに置くと claude install が書く installMethod が
# 既定の ~/.claude.json に落ち、実行時に読まれず「install method not set」になる。
ENV CLAUDE_CONFIG_DIR=/home/node/.claude

# 作業ディレクトリと設定を配置する。
# ディレクトリと .claude を root 所有にすることで、node ユーザーからは
# 設定ファイルもフックも削除・改変できない（sudo が無いため昇格もできない）。
#
# AGENTS.md / CLAUDE.md（調査エージェントへの指示）はイメージに焼かず、run.sh が
# container/instructions/survey-agents.md と survey-claude.md を、
# AGENTS.md / CLAUDE.md という名前で読み取り専用にマウントする。
# method/ と同じ「渡す資料」の扱いで、書き換えても再ビルドが要らない。
# ガード（settings.json / hooks）とは扱いを分ける。
RUN mkdir -p /home/node/aws-survey/.claude/hooks /home/node/aws-survey/out \
 && touch /home/node/aws-survey/CLAUDE.md /home/node/aws-survey/AGENTS.md
COPY settings.json               /home/node/aws-survey/.claude/settings.json
COPY hooks/aws-readonly-guard.sh /home/node/aws-survey/.claude/hooks/aws-readonly-guard.sh
COPY codex/config.toml /etc/codex/config.toml
COPY codex/requirements.toml /etc/codex/requirements.toml
COPY hooks/codex-guard.py /etc/codex/hooks/codex-guard.py
COPY hooks/aws-readonly-guard.sh /etc/codex/hooks/aws-readonly-guard.sh
COPY survey-status /usr/local/bin/survey-status
COPY survey-ui.sh  /usr/local/lib/survey-ui.sh
COPY bashrc        /home/node/.bashrc
# ec2 ラッパー: 登録済み EC2 の診断ゲートウェイに 1 コマンドを送る唯一の入口。root 所有で焼き込む。
# 鍵と接続設定（~/.aws-claude/ssh/）は一時キーと同じ読み取り専用マウントで届く。
COPY ec2           /usr/local/bin/ec2
RUN chown -R root:root /home/node/aws-survey \
 && chmod -R go-w      /home/node/aws-survey \
 && chmod 755          /home/node/aws-survey/.claude/hooks/aws-readonly-guard.sh \
 && chown node:node    /home/node/aws-survey/out \
 && chmod 755          /usr/local/bin/survey-status /usr/local/bin/ec2 \
 && chown node:node    /home/node/.bashrc

# Claude Code の状態ディレクトリ。イメージ側で node 所有にしておかないと、
# ここに名前付きボリュームを当てたときマウントポイントが root 所有になり、
# 非 root の node からトランスクリプトを書けなくなる（EACCES）。
RUN mkdir -p /home/node/.claude/projects /home/node/.claude/shell-snapshots \
 && mkdir -p /home/node/.codex \
 && chown -R node:node /home/node/.claude /home/node/.codex

# ネイティブ版を node ユーザーの ~/.local に入れ、踏み台の npm 版を消す。
# 以降 claude は ~/.local/bin/claude（PATH で /usr/local より先）に解決される。
USER node
RUN claude install latest
USER root
RUN npm uninstall -g @anthropic-ai/claude-code

USER node
RUN claude --version \
 && claude doctor 2>&1 | grep -q 'Auto-updates: enabled' \
 && claude doctor 2>&1 | grep -q 'Config install method: native'
WORKDIR /home/node/aws-survey
# リージョンは焼き込まない。claude-ro プロファイルの config 側に入っており、
# run.sh も environment.json の値を AWS_DEFAULT_REGION として渡す。
# ここに既定値を書くと、環境変数がプロファイルより優先されるため上書きが効かなくなる。
ENV AWS_CONFIG_FILE=/home/node/.aws-claude/config \
    AWS_SHARED_CREDENTIALS_FILE=/home/node/.aws-claude/credentials \
    AWS_PROFILE=claude-ro \
    AWS_PAGER=""

# ロケール。未設定だと C ロケールになり、ls が日本語のファイル名を
# '\346\227\245...' のようなエスケープで出す（out/ のフォルダ名もフェーズ名も日本語）。
# bookworm の glibc は C.UTF-8 を内蔵しているので、locales の導入は要らない。
# LC_ALL ではなく LANG にする。中で個別に上書きしたいときの余地を残す。
ENV LANG=C.UTF-8

# シェルで起動する。claude / codex の起動と終了はコンテナ内で自由に繰り返せる。
#   （quit しても会話は CLAUDE_CONFIG_DIR のボリュームに残るため claude -c で続けられる）
CMD ["bash"]
