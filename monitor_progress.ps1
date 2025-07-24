# 实时转录进度监控脚本

param(
    [string]$VideoUrl = "https://www.youtube.com/watch?v=7qEp4etf3GQ",
    [int]$MonitorInterval = 3  # 监控间隔（秒）
)

Write-Host "================================================" -ForegroundColor Green
Write-Host "     长视频转录进度实时监控" -ForegroundColor Green
Write-Host "================================================" -ForegroundColor Green

Write-Host "`n测试视频: $VideoUrl" -ForegroundColor Cyan
Write-Host "监控间隔: $MonitorInterval 秒" -ForegroundColor Gray

# 启动处理请求（异步）
$job = Start-Job -ScriptBlock {
    param($Url)
    
    $headers = @{
        "Content-Type" = "application/json"
    }
    
    $body = @{
        url = $Url
        platform = "youtube"
        location = "new"
        tags = @("progress-test")
    } | ConvertTo-Json
    
    try {
        $response = Invoke-RestMethod -Uri "http://localhost:5000/process" -Method Post -Headers $headers -Body $body -TimeoutSec 1200
        return $response
    } catch {
        return @{
            error = $_.Exception.Message
            success = $false
        }
    }
} -ArgumentList $VideoUrl

Write-Host "`n✓ 处理任务已启动，开始监控进度..." -ForegroundColor Green
Write-Host "按 Ctrl+C 停止监控`n" -ForegroundColor Yellow

$startTime = Get-Date
$lastProgress = -1
$consecutiveErrors = 0

try {
    while ($job.State -eq "Running") {
        try {
            # 获取转录进度
            $progress = Invoke-RestMethod -Uri "http://localhost:10095/progress" -Method Get -TimeoutSec 5
            
            if ($progress) {
                $currentTime = Get-Date
                $elapsed = ($currentTime - $startTime).TotalSeconds
                
                # 清屏并显示当前状态
                Clear-Host
                Write-Host "================================================" -ForegroundColor Green
                Write-Host "     长视频转录进度实时监控" -ForegroundColor Green
                Write-Host "================================================" -ForegroundColor Green
                
                Write-Host "`n📹 视频: $VideoUrl" -ForegroundColor Cyan
                Write-Host "⏱️  运行时间: $([math]::Round($elapsed, 1)) 秒" -ForegroundColor Gray
                Write-Host "🔄 更新时间: $(Get-Date -Format 'HH:mm:ss')" -ForegroundColor Gray
                
                Write-Host "`n" + "="*50 -ForegroundColor Blue
                Write-Host "          转录进度状态" -ForegroundColor Blue
                Write-Host "="*50 -ForegroundColor Blue
                
                # 状态显示
                $statusColor = switch ($progress.status) {
                    "idle" { "Gray" }
                    "starting" { "Yellow" }
                    "processing" { "Cyan" }
                    "completed" { "Green" }
                    "error" { "Red" }
                    default { "White" }
                }
                
                Write-Host "状态: " -NoNewline
                Write-Host $progress.status.ToUpper() -ForegroundColor $statusColor
                
                if ($progress.message) {
                    Write-Host "信息: $($progress.message)" -ForegroundColor Yellow
                }
                
                # 进度条显示
                if ($progress.total_chunks -gt 0) {
                    $progressPercent = [math]::Round($progress.progress, 1)
                    $barLength = 40
                    $filledLength = [math]::Floor(($progressPercent / 100) * $barLength)
                    $emptyLength = $barLength - $filledLength
                    
                    $progressBar = "█" * $filledLength + "░" * $emptyLength
                    
                    Write-Host "`n进度: [$progressBar] $progressPercent%" -ForegroundColor Green
                    Write-Host "音频块: $($progress.current_chunk)/$($progress.total_chunks)" -ForegroundColor Cyan
                    
                    # 预估时间
                    if ($progress.estimated_time -and $progress.estimated_time -gt 0) {
                        $estimatedMin = [math]::Round($progress.estimated_time / 60, 1)
                        Write-Host "预估剩余: $estimatedMin 分钟" -ForegroundColor Yellow
                    }
                }
                
                # 性能信息
                if ($progress.total_chunks -gt 0 -and $progress.current_chunk -gt 0) {
                    $avgTimePerChunk = $elapsed / $progress.current_chunk
                    Write-Host "`n平均每块耗时: $([math]::Round($avgTimePerChunk, 1)) 秒" -ForegroundColor Gray
                }
                
                $consecutiveErrors = 0
            }
            
        } catch {
            $consecutiveErrors++
            if ($consecutiveErrors -le 3) {
                Write-Host "." -NoNewline -ForegroundColor Red
            } else {
                Write-Host "`n⚠️ 无法获取进度信息 (连续错误: $consecutiveErrors)" -ForegroundColor Red
            }
        }
        
        Start-Sleep -Seconds $MonitorInterval
    }
    
    # 获取最终结果
    Write-Host "`n`n" + "="*50 -ForegroundColor Green
    Write-Host "          处理完成" -ForegroundColor Green
    Write-Host "="*50 -ForegroundColor Green
    
    $result = Receive-Job -Job $job
    
    if ($result.error) {
        Write-Host "❌ 处理失败: $($result.error)" -ForegroundColor Red
    } else {
        $totalTime = ((Get-Date) - $startTime).TotalSeconds
        Write-Host "✅ 处理成功完成!" -ForegroundColor Green
        Write-Host "⏱️ 总耗时: $([math]::Round($totalTime, 1)) 秒" -ForegroundColor Green
        
        if ($result.subtitle_content) {
            $contentLength = $result.subtitle_content.Length
            Write-Host "📝 字幕长度: $contentLength 字符" -ForegroundColor Cyan
            
            # 显示字幕预览
            $lines = $result.subtitle_content -split "`n" | Where-Object { $_.Trim() -ne "" } | Select-Object -First 5
            if ($lines.Count -gt 0) {
                Write-Host "`n字幕预览:" -ForegroundColor Yellow
                foreach ($line in $lines) {
                    Write-Host "  $line" -ForegroundColor Gray
                }
                if ($contentLength -gt 500) {
                    Write-Host "  ..." -ForegroundColor Gray
                }
            }
        }
        
        if ($result.source) {
            Write-Host "📊 数据来源: $($result.source)" -ForegroundColor Cyan
        }
    }
    
} catch {
    Write-Host "`n❌ 监控中断: $($_.Exception.Message)" -ForegroundColor Red
} finally {
    # 清理任务
    if ($job.State -eq "Running") {
        Stop-Job -Job $job
    }
    Remove-Job -Job $job -Force
    
    Write-Host "`n监控结束" -ForegroundColor Green
}