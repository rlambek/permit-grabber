Set-Location 'C:\Users\Max\projects\campflare-bot'

if (-not (Test-Path 'auth.json')) {
    Write-Host 'No auth.json found. You will hit the un-authenticated permit page (still useful — the segment control should still render).' -ForegroundColor Yellow
}

python inspect_permit.py 'https://www.recreation.gov/permits/250014'

Write-Host ''
Write-Host 'Inspector exited. Window stays open.' -ForegroundColor Green
