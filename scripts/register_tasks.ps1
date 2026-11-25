# Windows 작업 스케줄러에 자동 갱신 2개를 등록한다 (현재 사용자, 로그인 중일 때 실행, 창 없음).
#   FCO 빠른 갱신 : 2시간마다  (보유 매물 + 상위 후보 현재가, 2~4분)
#   FCO 일일 갱신 : 매일 05:30 (랭커 스쿼드 + 전체 카드 가격, 40~50분)
# 폴더를 옮기면 다시 실행해야 한다. 해제: scripts\unregister_tasks.ps1
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$pyw  = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\pythonw.exe'
$task = Join-Path $PSScriptRoot 'refresh_task.py'
if (-not (Test-Path $pyw))  { throw "pythonw.exe 를 찾을 수 없습니다: $pyw" }
if (-not (Test-Path $task)) { throw "refresh_task.py 를 찾을 수 없습니다: $task" }

function New-FcoAction([string]$mode) {
    New-ScheduledTaskAction -Execute $pyw -Argument "`"$task`" $mode" -WorkingDirectory $root
}

# 이전 실행이 아직 돌고 있으면 새 실행은 건너뜀, 놓친 실행은 PC 가 켜지면 실행, 배터리에서도 실행
$quickSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 1)
$dailySettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 3)

$quickTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(10) -RepetitionInterval (New-TimeSpan -Hours 2)
$dailyTrigger = New-ScheduledTaskTrigger -Daily -At '05:30'

Register-ScheduledTask -TaskName 'FCO 빠른 갱신' -Action (New-FcoAction 'quick') -Trigger $quickTrigger `
    -Settings $quickSettings -Description 'FC 매물 분석: 보유 매물 + 상위 후보 현재가 갱신 (2시간마다)' -Force | Out-Null
Register-ScheduledTask -TaskName 'FCO 일일 갱신' -Action (New-FcoAction 'daily') -Trigger $dailyTrigger `
    -Settings $dailySettings -Description 'FC 매물 분석: 랭커 스쿼드 + 전체 카드 가격 갱신 (매일 05:30)' -Force | Out-Null

foreach ($n in 'FCO 빠른 갱신', 'FCO 일일 갱신') {
    $t = Get-ScheduledTask -TaskName $n
    $i = Get-ScheduledTaskInfo -TaskName $n
    "{0} | 상태 {1} | 다음 실행 {2} | 실행 파일 {3} {4}" -f $n, $t.State, $i.NextRunTime, $t.Actions[0].Execute, $t.Actions[0].Arguments
}
