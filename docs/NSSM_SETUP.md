# NSSM サービス化 設定手順

ThreadsBotをWindows上でバックグラウンドサービスとして永続化するための手順。

## 前提条件

- Windows 10/11
- Python 3.11+ がインストール済み
- ThreadsBotが `C:\ThreadsBot` に配置済み

## 方法1: NSSM（推奨）

### 1. NSSMのインストール

```powershell
choco install nssm
```

chocolateyがない場合は https://nssm.cc/download からダウンロード。

### 2. サービス登録

```powershell
nssm install ThreadsBot "C:\Python311\python.exe" "-m core.scheduler all"
nssm set ThreadsBot AppDirectory "C:\ThreadsBot"
nssm set ThreadsBot AppStdout "C:\ThreadsBot\data\logs\service_stdout.log"
nssm set ThreadsBot AppStderr "C:\ThreadsBot\data\logs\service_stderr.log"
nssm set ThreadsBot AppRotateFiles 1
nssm set ThreadsBot AppRotateBytes 10485760
```

> Pythonパスは環境に合わせて変更してください（`where python` で確認）。

### 3. サービス操作

```powershell
# 起動
nssm start ThreadsBot

# ステータス確認
nssm status ThreadsBot

# 停止
nssm stop ThreadsBot

# 再起動
nssm restart ThreadsBot

# サービス削除（アンインストール時）
nssm remove ThreadsBot confirm
```

## 方法2: Task Scheduler

### 設定手順

1. `taskschd.msc` を開く
2. 「タスクの作成」→ 名前: `ThreadsBot`
3. トリガー:
   - 「スタートアップ時」
   - 「毎日 06:50」（二重保険）
4. 操作:
   - プログラム: `C:\Python311\python.exe`
   - 引数: `-m core.scheduler all`
   - 開始: `C:\ThreadsBot`
5. 設定:
   - 「タスクが既に実行中の場合は新しいインスタンスを開始しない」を選択
   - 「ユーザーがログオンしているかどうかにかかわらず実行する」を選択

### バックアップの自動実行

別タスクとして `scripts/backup.py` を登録:

1. 名前: `ThreadsBot-Backup`
2. トリガー: 毎日 04:00
3. 操作:
   - プログラム: `C:\Python311\python.exe`
   - 引数: `scripts/backup.py`
   - 開始: `C:\ThreadsBot`

## Heartbeat 監視

Supervisorが15分ごとに `system_state.json` の `last_health_check` を更新する。

外部監視（Uptime Kuma等）で `last_health_check` が **30分以上更新なし** の場合にアラートを設定:

```powershell
# PowerShellでのチェック例
$state = Get-Content "C:\ThreadsBot\data\state\system_state.json" | ConvertFrom-Json
$lastCheck = [DateTime]::Parse($state.last_health_check)
$diff = (Get-Date) - $lastCheck
if ($diff.TotalMinutes -gt 30) {
    Write-Warning "ThreadsBot heartbeat stale: $($diff.TotalMinutes) minutes ago"
    nssm restart ThreadsBot
}
```

## トラブルシューティング

| 症状 | 対処 |
|------|------|
| サービスが起動しない | `data/logs/service_stderr.log` を確認 |
| すぐに停止する | Python path、作業ディレクトリ、.envの存在を確認 |
| メモリリーク | `nssm restart ThreadsBot` で再起動 |
| ログが肥大化 | `AppRotateBytes` が設定されているか確認 |
