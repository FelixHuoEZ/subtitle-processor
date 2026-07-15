const LOCAL_EXTENSION_SETTING_KEYS = [
  'apiServerUrl',
  'webServerUrl',
  'accessClientId',
  'accessClientSecret'
];

async function loadLocalExtensionSettings() {
  try {
    const response = await fetch(chrome.runtime.getURL('local-settings.json'), {
      cache: 'no-store'
    });
    if (!response.ok) {
      return {};
    }

    const rawSettings = await response.json();
    if (!rawSettings || typeof rawSettings !== 'object' || Array.isArray(rawSettings)) {
      return {};
    }

    return LOCAL_EXTENSION_SETTING_KEYS.reduce((settings, key) => {
      if (typeof rawSettings[key] === 'string' && rawSettings[key].trim()) {
        settings[key] = rawSettings[key].trim();
      }
      return settings;
    }, {});
  } catch (error) {
    return {};
  }
}
