# 바탕화면에 'FC 매물 분석' 바로가기를 만든다 (더블클릭 → 대시보드 서버 켜기 + 브라우저로 열기, 창 없음).
# 폴더를 옮기면 다시 실행한다.
$ErrorActionPreference = 'Stop'
$root    = Split-Path -Parent $PSScriptRoot
$pyw     = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\pythonw.exe'
$script  = Join-Path $PSScriptRoot 'open_dashboard.pyw'
$desktop = [Environment]::GetFolderPath('Desktop')
$lnkPath = Join-Path $desktop 'FC 매물 분석.lnk'
if (-not (Test-Path $pyw)) { throw "pythonw.exe 를 찾을 수 없습니다: $pyw" }

$sh  = New-Object -ComObject WScript.Shell
$lnk = $sh.CreateShortcut($lnkPath)
$lnk.TargetPath       = $pyw
$lnk.Arguments        = "`"$script`""
$lnk.WorkingDirectory = $root
$lnk.Description      = 'FC 온라인 매물 분석 대시보드 열기'
$lnk.IconLocation     = "$env:SystemRoot\System32\imageres.dll,144"
$lnk.Save()
"바로가기 생성: $lnkPath"
