<#
.SYNOPSIS
    Standalone Windows Product Key Harvester for Intel NUCs
    Part of MGW_Kompy

.DESCRIPTION
    Runs directly inside Windows 10/11 on the NUCs.
    Extracts OEM BIOS Key (MSDM / SoftwareLicensingService) and Registry Keys.
    Can export to a local CSV, network share, or POST directly to the MGW_Kompy deployment server.

.PARAMETER ServerUrl
    Optional HTTP URL of MGW_Kompy server (e.g., http://192.168.1.1:8000)

.PARAMETER CsvPath
    Optional CSV path to append results (e.g., "D:\nuc_keys.csv")
#>

param(
    [string]$ServerUrl = "",
    [string]$CsvPath = "nuc_keys.csv"
)

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "     INTEL NUC WINDOWS KEY HARVESTER (MGW_Kompy)         " -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan

# 1. Hardware Info
$serial = (Get-CimInstance Win32_BIOS).SerialNumber
$model  = (Get-CimInstance Win32_ComputerSystem).Model
$activeNic = Get-NetAdapter | Where-Object Status -eq 'Up' | Select-Object -First 1
$mac    = if ($activeNic) { $activeNic.MacAddress.ToLower() } else { "00:00:00:00:00:00" }
$hostName = $env:COMPUTERNAME

Write-Host "[*] Hostname : $hostName"
Write-Host "[*] Model    : $model"
Write-Host "[*] Serial   : $serial"
Write-Host "[*] MAC      : $mac"

# 2. Extract Windows Product Key
$winKey = $null
$source = "NONE"

# Method A: OEM BIOS ACPI MSDM Key via CIM / WMI
try {
    $licService = Get-CimInstance -ClassName SoftwareLicensingService -ErrorAction SilentlyContinue
    if ($licService -and $licService.OA3xOriginalProductKey) {
        $winKey = $licService.OA3xOriginalProductKey.Trim()
        $source = "BIOS_MSDM"
    }
} catch {}

# Method B: Registry Backup Product Key
if (-not $winKey) {
    try {
        $regPath = "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\SoftwareProtectionPlatform"
        $regKey = (Get-ItemProperty -Path $regPath -Name "BackupProductKeyDefault" -ErrorAction SilentlyContinue).BackupProductKeyDefault
        if ($regKey -and ($regKey -match "^[A-Z0-9]{5}-[A-Z0-9]{5}-[A-Z0-9]{5}-[A-Z0-9]{5}-[A-Z0-9]{5}$")) {
            $winKey = $regKey.Trim()
            $source = "REGISTRY_BACKUP"
        }
    } catch {}
}

# Method C: Decode DigitalProductId
if (-not $winKey) {
    function Decode-DigitalProductId($digitalProductId) {
        if (-not $digitalProductId) { return $null }
        $keyStartIndex = 52
        $key = ""
        $isWin8 = [byte](($digitalProductId[66] / 6) -band 1)
        $digitalProductId[66] = [byte](($digitalProductId[66] -band 0xf7) -bor (($isWin8 -band 2) * 4))
        $digits = "BCDFGHJKMPQRTVWXY2346789"
        for ($i = 24; $i -ge 0; $i--) {
            $current = 0
            for ($j = 14; $j -ge 0; $j--) {
                $current = $current * 256
                $current = $digitalProductId[$j + $keyStartIndex] + $current
                $digitalProductId[$j + $keyStartIndex] = [byte]($current / 24)
                $current = $current % 24
            }
            $key = $digits[$current] + $key
            if (($i % 5 -eq 0) -and ($i -ne 0)) {
                $key = "-" + $key
            }
        }
        return $key
    }

    try {
        $dpi = (Get-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion" -Name "DigitalProductId" -ErrorAction SilentlyContinue).DigitalProductId
        if ($dpi) {
            $decoded = Decode-DigitalProductId $dpi
            if ($decoded -and ($decoded -match "^[A-Z0-9]{5}-[A-Z0-9]{5}-[A-Z0-9]{5}-[A-Z0-9]{5}-[A-Z0-9]{5}$")) {
                $winKey = $decoded
                $source = "DIGITAL_PRODUCT_ID"
            }
        }
    } catch {}
}

if ($winKey) {
    Write-Host "[SUCCESS] Windows Product Key: $winKey ($source)" -ForegroundColor Green
} else {
    Write-Host "[WARNING] No Windows key found." -ForegroundColor Yellow
    $winKey = "NOT_FOUND"
}

# 3. Save to local / network CSV
$csvRecord = [PSCustomObject]@{
    Timestamp    = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    Hostname     = $hostName
    MAC          = $mac
    Serial       = $serial
    Model        = $model
    WindowsKey   = $winKey
    KeySource    = $source
}

$csvExists = Test-Path -Path $CsvPath
$csvRecord | Export-Csv -Path $CsvPath -Append -NoTypeInformation -Encoding UTF8
Write-Host "[+] Saved to CSV file: $CsvPath" -ForegroundColor Cyan

# 4. Post to Deployment Server if provided
if ($ServerUrl) {
    $apiEndpoint = "$ServerUrl/api/register"
    $payload = @{
        mac         = $mac
        serial      = $serial
        model       = $model
        windows_key = $winKey
        key_source  = $source
        notes       = "Collected from Windows PS1"
    } | ConvertTo-Json

    try {
        Write-Host "[*] Transmitting to server: $apiEndpoint ..."
        $response = Invoke-RestMethod -Uri $apiEndpoint -Method Post -Body $payload -ContentType "application/json" -TimeoutSec 5
        Write-Host "[SUCCESS] Registered with server! Assigned IP: $($response.assigned_ip), Host: $($response.assigned_hostname)" -ForegroundColor Green
    } catch {
        Write-Host "[!] Could not send to server: $_" -ForegroundColor Red
    }
}

Write-Host "Done!" -ForegroundColor Green
