const keys = {
  previous: {key: "ArrowUp", code: "ArrowUp", windowsVirtualKeyCode: 38},
  next: {key: "ArrowDown", code: "ArrowDown", windowsVirtualKeyCode: 40},
  toggle: {key: " ", code: "Space", windowsVirtualKeyCode: 32}
};
let socket = null;
let retry = null;
let keepAlive = null;
const mediaByTab = new Map();
const feedRequests = new Map();
const videoUrlsByTab = new Map();
const observedTabs = new Set();
const downloadIds = new Map();

function report(message) {
  if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify(message));
}

function collectVideoUrls(root, result, depth = 0) {
  if (!root || typeof root !== "object" || depth > 18) return;
  if (Array.isArray(root)) {
    for (const item of root) collectVideoUrls(item, result, depth + 1);
    return;
  }
  const id = String(root.aweme_id ?? root.awemeId ?? "");
  if (/^\d{15,25}$/.test(id)) {
    const urls = [];
    const video = root.video || {};
    for (const address of [video.play_addr, video.play_addr_h264,
                           video.play_addr_265, video.download_addr,
                           video.play_addr_lowbr, root.play_addr, root.download_addr,
                           ...(video.bit_rate || []).map(item => item.play_addr)]) {
      for (const url of address?.url_list || []) {
        if (typeof url === "string" && /^https:\/\//.test(url)) urls.push(url);
      }
    }
    if (urls.length) result.set(id, [...new Set(urls)]);
  }
  for (const value of Object.values(root)) collectVideoUrls(value, result, depth + 1);
}

async function observe(tabId) {
  if (observedTabs.has(tabId)) return;
  try {
    await chrome.debugger.attach({tabId}, "1.3");
    observedTabs.add(tabId);
    await chrome.debugger.sendCommand({tabId}, "Network.enable", {
      maxTotalBufferSize: 50 * 1024 * 1024, maxResourceBufferSize: 10 * 1024 * 1024});
  } catch (error) {
    console.warn("无法观察抖音媒体请求:", error);
  }
}

chrome.debugger.onEvent.addListener((source, method, params) => {
  if (!source.tabId) return;
  if (method === "Network.responseReceived") {
    const response = params.response || {};
    const mime = (response.mimeType || "").toLowerCase();
    if (mime.startsWith("video/") || params.type === "Media") {
      const list = mediaByTab.get(source.tabId) || [];
      list.push({url: response.url, at: Date.now(), mime});
      mediaByTab.set(source.tabId, list.slice(-30));
    } else if (mime.includes("json") && /(?:feed|aweme|detail)/i.test(response.url || "")) {
      feedRequests.set(`${source.tabId}:${params.requestId}`, true);
    }
  } else if (method === "Network.loadingFinished") {
    const key = `${source.tabId}:${params.requestId}`;
    if (!feedRequests.delete(key)) return;
    chrome.debugger.sendCommand({tabId: source.tabId}, "Network.getResponseBody",
      {requestId: params.requestId}).then(body => {
        if (body.base64Encoded) return;
        const found = new Map();
        collectVideoUrls(JSON.parse(body.body), found);
        const known = videoUrlsByTab.get(source.tabId) || new Map();
        for (const [id, urls] of found) known.set(id, urls);
        videoUrlsByTab.set(source.tabId, known);
      }).catch(() => {});
  }
});

chrome.debugger.onDetach.addListener(source => {
  if (source.tabId) observedTabs.delete(source.tabId);
});

chrome.downloads.onChanged.addListener(async delta => {
  const task = downloadIds.get(delta.id);
  if (!task) return;
  const {videoId} = task;
  if (delta.bytesReceived) {
    const [item] = await chrome.downloads.search({id: delta.id});
    report({type: "browser-progress", videoId, received: item?.bytesReceived || 0,
            total: item?.totalBytes || 0});
  }
  if (delta.state?.current === "complete") {
    const [item] = await chrome.downloads.search({id: delta.id});
    report({type: "browser-complete", videoId, filename: item?.filename || ""});
    downloadIds.delete(delta.id);
  } else if (delta.state?.current === "interrupted") {
    downloadIds.delete(delta.id);
    const reason = delta.error?.current || "UNKNOWN";
    if (task.nextIndex < task.urls.length) {
      try {
        await startDownload(task);
        return;
      } catch (error) {
        report({type: "download-error", reason: String(error.message || error)});
        return;
      }
    }
    report({type: "download-error", reason: reason === "SERVER_FORBIDDEN"
      ? "媒体服务器拒绝 (403)" : `Chrome 下载中断：${reason}`});
  }
});

async function startDownload(task) {
  while (task.nextIndex < task.urls.length) {
    const url = task.urls[task.nextIndex++];
    try {
      const id = await chrome.downloads.download({url,
        filename: `douyin-remote/${task.videoId}.mp4`, conflictAction: "uniquify"});
      downloadIds.set(id, task);
      report({type: "browser-progress", videoId: task.videoId, received: 0, total: 0});
      return;
    } catch (error) {
      if (task.nextIndex >= task.urls.length) throw error;
    }
  }
}

function connect() {
  if (socket && [WebSocket.CONNECTING, WebSocket.OPEN].includes(socket.readyState)) return;
  clearTimeout(retry);
  socket = new WebSocket("ws://127.0.0.1:17853/");
  socket.onopen = () => {
    clearInterval(keepAlive);
    socket.send(JSON.stringify({type: "hello", protocol: 3}));
    keepAlive = setInterval(() => {
      if (socket?.readyState === WebSocket.OPEN) socket.send("ping");
    }, 20000);
  };
  socket.onmessage = async event => {
    try {
      const {command} = JSON.parse(event.data);
      if (command === "download") {
        await downloadCurrent();
        return;
      }
      const key = keys[command];
      if (!key) return;
      const tabs = await chrome.tabs.query({url: "https://www.douyin.com/*"});
      if (tabs.length) await dispatch(tabs[0].id, key);
    } catch (error) {
      console.error("抖音后台遥控失败:", error);
      if (socket?.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({type: "download-error", reason: String(error.message || error)}));
      }
    }
  };
  socket.onclose = () => {
    clearInterval(keepAlive);
    retry = setTimeout(connect, 2000);
  };
  socket.onerror = () => socket.close();
}

async function downloadCurrent() {
  const tabs = await chrome.tabs.query({url: "https://www.douyin.com/*"});
  if (!tabs.length) throw new Error("找不到抖音标签页");
  try {
    const reply = await chrome.tabs.sendMessage(tabs[0].id, {type: "current-video"});
    if (!reply?.videoId) throw new Error("无法识别当前视频");
    const urls = [...new Set(videoUrlsByTab.get(tabs[0].id)?.get(reply.videoId) || [])];
    if (!urls.length) throw new Error("尚未匹配当前视频，请切换一次视频再试");
    await startDownload({videoId: reply.videoId, urls, nextIndex: 0});
  } catch (error) {
    report({type: "download-error", reason: String(error.message || error)});
  }
}

async function dispatch(tabId, key) {
  const target = {tabId};
  const wasObserved = observedTabs.has(tabId);
  try {
    if (!wasObserved) await chrome.debugger.attach(target, "1.3");
    await chrome.debugger.sendCommand(target, "Input.dispatchKeyEvent", {type: "keyDown", ...key});
    await chrome.debugger.sendCommand(target, "Input.dispatchKeyEvent", {type: "keyUp", ...key});
  } finally {
    if (!wasObserved) try { await chrome.debugger.detach(target); } catch (_) {}
  }
}

chrome.runtime.onMessage.addListener(message => {
  if (message?.type === "connect") connect();
});
chrome.alarms.create("reconnect", {periodInMinutes: 0.5});
chrome.alarms.onAlarm.addListener(alarm => {
  if (alarm.name === "reconnect") {
    connect();
    chrome.tabs.query({url: "https://www.douyin.com/*"}).then(tabs =>
      tabs.forEach(tab => observe(tab.id)));
  }
});
chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);
chrome.tabs.onUpdated.addListener((tabId, info, tab) => {
  if (info.status === "complete" && tab.url?.startsWith("https://www.douyin.com/")) {
    connect();
    observe(tabId);
  }
});
chrome.tabs.query({url: "https://www.douyin.com/*"}).then(tabs => tabs.forEach(tab => observe(tab.id)));
connect();
