#!/usr/bin/env python3
"""
syntax_check.py
src/ 配下の全 .py ファイルの AST 構文チェックを行う。
標準ライブラリのみ使用。

使い方:
  python scripts/syntax_check.py
  python scripts/syntax_check.py src/telegram_bot.py   # 個別指定
"""
import ast
import glob
import io
import os
import sys


BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Windows CP932 端末でも Unicode 記号を出力できるよう stdout を UTF-8 に統一
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf_8"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


def check_file(path: str) -> str | None:
    """ファイルを AST パースして構文エラーがあれば説明文字列を返す。"""
    try:
        with open(path, encoding="utf-8") as fh:
            ast.parse(fh.read(), filename=path)
        return None  # OK
    except SyntaxError as e:
        return f"line {e.lineno}: {e.msg}"
    except Exception as e:
        return str(e)


def main() -> int:
    # 引数があれば個別ファイル、なければ src/*.py 全件
    if len(sys.argv) > 1:
        targets = sys.argv[1:]
    else:
        targets = sorted(glob.glob(os.path.join(BASE, "src", "*.py")))

    if not targets:
        print("⚠️  対象ファイルが見つかりません")
        return 1

    errors: list[tuple[str, str]] = []

    for path in targets:
        rel = os.path.relpath(path, BASE)
        err = check_file(path)
        if err:
            print(f"  ✗  {rel}  →  {err}")
            errors.append((rel, err))
        else:
            print(f"  ✓  {rel}")

    print()
    if errors:
        print(f"❌  {len(errors)} ファイルにエラー（計 {len(targets)} 件中）")
        return 1

    print(f"✅  全 {len(targets)} ファイル OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
