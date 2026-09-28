const button = document.getElementById('connect');
const status = document.getElementById('status');

button.addEventListener('click', async () => {
  button.disabled = true;
  status.textContent = '正在验证本机设置页和 ChatGPT 会话…';
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    const url = new URL(tab?.url || 'about:blank');
    if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1' || !url.port || url.pathname !== '/' || url.search) {
      throw new Error('settings_page_required');
    }
    const [probe] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      args: [url.origin],
      func: async (expectedOrigin) => {
        if (location.origin !== expectedOrigin || location.pathname !== "/" || location.search) return false;
        const session = sessionStorage.getItem('codexhub.runtime-settings.session');
        if (!session) return false;
        const response = await fetch('/api/status', { headers: { Authorization: `Bearer ${session}` }, cache: 'no-store' });
        const data = await response.json();
        return response.ok && data.runtime?.installed === true && typeof data.settings === 'object';
      },
    });
    if (probe?.result !== true) throw new Error('settings_page_required');
    const cookies = (await chrome.cookies.getAll({ url: 'https://chatgpt.com/' }))
      .filter(cookie => ['chatgpt.com', '.chatgpt.com'].includes(cookie.domain)
        && cookie.path === '/' && cookie.secure && !cookie.partitionKey);
    if (!cookies.length) throw new Error('chatgpt_login_required');
    const [result] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      args: [cookies, url.origin],
      func: async (accountCookies, expectedOrigin) => {
        if (location.origin !== expectedOrigin || location.pathname !== "/" || location.search) return { ok: false };
        const session = sessionStorage.getItem('codexhub.runtime-settings.session');
        if (!session) return { ok: false };
        window.dispatchEvent(new CustomEvent('codexhub-browser-account', { detail: { phase: 'verifying' } }));
        try {
          const response = await fetch('/api/browser-session', {
            method: 'POST', cache: 'no-store', credentials: 'same-origin',
            headers: { Authorization: `Bearer ${session}`, 'Content-Type': 'application/json' },
            body: JSON.stringify({ cookies: accountCookies }),
          });
          const data = await response.json();
          const ok = response.ok && data.authenticated === true && data.restart_required === true;
          window.dispatchEvent(new CustomEvent('codexhub-browser-account', { detail: { phase: ok ? 'saved' : 'failed' } }));
          return { ok };
        } catch {
          window.dispatchEvent(new CustomEvent('codexhub-browser-account', { detail: { phase: 'failed' } }));
          return { ok: false };
        }
      },
    });
    status.textContent = result?.result?.ok
      ? '账号已验证并保存。请在 CodexHub 中重启 ChatGPT 组件；此操作没有自动重启。'
      : '连接未完成。请确认此浏览器已登录 ChatGPT，并从 CodexHub 重新打开设置页后重试。';
  } catch {
    status.textContent = '请先在此浏览器登录 ChatGPT，然后在有效的 CodexHub Runtime Settings 页面使用此扩展。';
  } finally {
    button.disabled = false;
  }
});
