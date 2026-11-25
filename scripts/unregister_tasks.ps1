# register_tasks.ps1 로 등록한 자동 갱신 작업 2개를 해제한다.
foreach ($n in 'FCO 빠른 갱신', 'FCO 일일 갱신') {
    if (Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $n -Confirm:$false
        "해제: $n"
    } else {
        "등록되어 있지 않음: $n"
    }
}
