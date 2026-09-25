<div align="center">

<img src="logo.ico" alt="StudyNotes Logo" width="120" />

# StudyNotes — Automatic Lecture Recording, Transcription & Summarization

**A desktop app with a GUI that records any lecture or meeting's audio, transcribes it to text, and turns it into organized Markdown notes with rendered math equations — all with zero manual work.**

[![Version](https://img.shields.io/badge/version-1.5.0-blue)](../../releases/latest)
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Tkinter](https://img.shields.io/badge/GUI-Tkinter-4c4ddc?logo=python&logoColor=white)](https://docs.python.org/3/library/tkinter.html)
[![Gemini](https://img.shields.io/badge/AI-Gemini-8E75B2?logo=googlegemini&logoColor=white)](https://ai.google.dev/)
[![Groq](https://img.shields.io/badge/AI-Groq-F55036?logo=groq&logoColor=white)](https://groq.com/)
[![Whisper](https://img.shields.io/badge/STT-Whisper--large--v3-1f8a4c)](https://github.com/openai/whisper)

</div>

---

## What does this project do?

- Records your system audio while a lecture/meeting is playing — automatically split into chunks every 30 minutes, so it stays safe whether the session is short or hours long.
- Every recorded chunk is automatically compressed to **Opus** (if `ffmpeg` is installed) to save space, without interrupting the ongoing recording.
- When you're done, one click transcribes the audio to text (Groq Whisper first for speed, Gemini as a fallback), then turns that text into focused notes written in a lecturer's style (Gemini first, Groq as a fallback, with NVIDIA available as an optional extra provider).
- Long sessions are summarized chunk by chunk, then consolidated into one coherent document using a hierarchical merge, so quality stays high even for multi-hour recordings.
- Notes are saved as a cumulative Markdown file per lecture, and rendered right inside the app — including LaTeX equations (`$...$` and `$$...$$`) converted to images via matplotlib.
- Notes can be exported to a properly formatted **PDF** (right-to-left Arabic, syntax-highlighted code, highlight boxes and rendered equations included) using the browser already installed on the user's machine (Edge or Chrome), headless — no extra dependencies.
- The packaged Windows app can check for new versions on GitHub and update itself silently, with no technical steps required from the user.
- Everything is tracked with a status (recorded / transcribed / transcribed & explained) so you can always pick up where you left off, or delete a specific piece without breaking the rest.

---

## Features

| Feature | Detail |
|---|---|
| 🎙️ **System audio recording** | No external microphone needed, automatically split every 30 minutes, with pause/resume and a live status in the window title |
| 🗜️ **Automatic audio compression** | Background conversion to Opus after each chunk, with zero recording downtime |
| 📝 **Multi-provider transcription** | Groq (`whisper-large-v3`) as the default, Gemini as a fallback on failure |
| 🧠 **AI-powered summarization** | Turns raw transcripts into organized, lecturer-style notes (or structured meeting minutes), processed in chunks for long texts |
| 🔀 **Hierarchical merging** | Partial notes are consolidated in small batches, then merged level by level with dedicated consolidation prompts for higher quality on long sessions |
| 📄 **PDF export** | One click turns the rendered notes into a PDF via a headless local browser (Edge/Chrome) — same look as in-app (RTL Arabic, code highlighting, callout boxes, equations) |
| 🔄 **Self-updating** | The installed app checks GitHub Releases for a newer version and installs it silently in the background when you approve |
| 💬 **In-app feedback** | Rate the app and report ideas/problems from a built-in window, with offline queueing so nothing is lost |
| 🎓 **Subject-aware notes** | Pick a field per lecture (Engineering, Medicine, Law, Business, Languages, Math & Sciences, Humanities & History, or your own) to tailor the summarization prompt, plus optional 🔧 corrections and 💬 additions callouts |
| 🛡️ **Rate-limit resilience** | Automatic retries with backoff, request pacing, adaptive splitting of oversized chunks, and fallback between providers |
| ➗ **Math equation rendering** | `$...$` and `$$...$$` rendered as PNG images inside the app via matplotlib mathtext |
| 🗂️ **Per-lecture state tracking** | Tracks which audio chunks are transcribed/explained, with the ability to undo the last notes update |
| 🧹 **Selective deletion** | Delete audio/transcript/notes for a whole lecture, or a single file, without affecting the rest (with a typed-name confirmation) |
| 🖥️ **Bilingual GUI (Arabic/English)** | Built with Tkinter, with correct rendering of Arabic and mixed-language text |
| 📦 **Packaged as a real Windows installer** | Built with PyInstaller, then wrapped into a proper Setup.exe installer with Inno Setup |

---

## Releases & Changelog

Newest first. Every version is available on the [Releases page](../../releases).

### v1.5.0 — 2026-09-24

- 🐛 **Fixed PDF export failing in some cases:** exporting to PDF could raise `WinError 32` while cleaning up the temporary browser profile folder used for the headless Edge/Chrome print step — a leftover Chromium subprocess could still be holding a lock on a profile file for a moment after the browser was closed, even though the PDF itself had already been generated successfully. Cleanup now retries a few times instead of failing the whole export

### v1.4.0 — 2026-09-24

- 💬 **In-app feedback:** a new **"قيّم البرنامج"** button next to *Models* opens a rating window where you can leave your name (optional), a 1-5 star rating, what you think of the app, what you'd like added or changed, and any problems you found. You can also choose to attach your event log to help track down bugs
- 🛰️ **Never loses feedback:** if the server can't be reached (no internet, or the free Supabase project is paused after a quiet week), the message is saved on your computer and sent automatically the next time the app starts. No error is shown, and retries never create duplicates
- 🪶 **Zero extra dependencies:** feedback is sent with Python's built-in `urllib` (no `supabase` package), so the app and the `.exe` don't grow
- 🔀 **Hierarchical merge for the consolidation stage:** partial notes are now merged in small batches (max 4 chunks per call), then the batch results are merged together until a single final document remains, instead of pasting everything into one huge request. This noticeably improves note quality on long sessions (10+ chunks), where a very long input makes the model lose focus on details in the middle
- 🧾 **Dedicated consolidation prompts:** `CONSOLIDATION_PROMPT` for lecture notes and `MEETING_CONSOLIDATION_PROMPT` for meeting minutes (which gathers each section's items from all parts under a single heading instead of repeating it per part). Previously the merge stage reused the main summarization prompt
- 📁 **All prompts moved into a new `prompts.py`,** separating the API logic from the instruction text. Names are imported unchanged, so the rest of the code keeps working as-is
- 🛟 If merging a batch fails (e.g. API rate limit), its parts are concatenated as-is instead of losing the content
- 🔢 App version label updated to `v1.4.0`

### v1.3.0 — 2026-09-03

- 🎓 **Subject-aware summarization:** choose a field when creating a lecture (Engineering, Medicine, Law, Business, Languages, Math & Sciences, Humanities & History, or a custom one) and the notes prompt adapts to it. Existing lectures keep the original programming/computer-science behavior
- 🔧💬 **Optional callouts per lecture:** turn on corrections (🔧) and additions (💬) boxes for each lecture individually (off by default)
- 🟢 **NVIDIA as an optional summarization provider** (Nemotron), configured with `NVIDIA_API_KEY`, and suggested in the app when it could help with rate limits
- 🚀 **Redesigned first-run setup:** provider cards with logos, live API key validation, and a prompt for any missing key when you pick a provider that needs one
- 🛡️ **Rate-limit handling:** automatic retries with increasing delays on the same provider before falling back, plus pacing between chunk requests
- ✂️ **Adaptive splitting:** if a chunk is too large for the model, it is split automatically and retried
- 💾 **Progress checkpointing:** if a chunk fails after all retries, the notes finished so far are saved and the next run continues from where it stopped
- 📝 **"Notes only" mode** to convert only selected parts of a transcript into notes
- 👁️ **Transcript viewer** with copy support
- ⌨️ Copy/paste/cut/select-all shortcuts now work in input fields even with a non-English keyboard layout, plus right-click context menus
- 📦 New dependencies: `openai`, `Pillow`, `Pygments`

### v1.2.0 — 2026-08-28

- 🔢 **Sequential part numbering:** recordings are named `Lecture__Part 01`, `Part 02`, ... and continue from the highest existing number, so stopping and recording again later never overwrites or duplicates old parts
- 🔴 **Live status in the window title:** shows Recording / Paused while you record
- 🩹 The file currently being recorded is no longer listed as "corrupted" while it is still open
- 🧾 Transcript files now include a clear separator header before each part, showing which audio part the text came from
- 💻 Any code or command mentioned in a lecture or meeting is now always written inside a proper fenced Markdown code block, with the language specified
- 🎨 Refreshed delete-confirmation dialogs and notes viewer with colored headers and cleaner layout

### v1.1.0 — 2026-08-25

- 🐛 Fixed scrambled text order in the event log when Arabic and English mix (device names, error codes, arrows) — now uses Unicode Bidi Isolates instead of running bidi on the whole line at once
- 📋 Button to copy the entire event log to the clipboard
- 🛑 Ability to cancel a running transcription/summarization
- ⏸ Pause the recording (take a break) without closing the current part
- 💾 Warning before recording starts if disk space is low
- 🧮 Rough token estimate before you approve processing
- 💬 Simplified error messages for the most common API errors (wrong key, rate limit exceeded, connection problems)
- 🔒 Extra confirmation (typing the lecture name) before any permanent deletion
- 🔄 Refresh button and search inside the notes viewer
- ⚠️ Warning when closing the app during a running transcription/summarization (not only during recording)
- 🚀 First-run setup window for entering API keys

### v1.0.0 — 2026-08-23

- 🎉 Initial release
- 🎙️ System audio recording, automatically split into 30-minute chunks
- 🗜️ Background Opus compression via `ffmpeg`
- 📝 Transcription with Groq Whisper and Gemini as a fallback
- 🧠 Chunked AI summarization into lecturer-style Markdown notes
- ➗ LaTeX equation rendering inside the app via matplotlib
- 🗂️ Per-lecture state tracking, undo for the last notes update, and selective deletion
- 🖥️ Bilingual Tkinter GUI

---

## Project Structure

```
.
├── system/
│   ├── gui_app.py             # Main GUI application (Tkinter)
│   ├── first_run_setup.py     # First-run setup and API key dialogs
│   ├── record_session.py      # Command-line system audio recorder (no GUI)
│   ├── process_lecture.py     # Transcription + summarization + hierarchical merge (Groq / Gemini / NVIDIA)
│   ├── prompts.py             # All AI prompts (lecture notes, meetings, consolidation)
│   ├── pdf_export.py          # Converts rendered notes to PDF via a headless local Edge/Chrome
│   ├── updater.py             # Checks GitHub Releases for a newer version and installs it silently
│   ├── evaluation.py          # In-app feedback window + Supabase sender with offline queue
│   ├── feedback_setup.sql     # One-time SQL that creates the Supabase feedback table (insert-only)
│   ├── state_manager.py       # Folder setup and shared state across scripts
│   └── math_render.py         # Converts LaTeX to PNG images rendered in the GUI
├── StudyNotes.spec            # PyInstaller build config (produces the raw .exe)
├── StudyNotes.iss             # Inno Setup script that wraps the .exe into a distributable Setup.exe installer
├── requirements.txt           # All required packages
├── .env.example                # Environment variable template
└── (created automatically at runtime)
    ├── .state/                # Per-lecture state (JSON)
    ├── Sound_Recorded/        # Audio files (Opus/FLAC)
    ├── Transcript/            # Raw transcripts (txt)
    └── Markdown/              # Final notes (md)
```

> All of the runtime folders above are created automatically the first time you run any script — no need to create them manually.

---

## Requirements

- Python 3.11+
- `ffmpeg` (optional but recommended) for compressing audio to Opus — if missing, files stay as FLAC (larger size)
- Microsoft Edge or Google Chrome installed, only if you want to use PDF export (Edge already comes with Windows 10/11)
- At least one API key from:
  - [Google Gemini](https://ai.google.dev/) → `GEMINI_API_KEY`
  - [Groq](https://console.groq.com/) → `GROQ_API_KEY`
- Optional: an [NVIDIA](https://build.nvidia.com/) key → `NVIDIA_API_KEY` (extra summarization provider)

---

## Installation

### 1. Clone the repository

```bash
git clone <your-repo-url>
cd <your-repo-folder>
```

### 2. Create and activate a virtual environment

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env`:

```env
GEMINI_API_KEY="your-gemini-api-key"
GROQ_API_KEY="your-groq-api-key"

# Optional: extra summarization provider
NVIDIA_API_KEY="your-nvidia-api-key"

# Optional: set this if you want data folders stored somewhere other than
# next to the project itself
STUDYNOTES_DIR="D:\Agoor"
```

> If `STUDYNOTES_DIR` isn't set, the folders are created automatically next to the scripts themselves.
>
> You can also skip editing `.env` by hand: the first-run setup window in the app lets you paste your keys and validates them for you.

---

## Running the App

### GUI (recommended)

```bash
python system/gui_app.py
```

From the GUI you can: pick or create a lecture (with its subject), start/pause/stop recording, run transcription and summarization, convert selected parts to notes only, track the status of each audio chunk, export notes to PDF, and view notes with rendered equations directly.

### Command-line recording (no GUI)

```bash
python system/record_session.py
```

### Manually process a specific lecture

```bash
python system/process_lecture.py "lecture name"
```

---

## Feedback

Click **💬 قيّم البرنامج** in the main window to rate the app and tell us what to improve or what went wrong. It only needs an internet connection at that moment: if it's not available, your message is stored locally and delivered automatically later.

**Running your own copy?** Feedback is stored in a [Supabase](https://supabase.com/) table. To collect it in your own project:

1. Run `system/feedback_setup.sql` once in the Supabase SQL Editor. It creates the `feedback` table with Row Level Security so the public key can **insert only** (nobody can read, edit or delete rows with it).
2. Put your project URL and **anon (public)** key in `SUPABASE_URL` / `SUPABASE_ANON_KEY` at the top of `system/evaluation.py`. Never use the `service_role` key there.
3. While testing, set `DEBUG_SHOW_ERRORS = True` in `evaluation.py` to see why a send failed (e.g. `HTTP 401` = wrong key, `HTTP 404` = table missing).

---

## Building a Windows Installer (optional)

Building a distributable `Setup.exe` is a two-step process:

### 1. Build the raw executable with PyInstaller

```bash
pyinstaller StudyNotes.spec
```

This produces `dist/StudyNotes/StudyNotes.exe` — a working but "unpackaged" build (a folder of files, not something you'd hand to another user).

### 2. Wrap it into a real installer with Inno Setup

Open `StudyNotes.iss` in [Inno Setup Compiler](https://jrsoftware.org/isinfo.php) and compile it (`Build > Compile`, or `Ctrl+F9`). This reads the output of step 1 and produces a single `StudyNotes_Setup.exe` in the `Output/` folder — this is the file meant to be shared or attached to a GitHub Release, since it installs the app properly (Start Menu shortcut, uninstaller entry, etc.) instead of just unzipping a folder.

### Self-update mechanism

Once installed, the app checks `GITHUB_OWNER`/`GITHUB_REPO` (set at the top of `updater.py`) for a newer GitHub Release tag than its own `APP_VERSION` (in `gui_app.py`). If a release has a `.exe` asset attached, the app offers to download and install it silently (Inno Setup's `/VERYSILENT` flags), closing itself right before the new installer runs. Publishing a new version is just: bump `APP_VERSION`, rebuild both steps above, and publish a GitHub Release tagged with the same version number with `StudyNotes_Setup.exe` attached as an asset.

---

## Security Notes

- `.env` is excluded from version control — already covered by `.gitignore`.
- Data folders (`.state`, `Sound_Recorded`, `Transcript`, `Markdown`) are also excluded in `.gitignore` so personal lecture content never gets committed by accident.
- Transcription and summarization send audio/text content to third-party providers (Gemini / Groq / NVIDIA) — don't use this project with confidential content unless you've reviewed their privacy policies.
- Uninstalling the app does **not** delete your notes: recordings, transcripts and Markdown notes live under `%localappdata%\StudyNotes`, separate from the installed program files, and are left untouched by the uninstaller.

---

<div align="center">
<sub>StudyNotes · built with Python, Tkinter, Gemini &amp; Groq</sub>
</div>
