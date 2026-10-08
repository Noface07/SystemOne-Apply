"""jev-apply's local web app: `jev-apply ui`. A JSON API over the same files and commands the terminal uses, plus
the page in static/. The API is the contract a hosted site would keep later; the page only calls it.

It listens on 127.0.0.1 only. It can start batches that submit applications, so every API call must carry the
session token baked into the page, and requests for any other host are refused (no other website, and no
DNS-rebinding trick, can drive it)."""

import re
import secrets
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import batches, data, features, jobs, manage

STATIC = Path(__file__).parent / "static"


def create_app(token=None, port=8765):
    token = token or secrets.token_urlsafe(24)
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}", "testserver"}
    app = FastAPI(title="jev-apply", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.token = token

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.headers.get("host") not in hosts:
            return JSONResponse({"detail": "unknown host"}, status_code=403)
        if request.url.path.startswith("/api/") and not secrets.compare_digest(
            request.headers.get("x-jev-token", ""), token
        ):
            return JSONResponse({"detail": "missing or wrong session token"}, status_code=401)
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/", response_class=HTMLResponse)
    def index():
        page = (STATIC / "index.html").read_text(encoding="utf-8")
        return page.replace("__JEV_TOKEN__", token)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    # ---- overview -------------------------------------------------------------------------------------------

    @app.get("/api/system")
    def system():
        return data.system()

    @app.get("/api/profile")
    def profile(track: str):
        try:
            return data.get_profile(track)
        except ValueError as error:
            raise HTTPException(404, str(error)) from None

    @app.put("/api/profile")
    def save_profile(track: str, body: dict = Body(...)):
        if batches.active():
            raise HTTPException(409, "A batch is running: save your profile after it finishes.")
        try:
            return {"saved": data.save_profile(track, body.get("profile"), body.get("apply_to") or ())}
        except ValueError as error:
            raise HTTPException(400, str(error)) from None

    @app.post("/api/tracks")
    def new_track(body: dict = Body(...)):
        try:
            return {"track": manage.create_track(body.get("name"), body.get("base") or None)}
        except ValueError as error:
            raise HTTPException(400, str(error)) from None

    @app.put("/api/documents")
    async def upload_document(request: Request, track: str, kind: str = "resume", filename: str = "resume.pdf"):
        if batches.active():
            raise HTTPException(409, "A batch is running: change documents after it finishes.")
        content = await request.body()
        try:
            return manage.save_document(track, kind, filename, content)
        except ValueError as error:
            raise HTTPException(400, str(error)) from None

    @app.delete("/api/documents")
    def drop_document(track: str, kind: str):
        if batches.active():
            raise HTTPException(409, "A batch is running: change documents after it finishes.")
        try:
            return manage.remove_document(track, kind)
        except ValueError as error:
            raise HTTPException(400, str(error)) from None

    @app.get("/api/settings")
    def get_settings():
        return manage.settings()

    @app.put("/api/settings")
    def put_settings(body: dict = Body(...)):
        try:
            manage.set_env(body.get("values") or {})
        except ValueError as error:
            raise HTTPException(400, str(error)) from None
        return {"settings": manage.settings(), "models": data.models()}

    @app.get("/api/models")
    def models():
        return data.models()

    @app.post("/api/models")
    def set_model(body: dict = Body(...)):
        if batches.active():
            raise HTTPException(409, "A batch is running: switch the model after it finishes.")
        try:
            data.set_model(str(body.get("id") or ""))
        except ValueError as error:
            raise HTTPException(400, str(error)) from None
        return data.models()

    @app.get("/api/tracks")
    def tracks():
        return data.tracks()

    @app.get("/api/overview")
    def overview():
        apps = data.applications()
        open_q = sum(1 for q in data.questions() if not q["answer"])
        return {
            "applications": len(apps),
            "by_track": _count(apps, "track"),
            "by_day": _count([{**a, "day": (a["at"] or "")[:10]} for a in apps], "day"),
            "unconfirmed": sum(1 for a in apps if not a["confirmed"]),
            "open_questions": open_q,
            "recent": apps[:8],
            "active_batch": batches.active(),
        }

    # ---- history and runs -----------------------------------------------------------------------------------

    @app.get("/api/applications")
    def applications():
        return data.applications()

    @app.get("/api/runs")
    def runs(limit: int = 200):
        return data.run_reports()[:limit]

    @app.get("/api/runs/{run_id}")
    def run(run_id: str):
        found = data.run_detail(run_id)
        if not found:
            raise HTTPException(404, "no such run")
        return found

    # ---- questions inbox ------------------------------------------------------------------------------------

    @app.get("/api/questions")
    def questions():
        return data.questions()

    @app.post("/api/questions/{index}")
    def answer(index: int, body: dict = Body(...)):
        try:
            return data.answer_question(index, body.get("answer", ""))
        except IndexError:
            raise HTTPException(404, "no such question") from None

    # ---- finding jobs ---------------------------------------------------------------------------------------

    @app.get("/api/search/defaults")
    def search_defaults():
        return {"queries": jobs.default_queries(), "boards": bool(jobs.boards_file())}

    @app.post("/api/search")
    def search(body: dict = Body(...)):
        queries = {k: [q for q in v if q.strip()] for k, v in (body.get("queries") or {}).items() if v}
        if not queries:
            raise HTTPException(400, "pick at least one track and search words")
        known = {t["id"] for t in data.tracks()}
        if not set(queries) <= known:
            raise HTTPException(400, "unknown track")
        task = jobs.start_search(
            queries,
            location=body.get("location") or "India",
            posted=body.get("posted") or "week",
            max_years=body.get("max_years"),
            easy_apply=body.get("easy_apply", True),
            include_senior=body.get("include_senior", False),
        )
        return {"id": task}

    @app.get("/api/search/{task_id}")
    def search_status(task_id: str):
        found = jobs.get_search(task_id)
        if not found:
            raise HTTPException(404, "no such search")
        return found

    # ---- safety, preferences, Clef, insights, review queue, learn, autopilot ------------------------------------

    def guarded(fn, *args, status=400):
        try:
            return fn(*args)
        except ValueError as error:
            raise HTTPException(status, str(error)) from None

    @app.get("/api/safety")
    def safety(track: str):
        return guarded(features.get_safety, track, status=404)

    @app.put("/api/safety")
    def save_safety(track: str, body: dict = Body(...)):
        if batches.active():
            raise HTTPException(409, "A batch is running: change safety settings after it finishes.")
        return guarded(features.save_safety, track, body.get("values") or {})

    @app.get("/api/prefs")
    def prefs():
        return features.get_prefs()

    @app.put("/api/prefs")
    def save_prefs(body: dict = Body(...)):
        return guarded(features.save_prefs, body.get("values") or {})

    @app.get("/api/clef")
    def clef():
        return features.clef_status()

    @app.post("/api/clef/{action}")
    def clef_action(action: str):
        if action == "start":
            return features.clef_start()
        if action == "stop":
            if batches.active():
                raise HTTPException(409, "A batch is using Clef: stop the batch first.")
            return features.clef_stop()
        raise HTTPException(404, "start or stop")

    @app.get("/api/insights")
    def insights():
        return features.insights()

    @app.get("/api/queue")
    def queue():
        return features.queue()

    @app.post("/api/queue/{item_id:path}")
    def queue_done(item_id: str, body: dict = Body(default={})):
        return features.mark_done(item_id, body.get("done", True))

    @app.post("/api/learn")
    def learn(track: str, body: dict = Body(default={})):
        return {"id": guarded(features.start_learn, track, body.get("claude", True))}

    @app.get("/api/learn/{task_id}")
    def learn_state(task_id: str):
        found = features.learn_status(task_id)
        if not found:
            raise HTTPException(404, "no such task")
        return found

    @app.post("/api/learn-apply")
    def learn_apply(track: str, body: dict = Body(...)):
        if batches.active():
            raise HTTPException(409, "A batch is running: update your profile after it finishes.")
        return guarded(features.apply_learned, track, body.get("keys") or [])

    @app.get("/api/autopilot")
    def autopilot():
        return features.get_autopilot()

    @app.put("/api/autopilot")
    def save_autopilot(body: dict = Body(...)):
        return guarded(features.save_autopilot, body.get("values") or {})

    @app.post("/api/links")
    def links(body: dict = Body(...)):
        urls = [u for u in (body.get("urls") or []) if isinstance(u, str)][:100]
        if not urls:
            raise HTTPException(400, "paste at least one job link")
        system = data.system()
        years = int(system["years"]) if system.get("years") is not None else None
        return {"id": jobs.start_links(urls, max_years=years)}

    @app.get("/api/dismissed")
    def dismissed_list():
        return features.dismissed()

    @app.post("/api/dismiss")
    def dismiss(body: dict = Body(...)):
        return guarded(features.dismiss, body.get("key"), body.get("title", ""), body.get("company", ""),
                       bool(body.get("undo")))  # fmt: skip

    @app.get("/api/answers")
    def answers():
        return features.answer_bank()

    @app.put("/api/answers/{answer_id:path}")
    def update_answer(answer_id: str, body: dict = Body(...)):
        return guarded(features.update_answer, answer_id, body.get("answer", ""))

    @app.delete("/api/answers/{answer_id:path}")
    def delete_answer(answer_id: str):
        return guarded(features.delete_answer, answer_id)

    @app.get("/api/claude-usage")
    def claude_usage(days: int = 30):
        return features.claude_usage(max(1, min(365, days)))

    @app.get("/api/runs/{run_id}/receipt.png")
    def receipt(run_id: str):
        from fastapi.responses import FileResponse

        path = data.runs_dir() / run_id / "receipt.png"
        if not re.fullmatch(r"[\w.-]+", run_id) or not path.is_file():
            raise HTTPException(404, "no receipt screenshot")
        return FileResponse(path, media_type="image/png")

    @app.get("/api/boards")
    def board_list():
        return manage.boards()

    @app.post("/api/boards")
    def board_add(body: dict = Body(...)):
        try:
            entry, count = manage.add_board(body.get("board"))
        except ValueError as error:
            raise HTTPException(400, str(error)) from None
        return {"added": entry, "open_jobs": count, **manage.boards()}

    @app.delete("/api/boards")
    def board_remove(entry: str):
        try:
            manage.remove_board(entry)
        except ValueError as error:
            raise HTTPException(404, str(error)) from None
        return manage.boards()

    @app.post("/api/boards/scan")
    def boards(body: dict = Body(default={})):
        path = jobs.boards_file()
        if not path:
            raise HTTPException(404, "no data/boards.txt")
        lines = path.read_text(encoding="utf-8").splitlines()
        found, problems = jobs.scan_boards(lines, body.get("keywords") or (), body.get("max_years"))
        return {"jobs": found, "problems": problems, "file": data.rel(path)}

    # ---- batches --------------------------------------------------------------------------------------------

    @app.get("/api/batches")
    def batch_list():
        return batches.listing()

    @app.post("/api/batches")
    def batch_start(body: dict = Body(...)):
        known = {t["id"] for t in data.tracks()}
        tracks = [t for t in body.get("tracks") or sorted(known) if t in known]
        try:
            return {"id": batches.start(body.get("urls") or [], tracks, submit=bool(body.get("submit", True)))}
        except (RuntimeError, ValueError) as error:
            raise HTTPException(409, str(error)) from None

    @app.get("/api/batches/{batch_id}")
    def batch_state(batch_id: str):
        found = batches.get(batch_id)
        if not found:
            raise HTTPException(404, "no such batch")
        return found

    @app.post("/api/batches/{batch_id}/stop")
    def batch_stop(batch_id: str):
        if not batches.stop(batch_id):
            raise HTTPException(404, "no such batch")
        return {"stopped": True}

    return app


def _count(items, key):
    out = {}
    for item in items:
        value = item.get(key) or "unknown"
        out[value] = out.get(value, 0) + 1
    return out


def serve(port=8765, open_browser=True):
    import threading
    import webbrowser

    import uvicorn

    app = create_app(port=port)
    url = f"http://127.0.0.1:{port}/"
    if open_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    print(f"jev-apply UI on {url} (this machine only). Ctrl+C to stop.")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
