# 절전 모드 끄기/켜기 (바탕화면 바로가기용). 외출 중 휴대폰 접속이 끊기지 않게 PC 가 잠들지 않도록 한다.
#   off    : 지금 절전·최대 절전 시간을 data\power_backup.json 에 저장하고 '안 함'(0) 으로 바꿈
#   on     : 저장해 둔 시간으로 되돌림 (저장한 게 없으면 전원 연결 30분 / 배터리 15분)
#   status : 바꾸지 않고 지금 값만 보여 줌
# 모니터 꺼짐 · 화면 잠금은 건드리지 않는다 (화면이 꺼져도 PC 는 계속 켜져 있음).
param([ValidateSet('on', 'off', 'status')][string]$mode = 'status')
$ErrorActionPreference = 'Stop'
$root   = Split-Path -Parent $PSScriptRoot
$backup = Join-Path $root 'data\power_backup.json'

function Get-Idle([string]$setting) {
    $out = powercfg /query SCHEME_CURRENT SUB_SLEEP $setting
    $hex = @($out | Select-String -Pattern '0x[0-9a-fA-F]{8}' -AllMatches | ForEach-Object { $_.Matches[-1].Value })
    # 마지막 두 줄이 AC(전원 연결), DC(배터리) 현재 값
    [pscustomobject]@{ ac = [Convert]::ToInt32($hex[-2], 16); dc = [Convert]::ToInt32($hex[-1], 16) }
}
function Set-Idle([string]$setting, [int]$ac, [int]$dc) {
    powercfg /setacvalueindex SCHEME_CURRENT SUB_SLEEP $setting $ac | Out-Null
    powercfg /setdcvalueindex SCHEME_CURRENT SUB_SLEEP $setting $dc | Out-Null
}
function Fmt([int]$sec) { if ($sec -eq 0) { '안 함' } else { '{0}분' -f [math]::Round($sec / 60) } }
function Show([string]$msg) {
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show($msg, 'FC 매물 분석 · 절전') | Out-Null
}

$sleep = Get-Idle 'STANDBYIDLE'; $hib = Get-Idle 'HIBERNATEIDLE'
$now = "지금: 절전 전원 연결 {0} / 배터리 {1} · 최대 절전 {2} / {3}" -f (Fmt $sleep.ac), (Fmt $sleep.dc), (Fmt $hib.ac), (Fmt $hib.dc)

if ($mode -eq 'status') { $now; return }

if ($mode -eq 'off') {
    if ($sleep.ac -eq 0 -and $sleep.dc -eq 0 -and $hib.ac -eq 0 -and $hib.dc -eq 0) {
        Show "이미 절전 모드가 꺼져 있습니다.`n`n$now"; return
    }
    @{ sleep = $sleep; hibernate = $hib; saved_at = (Get-Date).ToString('s') } | ConvertTo-Json | Set-Content -Path $backup -Encoding UTF8
    Set-Idle 'STANDBYIDLE' 0 0; Set-Idle 'HIBERNATEIDLE' 0 0
    powercfg /setactive SCHEME_CURRENT
    Show "절전 모드를 껐습니다. PC 가 잠들지 않아 외출 중에도 휴대폰으로 접속할 수 있습니다.`n(모니터는 평소처럼 꺼질 수 있음)`n`n이전 값 저장: 절전 $(Fmt $sleep.ac) / $(Fmt $sleep.dc)"
    return
}

# on
if (Test-Path $backup) {
    $b = Get-Content $backup -Raw | ConvertFrom-Json
    $sac, $sdc, $hac, $hdc = [int]$b.sleep.ac, [int]$b.sleep.dc, [int]$b.hibernate.ac, [int]$b.hibernate.dc
} else { $sac, $sdc, $hac, $hdc = 1800, 900, 0, 0 }
if ($sac -eq 0 -and $sdc -eq 0) { $sac, $sdc = 1800, 900 }   # 저장된 값도 '안 함'이면 기본값
Set-Idle 'STANDBYIDLE' $sac $sdc; Set-Idle 'HIBERNATEIDLE' $hac $hdc
powercfg /setactive SCHEME_CURRENT
Show "절전 모드를 켰습니다: 전원 연결 $(Fmt $sac) / 배터리 $(Fmt $sdc) 뒤 절전.`n외출 중에는 휴대폰 접속이 끊길 수 있습니다."