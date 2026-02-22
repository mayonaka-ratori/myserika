#!/usr/bin/env python3
"""
bot_control.py
MY-SECRETARY ボットの起動・停止・状態確認・ログ表示を行う。
- start / stop  : 既存の PowerShell スクリプトに委譲（実績ある実装を再利用）
- status / logs : Python 標準ライブラリのみで実装
- restart       : stop → start の組み合わせ

使い方:
  python scripts/bot_control.py status
  python scripts/bot_control.py start
  python scripts/bot_control.py stop
  python scripts/bot_control.py restart
  python scripts/bot_control.py logs [行数=30]
"""
import io
import os
import subprocess
import sys
import time


BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PID_FILE = os.path.join(BASE, "logs", "secretary.pid")
LOG_FILE = os.path.join(BASE, "logs", "secretary.log")
START_PS1 = os.path.join(BASE, "start_secretary.ps1")
STOP_PS1 = os.path.join(BASE, "stop_secretary.ps1")

# Windows CP932 端末でも Unicode 記号を出力できるよう UTF-8 に統一
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf_8"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


# ── ユーティリティ / Utilities ───────────────────────────────────────────────

def _read_pid() -> int | None:
    """PID ファイルから PID を読む。なければ None。"""
    try:
        with open(PID_FILE, encoding="ascii") as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return None


def _is_alive(pid: int) -> bool:
    """tasklist で指定 PID のプロセスが存在するか確認する。"""
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True,
        )
        return str(pid) in result.stdout
    except Exception:
        return False


def _run_ps1(script_path: str) -> None:
    """PowerShell スクリプトを実行して stdout/stderr をそのまま表示する。"""
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass",
            "-File", script_path,
            BASE,
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if result.stdout.strip():
        print(result.stdout.strip())
    if result.stderr.strip():
        print(result.stderr.strip(), file=sys.stderr)


def _remove_pid() -> None:
    try:
        os.remove(PID_FILE)
    except FileNotFoundError:
        pass


# ── コマンド実装 / Command implementations ──────────────────────────────────

def cmd_status() -> None:
    pid = _read_pid()
    if pid and _is_alive(pid):
        print(f"🟢  稼働中  (PID: {pid})")
        print()
        _show_logs(8)
    else:
        print("🔴  停止中")
        _remove_pid()


def cmd_start() -> None:
    pid = _read_pid()
    if pid and _is_alive(pid):
        print(f"⚠️   既に稼働中です (PID: {pid})")
        return
    _run_ps1(START_PS1)


def cmd_stop() -> None:
    _run_ps1(STOP_PS1)


def cmd_restart() -> None:
    print("--- stop ---")
    cmd_stop()
    time.sleep(1.5)
    print("--- start ---")
    cmd_start()


def _show_logs(n: int = 30) -> None:
    # cmd.exe リダイレクトは CP932 で書かれることが多いため順番に試行する
    for enc in ("utf-8", "cp932", "utf-8-sig"):
        try:
            with open(LOG_FILE, encoding=enc, errors="replace") as f:
                lines = f.readlines()
            for line in lines[-n:]:
                print(line, end="")
            return
        except (FileNotFoundError, UnicodeDecodeError):
            pass
    print(f"⚠️   ログファイルが見つかりません: {LOG_FILE}")


def cmd_logs(n: int = 30) -> None:
    _show_logs(n)


# ── エントリーポイント / Entry point ────────────────────────────────────────

COMMANDS = {
    "status":  lambda args: cmd_status(),
    "start":   lambda args: cmd_start(),
    "stop":    lambda args: cmd_stop(),
    "restart": lambda args: cmd_restart(),
    "logs":    lambda args: cmd_logs(int(args[0]) if args else 30),
}


def main() -> int:
    argv = sys.argv[1:]
    if not argv or argv[0] not in COMMANDS:
        keys = " | ".join(COMMANDS)
        print(f"使い方: bot_control.py <{keys}> [行数]")
        return 1
    COMMANDS[argv[0]](argv[1:])
    return 0


if __name__ == "__main__":
    sys.exit(main())
