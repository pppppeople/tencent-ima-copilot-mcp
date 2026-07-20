(function () {
  if (window.__kouziImaAuthHelperInstalled) return;
  window.__kouziImaAuthHelperInstalled = true;

  const endpoint = 'http://127.0.0.1:8787/ima-auth';
  let sentKey = '';

  function normalizeHeaders(headers) {
    const out = {};
    if (!headers) return out;
    if (typeof Headers !== 'undefined' && headers instanceof Headers) {
      headers.forEach((value, key) => {
        out[String(key).toLowerCase()] = String(value);
      });
      return out;
    }
    if (Array.isArray(headers)) {
      for (const item of headers) {
        if (Array.isArray(item) && item.length >= 2) {
          out[String(item[0]).toLowerCase()] = String(item[1]);
        }
      }
      return out;
    }
    for (const key of Object.keys(headers)) {
      out[String(key).toLowerCase()] = String(headers[key]);
    }
    return out;
  }

  function shouldCapture(url) {
    return String(url || '').includes('/cgi-bin/assistant/qa');
  }

  async function sendAuth(headers) {
    const normalized = normalizeHeaders(headers);
    const bkn = normalized['x-ima-bkn'];
    const cookie = normalized['x-ima-cookie'];
    if (!bkn || !cookie) return;

    const key = `${bkn}:${cookie.slice(0, 32)}:${cookie.slice(-32)}`;
    if (key === sentKey) return;
    sentKey = key;

    try {
      const response = await fetch(endpoint, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ bkn, cookie })
      });
      if (response.ok) {
        console.info('[Kouzi IMA] auth updated');
      } else {
        console.warn('[Kouzi IMA] auth update failed', await response.text());
      }
    } catch (error) {
      console.warn('[Kouzi IMA] receiver unavailable', error);
      sentKey = '';
    }
  }

  const oldFetch = window.fetch;
  window.fetch = function patchedFetch(input, init) {
    try {
      const url = String((input && input.url) || input || '');
      if (shouldCapture(url)) {
        sendAuth((init && init.headers) || (input && input.headers));
      }
    } catch (_) {
      // Keep IMA behavior untouched even if the helper fails.
    }
    return oldFetch.apply(this, arguments);
  };

  const proto = XMLHttpRequest.prototype;
  const oldOpen = proto.open;
  const oldSetRequestHeader = proto.setRequestHeader;
  const oldSend = proto.send;

  proto.open = function patchedOpen(method, url) {
    this.__kouziImaUrl = String(url || '');
    this.__kouziImaHeaders = {};
    return oldOpen.apply(this, arguments);
  };

  proto.setRequestHeader = function patchedSetRequestHeader(key, value) {
    try {
      this.__kouziImaHeaders[String(key).toLowerCase()] = String(value);
    } catch (_) {
      // Ignore helper errors.
    }
    return oldSetRequestHeader.apply(this, arguments);
  };

  proto.send = function patchedSend() {
    try {
      if (shouldCapture(this.__kouziImaUrl)) {
        sendAuth(this.__kouziImaHeaders);
      }
    } catch (_) {
      // Ignore helper errors.
    }
    return oldSend.apply(this, arguments);
  };

  console.info('[Kouzi IMA] helper installed');
})();
