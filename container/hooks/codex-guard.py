#!/usr/bin/env python3
"""Codex PreToolUse adapter. AWS policy stays in aws-readonly-guard.sh.

This is an accident guard, not an independent audit or an OS security boundary.
Only the built-in shell, patch, and conversation tools are supported.
"""
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys

ROOT = Path('/home/node/aws-survey')
AWS_GUARD = Path(__file__).with_name('aws-readonly-guard.sh')
SAFE_COMMANDS = {'jq', 'survey-status', 'ls', 'cat', 'head', 'tail', 'wc', 'grep', 'rg', 'mkdir', 'pwd'}


def deny(reason):
    print(json.dumps({'hookSpecificOutput': {
        'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
        'permissionDecisionReason': reason,
    }}, ensure_ascii=False))
    raise SystemExit(0)


def output_path(value, cwd):
    path = Path(value)
    if not path.is_absolute():
        path = cwd / path
    resolved = path.resolve()
    if not resolved.is_relative_to(ROOT / 'out'):
        deny('編集先は out/ 配下に限定されています。')
    if 'aws-audit' in str(path) or 'aws-audit' in str(resolved):
        deny('監査ログは編集できません。')
    # Do not follow symlink aliases, even if the target is currently within out/.
    if any(p.is_symlink() for p in [path, *path.parents]):
        deny('シンボリックリンク経由の編集はできません。')


def check(event):
    name = event.get('tool_name')
    args = event.get('tool_input', {})
    cwd = Path(event.get('cwd', str(ROOT))).resolve()
    if name in {'update_plan', 'request_user_input'}:
        return
    if not isinstance(args, dict):
        deny('未知のツール入力です。')
    if name == 'apply_patch':
        patch = args.get('command')
        if not isinstance(patch, str):
            deny('パッチ形式を確認できません。')
        paths = re.findall(r'^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)$', patch, re.M)
        if not paths:
            deny('編集先を確認できません。')
        for path in paths:
            output_path(path, cwd)
        return
    if name != 'Bash':
        deny('この環境ではこのツールを使用できません。')
    command = args.get('command')
    if not isinstance(command, str) or not command.strip():
        deny('コマンドを確認できません。')
    if cwd != ROOT or args.get('cwd', str(ROOT)) != str(ROOT):
        deny('作業ディレクトリ直下から実行してください。')
    # No interactive processes or shell expansion. File edits use apply_patch.
    if any(c in command for c in ['\n', '\r', '`', '$', '\\']):
        deny('展開・複数行コマンドは使用できません。')
    lexer = shlex.shlex(command, posix=True, punctuation_chars=';&|<>()')
    lexer.whitespace_split = True
    lexer.commenters = ''
    tokens = list(lexer)
    if not tokens:
        deny('空のコマンドです。')
    if tokens[0] == 'aws':
        result = subprocess.run(['bash', str(AWS_GUARD)], input=json.dumps(event), text=True, capture_output=True)
        if result.returncode != 0:
            deny(result.stderr or 'AWS コマンドの検査に失敗しました。')
        return
    # shlex.split retains paths and quoted jq expressions as single arguments.
    words = shlex.split(command)
    if words[0] not in SAFE_COMMANDS:
        deny('許可された読み取りコマンドだけを使用してください。編集は apply_patch を使います。')
    # Conservative: these forms are unnecessary for reading saved JSON.
    if any(token and all(c in ';&|<>()' for c in token) for token in tokens):
        deny('パイプ・連結・リダイレクトは使用できません。')
    if any(s in command for s in ['.aws', '.codex', '.claude', '/etc/', '/proc/', '/dev/', 'aws-audit', 'codex-guard', 'aws-readonly-guard']):
        # Directory listing is useful for the environment check; never allow reading keys.
        if words != ['ls', '-la', '/home/node/.aws-claude']:
            deny('資格情報・ガード設定・監査ログにはアクセスできません。')
    if any(w == '--pre' or w.startswith('--pre=') or w in {'--hostname-bin'} or w.startswith('--hostname-bin=') for w in words):
        deny('外部プログラムを実行するオプションは使用できません。')
    # Reject file aliases to protected locations, including symlinked directories.
    for word in words[1:]:
        path = Path(word)
        path = path if path.is_absolute() else cwd / path
        if any(p.is_symlink() for p in [path, *path.parents]):
            deny('シンボリックリンク経由のアクセスはできません。')
    if words[0] == 'mkdir':
        for word in words[1:]:
            if word != '-p':
                output_path(word, cwd)


if __name__ == '__main__':
    try:
        check(json.load(sys.stdin))
    except Exception:
        deny('ツール入力を安全に検査できませんでした。')
