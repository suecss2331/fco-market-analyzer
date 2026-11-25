# 로그인할 때 외부 접속(Cloudflare 임시 터널)을 자동으로 켠다. 대시보드 서버가 꺼져 있으면 같이 켠다.
#   작업 이름: FCO 외부 접속   해제: Unregister-ScheduledTask -TaskName 'FCO 외부 접속' -Confirm:$false
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$pyw  = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\pythonw.exe'
if (-not (Test-Path $pyw)) { throw "pythonw.exe 를 찾을 수 없습니다: $pyw" }
if (-not (Test-Path (Join-Path $root 'tools\cloudflared.exe'))) { throw "tools\cloudflared.exe 가 없습니다" }
$action   = New-ScheduledTaskAction -Execute $pyw -Argument '-m fco.tunnel' -WorkingDirectory $root
$trigger  = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
# 시간 제한 없음, 실패하면 1분 뒤 다시 (최대 999번), 배터리에서도 실행, 이미 돌고 있으면 새로 안 띄움
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName 'FCO 외부 접속' -Action $action -Trigger $trigger -Settings $settings `
    -Description 'FC 매물 분석: 휴대폰 접속용 Cloudflare 임시 터널 (로그인 시 시작)' -Force | Out-Null
$t = Get-ScheduledTask -TaskName 'FCO 외부 접속'
"FCO 외부 접속 | 상태 {0} | {1} {2}" -f $t.State, $t.Actions[0].Execute, $t.Actions[0].Arguments