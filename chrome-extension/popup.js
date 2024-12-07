document.addEventListener('DOMContentLoaded', function() {
  // 加载保存的设置
  chrome.storage.sync.get(['serverUrl', 'readwiseToken', 'saveLocation'], function(items) {
    document.getElementById('serverUrl').value = items.serverUrl || '';
    document.getElementById('readwiseToken').value = items.readwiseToken || '';
    document.getElementById('saveLocation').value = items.saveLocation || 'new';
  });

  // 保存设置按钮点击事件
  document.getElementById('saveSettings').addEventListener('click', function() {
    const serverUrl = document.getElementById('serverUrl').value.trim();
    const readwiseToken = document.getElementById('readwiseToken').value.trim();
    const saveLocation = document.getElementById('saveLocation').value;
    
    // 保存设置到Chrome存储
    chrome.storage.sync.set({
      serverUrl: serverUrl,
      readwiseToken: readwiseToken,
      saveLocation: saveLocation
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
        
        // 发送到服务器
        fetch(`${items.serverUrl}/process_youtube`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            url: currentUrl,
            readwise_token: items.readwiseToken,
            location: items.saveLocation || 'new'
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
