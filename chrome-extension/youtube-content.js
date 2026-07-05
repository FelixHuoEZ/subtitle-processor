(function () {
  const ACTION_ID = 'subtitle-processor-youtube-action';
  const BUTTON_ID = 'subtitle-processor-youtube-button';
  const STYLE_ID = 'subtitle-processor-youtube-style';
  const TOAST_ID = 'subtitle-processor-youtube-toast';

  let refreshTimer = null;
  let lastUrl = '';
  let activeProcessId = null;

  injectStyles();
  scheduleRefresh();

  document.addEventListener('yt-navigate-finish', scheduleRefresh);
  window.addEventListener('popstate', scheduleRefresh);

  const observer = new MutationObserver(scheduleRefresh);
  observer.observe(document.documentElement, { childList: true, subtree: true });

  setInterval(() => {
    if (location.href !== lastUrl) {
      scheduleRefresh();
    }
  }, 1000);

  function scheduleRefresh() {
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(refreshAction, 250);
  }

  function refreshAction() {
    lastUrl = location.href;
    const videoId = extractVideoId(location.href);
    const existing = document.getElementById(ACTION_ID);

    if (!videoId) {
      if (existing) {
        existing.remove();
      }
      return;
    }

    const target = findInsertionTarget();
    if (!target) {
      ensureFixedAction(videoId);
      return;
    }

    const action = existing || createAction(videoId);
    action.classList.remove('subtitle-processor-fixed-action');
    if (action.parentElement !== target) {
      target.appendChild(action);
    }
    resetButtonForPage(action, videoId);
  }

  function findInsertionTarget() {
    return (
      document.querySelector('ytd-watch-metadata #top-level-buttons-computed') ||
      document.querySelector('#top-level-buttons-computed') ||
      document.querySelector('ytd-watch-metadata #owner') ||
      document.querySelector('ytd-reel-video-renderer[is-active] #actions')
    );
  }

  function ensureFixedAction(videoId) {
    if (!document.body) {
      return;
    }

    const action = document.getElementById(ACTION_ID) || createAction(videoId);
    action.classList.add('subtitle-processor-fixed-action');
    if (action.parentElement !== document.body) {
      document.body.appendChild(action);
    }
    resetButtonForPage(action, videoId);
  }

  function createAction(videoId) {
    const wrapper = document.createElement('div');
    wrapper.id = ACTION_ID;
    wrapper.dataset.videoId = videoId;

    const button = document.createElement('button');
    button.id = BUTTON_ID;
    button.type = 'button';
    button.textContent = '发送字幕处理';
    button.title = '发送当前 YouTube 页面到字幕处理后台';
    button.addEventListener('click', handleSubmitClick);

    wrapper.appendChild(button);
    return wrapper;
  }

  function resetButtonForPage(action, videoId) {
    if (action.dataset.videoId === videoId) {
      return;
    }

    activeProcessId = null;
    action.dataset.videoId = videoId;
    const button = action.querySelector(`#${BUTTON_ID}`);
    if (button) {
      button.disabled = false;
      button.textContent = '发送字幕处理';
    }
  }

  async function handleSubmitClick(event) {
    const button = event.currentTarget;
    const videoId = extractVideoId(location.href);

    if (!videoId) {
      showToast('无法识别当前 YouTube 视频', 'error');
      return;
    }

    button.disabled = true;
    button.textContent = '发送中...';
    showToast('正在发送到后台...', 'info');

    try {
      const response = await sendMessage({
        type: 'SUBMIT_YOUTUBE_URL',
        payload: {
          url: location.href,
          videoId,
          pageTitle: getPageTitle()
        }
      });

      if (!response || !response.success) {
        throw new Error((response && response.error) || '提交失败');
      }

      activeProcessId = response.process_id || null;
      button.textContent = '已发送';
      showToast('已发送到后台', 'success', response.result_url);

      if (String(response.status || '').toLowerCase() === 'completed' || response.readwise_url_only) {
        restoreSubmittedButton();
        return;
      }

      if (response.process_id && response.poll_url) {
        pollTaskStatus(response.process_id, response.poll_url, response.result_url);
      }
    } catch (error) {
      button.disabled = false;
      button.textContent = '发送字幕处理';
      showToast(formatError(error), 'error');
    }
  }

  async function pollTaskStatus(processId, pollUrl, resultUrl) {
    let attempts = 0;
    const maxAttempts = 120;

    while (activeProcessId === processId && attempts < maxAttempts) {
      attempts += 1;
      await sleep(5000);

      try {
        const status = await sendMessage({
          type: 'CHECK_SUBTITLE_TASK_STATUS',
          payload: {
            processId,
            pollUrl
          }
        });

        if (!status || !status.success) {
          continue;
        }

        const normalizedStatus = String(status.status || '').toLowerCase();
        if (normalizedStatus === 'completed') {
          showToast('后台处理完成', 'success', resultUrl);
          restoreSubmittedButton();
          return;
        }

        if (normalizedStatus === 'failed') {
          showToast(status.error || '后台处理失败', 'error', resultUrl);
          restoreSubmittedButton();
          return;
        }
      } catch (error) {
        // Polling is best-effort; the submitted task remains visible in the backend.
      }
    }
  }

  function restoreSubmittedButton() {
    const button = document.getElementById(BUTTON_ID);
    if (button) {
      button.disabled = false;
      button.textContent = '再次发送';
    }
  }

  function sendMessage(message) {
    return new Promise((resolve, reject) => {
      chrome.runtime.sendMessage(message, (response) => {
        const lastError = chrome.runtime.lastError;
        if (lastError) {
          reject(new Error(lastError.message));
          return;
        }
        resolve(response);
      });
    });
  }

  function showToast(message, type, linkUrl) {
    let toast = document.getElementById(TOAST_ID);
    if (!toast) {
      toast = document.createElement('div');
      toast.id = TOAST_ID;
      document.body.appendChild(toast);
    }

    toast.className = `subtitle-processor-toast subtitle-processor-toast-${type || 'info'}`;
    toast.textContent = '';

    const messageNode = document.createElement('span');
    messageNode.textContent = message;
    toast.appendChild(messageNode);

    if (linkUrl) {
      const link = document.createElement('a');
      link.href = linkUrl;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.textContent = '查看';
      toast.appendChild(link);
    }
  }

  function injectStyles() {
    if (document.getElementById(STYLE_ID)) {
      return;
    }

    const style = document.createElement('style');
    style.id = STYLE_ID;
    style.textContent = `
      #${ACTION_ID} {
        display: inline-flex;
        align-items: center;
        margin-left: 8px;
        vertical-align: middle;
        z-index: 9999;
      }

      #${ACTION_ID}.subtitle-processor-fixed-action {
        position: fixed;
        right: 20px;
        bottom: 24px;
        margin-left: 0;
      }

      #${BUTTON_ID} {
        min-width: 112px;
        height: 36px;
        padding: 0 14px;
        border: 1px solid var(--yt-spec-10-percent-layer, rgba(0, 0, 0, 0.12));
        border-radius: 18px;
        background: var(--yt-spec-badge-chip-background, rgba(0, 0, 0, 0.05));
        color: var(--yt-spec-text-primary, #0f0f0f);
        cursor: pointer;
        font-size: 14px;
        font-weight: 500;
        line-height: 36px;
        white-space: nowrap;
      }

      #${BUTTON_ID}:hover {
        background: var(--yt-spec-button-chip-background-hover, rgba(0, 0, 0, 0.1));
      }

      #${BUTTON_ID}:disabled {
        cursor: default;
        opacity: 0.7;
      }

      #${TOAST_ID} {
        position: fixed;
        right: 20px;
        bottom: 76px;
        display: flex;
        align-items: center;
        gap: 12px;
        max-width: min(360px, calc(100vw - 40px));
        padding: 10px 14px;
        border-radius: 8px;
        box-shadow: 0 8px 24px rgba(0, 0, 0, 0.24);
        color: #fff;
        font-size: 14px;
        line-height: 1.4;
        z-index: 999999;
      }

      #${TOAST_ID} a {
        color: #fff;
        font-weight: 600;
        text-decoration: underline;
      }

      .subtitle-processor-toast-info {
        background: #2563eb;
      }

      .subtitle-processor-toast-success {
        background: #15803d;
      }

      .subtitle-processor-toast-error {
        background: #b91c1c;
      }
    `;
    document.documentElement.appendChild(style);
  }

  function getPageTitle() {
    const titleNode =
      document.querySelector('ytd-watch-metadata h1 yt-formatted-string') ||
      document.querySelector('h1.ytd-watch-metadata') ||
      document.querySelector('h1');
    const title = titleNode && titleNode.textContent ? titleNode.textContent.trim() : '';
    return title || document.title.replace(/\s*-\s*YouTube\s*$/, '').trim();
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

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  function formatError(error) {
    return error && error.message ? error.message : String(error);
  }
})();
