const DEFAULT_API_SERVER_URL = 'https://readwise-api.gauss.surf';
const DEFAULT_WEB_SERVER_URL = 'https://readwise.gauss.surf';
const SYNC_STORAGE_KEYS = [
  'apiServerUrl',
  'webServerUrl',
  'serverUrl',
  'saveLocation',
  'tags',
  'hotwords'
];
const LOCAL_STORAGE_KEYS = ['accessClientId', 'accessClientSecret'];

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message || message.type === undefined) {
    return false;
  }

  if (message.type === 'SUBMIT_YOUTUBE_URL') {
    submitYouTubeUrl(message.payload || {}, sender)
      .then(sendResponse)
      .catch((error) => sendResponse({ success: false, error: formatError(error) }));
    return true;
  }

  if (message.type === 'CHECK_SUBTITLE_TASK_STATUS') {
    checkTaskStatus(message.payload || {})
      .then(sendResponse)
      .catch((error) => sendResponse({ success: false, error: formatError(error) }));
    return true;
  }

  return false;
});

async function submitYouTubeUrl(payload, sender) {
  const settings = await getSettings();
  const apiServerUrl = resolveApiServerUrl(settings);
  const webServerUrl = resolveWebServerUrl(settings);

  if (!apiServerUrl) {
    throw new Error('请先在 Subtitle Processor 扩展里设置 API 地址');
  }

  const currentUrl = payload.url || (sender.tab && sender.tab.url) || '';
  const videoId = payload.videoId || extractVideoId(currentUrl);

  if (!videoId) {
    throw new Error('无法提取视频ID，请确认当前页面是 YouTube 视频页面');
  }

  const requestBody = {
    url: currentUrl,
    platform: 'youtube',
    video_id: videoId,
    page_title: cleanPageTitle(payload.pageTitle || (sender.tab && sender.tab.title) || ''),
    location: settings.saveLocation || 'new',
    tags: parseList(payload.tags !== undefined ? payload.tags : settings.tags),
    hotwords: parseList(payload.hotwords !== undefined ? payload.hotwords : settings.hotwords),
    auto_start: true,
    request_source: 'chrome_extension'
  };

  const response = await fetch(`${apiServerUrl}/process`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...buildAccessHeaders(settings)
    },
    body: JSON.stringify(requestBody)
  });
  const data = await readJson(response);

  if (!response.ok || data.error || data.success === false) {
    throw new Error(data.error || data.message || `服务器返回 ${response.status}`);
  }

  const processId = data.process_id;
  return {
    ...data,
    success: true,
    process_id: processId,
    poll_url: processId ? toAbsoluteUrl(`/process/status/${processId}`, apiServerUrl) : undefined,
    result_url: processId ? toAbsoluteUrl(`/view/${processId}`, webServerUrl) : undefined
  };
}

async function checkTaskStatus(payload) {
  const settings = await getSettings();
  const apiServerUrl = resolveApiServerUrl(settings);

  if (!apiServerUrl) {
    throw new Error('请先在 Subtitle Processor 扩展里设置 API 地址');
  }

  const pollUrl = payload.pollUrl || (payload.processId ? `/process/status/${payload.processId}` : '');
  if (!pollUrl) {
    throw new Error('缺少任务状态地址');
  }

  const response = await fetch(toAbsoluteUrl(pollUrl, apiServerUrl), {
    headers: buildAccessHeaders(settings)
  });
  const data = await readJson(response);

  if (!response.ok || data.success === false) {
    throw new Error(data.error || `服务器返回 ${response.status}`);
  }

  return {
    ...data,
    success: true,
    done: ['completed', 'failed'].includes(String(data.status || '').toLowerCase())
  };
}

async function getSettings() {
  const [syncSettings, localSettings] = await Promise.all([
    chrome.storage.sync.get(SYNC_STORAGE_KEYS),
    chrome.storage.local.get(LOCAL_STORAGE_KEYS)
  ]);
  return { ...syncSettings, ...localSettings };
}

function resolveApiServerUrl(settings) {
  const legacyUrl = normalizeServerUrl(settings.serverUrl || '');
  if (legacyUrl === DEFAULT_WEB_SERVER_URL) {
    return DEFAULT_API_SERVER_URL;
  }
  return normalizeServerUrl(
    settings.apiServerUrl || legacyUrl || DEFAULT_API_SERVER_URL
  );
}

function resolveWebServerUrl(settings) {
  const legacyUrl = normalizeServerUrl(settings.serverUrl || '');
  return normalizeServerUrl(
    settings.webServerUrl || legacyUrl || DEFAULT_WEB_SERVER_URL
  );
}

function buildAccessHeaders(settings) {
  const clientId = String(settings.accessClientId || '').trim();
  const clientSecret = String(settings.accessClientSecret || '').trim();
  if (!clientId || !clientSecret) {
    return {};
  }
  return {
    'CF-Access-Client-Id': clientId,
    'CF-Access-Client-Secret': clientSecret
  };
}

function normalizeServerUrl(value) {
  const trimmed = String(value || '').trim();
  if (!trimmed) {
    return '';
  }

  const withProtocol = trimmed.includes('://') ? trimmed : `http://${trimmed}`;
  try {
    return new URL(withProtocol).origin;
  } catch (error) {
    return trimmed.replace(/\/+$/, '');
  }
}

function parseList(value) {
  if (Array.isArray(value)) {
    return value.map((item) => String(item).trim()).filter(Boolean);
  }

  return String(value || '')
    .split(/[,，]/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function cleanPageTitle(value) {
  return String(value || '').replace(/\s*-\s*YouTube\s*$/, '').trim();
}

async function readJson(response) {
  try {
    return await response.json();
  } catch (error) {
    return {};
  }
}

function toAbsoluteUrl(url, baseUrl) {
  return new URL(url, baseUrl).toString();
}

function formatError(error) {
  return error && error.message ? error.message : String(error);
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
