// 快速视频处理 - Chrome扩展后台脚本
// 支持YouTube、Bilibili等视频网站一键处理

const SERVER_URL = 'http://localhost:5000';

// 创建右键菜单
chrome.runtime.onInstalled.addListener(() => {
    // 视频页面右键菜单
    chrome.contextMenus.create({
        id: 'process-video-page',
        title: '📹 处理当前视频',
        contexts: ['page'],
        documentUrlPatterns: [
            '*://www.youtube.com/watch*',
            '*://youtu.be/*',
            '*://youtube.com/shorts/*',
            '*://www.bilibili.com/video/*',
            '*://b23.tv/*'
        ]
    });
    
    // 链接右键菜单
    chrome.contextMenus.create({
        id: 'process-video-link', 
        title: '🎬 处理视频链接',
        contexts: ['link'],
        targetUrlPatterns: [
            '*://www.youtube.com/watch*',
            '*://youtu.be/*', 
            '*://youtube.com/shorts/*',
            '*://www.bilibili.com/video/*',
            '*://b23.tv/*'
        ]
    });
    
    // 选中文本菜单
    chrome.contextMenus.create({
        id: 'process-video-text',
        title: '🔗 处理选中链接',
        contexts: ['selection']
    });
});

// 处理右键菜单点击
chrome.contextMenus.onClicked.addListener((info, tab) => {
    let videoUrl = '';
    
    switch (info.menuItemId) {
        case 'process-video-page':
            videoUrl = tab.url;
            break;
        case 'process-video-link':
            videoUrl = info.linkUrl;
            break;
        case 'process-video-text':
            videoUrl = info.selectionText.trim();
            break;
    }
    
    if (videoUrl) {
        processVideoUrl(videoUrl, tab.id);
    }
});

// 快捷键支持
chrome.commands.onCommand.addListener((command, tab) => {
    if (command === 'process-current-video') {
        if (isVideoPage(tab.url)) {
            processVideoUrl(tab.url, tab.id);
        } else {
            showNotification('当前页面不是支持的视频网站', 'error');
        }
    }
});

// 检查是否为视频页面
function isVideoPage(url) {
    const videoPatterns = [
        /youtube\.com\/watch/,
        /youtu\.be\//,
        /youtube\.com\/shorts/,
        /bilibili\.com\/video/,
        /b23\.tv\//
    ];
    
    return videoPatterns.some(pattern => pattern.test(url));
}

// 验证视频URL
function isValidVideoUrl(url) {
    const patterns = [
        /youtube\.com\/watch\?v=[\w-]+/,
        /youtu\.be\/[\w-]+/,
        /youtube\.com\/shorts\/[\w-]+/,
        /bilibili\.com\/video\/[a-zA-Z0-9]+/,
        /b23\.tv\/[\w]+/
    ];
    
    return patterns.some(pattern => pattern.test(url));
}

// 处理视频URL
async function processVideoUrl(url, tabId) {
    if (!isValidVideoUrl(url)) {
        showNotification('不支持的视频链接格式', 'error');
        return;
    }
    
    // 显示处理中状态
    setBadgeText('...', tabId);
    showNotification('正在处理视频...', 'info');
    
    try {
        const response = await fetch(`${SERVER_URL}/process`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({ url: url })
        });
        
        const result = await response.json();
        
        if (result.success || result.view_url) {
            const viewUrl = result.view_url ? 
                `${SERVER_URL}${result.view_url}` : 
                `${SERVER_URL}/view/${result.file_id}`;
            
            // 在新标签页打开结果
            chrome.tabs.create({ url: viewUrl });
            
            setBadgeText('✓', tabId);
            showNotification('处理成功！已在新标签页打开', 'success');
            
            // 3秒后清除badge
            setTimeout(() => setBadgeText('', tabId), 3000);
        } else {
            throw new Error(result.error || '处理失败');
        }
    } catch (error) {
        setBadgeText('✗', tabId);
        showNotification(`处理失败: ${error.message}`, 'error');
        
        // 5秒后清除badge
        setTimeout(() => setBadgeText('', tabId), 5000);
    }
}

// 设置扩展图标badge
function setBadgeText(text, tabId) {
    chrome.action.setBadgeText({
        text: text,
        tabId: tabId
    });
    
    if (text === '✓') {
        chrome.action.setBadgeBackgroundColor({ color: '#4CAF50', tabId: tabId });
    } else if (text === '✗') {
        chrome.action.setBadgeBackgroundColor({ color: '#F44336', tabId: tabId });
    } else {
        chrome.action.setBadgeBackgroundColor({ color: '#2196F3', tabId: tabId });
    }
}

// 显示通知
function showNotification(message, type = 'info') {
    const iconUrl = type === 'success' ? 'images/icon-success.png' : 
                   type === 'error' ? 'images/icon-error.png' : 
                   'images/icon-info.png';
    
    chrome.notifications.create({
        type: 'basic',
        iconUrl: iconUrl,
        title: '视频处理工具',
        message: message
    });
}