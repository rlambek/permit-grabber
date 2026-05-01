Set-Location 'C:\Users\Max\projects\campflare-bot'

Write-Host ''
Write-Host '=== Permit booking test run ===' -ForegroundColor Cyan
Write-Host 'Permit: Dinosaur Green And Yampa River Permit (250014)'
Write-Host 'Date:   2026-08-16'
Write-Host 'Group:  6'
Write-Host ''

# Credentials come from the OS keyring (run `python book_permit.py --store-creds` once)
# or from the RECGOV_EMAIL / RECGOV_PASSWORD env vars if you prefer to set them inline.

$alert = @{
    permit_name        = 'Dinosaur Green And Yampa River Permit'
    permit_url         = 'https://www.recreation.gov/permits/250014'
    segment            = 'Deerlodge Park, Yampa River'
    date               = '2026-08-16'
    group_size         = 6
    alert_received_at  = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
} | ConvertTo-Json -Compress

Write-Host ''
Write-Host 'Launching browser. Watch the window — solve any MFA/CAPTCHA in it if prompted.' -ForegroundColor Yellow
Write-Host ''

$alert | python book_permit.py --alert-stdin --unattended

Write-Host ''
Write-Host 'Script exited. This window will stay open.' -ForegroundColor Green
