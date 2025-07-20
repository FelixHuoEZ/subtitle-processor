# ===== SRT字幕转网页查看器 增强版 =====
# 基于原版脚本优化，增加Bilibili支持和配置选项

param(
    [string]$ServerUrl = "http://localhost:5000",
    [switch]$Silent = $false,
    [switch]$NoTimeline = $false
)

# 添加Windows Forms支持
Add-Type -AssemblyName System.Windows.Forms

# 设置输出编码为UTF8，以正确显示中文
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

if (-not $Silent) {
    Write-Host "=== SRT字幕转网页查看器 增强版 ===" -ForegroundColor Green
    Write-Host "时间: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" -ForegroundColor Green
    Write-Host "服务器: $ServerUrl" -ForegroundColor Blue
    Write-Host ""
}

# 获取剪贴板内容
function Get-ClipboardText {
    try {
        $clipboardText = [System.Windows.Forms.Clipboard]::GetText()
        return $clipboardText
    }
    catch {
        if (-not $Silent) {
            Write-Host "获取剪贴板文本失败: $_" -ForegroundColor Red
        }
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
        if (-not $Silent) {
            Write-Host "获取剪贴板文件失败: $_" -ForegroundColor Red
        }
        return $null
    }
}

# 处理文件路径列表
function Process-FilePaths {
    param(
        [string[]]$paths
    )
    
    $successCount = 0
    $failCount = 0
    
    foreach ($path in $paths) {
        if (-not $path) { continue }
        
        $path = $path.Trim()
        if (-not $Silent) {
            Write-Host "`n处理文件: $path"
        }
        
        if (-not (Test-Path $path)) {
            if (-not $Silent) {
                Write-Host "文件不存在" -ForegroundColor Red
            }
            $failCount++
            continue
        }
        
        try {
            $form = @{
                file = Get-Item -Path $path
            }
            
            # 添加时间轴显示设置
            $headers = @{}
            if ($NoTimeline -or $env:QUICKER_PARAM_SHOW_TIMELINE -eq "false") {
                $headers["X-Show-Timeline"] = "false"
            }
            
            $response = Invoke-RestMethod -Uri "$ServerUrl/upload" -Method Post -Form $form -Headers $headers
            
            if ($response.success) {
                if (-not $Silent) {
                    Write-Host "上传成功" -ForegroundColor Green
                }
                $viewUrl = "$ServerUrl$($response.url)"
                if (-not $Silent) {
                    Write-Host "查看地址: $viewUrl"
                }
                Start-Process $viewUrl
                $successCount++
            }
            else {
                if (-not $Silent) {
                    Write-Host "上传失败: $($response.error)" -ForegroundColor Red
                }
                $failCount++
            }
        }
        catch {
            if (-not $Silent) {
                Write-Host "处理失败: $_" -ForegroundColor Red
            }
            $failCount++
        }
    }
    
    if (-not $Silent) {
        Write-Host "`n处理完成: 成功 $successCount 个, 失败 $failCount 个"
    }
}

# 处理SRT内容
function Process-SrtContent {
    param(
        [string]$content
    )
    
    try {
        $tempFile = [System.IO.Path]::GetTempFileName()
        $content | Out-File -FilePath $tempFile -Encoding UTF8
        
        Process-FilePaths -paths @($tempFile)
        
        Remove-Item -Path $tempFile -ErrorAction SilentlyContinue
    }
    catch {
        if (-not $Silent) {
            Write-Host "处理SRT内容失败: $_" -ForegroundColor Red
        }
    }
}

# 增强的视频URL处理函数 - 支持YouTube和Bilibili
function Process-VideoUrl {
    param(
        [string]$url
    )
    
    try {
        # 检测平台类型
        $platform = "unknown"
        if ($url -match "youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/") {
            $platform = "youtube"
        }
        elseif ($url -match "bilibili\.com/video/|b23\.tv/") {
            $platform = "bilibili"
        }
        
        if (-not $Silent) {
            Write-Host "正在处理 $platform URL: $url" -ForegroundColor Yellow
        }
        
        # 使用统一的 /process 端点
        $response = Invoke-RestMethod -Uri "$ServerUrl/process" -Method Post -Body (@{
            url = $url
        } | ConvertTo-Json) -ContentType "application/json"
        
        if ($response.success -or $response.view_url) {
            if (-not $Silent) {
                Write-Host "处理成功" -ForegroundColor Green
            }
            
            $viewUrl = if ($response.view_url) { 
                "$ServerUrl$($response.view_url)" 
            } else { 
                "$ServerUrl/view/$($response.file_id)" 
            }
            
            if (-not $Silent) {
                Write-Host "查看地址: $viewUrl"
            }
            Start-Process $viewUrl
            
            # 复制结果链接到剪贴板
            [System.Windows.Forms.Clipboard]::SetText($viewUrl)
            
            return $true
        }
        else {
            $errorMsg = if ($response.error) { $response.error } else { "未知错误" }
            if (-not $Silent) {
                Write-Host "处理视频URL失败: $errorMsg" -ForegroundColor Red
            }
            return $false
        }
    }
    catch {
        if (-not $Silent) {
            Write-Host "处理视频URL时出错: $_" -ForegroundColor Red
        }
        return $false
    }
}

# 主程序入口
if (-not $Silent) {
    Write-Host "`n=== 开始检测输入内容 ===" -ForegroundColor Green
}

# 获取剪贴板信息
$clipboardText = Get-ClipboardText
$droppedFiles = Get-DroppedFiles

if (-not $Silent) {
    Write-Host "剪贴板内容类型: $(if ($clipboardText) { 'Text' } else { 'None' })"
    Write-Host "剪贴板文件数量: $(if ($droppedFiles) { $droppedFiles.Count } else { '0' })"
    
    # 显示QUICKER_SELECTED_FILES内容
    Write-Host "QUICKER_SELECTED_FILES:"
    if ($env:QUICKER_SELECTED_FILES) {
        Write-Host $env:QUICKER_SELECTED_FILES
    }
}

# 处理逻辑
$processed = $false

# 1. 检查是否是视频URL (增强支持)
if ($clipboardText -match "youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/|bilibili\.com/video/|b23\.tv/") {
    if (-not $Silent) {
        Write-Host "`n检测到视频URL，开始处理..."
    }
    Process-VideoUrl -url $clipboardText
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
    if (-not $Silent) {
        Write-Host "错误: 未找到有效的srt文件、视频URL或内容" -ForegroundColor Red
        Write-Host ""
        Write-Host "支持的视频平台:" -ForegroundColor Yellow
        Write-Host "• YouTube: youtube.com/watch, youtu.be, youtube.com/shorts" -ForegroundColor Gray
        Write-Host "• Bilibili: bilibili.com/video, b23.tv" -ForegroundColor Gray
    }
}

if (-not $Silent) {
    Write-Host "`n=== 处理完成 ===" -ForegroundColor Green
    Write-Host "窗口保持打开，您可以检查以上信息。" -ForegroundColor Yellow
    Read-Host "按回车键关闭窗口..."
}