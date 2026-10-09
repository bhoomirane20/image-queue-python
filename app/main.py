import time

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

from . import cache, db
from .jobs import (JobError, MAX_ATTEMPTS, STATES, progress, reclaim,
                   summarise, transition, validate_box, validate_upload)
from .queue import (PROCESSING, QUEUE, RETRY, lease_key, lease_seconds, push)

app = FastAPI(title="image-queue")


HTML_DASHBOARD = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>imgq // pipeline</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600&family=Space+Grotesk:wght@400;500;600&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg: #0d0d0d;
            --surface: #141414;
            --border: #262626;
            --border-hover: #404040;
            --fg: #e5e5e5;
            --muted: #737373;
            --accent: #ffffff;
            --code-font: 'JetBrains Mono', monospace;
            --sans-font: 'Space Grotesk', -apple-system, BlinkMacSystemFont, sans-serif;
        }

        * { box-sizing: border-box; margin: 0; padding: 0; }

        body {
            background-color: var(--bg);
            color: var(--fg);
            font-family: var(--sans-font);
            font-size: 13px;
            letter-spacing: -0.01em;
            line-height: 1.5;
            min-height: 100vh;
            padding: 2.5rem 1.5rem;
            display: flex;
            justify-content: center;
        }

        .layout {
            width: 100%;
            max-width: 860px;
            display: flex;
            flex-direction: column;
            gap: 2rem;
        }

        header {
            display: flex;
            justify-content: space-between;
            align-items: baseline;
            border-bottom: 1px solid var(--border);
            padding-bottom: 1rem;
        }

        .sys-title {
            font-family: var(--code-font);
            font-size: 0.9rem;
            font-weight: 600;
            text-transform: lowercase;
        }

        .sys-title span { color: var(--muted); }

        .sys-meta {
            font-family: var(--code-font);
            font-size: 0.75rem;
            color: var(--muted);
            display: flex;
            gap: 1.25rem;
        }

        .sys-meta a {
            color: var(--muted);
            text-decoration: none;
            border-bottom: 1px dotted var(--muted);
        }

        .sys-meta a:hover { color: var(--fg); border-color: var(--fg); }

        /* Metric Ticker Bar */
        .metrics-bar {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            border: 1px solid var(--border);
            background: var(--surface);
        }

        .metric-cell {
            padding: 0.75rem 1rem;
            border-right: 1px solid var(--border);
        }

        .metric-cell:last-child { border-right: none; }

        .metric-label {
            font-family: var(--code-font);
            font-size: 0.65rem;
            text-transform: uppercase;
            color: var(--muted);
            margin-bottom: 0.2rem;
        }

        .metric-val {
            font-family: var(--code-font);
            font-size: 1.25rem;
            font-weight: 500;
        }

        .section-header {
            font-family: var(--code-font);
            font-size: 0.75rem;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            color: var(--muted);
            margin-bottom: 0.75rem;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        /* Upload Area */
        .upload-box {
            background: var(--surface);
            border: 1px solid var(--border);
            padding: 1.5rem;
            display: flex;
            flex-direction: column;
            gap: 1.25rem;
        }

        .drop-target {
            border: 1px dashed var(--border-hover);
            padding: 2rem 1rem;
            text-align: center;
            cursor: pointer;
            transition: border-color 0.15s ease, background 0.15s ease;
            font-family: var(--code-font);
            font-size: 0.8rem;
            color: var(--muted);
        }

        .drop-target:hover, .drop-target.dragover {
            border-color: var(--fg);
            color: var(--fg);
            background: #1a1a1a;
        }

        .controls-row {
            display: flex;
            gap: 1rem;
            align-items: flex-end;
        }

        .field-group {
            display: flex;
            flex-direction: column;
            gap: 0.35rem;
            flex: 1;
        }

        label {
            font-family: var(--code-font);
            font-size: 0.7rem;
            color: var(--muted);
        }

        input[type="number"] {
            background: var(--bg);
            border: 1px solid var(--border);
            color: var(--fg);
            font-family: var(--code-font);
            font-size: 0.85rem;
            padding: 0.5rem 0.75rem;
            width: 100%;
            outline: none;
        }

        input[type="number"]:focus {
            border-color: var(--fg);
        }

        .btn-submit {
            background: var(--fg);
            color: var(--bg);
            border: 1px solid var(--fg);
            font-family: var(--code-font);
            font-size: 0.8rem;
            font-weight: 600;
            padding: 0.55rem 1.5rem;
            cursor: pointer;
            text-transform: lowercase;
            height: 33px;
            transition: background 0.15s ease, color 0.15s ease;
        }

        .btn-submit:hover {
            background: #ffffff;
            color: #000000;
        }

        /* Queue Table */
        .table-container {
            border: 1px solid var(--border);
            background: var(--surface);
            overflow-x: auto;
        }

        table {
            width: 100%;
            border-collapse: collapse;
            font-family: var(--code-font);
            font-size: 0.75rem;
            text-align: left;
        }

        th {
            border-bottom: 1px solid var(--border);
            color: var(--muted);
            font-weight: 400;
            padding: 0.6rem 0.85rem;
            text-transform: uppercase;
            font-size: 0.65rem;
        }

        td {
            padding: 0.65rem 0.85rem;
            border-bottom: 1px solid var(--border);
            color: var(--fg);
            white-space: nowrap;
        }

        tr:last-child td { border-bottom: none; }

        tr:hover td { background: rgba(255, 255, 255, 0.02); }

        .tag {
            display: inline-block;
            padding: 0.1rem 0.4rem;
            border: 1px solid var(--border);
            font-size: 0.65rem;
            text-transform: lowercase;
        }

        .tag.done { border-color: #525252; color: #a3a3a3; }
        .tag.processing { border-color: #d4d4d4; color: #ffffff; background: #262626; }
        .tag.queued { border-color: #404040; color: #737373; }
        .tag.failed, .tag.dead { border-color: #7f1d1d; color: #f87171; }

        .action-link {
            color: var(--fg);
            text-decoration: none;
            border-bottom: 1px solid var(--border-hover);
        }

        .action-link:hover {
            border-color: var(--fg);
        }
    </style>
</head>
<body>
    <div class="layout">
        <header>
            <div class="sys-title">imgq <span>// async pipeline</span></div>
            <div class="sys-meta">
                <span>host: 0.0.0.0:8080</span>
                <a href="/docs" target="_blank">openapi spec</a>
            </div>
        </header>

        <div class="metrics-bar">
            <div class="metric-cell">
                <div class="metric-label">queued</div>
                <div class="metric-val" id="stat-queued">0</div>
            </div>
            <div class="metric-cell">
                <div class="metric-label">in_flight</div>
                <div class="metric-val" id="stat-processing">0</div>
            </div>
            <div class="metric-cell">
                <div class="metric-label">completed</div>
                <div class="metric-val" id="stat-done">0</div>
            </div>
            <div class="metric-cell">
                <div class="metric-label">failed</div>
                <div class="metric-val" id="stat-failed">0</div>
            </div>
        </div>

        <div>
            <div class="section-header">
                <span>01. dispatch_job</span>
            </div>
            <div class="upload-box">
                <div class="drop-target" id="drop-zone">
                    <span id="drop-text">[+] select or drop image payload</span>
                    <input type="file" id="file-input" accept="image/*" style="display: none;">
                </div>
                <div class="controls-row">
                    <div class="field-group">
                        <label>box_width (px)</label>
                        <input type="number" id="width" value="200">
                    </div>
                    <div class="field-group">
                        <label>box_height (px)</label>
                        <input type="number" id="height" value="200">
                    </div>
                    <button class="btn-submit" id="upload-btn">submit_job</button>
                </div>
            </div>
        </div>

        <div>
            <div class="section-header">
                <span>02. pipeline_ledger</span>
                <span id="sync-status" style="font-size: 0.65rem; color: var(--muted);">poll: 2000ms</span>
            </div>
            <div class="table-container">
                <table>
                    <thead>
                        <tr>
                            <th>id</th>
                            <th>filename</th>
                            <th>target_box</th>
                            <th>state</th>
                            <th>output</th>
                        </tr>
                    </thead>
                    <tbody id="job-rows">
                        <tr><td colspan="5" style="color: var(--muted);">loading ledger...</td></tr>
                    </tbody>
                </table>
            </div>
        </div>
    </div>

    <script>
        const dropZone = document.getElementById('drop-zone');
        const fileInput = document.getElementById('file-input');
        const dropText = document.getElementById('drop-text');
        const uploadBtn = document.getElementById('upload-btn');
        let selectedFile = null;

        dropZone.addEventListener('click', () => fileInput.click());
        dropZone.addEventListener('dragover', (e) => { e.preventDefault(); dropZone.classList.add('dragover'); });
        dropZone.addEventListener('dragleave', () => dropZone.classList.remove('dragover'));
        dropZone.addEventListener('drop', (e) => {
            e.preventDefault();
            dropZone.classList.remove('dragover');
            if (e.dataTransfer.files.length) {
                selectedFile = e.dataTransfer.files[0];
                dropText.textContent = selectedFile.name + ' (' + selectedFile.size + ' B)';
            }
        });
        fileInput.addEventListener('change', (e) => {
            if (e.target.files.length) {
                selectedFile = e.target.files[0];
                dropText.textContent = selectedFile.name + ' (' + selectedFile.size + ' B)';
            }
        });

        uploadBtn.addEventListener('click', async () => {
            if (!selectedFile) return alert('error: missing file binary');
            const width = document.getElementById('width').value;
            const height = document.getElementById('height').value;

            try {
                const res = await fetch(`/jobs?width=${width}&height=${height}&filename=${encodeURIComponent(selectedFile.name)}`, {
                    method: 'POST',
                    headers: { 'Content-Type': selectedFile.type || 'image/png' },
                    body: selectedFile
                });
                if (res.ok) {
                    selectedFile = null;
                    dropText.textContent = '[+] select or drop image payload';
                    fetchJobs();
                } else {
                    const err = await res.json();
                    alert('error: ' + (err.detail || 'upload failed'));
                }
            } catch (err) {
                alert('error: ' + err.message);
            }
        });

        async function fetchJobs() {
            try {
                const res = await fetch('/jobs?limit=15');
                const data = await res.json();
                
                document.getElementById('stat-queued').textContent = data.summary.queued || 0;
                document.getElementById('stat-processing').textContent = data.summary.processing || 0;
                document.getElementById('stat-done').textContent = data.summary.done || 0;
                document.getElementById('stat-failed').textContent = (data.summary.failed || 0) + (data.summary.dead || 0);

                const tbody = document.getElementById('job-rows');
                if (!data.jobs.length) {
                    tbody.innerHTML = '<tr><td colspan="5" style="color: var(--muted);">[ledger empty]</td></tr>';
                    return;
                }

                tbody.innerHTML = data.jobs.map(j => `
                    <tr>
                        <td style="color: var(--muted);">#${j.job_id}</td>
                        <td>${j.filename}</td>
                        <td>${j.requested_box.width}×${j.requested_box.height}</td>
                        <td><span class="tag ${j.state}">${j.state}</span></td>
                        <td>
                            ${j.result_available 
                                ? `<a href="/jobs/${j.job_id}/result" target="_blank" class="action-link">download_result</a>` 
                                : '<span style="color: var(--muted);">-</span>'}
                        </td>
                    </tr>
                `).join('');
            } catch (err) {
                console.error(err);
            }
        }

        fetchJobs();
        setInterval(fetchJobs, 2000);
    </script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def index():
    return HTML_DASHBOARD


@app.get("/health")
def health():
    out = {"status": "ok", "postgres": False, "redis": False}
    try:
        db.query("SELECT 1")
        out["postgres"] = True
    except Exception as e:
        out["pg_error"] = str(e)
    try:
        cache.client().ping()
        out["redis"] = True
    except Exception as e:
        out["redis_error"] = str(e)
    return out if out["postgres"] and out["redis"] else JSONResponse(out, status_code=503)


def _job(job_id):
    row = db.one("SELECT id, state, attempts, filename, content_type, src_bytes,"
                 " src_width, src_height, box_width, box_height, out_width,"
                 " out_height, last_error, leased_at, created_at, updated_at,"
                 " (out_data IS NOT NULL) AS has_result"
                 " FROM jobs WHERE id=%s", (job_id,))
    if not row:
        raise HTTPException(404, "no such job")
    return row


def _public(row):
    return {"job_id": row["id"], "state": row["state"],
            "progress": progress(row["state"]),
            "attempts": row["attempts"], "max_attempts": MAX_ATTEMPTS,
            "filename": row["filename"], "size_bytes": row["src_bytes"],
            "source": {"width": row["src_width"], "height": row["src_height"]},
            "requested_box": {"width": row["box_width"], "height": row["box_height"]},
            "result": ({"width": row["out_width"], "height": row["out_height"]}
                       if row["state"] == "done" else None),
            "result_available": bool(row.get("has_result")),
            "last_error": row["last_error"],
            "created_at": row["created_at"], "updated_at": row["updated_at"]}


@app.post("/jobs", status_code=202)
async def submit(request: Request, width: int = 320, height: int = 320,
                 filename: str = "upload.bin"):
    """Takes the raw image body. Returns immediately - a worker does the work.

    Deliberately not a multipart form: that would need python-multipart, and
    `curl --data-binary @photo.png` is a simpler thing to explain.
    """
    body = await request.body()
    try:
        size = validate_upload(len(body), request.headers.get("content-type"))
        box_w, box_h = validate_box(width, height)
    except JobError as e:
        raise HTTPException(400, str(e))

    row = db.one(
        "INSERT INTO jobs (state, filename, content_type, src_bytes, src_data,"
        " box_width, box_height) VALUES ('queued',%s,%s,%s,%s,%s,%s) RETURNING id",
        (filename, request.headers.get("content-type", ""), size, body, box_w, box_h))
    push(row["id"])
    return {"job_id": row["id"], "state": "queued", "poll": f"/jobs/{row['id']}",
            "size_bytes": size, "box": {"width": box_w, "height": box_h}}


@app.get("/jobs")
def list_jobs(state: str = "", limit: int = 50):
    if state and state not in STATES:
        raise HTTPException(400, f"state must be one of {', '.join(STATES)}")
    if not 1 <= limit <= 500:
        raise HTTPException(400, "limit must be between 1 and 500")
    cols = ("SELECT id, state, attempts, filename, src_bytes, src_width,"
            " src_height, out_width, out_height, last_error, leased_at,"
            " created_at, updated_at, content_type, box_width, box_height,"
            " (out_data IS NOT NULL) AS has_result FROM jobs")
    if state:
        rows = db.query(cols + " WHERE state=%s ORDER BY id DESC LIMIT %s",
                        (state, limit))
    else:
        rows = db.query(cols + " ORDER BY id DESC LIMIT %s", (limit,))
    c = cache.client()
    return {"jobs": [_public(r) for r in rows],
            "summary": summarise(db.query("SELECT state FROM jobs")),
            "queue": {"waiting": c.llen(QUEUE), "in_flight": c.llen(PROCESSING),
                      "waiting_on_backoff": c.zcard(RETRY)}}


@app.get("/jobs/{job_id}")
def show(job_id: int):
    row = _job(job_id)
    out = _public(row)
    if row["state"] == "processing":
        ttl = cache.client().ttl(lease_key(job_id))
        out["lease_expires_in"] = ttl if ttl and ttl > 0 else 0
    if row["state"] == "done" and row["has_result"]:
        out["result_url"] = f"/jobs/{job_id}/result"
    return out


@app.get("/jobs/{job_id}/result")
def result(job_id: int):
    row = db.one("SELECT state, out_data, out_width, out_height FROM jobs WHERE id=%s",
                 (job_id,))
    if not row:
        raise HTTPException(404, "no such job")
    if row["state"] != "done":
        raise HTTPException(409, f"that job is {row['state']}, not done")
    if row["out_data"] is None:
        raise HTTPException(409, "that job has no stored image")
    return Response(bytes(row["out_data"]), media_type="image/png", headers={
        "X-Image-Width": str(row["out_width"]),
        "X-Image-Height": str(row["out_height"])})


@app.post("/jobs/{job_id}/retry")
def retry(job_id: int):
    """Push a failed job back onto the queue by hand.

    `done` and `dead` are terminal, so this answers 409 for them. If you think
    a dead job deserves another go, that is a new job.
    """
    row = _job(job_id)
    try:
        transition(row["state"], "queued")
    except JobError as e:
        raise HTTPException(409, str(e))
    db.query("UPDATE jobs SET state='queued', last_error=NULL, leased_at=NULL,"
             " updated_at=now() WHERE id=%s", (job_id,), fetch=False)
    cache.client().zrem(RETRY, str(job_id))
    push(job_id)
    return {"job_id": job_id, "state": "queued", "attempts": row["attempts"]}


@app.post("/admin/reap")
def reap():
    """Put back anything a dead worker was holding.

    The worker runs this on a loop too; it is exposed so you can watch it
    work. Nothing here trusts the worker to have cleaned up after itself.
    """
    c = cache.client()
    now = time.time()
    actions = []

    for raw in c.lrange(PROCESSING, 0, -1):
        job_id = int(raw)
        row = db.one("SELECT id, state, attempts, leased_at FROM jobs WHERE id=%s",
                     (job_id,))
        if not row:
            c.lrem(PROCESSING, 0, raw)
            actions.append({"job_id": job_id, "action": "dropped",
                            "reason": "no such job"})
            continue
        alive = c.exists(lease_key(job_id))
        leased = None if alive else (row["leased_at"].timestamp()
                                     if row["leased_at"] else None)
        if alive:
            decision = {"action": "leave", "reason": "lease is still live"}
        else:
            decision = reclaim({"state": row["state"], "attempts": row["attempts"],
                                "leased_at": leased}, now, lease_seconds=0.0001)
        if decision["action"] == "leave":
            continue
        # Claim the entry. If somebody else (the worker's own reaper) removed
        # it first, it is theirs to requeue - doing it twice runs the job twice.
        if c.lrem(PROCESSING, 1, raw) != 1:
            continue
        if decision["action"] == "requeue":
            db.query("UPDATE jobs SET state='queued', attempts=%s, leased_at=NULL,"
                     " last_error='worker died holding this job', updated_at=now()"
                     " WHERE id=%s", (decision["attempts"], job_id), fetch=False)
            push(job_id)
        else:
            db.query("UPDATE jobs SET state='dead', attempts=%s, leased_at=NULL,"
                     " last_error='worker died and the job is out of attempts',"
                     " updated_at=now() WHERE id=%s", (decision["attempts"], job_id),
                     fetch=False)
        actions.append({"job_id": job_id, **decision})

    due = c.zrangebyscore(RETRY, 0, now)
    for raw in due:
        c.zrem(RETRY, raw)
        job_id = int(raw)
        row = db.one("SELECT state FROM jobs WHERE id=%s", (job_id,))
        if not row or row["state"] != "failed":
            continue
        db.query("UPDATE jobs SET state='queued', updated_at=now() WHERE id=%s",
                 (job_id,), fetch=False)
        push(job_id)
        actions.append({"job_id": job_id, "action": "backoff_elapsed"})

    return {"checked_in_flight": c.llen(PROCESSING) + len(actions),
            "actions": actions, "lease_seconds": lease_seconds()}


@app.get("/stats")
def stats():
    c = cache.client()
    return {"summary": summarise(db.query("SELECT state FROM jobs")),
            "queue": {"waiting": c.llen(QUEUE), "in_flight": c.llen(PROCESSING),
                      "waiting_on_backoff": c.zcard(RETRY)},
            "slowest": db.query(
                "SELECT id, filename, state,"
                " extract(epoch FROM updated_at - created_at) AS seconds"
                " FROM jobs WHERE state='done' ORDER BY seconds DESC LIMIT 5")}
