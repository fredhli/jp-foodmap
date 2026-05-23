# Sync Setup Guide

Sync your saved restaurants, hidden restaurants (the ones you've dismissed so they no longer appear on the map), and custom landmarks (sights, hotels, etc.) to the cloud so they're shared across devices.

---

## Steps

1. **Sign up / Sign in to GitHub**
   Open <https://github.com> and sign in, or sign up if you don't have an account.

   ![GitHub home page](step1_github_register.png)

2. **Create a new Gist**
   Open <https://gist.github.com>. Set the filename to `favorites.json`, paste `[]` as the content, and click **Create secret gist** in the bottom-right.

   ![New Gist](step2_create_gist.png)

   Once created, look at the address bar — the hex string after `gist.github.com/<username>/` is the **Gist ID**. Copy it; you'll paste it into the website in a moment.

   ![Gist ID in the URL](step2_create_gist_2.jpg)

3. **Generate a PAT**
   Open <https://github.com/settings/tokens?type=beta> and click **Generate new token** in the top-right.

   ![PAT settings page](step3_PAT.png)

   - Token name: anything you like
   - Expiration: **No expiration** recommended
   - Scroll down to **Permissions** → **Add permissions** → search for and tick **Gists**

   ![No expiration + select Gists](step3_PAT_2.png)

   Change **Gists** Access to **Read and write**, then scroll to the bottom and click **Generate token**.

   ![Gists set to Read and write](step3_PAT_3.png)

   **Copy the `github_pat_...` string immediately** — it's only shown once, and you won't see it again after you leave the page.

   ![Generated PAT](step3_PAT_4.png)

4. **Paste into the website**
   Back on the map → bottom-left 🎛 → scroll to the bottom → **⚙️ Sync settings** → paste your Gist ID + PAT → **Save & test**.

   ![Sync settings panel](step4_fill.jpg)

When you see **✓ Configured**, you're set. From then on every ⭐ / 🚫 auto-syncs.

---

## Other devices / other browsers

In **Sync settings**, paste the same Gist ID / PAT — your saved restaurants, hidden list, and custom sights will be pulled from the cloud on demand.

---

## FAQ

- **The bottom-left button is still flashing red.** Refresh the page. If it's still flashing, check the status line at the bottom of Sync Settings — *HTTP 403* usually means your PAT doesn't have *Read and write*; redo step 3.
- **What if I skip Gist entirely?** All your edits stay on this device only. As soon as you switch browsers, switch devices, or clear your cache, they're gone.
- **What if I fill in the Gist but not the PAT?** You can view your saved / hidden / sights lists, but edits won't sync to the cloud (read-only).
