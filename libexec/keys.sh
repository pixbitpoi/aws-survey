#!/usr/bin/env bash
# 一時キーの状態の判定。aws-survey（段階の判定・status）と、一時キーでコンテナに 1 コマンド走らせる
# libexec/container.sh が source する。ファイルだけを見て AWS は叩かない。
# key_state は AWS_DIR と DURATION を使うので、load-env.sh のあとで呼ぶ（読み込むのは先でもよい）。

# ISO 8601（AWS の Expiration: 2026-01-02T03:04:05+00:00 / ...Z）を epoch 秒に。読めなければ空。
iso_to_epoch() {
  local s="$1" n
  n=$(printf '%s' "$s" | sed -E 's/\.[0-9]+//; s/\+00:00$/Z/; s/UTC$/Z/')
  case "$n" in
    *Z) jq -rn --arg t "$n" '$t | fromdateiso8601' 2>/dev/null && return 0 ;;
  esac
  date -d "$s" +%s 2>/dev/null && return 0
  date -j -f '%Y-%m-%dT%H:%M:%S%z' "${s/%Z/+0000}" +%s 2>/dev/null
}
mtime_of() { stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null; }

# 一時キーの状態。KEY_STATE = missing | expired | valid | unknown、KEY_EXP / KEY_LEFT_MIN / KEY_TOTAL_MIN / KEY_NOTE
key_state() {
  local cred="$AWS_DIR/credentials" session="$AWS_DIR/session.json" exp="" dur="" mt exp_epoch now
  KEY_STATE=missing; KEY_EXP=""; KEY_LEFT_MIN=""; KEY_TOTAL_MIN=""; KEY_NOTE=""
  [ -f "$cred" ] || return 0
  if [ -f "$session" ]; then
    exp=$(jq -r '.expiration // empty' "$session" 2>/dev/null)
    dur=$(jq -r '.duration_seconds // empty' "$session" 2>/dev/null)
  fi
  [ -n "$dur" ] || dur="$DURATION"
  if [ -z "$exp" ]; then
    # 発行元の情報が無いときは、credentials の更新時刻 + duration_seconds で推定する
    if mt=$(mtime_of "$cred") && [ -n "$mt" ]; then
      exp_epoch=$(( mt + dur )); KEY_NOTE="（credentials の更新時刻から推定）"
      exp=$(date -u -r "$exp_epoch" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d "@$exp_epoch" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null)
    fi
  else
    exp_epoch=$(iso_to_epoch "$exp")
  fi
  KEY_EXP="$exp"
  if [ -z "${exp_epoch:-}" ]; then KEY_STATE=unknown; return 0; fi
  now=$(date +%s)
  KEY_LEFT_MIN=$(( (exp_epoch - now) / 60 ))
  KEY_TOTAL_MIN=$(( dur / 60 ))
  if [ "$exp_epoch" -le "$now" ]; then KEY_STATE=expired; else KEY_STATE=valid; fi
}
