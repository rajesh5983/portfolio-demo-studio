# OBS Setup Guide (one-time)

`engine/demo_bot.py` controls OBS via its WebSocket server. Configure OBS
once before running the bot.

## 1. Enable the WebSocket server

1. In OBS, go to **Tools → WebSocket Server Settings**.
2. Check **Enable WebSocket server**.
3. Set **Server Port** to `4455`.
4. Click **Generate Password** (or set your own), then **Apply**.
5. Copy the password into `OBS_WEBSOCKET_PASSWORD` in your `.env` file.

## 2. Set the recording format and path

1. Go to **Settings → Output**.
2. Set **Recording Format** to `mp4`.
3. Set **Recording Path** to:

   ```
   C:\Repos\portfolio-demo-studio\projects\workershield\assets\recordings\
   ```

   `demo_bot.py` sets this directory via the WebSocket API before each
   segment, but pointing OBS here by default avoids surprises if the bot
   is run manually or partway through a session.

## 3. Set resolution and frame rate

1. Go to **Settings → Video**.
2. Set **Base (Canvas) Resolution** and **Output (Scaled) Resolution** to
   `1920x1080`.
3. Set **Common FPS Values** to `30`.

## 4. Set the encoder

1. Go to **Settings → Output → Recording**.
2. Set **Encoder** to `x264`.
3. Set **Rate Control** to `CRF` and **CRF** to `18`.

## 5. Verify

With OBS running, run:

```powershell
python engine/test_obs.py
```

This connects over the WebSocket API, prints the OBS version and current
scene, and confirms the connection is working.

## 6. Launch Chrome with remote debugging enabled

`engine/demo_bot.py` connects to a Chrome session over the Chrome DevTools
Protocol (CDP) instead of launching a new browser — it reuses the tabs in
that session (WorkerShield, Qdrant, etc.) rather than opening new ones.

Modern Chrome **silently ignores `--remote-debugging-port` when launched
against your normal/default profile** (a security hardening to stop CDP
from being used to steal session cookies). To enable CDP, launch Chrome with
a separate, dedicated `--user-data-dir`:

```powershell
"C:\Program Files\Google\Chrome\Application\chrome.exe" `
  --remote-debugging-port=9222 `
  --user-data-dir="C:\Repos\portfolio-demo-studio\.chrome-automation-profile" `
  --no-first-run --no-default-browser-check `
  "http://localhost:7860" `
  "http://192.168.100.10:6333/dashboard" `
  "http://localhost:6006/projects"
```

This opens a separate Chrome window (its own profile, no cookies/extensions
from your main browser) with the WorkerShield, Qdrant, and Phoenix tabs
pre-loaded. Verify it worked before running the bot:

```powershell
curl http://localhost:9222/json/list
```

You should see all three tabs listed with `"type": "page"`. If `9222` isn't
reachable, check that no other Chrome instance is using
`.chrome-automation-profile` and that the port isn't already in use.

`engine/launch_chrome.py` automates this (and `engine/run_demo.py` runs it
automatically if CDP isn't already reachable on `CHROME_CDP_PORT`).

Use this dedicated window for the demo recording — don't close it or open/
close tabs in it manually while `demo_bot.py` is running, since it finds
the WorkerShield/Qdrant tabs by URL and switches between them with
`page.bring_to_front()`.
