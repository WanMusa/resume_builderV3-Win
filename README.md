# Resume Builder V3 — Windows desktop

Native **Python 3.12 + PySide6 + SQLite** application. Your existing Chrome
extension captures adverts; this app discovers its JSON exports and generates
local **resume and cover-letter DOCX** files only when you press **Process**.
No server, Node, Docker, PostgreSQL, Supabase, browser automation, localhost API,
native messaging, VPS deployment, or extension changes are needed.

**Personal-test release:** offline mock mode is the default. All bundled career
data is fictional. Mock documents say **NOT FOR SUBMISSION**. Live OpenAI is
implemented but requires your own verified evidence, API key, internet access,
and human review of the final documents. There is no automatic paid generation
on discovery.

## Download and run on Windows

1. Open this repository's **Actions → Test and package Windows desktop**.
2. Select a **successful run for the PR/commit you want to test**. If needed,
   use **Run workflow** on the implementation branch after it is available.
   Builds run on pushes and pull requests without provider keys.
   If GitHub shows **action_required**, approve the workflow run as repository
   owner before expecting a Windows artifact; an unstarted run has no build logs.
3. Under **Artifacts**, download **ResumeBuilderV3-Windows**. GitHub may require
   you to sign in. Extract the artifact ZIP, then extract its contained
   `ResumeBuilderV3-Windows.zip`.
4. Keep the **entire `ResumeBuilderV3` folder**, including `_internal` and all
   supporting files. Double-click **`ResumeBuilderV3.exe`**. Do not move the EXE
   alone. No Python installation is needed for this packaged build.

This is PyInstaller **onedir**, windowed (no console), **not onefile**. No
installer, signing, UPX, obfuscation, elevation, hidden startup, or auto-update
is included. Antivirus/SmartScreen may flag an unsigned executable; this project
does not promise immunity and does not recommend exclusions or disabling
protection. Review the source/build provenance and use your normal security
process. Signing can be considered later.

Windows CI builds the actual EXE and runs a frozen resource/SQLite smoke check.
Linux source tests or a Linux frozen build **do not establish Windows desktop
compatibility**. The manual Windows checklist below remains required, including
Credential Manager, Word locks, and Explorer launch.

## Source run (PowerShell)

Install 64-bit Python **3.12**. In your existing checkout's root:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
$env:QT_QPA_PLATFORM = "offscreen"
python -m pytest -q
Remove-Item Env:QT_QPA_PLATFORM
python -m resume_builder
```

No tests contact a live provider. `pytest`, packaging and dependency versions
are declared in `pyproject.toml`; there is no separate lint tool.

To build on Windows from that environment:

```powershell
.\packaging\build-windows.ps1
```

The script builds the complete distribution, collects dependency notices,
checks frozen startup offline, and creates `dist\ResumeBuilderV3-Windows.zip`.
For a separate isolated frozen check (does not open a window or call an API):

```powershell
$check = Join-Path $env:TEMP "ResumeBuilderV3-check"
Start-Process .\dist\ResumeBuilderV3\ResumeBuilderV3.exe -ArgumentList "--smoke-check", "`"$check`"" -Wait
Get-Content (Join-Path $check "smoke-result.json")
```

Expected: `"ok": true`, resources and SQLite true, network calls zero.

## First offline demonstration

1. Create **two separate, non-overlapping folders**, e.g.
   `Documents\ResumeBuilderDemo\inbox` and `Documents\ResumeBuilderDemo\output`.
   Avoid drive roots. The output folder must not contain the inbox or vice versa.
2. Launch the app. In **Settings**, select those folders, provider **mock**,
   `resume-default`, `letter-default`, and `fictional-example`. Save settings.
3. Copy the fictional sample into the inbox. From a source checkout:

   ```powershell
   Copy-Item .\resume_builder\assets\sample-job.json "$HOME\Documents\ResumeBuilderDemo\inbox\sample-job.json"
   ```

   In the packaged distribution it is under
   `_internal\resume_builder\assets\sample-job.json`.
4. In **Jobs**, wait for two stable polling observations (normally a few
   seconds), or use **Scan now** twice. A row appears as **ready** (unprocessed).
   Inspect the full advert and metadata. No model request occurs.
5. Select the row and press **Process selected**. Observe
   queued → generating → rendering → completed. Open the artifact/output folder:
   there are two real DOCX files, visibly **NOT FOR SUBMISSION**.
6. In **Prompts**, edit and Save a prompt. In **Base Resumes**, edit a field and
   Save. These update actual local files with backups, not just the screen.
7. Close and reopen. Settings, history and library edits remain. Archive the
   completed row; it disappears unless **Include archived** is checked. Restore
   it there. Try cleanup on this fictional history only.

### Everyday workflow and Jobs tab

Configure the Chrome extension's download/Save As destination to match the
app's inbox. Chrome cannot silently write to every arbitrary folder; this
native app can write to your selected writable output folder.

The app periodically scans `.json` files, ignoring partial download suffixes
and symbolic links. A file must be stable before import. Malformed files/jobs
produce useful invalid rows; other jobs in the folder/envelope still import.
Discovery is persistent and deduplicated by source ID plus normalized content
hash, not just filename. Changed job content produces another input revision.
The app never fetches the source URL or reduces the full advert to extracted
requirements.

Search/filter rows, preview full text and timestamps, open source files or the
source website, and process selected rows. One worker runs **serially** in the
background. Duplicate enqueue is blocked; one failure does not stop later jobs.
All file-opening uses native APIs, not shell execution of captured text.

**Restart:** previous queued/in-flight work becomes **interrupted**, and nothing
is silently resumed. Select interrupted jobs and press Process explicitly.
The window warns when quitting active work; minimizing keeps it running.
Quitting does **not** guarantee cancellation of a request already received by
the provider. Timeouts are uncertain: inspect your provider usage before
manually retrying a failed call.

**Retries and revisions:** successful resume/letter calls are independently
checkpointed in SQLite before rendering. Process on a failed item reuses those
checkpoints; when both exist, rendering retry makes **zero paid calls**.
For saved attempts, the button reads **Retry selected (frozen inputs / checkpoints)**.
If the frozen output path is unusable, **Render saved outputs to folder…**
creates a render-only revision at an explicitly chosen destination, reusing both
checkpoints with no API request.
Regenerate is a separate explicit paid action producing a new revision.
Output lives in bounded, per-job/revision directories with meaningful
filenames. Existing user-edited files are never silently overwritten. If a
folder is unavailable, Word locks a file, or a Windows path is too long, fix the
folder/lock/path and retry. Generated files are tracked separately from source
files.

## Prompts and base resumes

**Prompts:** choose resume/letter type, edit, Save, or Save as new. Supported
placeholders: `{job_description}` and `{evidence}`, each at most once. If omitted,
the adapter appends that context once. Escape literal braces as `{{` and `}}`.
Technical JSON output schemas are attached by the provider, not editable prompt
content. Unsaved edits have Save/Discard/Cancel handling.

**Base Resumes:** a structured form covers identity/contact/links, career
profile, skills, roles, stable role and achievement evidence IDs, supporting
details, critical evidence, education, certifications, references and additional
fixed sections. Repeatable entries can be added, removed and reordered. Multiple
named resumes can be created/duplicated and imported/exported.

Imports accept **this app's schema-version-1 base JSON only**, validated without
discarding unknown fields. **V2 base JSON compatibility is not claimed**:
map it explicitly into this schema after reviewing every field. The bundled
`fictional-example.json` illustrates the complete supported structure.

Fixed identity, employers, titles, dates, education and certifications are
inserted by Python, never taken from model output. Only summary, expertise
selection, evidence-linked achievement wording and letter paragraphs are
generated. Unknown IDs, role/evidence mismatches, unsupported expertise and
unsupported numeric metrics are rejected; critical evidence must be retained.
These checks **cannot prove semantic truth or guarantee ATS success**.
Review every final document before submission.

Before live generation, replace the fictional data with your own real evidence,
clear **Example**, and explicitly confirm **verified** facts in the form.
User files belong in your local user-data folder, never in this public checkout.
Importing a real resume marked verified asks you to confirm its facts again;
declining leaves it unverified rather than silently approving imported evidence.

## Providers, privacy and reproducibility

- **mock:** offline, no API key; conspicuous demo content.
- **openai:** OpenAI **Chat Completions** (`/v1/chat/completions`) with strict
  `response_format.type=json_schema`. The default model is `gpt-4.1-mini`;
  your chosen model must actually support that API and structured outputs.
- **compatible (advanced):** explicitly acknowledge sending evidence/key to the
  configured trusted HTTPS endpoint. Choose strict structured output or explicit
  **JSON-object mode**; JSON-object mode is not provider schema enforcement,
  although local schema/evidence validation still runs. Endpoint compatibility
  must be tested; no Anthropic/Gemini or arbitrary-model compatibility is claimed.

There is **no silent JSON-mode downgrade** and no automatic provider retry/repair.
Refusal, incomplete output, authentication, rate limits, server failures,
unsupported schemas, bad JSON and network timeouts fail with sanitized guidance.
Requests have a bounded timeout, TLS verification, no redirect following and no
environment-proxy inheritance. No tools, browser execution, or remote actions.
Only use endpoints you trust; you are responsible for their data-handling terms.

**Test connection** is a separate explicit gesture: its tiny structured request
can cost tokens. It tests authentication/network/schema capability, not factual
quality. Saving settings does not run generation. Input budget warnings never
truncate an advert or evidence silently: explicitly permit a larger request or
edit your input/budget. Character counts are not token or price estimates.

The selected prompt **content**, base resume, provider/model, output policy and
configuration are frozen at enqueue time with hashes; later edits cannot change
queued jobs. Generated packages, usage/cached-token information when supplied,
and hashes remain in local SQLite for reproducibility. API keys are never part
of snapshots.

### Local storage and credentials

Default user data: **`%LOCALAPPDATA%\ResumeBuilderV3-Win`**, not beside the EXE.
It contains `history.sqlite3`, `settings.json`, `prompts`, `bases`, and
`history` (editable-file version backups). Bundled defaults seed missing library
files only; an upgrade does not overwrite existing edits. An isolated source
session can use `python -m resume_builder --data-dir <absolute-test-folder>`.
A lock prevents two instances using the same data folder from running together.

API-key input is masked. Persistent keys use **Windows Credential Manager**
directly through keyring's Windows backend; unsupported/unavailable secure
storage fails closed rather than falling back to plaintext. Session-only keys
are available for development and testing. Keys are bound to their configured
HTTPS endpoint: changing services never reuses another service's key, and queued
jobs retrieve credentials for their frozen endpoint. Delete-key acts on the
selected endpoint. No environment-variable export,
SQLite/plain-settings key storage, telemetry, or raw API/private-resume logging.
Settings/DB backups contain no API key, but **do contain private resume/job data**.
Generated DOCX, SQLite, prompt/base files and backups are **not encrypted by the
app**: protect your OS account, selected folders and backups accordingly.

### Archive, cleanup, reset and recovery

Archive preserves deduplication. Restore archived rows with Include archived.
Settings offers date-cutoff cleanup with a preview count and confirmation,
database backup, and strongly confirmed history reset. Cleanup/reset are
protected while queued/generating/rendering jobs exist.

Default maintenance removes **DB job history only**, not prompts, base resumes,
settings, credentials, source JSON or output files. A separate minimal import
ledger retains source IDs/content hashes, so deleted history **does not suddenly
reappear** from unchanged inbox files. Reset's **clear deduplication ledger**
option deliberately permits rediscovery; read its warning before confirming.
Generated-file deletion is intentionally not implemented; delete your own
documents in Explorer if desired. The app never recursively wipes selected
folders.

SQLite uses per-thread connections, serialized short write transactions, WAL,
busy timeout and a versioned schema. Existing databases are backed up with
SQLite's backup API before supported initialization/migration work. Unsupported
legacy/newer schemas fail without destructive recreation. If startup fails,
preserve the whole user-data folder, including WAL/SHM sidecars, while the app
is closed; do not replace the database with an empty one. Restore a known-good
backup to a separate test data folder before replacing your working copy.

## Windows acceptance checklist

Use fictional data and a fresh test directory first:

- [ ] Extract the entire artifact folder; launch the EXE from Explorer without
      Python installed, no administrator permission and no console.
- [ ] Complete the offline demo above; verify both files open in Word/LibreOffice,
      correct fixed contact/employer/date/education sections, tailored text,
      and NOT FOR SUBMISSION markings.
- [ ] Drop a malformed file, a multi-job envelope with one malformed job, and a
      `.json.crdownload`; good jobs import, bad ones explain their errors, and
      partial downloads do not import. Duplicate files do not duplicate rows.
- [ ] Select several ready jobs; no generation before Process, one at a time
      afterward, responsive UI. A failing item does not block the next.
- [ ] Edit/save prompts and all base form sections, reorder entries, restart,
      and inspect the saved local JSON/text and backup files.
- [ ] Queue jobs then edit the selected prompt/base; already queued snapshots
      remain unchanged. Quit during work only after reading the warning;
      restart shows interrupted work without automatic API calls.
- [ ] Make output unavailable; observe failure, restore it, then retry from
      generated checkpoints without another provider call. Check existing edited
      DOCX files are not overwritten; try an open Word file and a long path.
- [ ] Archive/restore, preview cleanup, back up DB and reset test history; source
      and output files survive. Confirm retained-ledger vs explicit reimport.
- [ ] With real evidence only: save/reload a key in Windows Credential Manager,
      explicitly Test connection, process one live job, inspect usage and human
      review the documents. Test invalid key/schema errors without exposing a key.
- [ ] Launch a second instance using the same data directory; it must refuse.

Automated coverage includes temp-directory intake/history, serial queue/restart,
snapshots, mocked providers, checkpoint retries, real DOCX content, form
round-trips, editable library backups, credential isolation and resource loading.
CI has no live keys and cannot certify real provider billing, factual accuracy,
Windows desktop/Word behavior, SmartScreen behavior or every compatible endpoint.

## License and source reuse

The original Apache-2.0 `LICENSE` is preserved. See `THIRD_PARTY_NOTICES.txt`
and packaged `dependency-licenses` for dependencies, including Qt/PySide6's
open-source license obligations.

Read-only references: V3 README at
`38412622a0bdfbe479494e3832e76e63ac3ea429` (bounded calls/checkpoints, not its
PostgreSQL worker); V2 `src/render_documents.py` at
`41516fe5b27824617c0a33c151c961515cfc7bed` (fixed/variable layout concepts,
no private assets copied); extension README at
`ac76624d21c08d6690df4da9b38830285aeba874` (schema-v2 capture/export contract).
No changes or deployments to those repositories or infrastructure are included.
