# YouTube Batch Transcript Extractor

A lightweight online app that takes **many YouTube links at once** and gives back clean transcripts in
**Hindi (Unicode Devanagari)**, **English**, the **original language**, or **bilingual Hindi + English** —
as **TXT, Word (DOCX) and PDF**, individually, combined, or all in one **ZIP**.

It is a Python + Streamlit app, kept on **GitHub** and hosted free on **Streamlit Community Cloud**.
Nothing has to be installed on your own computer.

---

## What it does

Four simple steps: **Add Links → Settings → Process → Download**

| Step | What happens |
|---|---|
| 1. Add Links | Paste up to 50 links (normal, `youtu.be`, Shorts, live, embed) or upload a .txt/.csv list. Duplicates and non-YouTube links are removed. |
| 2. Settings | Choose Hindi / English / Original / Bilingual, file types, time stamps. *Advanced options*: translation engine, auto-generated captions, paragraph length, pause between videos, optional speech-to-text. |
| 3. Process | Videos are processed one by one with a progress bar. If one fails, the rest continue. Each failure gets a plain-language reason. Preview and **correct** every transcript before download. Failed videos can be retried. |
| 4. Download | One ZIP with individual files + combined files + a processing report, or download single files. |

### How the text is obtained (in this order)
1. Captions uploaded by the video owner in the chosen language.
2. YouTube's auto-generated captions in that language (can be switched off).
3. YouTube's own translation of the original captions.
4. Google Translate (free web service, via `deep-translator`).
5. *Optional, off by default:* speech-to-text (yt-dlp + faster-whisper) for videos with no captions.

Existing caption tracks are used by default because they are fast, light and reliable on free hosting.
Translated text is clearly marked as **machine translated** in the preview and in every file.

### Honest limits
- **Not every YouTube video can be transcribed.** Videos without captions, private, members-only,
  age-restricted or region-locked videos will fail with an explanation.
- **YouTube sometimes blocks cloud servers.** Free cloud hosting shares internet addresses that YouTube
  may block. The app detects this, stops hammering YouTube after 3 blocked videos in a row, and tells you.
  Fixes: retry later, run the app locally, or add a proxy in Secrets (see below).
- Playlists are not expanded — paste individual video links.
- Romanised Hindi captions ("Hinglish" in English letters) are flagged; they are not auto-converted to Devanagari.
- Mixed Hindi-English speech is kept as YouTube captioned it; English words may appear in Devanagari.

---

## Project structure

```
youtube-transcript-extractor/
├── streamlit_app.py            # the app (4-step interface)
├── requirements.txt            # Python packages for Streamlit Cloud
├── requirements-dev.txt        # + test tools
├── requirements-stt.txt        # optional speech-to-text packages (heavy)
├── packages.txt                # system package (extra Devanagari fonts)
├── README.md
├── .gitignore                  # keeps secrets & user files out of GitHub
├── .streamlit/
│   ├── config.toml             # theme + server settings
│   └── secrets.toml.example    # template only — real secrets go in Streamlit Cloud
├── modules/
│   ├── youtube_handler.py      # link parsing, video titles
│   ├── transcript_extractor.py # caption tracks, errors, proxy, optional speech-to-text
│   ├── language_processor.py   # Unicode clean-up, script detection, translation
│   ├── transcript_formatter.py # paragraphs, time stamps, editable text, bilingual pairing
│   ├── export_manager.py       # TXT / DOCX / PDF / ZIP (in memory)
│   └── batch_processor.py      # one-by-one processing that never stops on a failure
├── assets/
│   ├── style.css               # My Application Style
│   └── fonts/                  # Noto Sans Devanagari (SIL Open Font License) for Hindi PDFs
└── tests/
    ├── conftest.py             # fake YouTube API (tests need no internet)
    ├── test_transcripts.py
    ├── test_exports.py
    └── test_app_smoke.py       # clicks through all 4 steps of the real app
```

### Hindi Unicode output
- **TXT**: UTF-8.
- **DOCX**: Devanagari uses the complex-script font *Nirmala UI* (built into Windows 8+; Word substitutes
  Mangal/Kokila on older systems).
- **PDF**: the bundled *Noto Sans Devanagari* font is embedded and HarfBuzz text shaping is used, so
  conjuncts (क्ष, श्र, द्व), matras and reph render correctly.

---

## Deploy online — step by step (no coding needed)

**Step 1 — Create the repository.** Sign in at <https://github.com>, click **New repository**, name it
`youtube-transcript-extractor`, choose **Public** (or Private), and click **Create repository**.

**Step 2 — Upload the files.** Unzip the project on your computer. In the new repository click
**Add file → Upload files**, then drag in **the contents of the unzipped folder** (`streamlit_app.py`,
`requirements.txt`, `modules`, `assets`, `tests`, `.streamlit`, …). Click **Commit changes**.
> Hidden items: `.gitignore` and the `.streamlit` folder start with a dot. On Mac press
> `Cmd + Shift + .` in Finder to see them; on Windows enable *View → Hidden items*. Make sure
> `.streamlit/config.toml` is uploaded (it holds the theme).

**Step 3 — Open Streamlit Cloud:** <https://share.streamlit.io>

**Step 4 — Sign in with GitHub** and allow Streamlit to access your repositories (for a private repo,
grant access to it).

**Step 5 — Create the app.** Click **Create app → Deploy a public app from GitHub** (wording may vary),
then pick repository `youtube-transcript-extractor` and branch `main`.

**Step 6 — Entrypoint:** set *Main file path* to `streamlit_app.py`.

**Step 7 — Dependencies and secrets.** Open **Advanced settings**:
- *Python version*: choose **3.12** (recommended).
- *Secrets*: paste anything you need from `.streamlit/secrets.toml.example` (all optional).
  Dependencies are installed automatically from `requirements.txt` and `packages.txt`.
- Optionally choose a custom app URL, e.g. `my-transcripts`.

**Step 8 — Click Deploy.** The first build takes 2–5 minutes.

**Step 9 — Open the generated URL** (like `https://my-transcripts.streamlit.app`). Bookmark it — it works
on phone and desktop.

**Step 10 — Test it** with the checklist below.

### Test checklist after deployment
1. **Hindi Unicode**: paste a Hindi news/talk video that shows the **CC** button → choose *Hindi* →
   Process → open the PDF and DOCX; Devanagari should read correctly (try words like क्षेत्र, श्रृंखला).
2. **English**: same video → choose *English* → the source line should say *machine translated*.
3. **Bilingual**: choose *Bilingual* → the DOCX shows a 3-column table (Time | हिंदी | English).
4. **Multiple links**: paste 5 links including one bad link and one video without captions → the others
   still finish; the bad ones show a reason.
5. **Correction**: edit a word in the preview → download → the edit appears in the files.
6. **ZIP**: Download all as ZIP → contains `Individual/…`, `All_Transcripts.*`, `Processing_Report.txt`.
7. **Mobile**: open the URL on a phone; the stepper collapses to numbered dots.

---

## Updating the app later (through GitHub)

1. Open the repository on GitHub, click the file to change (e.g. `assets/style.css`), click the ✏️
   **pencil** icon, edit, then **Commit changes** to the `main` branch.
   To replace files, use **Add file → Upload files** with the same names.
2. Streamlit Cloud notices the commit and **redeploys automatically** within a minute or two.
   Changes to `requirements.txt` trigger a full rebuild (a few minutes).
3. If the app looks stuck: on <https://share.streamlit.io> open the app's **⋮** menu → **Reboot app**.
4. Secrets are changed in the app's **Settings → Secrets** on Streamlit Cloud — never in GitHub.

---

## Secrets (all optional)

| Key | Purpose |
|---|---|
| `APP_PASSWORD` | Simple password screen before the app opens. |
| `WEBSHARE_PROXY_USERNAME`, `WEBSHARE_PROXY_PASSWORD` | Rotating residential proxy (Webshare) when YouTube blocks the cloud server. |
| `PROXY_HTTP_URL`, `PROXY_HTTPS_URL` | Any other HTTP(S) proxy. |
| `MAX_VIDEOS_PER_BATCH` | Change the 50-video limit (keep it modest on free hosting). |
| `ENABLE_SPEECH_TO_TEXT`, `WHISPER_MODEL`, `STT_MAX_MINUTES` | Optional speech-to-text fallback. |

**Private access:** besides `APP_PASSWORD`, a Streamlit Cloud app can be made private
(app **Settings → Sharing**: only invited viewers can open it; free accounts can keep one private app).

---

## Privacy and storage

- The app runs **online**: links are sent to the server, which fetches captions from YouTube and
  (if translation is needed) sends caption text to Google Translate.
- Transcripts live only **in the memory of your browser session** on the server. Files are generated
  **in memory** for download; speech-to-text audio (if enabled) is written to a temporary folder and
  deleted immediately after use, and left-over temp folders are removed when the server restarts.
- Nothing is saved to GitHub. `.gitignore` blocks secrets, cookies and transcript files.
- **No permanent history.** Closing the tab, a long idle period, or a server restart/redeploy clears the
  results — just process the links again. Jobs are **not** resumable after a restart (within the same
  open tab, interrupted batches can be continued with *Continue with remaining*).
- If you want a permanent history, you would need external storage (for example Google Sheets/Drive,
  Supabase or an S3 bucket) with its keys in Secrets — the server's own disk is wiped on every restart,
  so local SQLite or temp files cannot be used for that.

---

## Optional speech-to-text fallback

Off by default because free hosting has ~1 GB memory and YouTube often blocks audio downloads from cloud
servers. To try it:
1. Copy the two lines from `requirements-stt.txt` into `requirements.txt` and commit.
2. Add `ENABLE_SPEECH_TO_TEXT = "true"` in Secrets (optionally `WHISPER_MODEL = "tiny"`).
3. In the app: *Advanced options* → tick speech-to-text and confirm you have permission to process the audio.

Only videos up to `STT_MAX_MINUTES` (default 20) are attempted. If unavailable, the option is greyed out
with the reason shown. Use it only for audio you are permitted to download and process.

---

## Run on your own computer (optional)

Useful if YouTube blocks the cloud server.

```bash
git clone https://github.com/<your-username>/youtube-transcript-extractor.git
cd youtube-transcript-extractor
python -m venv .venv
# Windows: .venv\Scripts\activate     macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
streamlit run streamlit_app.py
```

## Run the tests

```bash
pip install -r requirements-dev.txt
pytest -q
```
The tests use a fake YouTube API, so they run without internet. They cover link parsing, Hindi Unicode
normalisation, all four language modes, translation fallbacks, error explanations, batch continuation,
TXT/DOCX/PDF/ZIP export with Hindi text, and a full click-through of the app.

---

## Troubleshooting

| Message | Meaning / fix |
|---|---|
| *YouTube is blocking requests from this server* | Cloud IP blocked. Retry later, add a proxy secret, or run locally. |
| *Captions are turned off* | The owner disabled captions. Only speech-to-text could help. |
| *No Hindi captions … translation is switched off* | Set *Translation* to *Automatic* in Advanced options. |
| *Translation into Hindi failed* | Google Translate rate-limited the server. Retry the failed videos after a minute. |
| *Age-restricted / private / unavailable* | YouTube does not serve captions for these without sign-in. |
| PDF takes a while | Long videos (1–2 h) need a few seconds each to lay out Hindi text correctly. |

## Credits and licences
- Transcripts: [youtube-transcript-api](https://github.com/jdepoix/youtube-transcript-api).
- Translation: [deep-translator](https://github.com/nidhaloff/deep-translator).
- Font: Noto Sans Devanagari, © The Noto Project Authors, SIL Open Font License 1.1 (`assets/fonts/OFL.txt`).
- Use transcripts in line with YouTube's Terms of Service and the rights of the video owners.
