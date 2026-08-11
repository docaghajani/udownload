const HOST = "com.ideveloper.udownload.native";
const DEFAULTS = { intercept: true, sendCookies: true, prompt: true };
const recent = new Map();

async function settings() {
  return await chrome.storage.local.get(DEFAULTS);
}

function sendNative(message) {
  return new Promise((resolve) => {
    chrome.runtime.sendNativeMessage(HOST, message, (reply) => {
      const error = chrome.runtime.lastError;
      resolve(error ? {ok: false, error: error.message} : (reply || {ok: true}));
    });
  });
}

async function cookieHeader(url) {
  const opts = await settings();
  if (!opts.sendCookies || !/^https?:/i.test(url)) return "";
  try {
    const cookies = await chrome.cookies.getAll({url});
    return cookies.map(c => `${c.name}=${c.value}`).join("; ");
  } catch (_) {
    return "";
  }
}

function createMenus() {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({id: "udm-link", title: "Download with UDM", contexts: ["link", "image", "video", "audio"]});
    chrome.contextMenus.create({id: "udm-selected", title: "Download selected links with UDM", contexts: ["selection", "page"]});
    chrome.contextMenus.create({id: "udm-all", title: "Download all links with UDM", contexts: ["page"]});
  });
}
chrome.runtime.onInstalled.addListener(createMenus);
chrome.runtime.onStartup.addListener(createMenus);

async function collectLinks(tabId, mode) {
  const results = await chrome.scripting.executeScript({
    target: {tabId},
    func: (requestedMode) => {
      const absolute = (value) => {
        try { return new URL(value, document.baseURI).href; } catch (_) { return ""; }
      };
      const selected = [];
      if (requestedMode === "selected") {
        const selection = window.getSelection();
        if (selection && selection.rangeCount) {
          const range = selection.getRangeAt(0);
          document.querySelectorAll("a[href]").forEach(a => {
            try { if (range.intersectsNode(a)) selected.push(a); } catch (_) {}
          });
        }
      }
      const nodes = requestedMode === "selected" ? selected : [...document.querySelectorAll("a[href],video[src],audio[src],source[src]")];
      const seen = new Set();
      const links = [];
      for (const node of nodes) {
        const raw = node.href || node.src || "";
        const url = absolute(raw);
        if (!url || seen.has(url) || !/^(https?|ftp):/i.test(url)) continue;
        seen.add(url);
        links.push({url, text: (node.innerText || node.title || node.getAttribute("download") || "").trim(), pageUrl: location.href});
        if (links.length >= 1500) break;
      }
      if (requestedMode === "selected" && !links.length) {
        const text = String(window.getSelection() || "");
        const matches = text.match(/https?:\/\/[^\s<>'\"]+/g) || [];
        for (const value of matches) {
          if (!seen.has(value)) links.push({url: value, text: "", pageUrl: location.href});
        }
      }
      return links;
    },
    args: [mode]
  });
  return (results[0] && results[0].result) || [];
}

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId === "udm-link") {
    const url = info.linkUrl || info.srcUrl;
    if (!url) return;
    await sendNative({action: "add_url", url, pageUrl: info.pageUrl || tab?.url || "", cookies: await cookieHeader(url), userAgent: navigator.userAgent});
    return;
  }
  if (!tab?.id) return;
  const mode = info.menuItemId === "udm-selected" ? "selected" : "all";
  const links = await collectLinks(tab.id, mode);
  await sendNative({action: "select_links", links, pageUrl: info.pageUrl || tab.url || ""});
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  (async () => {
    if (message.action === "collect") {
      const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
      const links = tab?.id ? await collectLinks(tab.id, message.mode || "all") : [];
      const result = await sendNative({action: "select_links", links, pageUrl: tab?.url || ""});
      sendResponse(result);
    } else if (message.action === "ping") {
      sendResponse(await sendNative({action: "ping"}));
    }
  })();
  return true;
});

chrome.downloads.onCreated.addListener(async (item) => {
  const opts = await settings();
  if (!opts.intercept) return;
  const url = item.finalUrl || item.url || "";
  if (!/^(https?|ftp):/i.test(url)) return;
  const now = Date.now();
  if (recent.has(url) && now - recent.get(url) < 5000) return;
  recent.set(url, now);
  setTimeout(() => recent.delete(url), 6000);
  const ready = await sendNative({action: "ping"});
  if (!ready || ready.ok === false) return;
  try { await chrome.downloads.cancel(item.id); } catch (_) {}
  try { await chrome.downloads.erase({id: item.id}); } catch (_) {}
  await sendNative({
    action: "add_url",
    url,
    filename: item.filename ? item.filename.split(/[\\/]/).pop() : "",
    referrer: item.referrer || "",
    cookies: await cookieHeader(url),
    userAgent: navigator.userAgent,
    intercepted: true
  });
});
