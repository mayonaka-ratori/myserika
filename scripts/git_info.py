#!/usr/bin/env python3
"""
git_info.py
git status / diff の情報を整理して出力する。/commit スキルから呼ばれる。
標準ライブラリのみ使用。

出力:
  1. 変更ファイル一覧 (git status --short)
  2. diff 内容（1000行上限でトリミング）
  3. 直近3コミット（スタイル参考）
"""
import io
import os
import subprocess
import sys


BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Windows CP932 端末でも Unicode 記号を出力できるよう UTF-8 に統一
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf_8"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

DIFF_MAX_LINES = 300  # Haiku に渡す diff の最大行数（トークン節約）


def git(args: list[str]) -> str:
    """git コマンドを実行して stdout を返す。エラーは空文字。"""
    try:
        r = subprocess.run(
            ["git", "-C", BASE] + args,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        return r.stdout.strip()
    except Exception as e:
        return f"(git error: {e})"


def section(title: str, body: str) -> None:
    if body:
        print(f"=== {title} ===")
        print(body)
        print()


def main() -> int:
    # 1. 変更ファイル一覧
    status = git(["status", "--short"])
    if not status:
        print("変更なし — コミットするものはありません")
        return 0

    section("変更ファイル", status)

    # 2. ステージング済み diff
    staged = git(["diff", "--cached"])
    staged_lines = staged.splitlines()
    if staged_lines:
        trimmed = "\n".join(staged_lines[:DIFF_MAX_LINES])
        if len(staged_lines) > DIFF_MAX_LINES:
            trimmed += f"\n... (+{len(staged_lines) - DIFF_MAX_LINES} 行省略)"
        section(f"staged diff ({min(len(staged_lines), DIFF_MAX_LINES)} 行)", trimmed)

    # 3. 未ステージ diff（stat のみ — 内容は staged で把握できるため）
    unstaged_stat = git(["diff", "--stat"])
    section("unstaged (stat)", unstaged_stat)

    # 4. 直近3コミット（メッセージスタイルの参考）
    log = git(["log", "--oneline", "-3"])
    section("直近コミット（スタイル参考）", log)

    return 0


if __name__ == "__main__":
    sys.exit(main())
