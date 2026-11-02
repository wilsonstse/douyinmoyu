// Wake the extension worker after a browser restart or page reload.
chrome.runtime.sendMessage({type: "connect"}).catch(() => {});

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type !== "current-video") return;
  const video = [...document.querySelectorAll("video")]
    .filter(element => {
      const rect = element.getBoundingClientRect();
      return rect.height > 0 && rect.top >= 0 && rect.top < innerHeight;
    })
    .sort((a, b) => Math.abs(a.getBoundingClientRect().top) -
                    Math.abs(b.getBoundingClientRect().top))[0];
  const slide = video?.closest(".sliderVideo");
  const match = slide?.className.match(/(?:^|\s)video_(\d+)/);
  sendResponse({videoId: slide?.getAttribute("data-e2e-vid") || match?.[1] || null});
});
