document.addEventListener('DOMContentLoaded', function() {
  const defaultApiServerUrl = 'https://readwise-api.gauss.surf';
  const defaultWebServerUrl = 'https://readwise.gauss.surf';

  Promise.all([
    chrome.storage.sync.get([
      'apiServerUrl', 'webServerUrl', 'serverUrl', 'saveLocation', 'tags', 'hotwords'
    ]),
    chrome.storage.local.get(['accessClientId', 'accessClientSecret'])
  ]).then(function(results) {
    const items = results[0];
    const credentials = results[1];
    const legacyUrl = items.serverUrl || '';
    const apiUrl = legacyUrl === defaultWebServerUrl ? defaultApiServerUrl : legacyUrl;

    document.getElementById('apiServerUrl').value = items.apiServerUrl || apiUrl || defaultApiServerUrl;
    document.getElementById('webServerUrl').value = items.webServerUrl || legacyUrl || defaultWebServerUrl;
    document.getElementById('accessClientId').value = credentials.accessClientId || '';
    document.getElementById('accessClientSecret').value = credentials.accessClientSecret || '';
    document.getElementById('saveLocation').value = items.saveLocation || 'new';
    document.getElementById('tags').value = items.tags || '';
    document.getElementById('hotwords').value = items.hotwords || '';
    chrome.storage.sync.remove(['readwiseToken']);
  });

  // 保存设置按钮点击事件
  document.getElementById('saveSettings').addEventListener('click', function() {
    const apiServerUrl = document.getElementById('apiServerUrl').value.trim();
    const webServerUrl = document.getElementById('webServerUrl').value.trim();
    const accessClientId = document.getElementById('accessClientId').value.trim();
    const accessClientSecret = document.getElementById('accessClientSecret').value.trim();
    const saveLocation = document.getElementById('saveLocation').value;
    const tags = document.getElementById('tags').value.trim();
    const hotwords = document.getElementById('hotwords').value.trim();
    
    // 保存设置到Chrome存储
    const syncSave = chrome.storage.sync.set({
      apiServerUrl: apiServerUrl,
      webServerUrl: webServerUrl,
      saveLocation: saveLocation,
      tags: tags,
      hotwords: hotwords
    });
    const localSave = chrome.storage.local.set({
      accessClientId: accessClientId,
      accessClientSecret: accessClientSecret
    });
    Promise.all([syncSave, localSave]).then(function() {
      chrome.storage.sync.remove(['serverUrl', 'readwiseToken']);
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
    chrome.storage.sync.get(['apiServerUrl', 'saveLocation', 'tags', 'hotwords'], function(items) {
      if (!items.apiServerUrl) {
        chrome.storage.sync.set({ apiServerUrl: defaultApiServerUrl });
        items.apiServerUrl = defaultApiServerUrl;
      }

      if (!items.apiServerUrl) {
        status.textContent = '请先设置 API 地址！';
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

        // 处理hotwords - 使用当前输入框的值
        const currentHotwords = document.getElementById('hotwords').value.trim();
        let hotwordsList = [];
        if (currentHotwords) {
          // 支持中英文逗号
          hotwordsList = currentHotwords.split(/[,，]/).map(word => word.trim()).filter(word => word);
        }

        // 提取video_id
        const videoId = extractVideoId(currentUrl);
        if (!videoId) {
          status.textContent = '无法提取视频ID，请确保是YouTube视频页面';
          status.className = 'error';
          return;
        }
        
        chrome.runtime.sendMessage({
          type: 'SUBMIT_YOUTUBE_URL',
          payload: {
            url: currentUrl,
            videoId: videoId,
            pageTitle: tabs[0].title || '',
            tags: tagsList,
            hotwords: hotwordsList
          }
        }, function(data) {
          if (chrome.runtime.lastError) {
            status.textContent = '错误: ' + chrome.runtime.lastError.message;
            status.className = 'error';
            return;
          }

          if (data && data.success) {
            if (data.readwise_url) {
              showStatusLink(status, '已保存到 Readwise Reader', data.readwise_url, 'success');
              return;
            }

            status.textContent = '已发送到后台，等待 Readwise 链接...';
            status.className = 'success';
            if (data.process_id && data.poll_url) {
              pollTaskStatus(data.process_id, data.poll_url, data.result_url);
            }
          } else {
            status.textContent = '错误: ' + ((data && (data.error || data.message)) || '处理失败');
            status.className = 'error';
          }
        });
      });
    });
  });
});

async function pollTaskStatus(processId, pollUrl, fallbackUrl) {
  const status = document.getElementById('status');
  const maxAttempts = 120;

  for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    await sleep(5000);

    const response = await sendRuntimeMessage({
      type: 'CHECK_SUBTITLE_TASK_STATUS',
      payload: {
        processId,
        pollUrl
      }
    });

    if (!response || !response.success) {
      continue;
    }

    const normalizedStatus = String(response.status || '').toLowerCase();
    if (normalizedStatus === 'completed') {
      const linkUrl = response.readwise_url || response.reader_url || fallbackUrl;
      showStatusLink(
        status,
        response.readwise_url ? '已保存到 Readwise Reader' : '后台处理完成',
        linkUrl,
        'success'
      );
      return;
    }

    if (normalizedStatus === 'failed') {
      showStatusLink(status, response.error || '后台处理失败', fallbackUrl, 'error');
      return;
    }
  }

  showStatusLink(status, '后台仍在处理，可稍后查看任务详情', fallbackUrl, 'success');
}

function sendRuntimeMessage(message) {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage(message, (response) => {
      if (chrome.runtime.lastError) {
        resolve({ success: false, error: chrome.runtime.lastError.message });
        return;
      }
      resolve(response);
    });
  });
}

function showStatusLink(status, message, linkUrl, className) {
  status.textContent = '';
  status.className = className || '';

  const messageNode = document.createElement('span');
  messageNode.textContent = message;
  status.appendChild(messageNode);

  if (linkUrl) {
    status.appendChild(document.createTextNode(' '));
    const link = document.createElement('a');
    link.href = linkUrl;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    link.textContent = '查看';
    status.appendChild(link);
  }
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function extractVideoId(url) {
  try {
    const normalizedUrl = url.includes('://') ? url : `https://${url}`;
    const parsed = new URL(normalizedUrl);
    const host = parsed.hostname.toLowerCase();
    const pathParts = parsed.pathname.split('/').filter(Boolean);

    if (host === 'youtu.be') {
      return pathParts[0] || null;
    }

    if (!(host === 'youtube.com' || host.endsWith('.youtube.com'))) {
      return null;
    }

    const watchVideoId = parsed.searchParams.get('v');
    if (watchVideoId) {
      return watchVideoId;
    }

    if (pathParts.length >= 2 && ['shorts', 'live', 'embed', 'v'].includes(pathParts[0])) {
      return pathParts[1];
    }
  } catch (error) {
    return null;
  }

  return null;
}
