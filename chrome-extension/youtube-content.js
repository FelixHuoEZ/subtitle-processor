(function () {
  const ACTION_ID = 'subtitle-processor-youtube-action';
  const BUTTON_ID = 'subtitle-processor-youtube-button';
  const REPROCESS_BUTTON_ID = 'subtitle-processor-youtube-reprocess';
  const STYLE_ID = 'subtitle-processor-youtube-style';
  const TOAST_ID = 'subtitle-processor-youtube-toast';
  const READER_STATUS_RETRY_INTERVAL_MS = 5000;
  const READER_STATUS_MAX_WARMING_RETRIES = 6;

  let refreshTimer = null;
  let lastUrl = '';
  let activeProcessId = null;
  let readerStatusRequestSequence = 0;

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
    ensureReaderStatus(action, videoId);
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
    ensureReaderStatus(action, videoId);
  }

  function createAction(videoId) {
    const wrapper = document.createElement('div');
    wrapper.id = ACTION_ID;
    wrapper.dataset.videoId = videoId;
    wrapper.dataset.readerStatusRetryCount = '0';

    const button = document.createElement('button');
    button.id = BUTTON_ID;
    button.type = 'button';
    button.textContent = '检查中…';
    button.title = '正在检查 Reader';
    button.disabled = true;
    button.dataset.mode = 'checking';
    button.addEventListener('click', handlePrimaryClick);

    const reprocessButton = document.createElement('button');
    reprocessButton.id = REPROCESS_BUTTON_ID;
    reprocessButton.type = 'button';
    reprocessButton.textContent = '重新处理';
    reprocessButton.title = '重新发送当前视频到字幕处理后台';
    reprocessButton.hidden = true;
    reprocessButton.addEventListener('click', handleSubmitClick);

    wrapper.appendChild(button);
    wrapper.appendChild(reprocessButton);
    return wrapper;
  }

  function resetButtonForPage(action, videoId) {
    if (action.dataset.videoId === videoId) {
      return;
    }

    activeProcessId = null;
    readerStatusRequestSequence += 1;
    action.dataset.videoId = videoId;
    action.dataset.readerStatusVideoId = '';
    action.dataset.readerStatusRetryCount = '0';
    setCheckingState(action);
  }

  function handlePrimaryClick(event) {
    const button = event.currentTarget;
    const action = button.closest(`#${ACTION_ID}`);
    const mode = button.dataset.mode;

    if (mode === 'saved') {
      const readerUrl = action && action.dataset.readerUrl;
      if (readerUrl) {
        window.open(readerUrl, '_blank', 'noopener,noreferrer');
      }
      return;
    }

    if (mode === 'unknown') {
      const videoId = extractVideoId(location.href);
      if (action && videoId) {
        action.dataset.readerStatusRetryCount = '0';
        ensureReaderStatus(action, videoId, true);
      }
      return;
    }

    handleSubmitClick(event);
  }

  async function ensureReaderStatus(action, videoId, forceRefresh, warmingRetry) {
    if (!action || action.dataset.videoId !== videoId) {
      return;
    }
    if (
      !forceRefresh &&
      !warmingRetry &&
      action.dataset.readerStatusVideoId === videoId
    ) {
      return;
    }

    if (forceRefresh) {
      action.dataset.readerStatusRetryCount = '0';
    }

    const requestSequence = ++readerStatusRequestSequence;
    action.dataset.readerStatusVideoId = videoId;
    setCheckingState(action);

    try {
      const response = await sendMessage({
        type: 'CHECK_YOUTUBE_READER_STATUS',
        payload: {
          url: location.href,
          videoId,
          forceRefresh: Boolean(forceRefresh)
        }
      });

      if (
        requestSequence !== readerStatusRequestSequence ||
        action.dataset.videoId !== videoId
      ) {
        return;
      }

      if (!response || !response.success) {
        action.dataset.readerStatusRetryCount = '0';
        setUnknownState(action);
        return;
      }

      if (response.saved && response.reader_url) {
        action.dataset.readerStatusRetryCount = '0';
        setSavedState(action, response.reader_url);
        return;
      }

      if (response.status === 'not_saved' || response.saved === false) {
        action.dataset.readerStatusRetryCount = '0';
        setNotSavedState(action);
        return;
      }

      if (
        response.status === 'unknown' &&
        response.reason === 'reader_index_warming' &&
        scheduleReaderStatusRetry(action, videoId)
      ) {
        return;
      }

      action.dataset.readerStatusRetryCount = '0';
      setUnknownState(action);
    } catch (error) {
      if (
        requestSequence === readerStatusRequestSequence &&
        action.dataset.videoId === videoId
      ) {
        action.dataset.readerStatusRetryCount = '0';
        setUnknownState(action);
      }
    }
  }

  function scheduleReaderStatusRetry(action, videoId) {
    const retryCount = Number(action.dataset.readerStatusRetryCount || '0');
    if (retryCount >= READER_STATUS_MAX_WARMING_RETRIES) {
      return false;
    }

    action.dataset.readerStatusRetryCount = String(retryCount + 1);
    setCheckingState(action);
    setTimeout(() => {
      if (action.dataset.videoId === videoId) {
        ensureReaderStatus(action, videoId, false, true);
      }
    }, READER_STATUS_RETRY_INTERVAL_MS);
    return true;
  }

  function setCheckingState(action) {
    const button = action.querySelector(`#${BUTTON_ID}`);
    const reprocessButton = action.querySelector(`#${REPROCESS_BUTTON_ID}`);
    action.dataset.readerUrl = '';
    if (button) {
      button.disabled = true;
      button.dataset.mode = 'checking';
      button.textContent = '检查中…';
      button.title = '正在检查 Reader';
    }
    if (reprocessButton) {
      reprocessButton.hidden = true;
      reprocessButton.disabled = false;
    }
  }

  function setSavedState(action, readerUrl) {
    const button = action.querySelector(`#${BUTTON_ID}`);
    const reprocessButton = action.querySelector(`#${REPROCESS_BUTTON_ID}`);
    action.dataset.readerUrl = readerUrl || '';
    if (button) {
      button.disabled = false;
      button.dataset.mode = 'saved';
      button.textContent = '已剪藏 ↗';
      button.title = '打开 Reader 文章';
    }
    if (reprocessButton) {
      reprocessButton.hidden = false;
      reprocessButton.disabled = false;
    }
  }

  function setNotSavedState(action) {
    const button = action.querySelector(`#${BUTTON_ID}`);
    const reprocessButton = action.querySelector(`#${REPROCESS_BUTTON_ID}`);
    action.dataset.readerUrl = '';
    if (button) {
      button.disabled = false;
      button.dataset.mode = 'submit';
      button.textContent = '剪藏';
      button.title = '发送当前视频到字幕处理后台';
    }
    if (reprocessButton) {
      reprocessButton.hidden = true;
      reprocessButton.disabled = false;
    }
  }

  function setUnknownState(action) {
    const button = action.querySelector(`#${BUTTON_ID}`);
    const reprocessButton = action.querySelector(`#${REPROCESS_BUTTON_ID}`);
    action.dataset.readerUrl = '';
    if (button) {
      button.disabled = false;
      button.dataset.mode = 'unknown';
      button.textContent = '状态未知';
      button.title = '点击重新检查 Reader';
    }
    if (reprocessButton) {
      reprocessButton.hidden = true;
      reprocessButton.disabled = false;
    }
  }

  function setSubmittingState(action) {
    const button = action.querySelector(`#${BUTTON_ID}`);
    const reprocessButton = action.querySelector(`#${REPROCESS_BUTTON_ID}`);
    if (button) {
      button.disabled = true;
      button.dataset.mode = 'submitting';
      button.textContent = '发送中…';
    }
    if (reprocessButton) {
      reprocessButton.disabled = true;
    }
  }

  async function handleSubmitClick(event) {
    const action = event.currentTarget.closest(`#${ACTION_ID}`);
    const videoId = extractVideoId(location.href);

    if (!videoId || !action) {
      showToast('无法识别当前 YouTube 视频', 'error');
      return;
    }

    setSubmittingState(action);
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
      const readerUrl = getReaderUrl(response);
      const preferredUrl = getPreferredResultUrl(response, response.result_url);
      showToast(
        readerUrl ? '已保存到 Readwise Reader' : '已发送到后台',
        'success',
        preferredUrl
      );

      if (String(response.status || '').toLowerCase() === 'completed') {
        if (readerUrl) {
          setSavedState(action, readerUrl);
        } else {
          ensureReaderStatus(action, videoId, true);
        }
        return;
      }

      if (response.process_id && response.poll_url) {
        pollTaskStatus(
          response.process_id,
          response.poll_url,
          response.result_url,
          videoId
        );
      }
    } catch (error) {
      ensureReaderStatus(action, videoId, true);
      showToast(formatError(error), 'error');
    }
  }

  async function pollTaskStatus(processId, pollUrl, resultUrl, videoId) {
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
        if (isReadwiseParseFailed(status)) {
          showToast(
            status.readwise_parse_message || status.error || 'Readwise 未抓到字幕，请在后台强制本地全文重发',
            'error',
            getPreferredResultUrl(status, resultUrl)
          );
          refreshCurrentAction(videoId);
          return;
        }

        if (normalizedStatus === 'completed') {
          const readerUrl = getReaderUrl(status);
          const linkUrl = getPreferredResultUrl(status, resultUrl);
          showToast(
            readerUrl ? '已保存到 Readwise Reader' : '后台处理完成',
            'success',
            linkUrl
          );
          const action = currentActionForVideo(videoId);
          if (action && readerUrl) {
            setSavedState(action, readerUrl);
          } else {
            refreshCurrentAction(videoId);
          }
          return;
        }

        if (normalizedStatus === 'failed') {
          showToast(status.error || '后台处理失败', 'error', resultUrl);
          refreshCurrentAction(videoId);
          return;
        }
      } catch (error) {
        // Polling is best-effort; the submitted task remains visible in the backend.
      }
    }

    refreshCurrentAction(videoId);
  }

  function currentActionForVideo(videoId) {
    const action = document.getElementById(ACTION_ID);
    if (action && action.dataset.videoId === videoId) {
      return action;
    }
    return null;
  }

  function refreshCurrentAction(videoId) {
    const action = currentActionForVideo(videoId);
    if (action) {
      ensureReaderStatus(action, videoId, true);
    }
  }

  function getPreferredResultUrl(response, fallbackUrl) {
    return getReaderUrl(response) || fallbackUrl;
  }

  function getReaderUrl(response) {
    return response && (
      response.readwise_url ||
      response.reader_url ||
      response.readwise_fallback_url ||
      response.readwise_url_only_url
    );
  }

  function isReadwiseParseFailed(status) {
    const normalizedStatus = String((status && status.status) || '').toLowerCase();
    const parseStatus = String((status && status.readwise_parse_status) || '').toLowerCase();
    return normalizedStatus === 'readwise_parse_failed' || parseStatus === 'failed';
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
        gap: 6px;
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

      #${BUTTON_ID},
      #${REPROCESS_BUTTON_ID} {
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

      #${BUTTON_ID}:hover,
      #${REPROCESS_BUTTON_ID}:hover {
        background: var(--yt-spec-button-chip-background-hover, rgba(0, 0, 0, 0.1));
      }

      #${BUTTON_ID}:disabled,
      #${REPROCESS_BUTTON_ID}:disabled {
        cursor: default;
        opacity: 0.7;
      }

      #${BUTTON_ID}[data-mode='saved'] {
        border-color: rgba(22, 163, 74, 0.35);
        color: #15803d;
      }

      #${REPROCESS_BUTTON_ID} {
        height: 30px;
        padding: 0 10px;
        border-radius: 15px;
        font-size: 12px;
        line-height: 30px;
        opacity: 0.78;
      }

      #${REPROCESS_BUTTON_ID}[hidden] {
        display: none;
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
