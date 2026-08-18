# Charticks — Setup Guide

Follow these steps in order. Total time: about 10 minutes.

You do **not** need to understand what any of this does. Just follow along.

---

## Before you start

You need:

- A **Windows PC** (Windows 10 or 11)
- An **internet connection**
- The two files that were sent to you:
  - `Charticks-Setup-0.1.0.exe` ← the app
  - `setup-tester.bat` ← the helper script

Put both files somewhere easy to find, like your **Desktop**.

---

## Step 1 — Install Python

Charticks has an engine underneath it that runs on something called Python.
You need to install it once.

1. Go to **https://www.python.org/downloads/**
2. Click the big yellow **"Download Python"** button
3. Open the file you just downloaded

4. ### ⚠️ THE MOST IMPORTANT STEP
   On the very first screen, look at the **bottom**.
   There is a checkbox: **"Add python.exe to PATH"**

   **TICK THAT BOX.**

   It is not ticked by default. If you miss it, Charticks will open but will
   show no data at all, and you will have to uninstall Python and start over.

5. Now click **"Install Now"**
6. Wait for it to finish, then click **Close**

---

## Step 2 — Run the setup script

1. Find **`setup-tester.bat`** (the file that was sent to you)
2. **Double-click it**

A black window will open and text will scroll past. This is normal.

3. Wait until it says:

```
  ============================================
    SUCCESS - setup is complete.
  ============================================
```

4. Press any key to close the window

> **If it says "Python is not installed yet"** — Step 1 didn't work.
> Most likely the PATH checkbox was missed. Uninstall Python
> (Start → "Add or remove programs" → Python → Uninstall),
> then redo Step 1 and be sure to tick that box.

> **If you see a red error window** — take a screenshot of the whole
> window and send it over. Don't try to fix it yourself.

---

## Step 3 — Install Charticks

1. Double-click **`Charticks-Setup-0.1.0.exe`**

2. Windows may show a blue box saying
   **"Windows protected your PC"**.
   This is expected — the app isn't code-signed yet.

   - Click **"More info"**
   - Then click **"Run anyway"**

3. Follow the installer and let it finish

---

## Step 4 — Start the app

Open **Charticks** from your Desktop or Start Menu.

The window will open. **Wait about 10 seconds** on first launch — the engine is
starting in the background.

---

## Step 5 — Connect a broker

Charticks shows no market data until a broker account is connected.

1. Go to the **Brokers** screen
2. Click **Add Account**
3. Pick your broker and fill in the details you were given
4. Click **Connect**

The status dot should turn **green**.

---

## What "working" looks like

- The status dot next to your account is **green**
- Index prices (NIFTY etc.) are showing numbers and **changing**
- The **Option Chain** shows a list of strikes with prices

If prices show but never change, the market may simply be closed —
Indian markets are open **9:15 AM to 3:30 PM, Monday to Friday**.

---

## If something goes wrong

Please send these three things — it makes problems much faster to fix:

1. **A screenshot** of the whole Charticks window
2. **What you did** just before it went wrong
3. **The logs.** In the app: **Settings → Diagnostics → Save Diagnostics ZIP**.
   It writes `charticks-logs-<date>.zip` to your Desktop and opens the folder
   with the file selected — attach that.

   If the app will not start, the same files are on disk. Paste this into the
   File Explorer address bar and send everything in it:

   ```
   Documents\Charticks\logs
   ```

### Common problems

| What you see | What it means | What to do |
|---|---|---|
| App opens but everything is blank/zero | Python wasn't found | Redo Step 1, tick the PATH box |
| "Windows protected your PC" | App isn't code-signed | Click "More info" → "Run anyway" |
| Broker dot is red or orange | Login failed | Re-check the credentials; some expire daily |
| Prices show but never move | Market is closed | Try during 9:15 AM – 3:30 PM on a weekday |

---

## Important

- This is a **test build**. Please use a **paper trading / test account** unless
  you have been told otherwise.
- **Never share your broker credentials** — including in screenshots.
  Blur or crop them out before sending anything.
