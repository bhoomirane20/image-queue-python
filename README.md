# Image Processing Queue

**Language:** Python (FastAPI) &nbsp;|&nbsp; **Needs:** Postgres + Redis

This is a **starter**. The application already works. Your job is everything
that gets it building, tested and running in CI.

---

## You do not need Python installed

You will build this into a container, and the container brings its own
Python 3.12. You are not being asked to extend the app — you are being asked
to ship it.

---

## 1. What this app needs

| | |
|---|---|
| **Runtime** | Python 3.12 |
| **Install dependencies** | `pip install -r requirements.txt` |
| **Start the API** | `uvicorn app.main:app --host 0.0.0.0 --port 8080` |
| **Start the worker** | `python -m app.worker` &nbsp;(same image, second container) |
| **Listens on** | port 8080, bound to `0.0.0.0` |
| **Environment variables** | `DATABASE_URL`, `REDIS_URL`; optionally `LEASE_SECONDS` (default 30) and `SLOW_MS` (default 0) on the worker |
| **Needs running first** | Postgres, Redis, and the migrations applied |

### What it does

Upload an image and get a job id back straight away. A separate worker process picks the job off a Redis queue, resizes the image to fit the box you asked for, and writes the result back. Poll the job id to see queued, processing, done, failed or dead. Failures are retried with a backoff and buried after three attempts.

### Endpoints

```
GET  /health
POST /jobs?width=320&height=320&filename=cat.png      raw image body, returns 202
GET  /jobs                        every job, a state summary and the queue depth
GET  /jobs/{id}                   state, attempts, and the lease if it is running
GET  /jobs/{id}/result            the resized PNG itself
POST /jobs/{id}/retry             requeue a failed job (done/dead are terminal)
POST /admin/reap                  reclaim anything a dead worker was holding
GET  /stats                       queue depth and the slowest completed jobs

Try it:
  curl -s --data-binary @photo.png -H 'content-type: image/png' \
       'localhost:8080/jobs?width=200&height=200&filename=photo.png'
  curl -s localhost:8080/jobs/6
  curl -s localhost:8080/jobs/6/result -o thumb.png
```

`/health` reports Postgres and Redis **separately**. If it says
`postgres: false` the app started fine and your compose wiring is wrong —
do not go looking in the application code.

### This app is two processes, not one

`web` serves the API. `worker` does the resizing. They are the **same
image** with different commands and the same environment, and the worker
publishes no ports because nothing connects to it — it talks to Postgres
and Redis only.

```yaml
worker:
  build: .
  command: ["python", "-m", "app.worker"]
```

If you forget it everything still starts, `/health` still says `ok`, and
every job you submit stays `queued` for ever. That is the single most
common way to get this one wrong.

### Migrations

`migrations/` holds `.sql` files applied **in filename order** before the app
starts. They create the tables and insert sample data. A container running
`psql` over them in order is enough; you do not need a migration tool.

---

## 2. What you must write

| File | What it has to do |
|---|---|
| `Dockerfile` | Install dependencies **before** copying source, pin the base image, do not run as root. |
| `docker-compose.yml` | API **and worker** + Postgres + Redis + a migration step, one `docker compose up`. |
| `.circleci/config.yml` | lint → unit tests → integration tests → secret scan → image build |
| Unit tests | For `app/jobs.py`. No database, no network. |
| Integration tests | Against a real Postgres and Redis as CircleCI service containers. |

Then push your image to **your own Docker Hub account**, tagged `:1.0`.

### When it works

```bash
docker compose up --build
curl localhost:8080/health
```

```json
{"status":"ok","postgres":true,"redis":true}
```

---

## Where the marks are

`app/jobs.py` is **pure logic** — plain functions over plain data, no
database and no HTTP. Start your tests there. Use pytest:
`pytest --cov=app --cov-report=term-missing`. Minimum 70%.

`reclaim` is the heart of it. Hand it a processing job with a lease that ended one second ago and assert it says requeue; hand it the same job with the attempts used up and assert it says bury; hand it a job that is already done and assert it says leave.

## Why Redis is here

Redis is the queue, not a cache - lose it and you lose work, which is the whole point of the hard part below. `imgq:queue` is a list the API RPUSHes onto and the worker takes from with BLMOVE, which moves the id onto `imgq:processing` in the same operation. `imgq:lease:{id}` is a key with a TTL written immediately afterwards. `imgq:retry` is a sorted set scored by the time a failed job may be tried again.

## The hard part

A worker that crashes mid-job must not lose the job silently.

Prove it rather than assert it. Set `SLOW_MS: '20000'` on the worker in compose, submit a job, and while it is processing run `docker compose kill -s KILL worker`. Then watch: the id is still on `imgq:processing`, `imgq:lease:{id}` expires on its own, and the next reap moves the job back to queued with its attempt count incremented.

Then explain the design. Why BLMOVE rather than LPOP - what is the window LPOP opens, and how wide is it? Why a lease with a TTL rather than the worker promising to clean up after itself? Why does the attempt counter have to go up on a reclaim, and what happens to a job that reliably kills whatever picks it up if it does not? Finally: this design can run a job twice - a worker that finishes the resize and dies before writing 'done' will have its job reclaimed. Say why that is the right trade, and what you would have to change to make it exactly-once.

Write your answer in your README. It is worth more marks than the feature.

---

## Getting unstuck

| Symptom | Almost always |
|---|---|
| `/health` says `postgres: false` | Wrong hostname. In compose the host is the **service name**, not `localhost`. |
| Page will not load, logs fine | No `ports:` mapping, or bound to `127.0.0.1` not `0.0.0.0`. |
| `relation "..." does not exist` | Migrations did not run, or the app started before they finished. |
| Build takes minutes each time | `COPY . .` is above your dependency install. |
| CI cannot reach the database | In CircleCI service containers the host **is** `localhost` — opposite of compose. |
