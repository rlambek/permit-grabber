Set-Location 'C:\Users\Max\projects\campflare-bot'
$Host.UI.RawUI.WindowTitle = 'permit-grabber: watching for Yampa alerts'

Write-Host '=== UNATTENDED WATCH LOOP ===' -ForegroundColor Cyan
Write-Host 'Polling Gmail every 60s. Booking fires when an alert with a permit URL listed in permit_config.json arrives.' -ForegroundColor Cyan
Write-Host 'Leave this window open. Ctrl+C to stop.' -ForegroundColor Yellow
Write-Host ''

python scan_alerts.py --watch 60 | python book_permit.py --alert-stdin --unattended
