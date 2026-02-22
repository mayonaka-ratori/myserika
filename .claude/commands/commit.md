---
description: "変更内容を確認してコミットメッセージを生成し git commit & push する"
allowed-tools:
  - Bash
model: claude-haiku-4-5-20251001
---

以下の手順でコミットを実行してください。

## Step 1: 変更内容を確認

```bash
python C:/Users/hosom/my-secretary/scripts/git_info.py
```

変更がなければ「コミットするものはありません」と伝えて終了してください。

## Step 2: コミットメッセージを生成

diff の内容から**日本語**で簡潔なコミットメッセージを生成してください。

ルール:
- 1行目: `動詞: 変更の要約`（50字以内）
  - 例: `Add: /todo /tasks /done Telegram コマンド実装`
  - 例: `Fix: quiet モード中の重複通知を修正`
  - 例: `Refactor: task_manager の優先度判定ロジックを整理`
- 引数 `$ARGUMENTS` が指定されている場合はそれをそのままメッセージに使う

## Step 3: ステージングしてコミット & プッシュ

```bash
git -C C:/Users/hosom/my-secretary add src/ AGENT.md README.md docs/ scripts/ .claude/commands/ --ignore-errors
git -C C:/Users/hosom/my-secretary status
```

上記で確認後、以下を実行（メッセージは生成したもので置き換える）:

```bash
git -C C:/Users/hosom/my-secretary commit -m "$(cat <<'EOF'
【生成したメッセージをここに入れる】

Co-Authored-By: Claude Haiku <noreply@anthropic.com>
EOF
)"
git -C C:/Users/hosom/my-secretary push origin main
```

## Step 4: 結果を報告

コミットハッシュと push 結果を1〜2行で報告してください。
