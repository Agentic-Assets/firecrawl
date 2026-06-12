# Domain Availability Checker using Firecrawl API
# Reads domains from domains.txt and checks availability via instantdomainsearch.com

$apiUrl = "http://localhost:3002/v1/scrape"
$apiKey = $env:FIRECRAWL_API_KEY
$domainsFile = "domains.txt"
$resultsFile = "domain-availability-results.json"

# Read domains from file
if (-not (Test-Path $domainsFile)) {
    Write-Host "Error: $domainsFile not found!" -ForegroundColor Red
    exit 1
}

$domains = Get-Content $domainsFile | Where-Object { $_.Trim() -ne "" }
$results = @()

Write-Host "Checking $($domains.Count) domains..." -ForegroundColor Cyan
Write-Host ""

$counter = 0
foreach ($domain in $domains) {
    $counter++
    Write-Host "[$counter/$($domains.Count)] Checking $domain..." -ForegroundColor Yellow
    
    $searchUrl = "https://instantdomainsearch.com/?q=$domain"
    
    # Prepare the request body
    $body = @{
        url = $searchUrl
        formats = @("markdown", "extract")
        extract = @{
            prompt = "Determine if the domain $domain is available or taken. Look for status indicators like 'Available', 'Taken', 'Registered', or pricing information. Return a JSON object with: status (available/taken/unknown), price (if available), and any relevant notes."
        }
        waitFor = 5000
    } | ConvertTo-Json -Depth 5
    
    try {
        $headers = @{}
        if ($apiKey) {
            $headers["Authorization"] = "Bearer $apiKey"
        }

        $response = Invoke-RestMethod -Uri $apiUrl -Method Post -ContentType "application/json" `
            -Headers $headers -Body $body -ErrorAction Stop
        
        # Parse availability from markdown content if extract not available
        $availability = "unknown"
        $price = $null
        $notes = ""
        
        if ($response.data.extract) {
            # Use extract result if available
            $extractData = $response.data.extract
            if ($extractData -is [string]) {
                try {
                    $extractData = $extractData | ConvertFrom-Json
                } catch {
                    $notes = $extractData
                }
            }
            $availability = $extractData.status
            $price = $extractData.price
            $notes = $extractData.notes
        } elseif ($response.data.markdown) {
            # Parse from markdown content
            $markdown = $response.data.markdown
            if ($markdown -match "(?i)(available|for sale|register)") {
                $availability = "available"
                if ($markdown -match '\$[\d,]+') {
                    $price = ($matches[0])
                }
            } elseif ($markdown -match "(?i)(taken|registered|unavailable)") {
                $availability = "taken"
            }
            $notes = "Parsed from markdown content"
        }
        
        $result = @{
            domain = $domain
            url = $searchUrl
            timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
            status = "success"
            availability = $availability
            price = $price
            notes = $notes
            has_extract = ($null -ne $response.data.extract)
            has_markdown = ($null -ne $response.data.markdown)
        }
        
        Write-Host "  ✓ Success - Status: $availability" -ForegroundColor Green
        if ($price) {
            Write-Host "    Price: $price" -ForegroundColor Gray
        }
        
    } catch {
        $result = @{
            domain = $domain
            url = $searchUrl
            timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
            status = "error"
            error = $_.Exception.Message
        }
        
        Write-Host "  ✗ Error: $($_.Exception.Message)" -ForegroundColor Red
    }
    
    $results += $result
    
    # Small delay to avoid rate limiting
    Start-Sleep -Milliseconds 500
}

# Save results to JSON file
$results | ConvertTo-Json -Depth 10 | Out-File -FilePath $resultsFile -Encoding utf8

Write-Host ""
Write-Host "Completed! Results saved to $resultsFile" -ForegroundColor Green
Write-Host "Total checked: $($results.Count)" -ForegroundColor Cyan

# Display summary
$successful = ($results | Where-Object { $_.status -eq "success" }).Count
$failed = ($results | Where-Object { $_.status -eq "error" }).Count
Write-Host "Successful: $successful" -ForegroundColor Cyan
Write-Host "Failed: $failed" -ForegroundColor Cyan
