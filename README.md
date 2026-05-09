# Sidak's Job Bot

Every morning at 7am IST, this scrapes fresh Data Analyst jobs across India, scores them against your verified profile, rewrites your resume per job (no hallucination), compiles PDFs, uploads them to Google Drive, and emails you a digest with apply links.

**Cost: ₹0/month.** Runs entirely on GitHub Actions free tier + Gemini free tier.

---

## What you need to set up (one time, ~30 minutes)

### 1. Fork this repo to your GitHub
Make it **private** — your profile JSON has personal info.

### 2. Get a Gemini API key (free)
- Go to https://aistudio.google.com/apikey
- Click "Create API key"
- Copy it. Free tier: 1,500 requests/day — enough for ~100 jobs/day.

### 3. Create a Gmail App Password
- Go to https://myaccount.google.com/apppasswords (2FA must be on)
- App: "Mail", Device: "Other → JobBot"
- Copy the 16-character password.

### 4. Create a Google Drive folder
- New folder named "Tailored Resumes"
- Open it. The URL is `https://drive.google.com/drive/folders/<FOLDER_ID>` — copy `<FOLDER_ID>`.

### 5. Get Google OAuth token (for Drive upload)
- Go to https://console.cloud.google.com → New Project "JobBot"
- APIs & Services → Library → enable "Google Drive API"
- APIs & Services → OAuth consent screen → External → fill in basics, add yourself as a test user
- Credentials → Create OAuth 2.0 Client ID → "Desktop app"
- Download the JSON, rename to `client_secrets.json`
- On your Mac, run this once to mint a token:
  ```bash
  pip install google-auth-oauthlib
  python -c "
  from google_auth_oauthlib.flow import InstalledAppFlow
  flow = InstalledAppFlow.from_client_secrets_file(
      'client_secrets.json', ['https://www.googleapis.com/auth/drive.file'])
  creds = flow.run_local_server(port=0)
  print(creds.to_json())
  "
  ```
- Copy the printed JSON — that's your `GOOGLE_OAUTH_TOKEN_JSON`.

### 6. Add secrets to GitHub
In your forked repo: **Settings → Secrets and variables → Actions → New repository secret**. Add five:

| Name | Value |
|---|---|
| `GEMINI_API_KEY` | from step 2 |
| `GMAIL_USER` | your Gmail address |
| `GMAIL_APP_PASSWORD` | from step 3 |
| `DRIVE_FOLDER_ID` | from step 4 |
| `GOOGLE_OAUTH_TOKEN_JSON` | the full JSON from step 5 |

### 7. Edit your profile
Open `config/sidak_profile.json` and:
- Fill in your real LinkedIn / GitHub / LeetCode URLs (currently placeholders)
- Verify everything else is accurate — the bot will NEVER claim anything outside this file.

### 8. Test it
- Go to **Actions** tab → "Daily Job Bot" → "Run workflow"
- Wait ~5 minutes
- Check your Gmail. You should get a digest.

If it works, you're done. It now runs every day at 7am IST automatically.

---

## How to update your profile
When you add a new project or job, just edit `config/sidak_profile.json` and push. Next morning's run uses the new info.

## How to change target roles or locations
Edit `SEARCH_TERMS` and `LOCATIONS` at the top of `src/main.py`.

## How to change the ATS threshold
Default is 60. Edit `MIN_ATS_SCORE` in `src/main.py`. Lower = more applications, more noise.

## Cost monitoring
- GitHub Actions free tier: 2,000 minutes/month. This uses ~30 min/day = 900/month. Safe.
- Gemini free tier: 1,500 requests/day. We cap at ~100 jobs/day. Safe.
- Google Drive: 15GB free. Each PDF ~80KB. ~180,000 PDFs before you fill it. Safe.

## When something breaks
- **No jobs found**: LinkedIn probably blocked the scraper. Naukri+Indeed should still work — check the logs in Actions.
- **Tectonic fails**: usually a LaTeX special character in a JD-derived field. The escape function handles most; if a new one slips through, log the failing TeX and add it to `latex_escape()`.
- **Gemini returns invalid JSON**: temperature is already 0.3; if it persists, lower to 0.1 or add a retry.
