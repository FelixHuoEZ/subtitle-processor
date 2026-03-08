# 添加Windows Forms支持
Add-Type -AssemblyName System.Windows.Forms

# 设置输出编码为UTF8，以正确显示中文
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

Write-Host "=== SRT字幕转网页查看器 开始执行 ===" -ForegroundColor Green
Write-Host "时间: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" -ForegroundColor Green
Write-Host ""

# 获取剪贴板内容
function Get-ClipboardText {
    try {
        $clipboardText = [System.Windows.Forms.Clipboard]::GetText()
        return $clipboardText
    }
    catch {
        Write-Host "获取剪贴板文本失败: $_" -ForegroundColor Red
        return $null
    }
}

# 获取剪贴板文件
function Get-DroppedFiles {
    try {
        $files = [System.Windows.Forms.Clipboard]::GetFileDropList()
        if ($files.Count -gt 0) {
            return $files
        }
        return $null
    }
    catch {
        Write-Host "获取剪贴板文件失败: $_" -ForegroundColor Red
        return $null
    }
}

# 处理文件路径列表
function Process-FilePaths {
    param (
        [string[]]$paths
    )
    
    $successCount = 0
    $failCount = 0
    
    foreach ($path in $paths) {
        if (-not $path) { continue }
        
        $path = $path.Trim()
        Write-Host "`n处理文件: $path"
        
        if (-not (Test-Path $path)) {
            Write-Host "文件不存在" -ForegroundColor Red
            $failCount++
            continue
        }
        
        try {
            $form = @{
                file = Get-Item -Path $path
            }
            
            # 添加时间轴显示设置
            $headers = @{}
            if ($env:QUICKER_PARAM_SHOW_TIMELINE -eq "false") {
                $headers["X-Show-Timeline"] = "false"
            }
            
            $response = Invoke-RestMethod -Uri "http://localhost:5000/upload" -Method Post -Form $form -Headers $headers
            
            if ($response.success) {
                Write-Host "上传成功" -ForegroundColor Green
                $viewUrl = "http://localhost:5000$($response.url)"
                Write-Host "查看地址: $viewUrl"
                Start-Process $viewUrl
                $successCount++
            }
            else {
                Write-Host "上传失败: $($response.error)" -ForegroundColor Red
                $failCount++
            }
        }
        catch {
            Write-Host "处理失败: $_" -ForegroundColor Red
            $failCount++
        }
    }
    
    Write-Host "`n处理完成: 成功 $successCount 个, 失败 $failCount 个"
}

# 处理SRT内容
function Process-SrtContent {
    param (
        [string]$content
    )
    
    try {
        $tempFile = [System.IO.Path]::GetTempFileName()
        $content | Out-File -FilePath $tempFile -Encoding UTF8
        
        Process-FilePaths -paths @($tempFile)
        
        Remove-Item -Path $tempFile -ErrorAction SilentlyContinue
    }
    catch {
        Write-Host "处理SRT内容失败: $_" -ForegroundColor Red
    }
}

# 处理YouTube URL
function Process-YouTubeUrl {
    param (
        [string]$url
    )
    
    try {
        Write-Host "正在处理YouTube URL: $url" -ForegroundColor Yellow
        $response = Invoke-RestMethod -Uri "http://localhost:5000/process_youtube" -Method Post -Body (@{
            url = $url
        } | ConvertTo-Json) -ContentType "application/json"
        
        if ($response.view_url) {
            Write-Host "处理成功" -ForegroundColor Green
            $viewUrl = "http://localhost:5000$($response.view_url)"
            Write-Host "查看地址: $viewUrl"
            Start-Process $viewUrl
            return $true
        }
        else {
            $errorMsg = if ($response.error) { $response.error } else { "未知错误" }
            Write-Host "处理YouTube URL失败: $errorMsg" -ForegroundColor Red
            return $false
        }
    }
    catch {
        Write-Host "处理YouTube URL时出错: $_" -ForegroundColor Red
        return $false
    }
}

# 主程序入口
Write-Host "`n=== SRT字幕转网页查看器 开始执行 ===" -ForegroundColor Green
Write-Host "时间: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')`n"

# 获取剪贴板信息
$clipboardText = Get-ClipboardText
$droppedFiles = Get-DroppedFiles

Write-Host "剪贴板内容类型: $(if ($clipboardText) { 'Text' } else { 'None' })"
Write-Host "剪贴板文件数量: $(if ($droppedFiles) { $droppedFiles.Count } else { '0' })"

# 显示QUICKER_SELECTED_FILES内容
Write-Host "QUICKER_SELECTED_FILES:"
if ($env:QUICKER_SELECTED_FILES) {
    Write-Host $env:QUICKER_SELECTED_FILES
}

# 处理逻辑
$processed = $false

# 1. 检查是否是YouTube URL
if ($clipboardText -match "(youtube\.com/(watch\?v=|shorts/|live/|embed/|v/)|youtu\.be/)") {
    Write-Host "`n检测到YouTube URL，开始处理..."
    Process-YouTubeUrl -url $clipboardText
    $processed = $true
}

# 2. 检查QUICKER_SELECTED_FILES
if (-not $processed -and $env:QUICKER_SELECTED_FILES) {
    $files = $env:QUICKER_SELECTED_FILES -split '\|'
    if ($files) {
        Process-FilePaths -paths $files
        $processed = $true
    }
}

# 3. 检查剪贴板文件
if (-not $processed -and $droppedFiles) {
    Process-FilePaths -paths $droppedFiles
    $processed = $true
}

# 4. 检查剪贴板文本是否包含文件路径
if (-not $processed -and $clipboardText -match "^([a-zA-Z]:\\|\\\\).*\.(srt|ass|ssa)$") {
    $paths = $clipboardText -split "`n" | ForEach-Object { $_.Trim() }
    Process-FilePaths -paths $paths
    $processed = $true
}

# 5. 检查剪贴板文本是否是SRT内容
if (-not $processed -and $clipboardText -match "^\d+\r?\n\d{2}:\d{2}:\d{2},\d{3}") {
    Process-SrtContent -content $clipboardText
    $processed = $true
}

if (-not $processed) {
    Write-Host "错误: 未找到有效的srt文件、YouTube URL或内容" -ForegroundColor Red
}

Write-Host "`n=== 处理完成 ===" -ForegroundColor Green
Write-Host "窗口保持打开，您可以检查以上信息。" -ForegroundColor Yellow
Read-Host "按回车键关闭窗口..."
