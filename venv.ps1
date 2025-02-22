# --- ✅ PowerShell 7.5.0を最初に確認・自動切り替え ---
if ($PSVersionTable.PSVersion.Major -ne 7 -or $PSVersionTable.PSVersion.Minor -ne 5) {
    Write-Output "⚡ PowerShell 7.5.0 で再起動します..."
    & pwsh -NoExit -Command "& '$PSCommandPath'"
    exit
}

Write-Output "✅ PowerShell 7.5.0が確認されました。処理を開始します..."

# --- UTF-8エンコーディングを設定 (PowerShell 7ではデフォルトでUTF-8) ---
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

Write-Output "✅ UTF-8エンコーディングが設定されました。"

# --- PostgreSQLのパスを環境変数に追加 ---
$env:Path += ";C:\Program Files\PostgreSQL\17\bin"

# --- 仮想環境を有効化 ---
if (Test-Path .\.venv\Scripts\Activate.ps1) {
    Write-Output "✅ 仮想環境を有効化します..."
    & .\.venv\Scripts\Activate.ps1
} else {
    Write-Output "❌ 仮想環境が見つかりません。仮想環境を作成します..."
    python -m venv .venv
    & .\.venv\Scripts\Activate.ps1
}

# --- PostgreSQL接続用の一時SQLファイルをBOMなしUTF-8で作成 ---
$tempSqlFile = ".\temp_connect.sql"
$utf8NoBomEncoding = New-Object System.Text.UTF8Encoding($false)  # BOMなしUTF-8エンコーディング

[System.IO.File]::WriteAllLines($tempSqlFile, @(
    "SHOW SERVER_ENCODING;"                  # サーバーエンコーディング確認
    "SET client_encoding TO 'SJIS';"         # クライアントエンコーディングを設定
    "SELECT '✅ PostgreSQLへの接続に成功しました！ AS メッセージ';"
    "\q"
), $utf8NoBomEncoding)

# --- パスワードとクライアントエンコーディングを環境変数に設定してpsqlを実行 ---
$env:PGPASSWORD = 'doxu1357'
$env:PGCLIENTENCODING = "SJIS"  # クライアントエンコーディングを環境変数で設定

# --- psql実行と出力をUTF-8でファイルに保存して確認 ---
$outputFile = ".\output_check.txt"
psql -U domyhehehe -h localhost -d horse -f $tempSqlFile | Out-File -FilePath $outputFile -Encoding utf8

# --- 出力ファイルの内容をUTF-8として読み取り、ターミナルに表示 ---
Write-Output "🔍 'output_check.txt' の内容を確認します: 正常なら日本語表示も成功しています。"
Get-Content -Path $outputFile -Encoding utf8

# --- 一時ファイルを削除 ---
Remove-Item $tempSqlFile -ErrorAction SilentlyContinue

Write-Output "🎉 ✅ 全ての処理が完了しました！"
Pause
