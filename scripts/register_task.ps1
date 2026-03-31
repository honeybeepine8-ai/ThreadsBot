# ThreadsBot タスクスケジューラ登録スクリプト
# 管理者として実行してください

$action = New-ScheduledTaskAction -Execute "C:\ThreadsBot\scripts\start_bot.bat"
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit 0 -StartWhenAvailable -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest

Register-ScheduledTask `
    -TaskName "ThreadsBot_AutoStart" `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Force

Write-Host "登録完了: ThreadsBot_AutoStart" -ForegroundColor Green
Write-Host "ログオン時に自動起動します。" -ForegroundColor Green
