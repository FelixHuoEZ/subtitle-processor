document.addEventListener('DOMContentLoaded', function() {
  // 加载保存的设置
  chrome.storage.sync.get(['serverUrl', 'readwiseToken', 'saveLocation', 'tags'], function(items) {
    document.getElementById('serverUrl').value = items.serverUrl || '';
    document.getElementById('readwiseToken').value = items.readwiseToken || '';
    document.getElementById('saveLocation').value = items.saveLocation || 'new';
    document.getElementById('tags').value = items.tags || '';
  });

  // 保存设置按钮点击事件
  document.getElementById('saveSettings').addEventListener('click', function() {
    const serverUrl = document.getElementById('serverUrl').value.trim();
    const readwiseToken = document.getElementById('readwiseToken').value.trim();
    const saveLocation = document.getElementById('saveLocation').value;
    const tags = document.getElementById('tags').value.trim();
    
    // 保存设置到Chrome存储
    chrome.storage.sync.set({
      serverUrl: serverUrl,
      readwiseToken: readwiseToken,
      saveLocation: saveLocation,
      tags: tags
    }, function() {
      const status = document.getElementById('status');
      status.textContent = '设置已保存！';
      status.className = 'success';
      setTimeout(function() {
        status.textContent = '';
      }, 2000);
    });
  });

  // 提取URL按钮点击事件
  document.getElementById('extractUrl').addEventListener('click', function() {
    const status = document.getElementById('status');
    
    // 获取设置
    chrome.storage.sync.get(['serverUrl', 'readwiseToken', 'saveLocation'], function(items) {
      if (!items.serverUrl || !items.readwiseToken) {
        status.textContent = '请先设置服务器地址和Readwise Token！';
        status.className = 'error';
        return;
      }

      // 获取当前标签页的URL
      chrome.tabs.query({active: true, currentWindow: true}, function(tabs) {
        const currentUrl = tabs[0].url;
        
        // 处理tags - 使用当前输入框的值
        const currentTags = document.getElementById('tags').value.trim();
        let tagsList = [];
        if (currentTags) {
          // 支持中英文逗号
          tagsList = currentTags.split(/[,，]/).map(tag => tag.trim()).filter(tag => tag);
        }

        // 提取video_id
        const videoId = extractVideoId(currentUrl);
        if (!videoId) {
          status.textContent = '无法提取视频ID，请确保是YouTube视频页面';
          status.className = 'error';
          return;
        }
        
        // 发送到服务器
        fetch(`${items.serverUrl}/process`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            url: currentUrl,
            platform: 'youtube',
            video_id: videoId,
            readwise_token: items.readwiseToken,
            location: items.saveLocation || 'new',
            tags: tagsList
          })
        })
        .then(response => response.json())
        .then(data => {
          if (data.success) {
            status.textContent = '成功发送到服务器！';
            status.className = 'success';
          } else {
            throw new Error(data.message || '处理失败');
          }
        })
        .catch(error => {
          status.textContent = '错误: ' + error.message;
          status.className = 'error';
        });
      });
    });
  });
});

function extractVideoId(url) {
  // 匹配常规YouTube URL
  let match = url.match(/[?&]v=([^&]+)/);
  if (match) {
    return match[1];
  }
  
  // 匹配短链接
  match = url.match(/youtu\.be\/([^?]+)/);
  if (match) {
    return match[1];
  }
  
  return null;
}
