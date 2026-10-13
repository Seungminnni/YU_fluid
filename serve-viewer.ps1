param(
    [Parameter(Mandatory = $true)][string]$Root,
    [int]$Port = 0,
    [Parameter(Mandatory = $true)][string]$PortFile
)

$ErrorActionPreference = "Stop"
$rootFull = [IO.Path]::GetFullPath($Root).TrimEnd("\")
$rootPrefix = $rootFull + "\"
$listener = New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback, $Port)
$listener.Start()
$actualPort = ([Net.IPEndPoint]$listener.LocalEndpoint).Port
[IO.File]::WriteAllText($PortFile, [string]$actualPort, [Text.Encoding]::ASCII)
$lastRequest = [DateTime]::UtcNow

function Get-ContentType([string]$Path) {
    switch ([IO.Path]::GetExtension($Path).ToLowerInvariant()) {
        ".html" { return "text/html; charset=utf-8" }
        ".htm"  { return "text/html; charset=utf-8" }
        ".js"   { return "application/javascript; charset=utf-8" }
        ".css"  { return "text/css; charset=utf-8" }
        ".json" { return "application/json; charset=utf-8" }
        ".svg"  { return "image/svg+xml" }
        ".png"  { return "image/png" }
        ".jpg"  { return "image/jpeg" }
        ".jpeg" { return "image/jpeg" }
        ".gif"  { return "image/gif" }
        ".webp" { return "image/webp" }
        ".ico"  { return "image/x-icon" }
        ".txt"  { return "text/plain; charset=utf-8" }
        ".csv"  { return "text/csv; charset=utf-8" }
        default  { return "application/octet-stream" }
    }
}

function Send-TextResponse($Stream, [int]$Status, [string]$Reason, [string]$Text, [bool]$HeadOnly = $false) {
    $body = [Text.Encoding]::UTF8.GetBytes($Text)
    $header = "HTTP/1.1 $Status $Reason`r`nContent-Type: text/plain; charset=utf-8`r`nContent-Length: $($body.Length)`r`nConnection: close`r`n`r`n"
    $headerBytes = [Text.Encoding]::ASCII.GetBytes($header)
    $Stream.Write($headerBytes, 0, $headerBytes.Length)
    if (-not $HeadOnly) { $Stream.Write($body, 0, $body.Length) }
}

try {
    while (([DateTime]::UtcNow - $lastRequest).TotalHours -lt 4) {
        if (-not $listener.Pending()) {
            Start-Sleep -Milliseconds 100
            continue
        }
        $client = $listener.AcceptTcpClient()
        $lastRequest = [DateTime]::UtcNow
        try {
            $client.ReceiveTimeout = 10000
            $client.SendTimeout = 10000
            $stream = $client.GetStream()
            $reader = New-Object IO.StreamReader($stream, [Text.Encoding]::ASCII, $false, 1024, $true)
            $requestLine = $reader.ReadLine()
            while (($headerLine = $reader.ReadLine()) -ne $null -and $headerLine -ne "") { }
            if ([string]::IsNullOrWhiteSpace($requestLine)) {
                Send-TextResponse $stream 400 "Bad Request" "Bad Request"
                continue
            }
            $parts = $requestLine.Split(" ")
            if ($parts.Length -lt 2 -or ($parts[0] -ne "GET" -and $parts[0] -ne "HEAD")) {
                Send-TextResponse $stream 405 "Method Not Allowed" "Method Not Allowed"
                continue
            }
            $headOnly = $parts[0] -eq "HEAD"
            $urlPath = [Uri]::UnescapeDataString(($parts[1] -split "\?", 2)[0])
            if ($urlPath -eq "/_cfd_twin_health") {
                Send-TextResponse $stream 200 "OK" "YU_FLUID_VIEWER_OK" $headOnly
                continue
            }
            $relative = $urlPath.TrimStart("/").Replace("/", "\")
            $fullPath = [IO.Path]::GetFullPath((Join-Path $rootFull $relative))
            if ($fullPath -ne $rootFull -and -not $fullPath.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
                Send-TextResponse $stream 403 "Forbidden" "Forbidden" $headOnly
                continue
            }
            if ([IO.Directory]::Exists($fullPath)) { $fullPath = Join-Path $fullPath "index.html" }
            if (-not [IO.File]::Exists($fullPath)) {
                Send-TextResponse $stream 404 "Not Found" "Not Found" $headOnly
                continue
            }
            $file = [IO.File]::OpenRead($fullPath)
            try {
                $contentType = Get-ContentType $fullPath
                $responseHeader = "HTTP/1.1 200 OK`r`nContent-Type: $contentType`r`nContent-Length: $($file.Length)`r`nCache-Control: no-cache`r`nConnection: close`r`n`r`n"
                $responseBytes = [Text.Encoding]::ASCII.GetBytes($responseHeader)
                $stream.Write($responseBytes, 0, $responseBytes.Length)
                if (-not $headOnly) { $file.CopyTo($stream) }
            }
            finally {
                $file.Dispose()
            }
        }
        catch {
            try { Send-TextResponse $stream 500 "Internal Server Error" "Internal Server Error" } catch { }
        }
        finally {
            $client.Close()
        }
    }
}
finally {
    $listener.Stop()
    Remove-Item -LiteralPath $PortFile -Force -ErrorAction SilentlyContinue
}
