---
description: "src/*.py の構文チェック（AST パース）を実行して結果を報告する"
allowed-tools:
  - Bash
model: claude-haiku-4-5-20251001
---

以下のコマンドを実行して結果を**そのまま**表示してください。

```bash
python C:/Users/hosom/my-secretary/scripts/syntax_check.py $ARGUMENTS
```

- ✓ が全行なら「全ファイル OK」と一言添えてください
- ✗ がある場合はファイル名と行番号を強調して伝えてください
- 追加の説明や提案は不要です
