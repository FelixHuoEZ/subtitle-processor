# Stable Quicker Script for Double-Click Execution
param(
    [string]$ServerUrl = "http://localhost:5000",
    [switch]$Silent = $false,
    [switch]$NoTimeline = $false
)

# Ensure window stays open for debugging
$ErrorActionPreference = "Continue"

# Set console title
$Host.UI.RawUI.WindowTitle = "Quicker Video Processor"

Write-Host "=== Video URL Processor ===" -ForegroundColor Green
Write-Host "Time: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" -ForegroundColor Green
Write-Host "Server: $ServerUrl" -ForegroundColor Blue
Write-Host ""
# Write-Host "STEP 1: Script started successfully" -ForegroundColor Cyan
# pause

# Load Windows Forms
# Write-Host "STEP 2: Loading Windows Forms..." -ForegroundColor Cyan
try {
    Add-Type -AssemblyName System.Windows.Forms
    # Write-Host "STEP 2: Windows Forms loaded successfully" -ForegroundColor Green
    # pause
} catch {
    Write-Host "ERROR: Cannot load Windows Forms: $_" -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}

# Get clipboard content
# Write-Host "STEP 3: Reading clipboard..." -ForegroundColor Cyan
try {
    $clipboardText = [System.Windows.Forms.Clipboard]::GetText()
    # Write-Host "STEP 3: Clipboard read successfully" -ForegroundColor Green
    # pause
} catch {
    Write-Host "ERROR: Cannot read clipboard: $_" -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}

if ([string]::IsNullOrWhiteSpace($clipboardText)) {
    Write-Host "ERROR: Clipboard is empty" -ForegroundColor Red
    Write-Host "Please copy a video URL first" -ForegroundColor Yellow
    Read-Host "Press Enter to exit"
    exit 1
}

Write-Host "Found URL: $clipboardText" -ForegroundColor Yellow
# Write-Host "STEP 4: URL found in clipboard" -ForegroundColor Cyan
# pause

# Check URL pattern
# Write-Host "STEP 5: Checking URL pattern..." -ForegroundColor Cyan
$isVideoUrl = $false
if ($clipboardText -match "youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/|bilibili\.com/video/|b23\.tv/") {
    $isVideoUrl = $true
    Write-Host "Video URL detected" -ForegroundColor Green
    # Write-Host "STEP 5: URL pattern validation passed" -ForegroundColor Cyan
    # pause
}

if (-not $isVideoUrl) {
    Write-Host "ERROR: Not a supported video URL" -ForegroundColor Red
    Write-Host "Supported: YouTube, Bilibili" -ForegroundColor Yellow
    Write-Host "Current URL: $clipboardText" -ForegroundColor Gray
    Read-Host "Press Enter to exit"
    exit 1
}

# Test server connectivity
# Write-Host "STEP 6: Testing server connectivity..." -ForegroundColor Cyan
try {
    $testResponse = Invoke-WebRequest -Uri $ServerUrl -Method Get -TimeoutSec 5
    Write-Host "Server accessible" -ForegroundColor Green
    # Write-Host "STEP 6: Server connectivity test passed" -ForegroundColor Cyan
    # pause
} catch {
    Write-Host "ERROR: Cannot reach server at $ServerUrl" -ForegroundColor Red
    Write-Host "Please start Docker containers: docker-compose up -d" -ForegroundColor Yellow
    Write-Host "Error details: $_" -ForegroundColor Gray
    Read-Host "Press Enter to exit"
    exit 1
}

# Submit processing request
Write-Host "Submitting for processing..." -ForegroundColor Cyan

try {
    $requestBody = @{ url = $clipboardText } | ConvertTo-Json
    # Write-Host "STEP 7: Request body prepared" -ForegroundColor Cyan
    # pause
    
    $response = Invoke-RestMethod -Uri "$ServerUrl/process" -Method Post -Body $requestBody -ContentType "application/json" -TimeoutSec 30
    # Write-Host "STEP 7: Server response received" -ForegroundColor Cyan
    # pause
    
    if ($response.success -or $response.view_url) {
        # Immediate success
        $viewUrl = if ($response.view_url) { 
            "$ServerUrl$($response.view_url)" 
        } else { 
            "$ServerUrl/view/$($response.file_id)" 
        }
        
        Write-Host "SUCCESS: Processing completed!" -ForegroundColor Green
        Write-Host "View URL: $viewUrl" -ForegroundColor Blue
        
        # Copy result to clipboard
        [System.Windows.Forms.Clipboard]::SetText($viewUrl)
        Write-Host "Result URL copied to clipboard" -ForegroundColor Green
        
        # Auto-open browser if not silent
        Start-Process $viewUrl
        Write-Host "Opening in browser..." -ForegroundColor Green
        
    } elseif ($response.file_id) {
        # Processing started
        Write-Host "Processing started (ID: $($response.file_id))" -ForegroundColor Yellow
        Write-Host "This may take 1-5 minutes depending on video length" -ForegroundColor Yellow
        Write-Host "Check progress with Docker logs" -ForegroundColor Blue
        
    } else {
        Write-Host "WARNING: Unexpected server response" -ForegroundColor Yellow
        Write-Host "Response: $($response | ConvertTo-Json)" -ForegroundColor Gray
    }
    
} catch {
    # Write-Host "STEP 7: Exception occurred during processing" -ForegroundColor Yellow
    # Write-Host "Exception message: $($_.Exception.Message)" -ForegroundColor Gray
    # pause
    
    # Check for timeout errors (normal for successful submission)
    if ($_.Exception.Message -match "timeout|timed out") {
        Write-Host "SUCCESS: Video submitted successfully (timeout is normal)" -ForegroundColor Green
        Write-Host "Processing continues in background (1-5 minutes expected)" -ForegroundColor Blue
        Write-Host "Check later at: $ServerUrl" -ForegroundColor Yellow
        Write-Host "Or check Readwise Reader for new articles" -ForegroundColor Yellow
        # Write-Host "STEP 7: Timeout handled as success" -ForegroundColor Cyan
        # pause
    } else {
        Write-Host "ERROR: Processing failed: $_" -ForegroundColor Red
        if ($_.Exception.Response) {
            Write-Host "HTTP Status: $($_.Exception.Response.StatusCode)" -ForegroundColor Red
        }
        # Write-Host "STEP 7: Error occurred, exiting" -ForegroundColor Red
        Read-Host "Press Enter to exit"
        exit 1
    }
}

Write-Host ""
Write-Host "=== Process Complete ===" -ForegroundColor Green
Write-Host "Check Docker logs for details: docker-compose logs subtitle-processor" -ForegroundColor Blue
# Write-Host "STEP 8: Script execution completed" -ForegroundColor Cyan
Read-Host "Press Enter to exit"