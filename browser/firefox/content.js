browser.runtime.onMessage.addListener((message) => {
  if (message.action !== "collect-page-links") return;
  const mode = message.mode || "all";
  const selected = [];
  if (mode === "selected") {
    const selection = window.getSelection();
    if (selection && selection.rangeCount) {
      const range = selection.getRangeAt(0);
      document.querySelectorAll("a[href]").forEach(a => { try { if (range.intersectsNode(a)) selected.push(a); } catch (_) {} });
    }
  }
  const nodes = mode === "selected" ? selected : [...document.querySelectorAll("a[href],video[src],audio[src],source[src]")];
  const seen = new Set();
  const links = [];
  for (const node of nodes) {
    let url = "";
    try { url = new URL(node.href || node.src || "", document.baseURI).href; } catch (_) {}
    if (!url || seen.has(url) || !/^(https?|ftp):/i.test(url)) continue;
    seen.add(url);
    links.push({url, text:(node.innerText||node.title||node.getAttribute("download")||"").trim(), pageUrl:location.href});
    if (links.length >= 1500) break;
  }
  return Promise.resolve(links);
});
