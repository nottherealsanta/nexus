// POC: the NSH web app rendered inside the terminal by PixelGUI
// (https://github.com/zenbu-labs/terminal-browser/tree/main/pixel).
//
// PixelGUI runs this file inside its patched Electron runtime: the page renders in an
// offscreen Chromium WebView and reaches the terminal as kitty graphics frames, with
// mouse and keyboard forwarded back. `scripts/nexus pixel` starts the NSH host
// (scripts/serve.mjs) and passes its URL as the first argument.
//
//   pixel . -- http://127.0.0.1:4821
//
// Ctrl-Q quits; every other key (Ctrl-C included) goes to the page.
const { createRoot } = require("@zenbu-labs/pixel");

const url = process.argv[2] || process.env.NEXUS_PIXEL_URL || "http://127.0.0.1:4820";

const root = createRoot({
  name: "Nexus",
  onKey(event) {
    if (event.kind === "press" && event.mods.ctrl && event.key === "q") {
      root.stop();
      return true;
    }
  },
});

// A persistent partition keeps the web app's local storage (theme, layout) between runs.
const page = root.loadURL(url, { partition: "persist:nexus-pixel" });
page.onChange((state) => root.setTitle(state.title || "Nexus"));
