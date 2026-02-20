@echo off
:: ============================================================
:: start_secretary.bat  -  MY-SECRETARY バックグラウンド起動
:: ============================================================
::
:: 【スタートアップへの自動起動登録】
::
::   [方法A: スタートアップフォルダ（手軽）]
::     1. Win+R → shell:startup → Enter
::     2. このファイルを右クリック →「ショートカットの作成」
::     3. 作成されたショートカットを shell:startup フォルダへ移動
::        ※ ファイル本体ではなくショートカットを置くこと
::
::   [方法B: タスクスケジューラ（推奨・完全ウィンドウレス）]
::     1. スタートメニュー →「タスクスケジューラ」で検索・起動
::     2. 右ペイン「基本タスクの作成」をクリック
::     3. 名前: MY-SECRETARY
::        トリガー: ログオン時
::     4. 操作の設定:
::          プログラム/スクリプト: powershell.exe
::          引数の追加: -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "C:\Users\hosom\my-secretary\start_secretary.ps1"
::     5.「完了」→ 条件タブで「AC電源時のみ」のチェックを外す
::        → 完全にウィンドウなし・確実に起動
::
:: 【ログ確認】  logs\secretary.log をメモ帳等で開く
:: 【停止方法】  stop_secretary.bat をダブルクリック
:: ============================================================

set "BASE=%~dp0"
if "%BASE:~-1%"=="\" set "BASE=%BASE:~0,-1%"

:: start_secretary.ps1 を -WindowStyle Hidden で実行
:: ダブルクリック時: このウィンドウが一瞬出る → PowerShell は非表示
:: タスクスケジューラ登録時: 完全にウィンドウなし
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "%BASE%\start_secretary.ps1" "%BASE%"
