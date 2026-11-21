# Beginner-Friendly Installation & Phone Setup Guide

## 1. Running Locally on Windows 10
1. Install **Python 3.11+** from [python.org](https://www.python.org/downloads/) (check **"Add Python to PATH"** during setup).
2. Extract `steam-free-to-keep-tracker.zip` to a folder such as `C:\steam-ftk-tracker`.
3. Open **Command Prompt** in that folder and run:
   ```cmd
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   copy .env.example .env
   ```
4. Verify everything works by running the automated test suite:
   ```cmd
   python tests\run_suite.py
   ```
5. Start the dashboard and background tracker:
   ```cmd
   python -m backend.cli serve --with-scheduler
   ```
6. Open Google Chrome and visit `http://127.0.0.1:8000`.

> **Note:** Running on your Windows laptop stops when your laptop is shut down. For true 24/7 operation while your laptop and phone are off, follow [DEPLOYMENT.md](DEPLOYMENT.md).

## 2. Setting Up Free Android Phone Notifications (No Dashboard Required)

### Option A: Telegram Bot (Recommended — Unlimited Offline History)
1. Open Telegram on your Android phone and search for **@BotFather**.
2. Send `/newbot`, choose a name (e.g., `My Steam FTK Tracker`), and copy the **Bot Token**.
3. Search for **@userinfobot**, tap Start, and copy your numeric **Chat ID**.
4. Open your new bot in Telegram and tap **Start** (required so the bot can message you).
5. Put these values in your server's `.env` or hosting environment variables:
   ```env
   TELEGRAM_BOT_TOKEN=your_bot_token_here
   TELEGRAM_CHAT_ID=your_numeric_chat_id
   ```

### Option B: ntfy.sh Android App (Instant Push via FCM)
1. Install the **ntfy** app from Google Play Store or F-Droid.
2. Tap **+** and subscribe to a unique, private topic name (e.g., `steam-ftk-harsh-849201`).
3. Allow **Notifications** and disable battery optimization for ntfy in Android Settings -> Apps -> ntfy -> Battery -> **Unrestricted**.
4. Set `NTFY_TOPIC=steam-ftk-harsh-849201` in your server's environment variables.
5. *Retention note:* The public `ntfy.sh` free server caches messages for **12 hours** if your phone is completely offline. Using **both** Telegram and ntfy gives you instant push alerts plus permanent offline history.

## 3. Installing the PWA on Your Android Phone (One-Tap Home Screen App)
1. Deploy the backend over HTTPS (see [DEPLOYMENT.md](DEPLOYMENT.md)).
2. Open your HTTPS tracker URL in **Chrome on Android**.
3. Tap the **Install App** button in the top bar (or Chrome's **⋮ menu -> Add to Home screen / Install app**).
4. The tracker now opens full-screen with one tap and works offline using its cached Service Worker + IndexedDB history.
