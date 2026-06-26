// HermesLite Chrome Extension — service worker
// Default: icon click opens the side panel on the right.
chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true });

// Listen for "pop-out" requests from the sidepanel to open a floating window.
chrome.runtime.onMessage.addListener((msg) => {
  if (msg && msg.action === "popout") {
    chrome.windows.create({
      url: "sidepanel.html",
      type: "popup",
      width: 420,
      height: 640,
    });
  }
});
