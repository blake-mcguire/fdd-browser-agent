"""
Business Owner Lookup — Server v6

Input: a Google Places XLSX with one business per row.
Output: the same XLSX with "Owner 1..N" columns appended, populated from SOS.

Single-pool architecture: SOS only. Company / person enrichment removed —
this pipeline only resolves business name + state to owner names.
"""

import asyncio
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

load_dotenv()

from config import (
    GOOGLE_API_KEY, SOS_CONCURRENCY, GLOBAL_BROWSER_CAP,
)
from models import EntityRecord
from extraction import extract_businesses_from_xlsx
from sos_agent import sos_lookup_batch, build_people_list
from xlsx_builder import build_xlsx_with_owners, write_audit_trail
from llm import is_key_dead, APIKeyDeadError, GeminiOverloadedError, GeminiRateLimitError


# ── Logging ───────────────────────────────────────────────────
_log_dir = Path(__file__).parent / "logs"
_log_dir.mkdir(exist_ok=True)
_server_start_ts = time.strftime("%Y-%m-%d_%H-%M-%S")
_session_log = _log_dir / f"session_{_server_start_ts}.log"
_latest_log = _log_dir / "latest.log"
_log_fmt = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s")

_root = logging.getLogger()
if not any(isinstance(h, logging.FileHandler) for h in _root.handlers):
    _file_handler = logging.FileHandler(_session_log, encoding="utf-8")
    _file_handler.setLevel(logging.INFO)
    _file_handler.setFormatter(_log_fmt)
    _root.addHandler(_file_handler)
_root.setLevel(logging.INFO)

try:
    if _latest_log.is_symlink() or _latest_log.exists():
        _latest_log.unlink()
    _latest_log.symlink_to(_session_log.name)
except OSError:
    import shutil
    shutil.copy2(_session_log, _latest_log)

logger = logging.getLogger("fdd-agent")

# ── Paths ─────────────────────────────────────────────────────
JOBS_DIR = Path(os.getenv("JOBS_DIR", "./jobs"))
JOBS_DIR.mkdir(parents=True, exist_ok=True)
BOOT_ID = str(uuid.uuid4())

logger.info(
    f"═══ SERVER BOOT ═══  ts={_server_start_ts}  boot_id={BOOT_ID}  sos={SOS_CONCURRENCY}"
)


# ── Global Queues ─────────────────────────────────────────────
_sos_queue: asyncio.Queue = asyncio.Queue()          # (job_id, state, [entity_dicts], api_key)
_dispatcher_tasks: list = []

# In-memory job registry
_jobs: dict[str, dict] = {}


# ── Lifespan ──────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    for i in range(SOS_CONCURRENCY):
        t = asyncio.create_task(_sos_dispatcher(i + 1))
        _dispatcher_tasks.append(t)

    logger.info(f"Started {SOS_CONCURRENCY} SOS dispatchers")
    yield
    for t in _dispatcher_tasks:
        t.cancel()
    logger.info("Dispatchers shut down")


def _fail_job_if_key_dead(job: dict, api_key: str) -> bool:
    """Check if API key is dead and fail the job if so. Returns True if dead."""
    if is_key_dead(api_key) and job["status"] == "running":
        _fail_job(job, "Gemini API key suspended or quota exhausted. Please check your API key and billing.")
        return True
    return False


def _fail_job(job: dict, error_message: str):
    """Fail a job with a client-facing error message."""
    if job["status"] != "running":
        return
    job["status"] = "failed"
    job["error"] = error_message
    job["current_step"] = f"FAILED — {error_message[:80]}"
    ts = time.strftime("%H:%M:%S")
    job["log"].append(f"[{ts}] ABORTED — {error_message}")
    logger.error(f"Job {job['job_id'][:8]} failed: {error_message[:200]}")


# ── Step tracker helpers ─────────────────────────────────────

def _add_step(job: dict, step_id: str, label: str, phase: str, status: str = "processing", detail: str = ""):
    """Add a new step entry to the job's step tracker."""
    ts = time.strftime("%H:%M:%S")
    job["steps"].append({
        "id": step_id,
        "label": label,
        "phase": phase,
        "status": status,
        "detail": detail,
        "ts": ts,
    })

def _update_step(job: dict, step_id: str, status: str, detail: str = ""):
    """Update an existing step's status and detail."""
    ts = time.strftime("%H:%M:%S")
    for step in job["steps"]:
        if step["id"] == step_id:
            step["status"] = status
            step["detail"] = detail
            step["ts"] = ts
            return
    # If not found (shouldn't happen), add it
    job["steps"].append({
        "id": step_id, "label": step_id, "phase": "unknown",
        "status": status, "detail": detail, "ts": ts,
    })


# ══════════════════════════════════════════════════════════════
# POOL 1: SOS DISPATCHERS
# ══════════════════════════════════════════════════════════════

async def _sos_dispatcher(worker_id: int):
    """Pull state batches from SOS queue, run batch SOS lookup, feed company + person queues."""
    logger.info(f"SOS Dispatcher {worker_id} ready")

    while True:
        try:
            job_id, state, entity_dicts, api_key = await _sos_queue.get()
        except asyncio.CancelledError:
            break

        job = _jobs.get(job_id)
        if not job:
            _sos_queue.task_done()
            continue

        # Fail-fast: skip entire batch if API key is dead
        if _fail_job_if_key_dead(job, api_key):
            job["sos_completed"] += len(entity_dicts)
            _sos_queue.task_done()
            _maybe_finalize(job_id)
            continue

        batch_start = time.time()
        ts = time.strftime("%H:%M:%S")
        job["log"].append(f"[{ts}] SOS batch: {state} ({len(entity_dicts)} entities)")
        job["current_step"] = f"SOS batch: {state} ({len(entity_dicts)} entities)"

        logger.info(
            f"── SOS BATCH START  [W{worker_id}] job={job_id[:8]}  "
            f"state={state}  entities={len(entity_dicts)}  "
            f"({job['sos_completed']}/{job['sos_total']})"
        )

        # Track which entities in THIS batch completed via callback
        batch_completed_indices: set[int] = set()

        async def _on_entity_start(entity_dict):
            """Callback fired before each entity starts SOS lookup."""
            entity_name = entity_dict.get("entity_name", "?")
            step_id = f"sos:{state}:{entity_name}"
            _add_step(job, step_id, f"SOS Lookup — {entity_name} ({state})", "sos")

        async def _on_entity_result(entity_dict, sos_result):
            """Callback fired after each entity in the batch completes."""
            # Mark this entity's index within the batch as done
            batch_idx = next(
                (i for i, ed in enumerate(entity_dicts) if ed is entity_dict), -1
            )
            if batch_idx >= 0:
                batch_completed_indices.add(batch_idx)

            entity_name = entity_dict.get("entity_name", "?")
            people = build_people_list(sos_result)

            rec = EntityRecord(
                entity_name=entity_name,
                state=state,
                original_row_index=entity_dict.get("row_index", 0),
                address=entity_dict.get("address", ""),
                source_file=job.get("filename", ""),
                registered_agent=sos_result.registered_agent,
                agent_address=sos_result.agent_address,
                entity_status=sos_result.entity_status,
                formation_date=sos_result.formation_date,
                entity_type=sos_result.entity_type,
                dba_name=sos_result.dba_name,
                sos_source_url=sos_result.source_url,
                sos_confidence=sos_result.confidence,
                sos_error=sos_result.error,
                people=people,
            )
            job["records"].append(rec)

            job["sos_completed"] += 1
            _update_progress(job)
            _maybe_finalize(job_id)

            # Update step tracker with result details
            step_id = f"sos:{state}:{entity_name}"
            if sos_result.confidence == "FAILED" or sos_result.error:
                err_short = sos_result.error[:80] if sos_result.error else "Unknown error"
                _update_step(job, step_id, "failed", err_short)
            else:
                officer_count = len(people)
                addr_count = sum(
                    1 for p in people
                    if p.address and p.address not in ("UNKNOWN", "N/A", "")
                )
                agent_str = ""
                if sos_result.registered_agent not in ("UNKNOWN", "NOT FOUND", "N/A", "NONE", ""):
                    agent_str = f"Agent: {sos_result.registered_agent[:30]}"
                parts = []
                parts.append(f"{officer_count} officer{'s' if officer_count != 1 else ''}")
                parts.append(f"{addr_count} address{'es' if addr_count != 1 else ''}")
                if agent_str:
                    parts.append(agent_str)
                _update_step(job, step_id, "success", ", ".join(parts))

            ts2 = time.strftime("%H:%M:%S")
            job["log"].append(f"[{ts2}] SOS done: {entity_name} — {len(people)} people")
            logger.info(
                f"── SOS DONE   [W{worker_id}] job={job_id[:8]}  "
                f"entity=\"{entity_name}\"  people={len(people)}"
            )

        try:
            await sos_lookup_batch(
                entities=entity_dicts,
                state=state,
                api_key=api_key,
                on_result=_on_entity_result,
                on_start=_on_entity_start,
            )

        except GeminiOverloadedError as e:
            # Gemini is down — fail the entire job, don't grind through remaining entities
            _fail_job(job, str(e))
            # Mark unprocessed entities as failed
            for i, entity_dict in enumerate(entity_dicts):
                if i in batch_completed_indices:
                    continue
                entity_name = entity_dict.get("entity_name", "?")
                step_id = f"sos:{state}:{entity_name}"
                _update_step(job, step_id, "failed", "Gemini API overloaded")
                job["sos_completed"] += 1

        except GeminiRateLimitError as e:
            # Rate limited — fail the entire job
            _fail_job(job, str(e))
            for i, entity_dict in enumerate(entity_dicts):
                if i in batch_completed_indices:
                    continue
                entity_name = entity_dict.get("entity_name", "?")
                step_id = f"sos:{state}:{entity_name}"
                _update_step(job, step_id, "failed", "Gemini rate limit exceeded")
                job["sos_completed"] += 1

        except APIKeyDeadError as e:
            # API key is dead — fail the entire job
            _fail_job(job, str(e))
            for i, entity_dict in enumerate(entity_dicts):
                if i in batch_completed_indices:
                    continue
                entity_name = entity_dict.get("entity_name", "?")
                step_id = f"sos:{state}:{entity_name}"
                _update_step(job, step_id, "failed", "API key invalid")
                job["sos_completed"] += 1

        except Exception as e:
            elapsed = round(time.time() - batch_start, 1)
            logger.error(
                f"── SOS BATCH FAIL  [W{worker_id}] job={job_id[:8]}  "
                f"state={state}  elapsed={elapsed}s  error={e}",
                exc_info=True,
            )
            # Store stubs for entities in THIS batch not yet processed
            for i, entity_dict in enumerate(entity_dicts):
                if i in batch_completed_indices:
                    continue
                entity_name = entity_dict.get("entity_name", "?")
                step_id = f"sos:{state}:{entity_name}"
                _update_step(job, step_id, "failed", str(e)[:80])
                rec = EntityRecord(
                    entity_name=entity_name,
                    state=state,
                    original_row_index=entity_dict.get("row_index", 0),
                    address=entity_dict.get("address", ""),
                    source_file=job.get("filename", ""),
                    sos_error=str(e),
                )
                job["records"].append(rec)
                job["sos_completed"] += 1

        finally:
            elapsed = round(time.time() - batch_start, 1)
            logger.info(
                f"── SOS BATCH END  [W{worker_id}] job={job_id[:8]}  "
                f"state={state}  elapsed={elapsed}s"
            )
            _sos_queue.task_done()
            _update_progress(job)
            _maybe_finalize(job_id)


# ══════════════════════════════════════════════════════════════
# PROGRESS + FINALIZATION
# ══════════════════════════════════════════════════════════════

def _update_progress(job: dict):
    total = job["sos_total"]
    done = job["sos_completed"]
    if total > 0:
        job["progress_pct"] = min(95, int(done / total * 100))


def _maybe_finalize(job_id: str):
    job = _jobs.get(job_id)
    if not job or job["status"] != "running":
        return
    if job["sos_completed"] >= job["sos_total"]:
        asyncio.create_task(_finalize_job(job_id))


async def _finalize_job(job_id: str):
    """Append Owner 1..N columns to the original XLSX and mark job done."""
    job = _jobs.get(job_id)
    if not job or job["status"] != "running":
        return
    try:
        job["current_step"] = "Building output spreadsheet…"
        records = job["records"]
        original_bytes = job.get("input_bytes") or b""

        xlsx_bytes = build_xlsx_with_owners(original_bytes, records)
        out_path = JOBS_DIR / f"{job_id}.xlsx"
        out_path.write_bytes(xlsx_bytes)

        write_audit_trail(records, JOBS_DIR, job_id)

        job["status"] = "done"
        job["progress_pct"] = 100
        job["current_step"] = "Complete"
        job["output_path"] = str(out_path)
        # Drop the cached input bytes so we don't hold them after finalize
        job["input_bytes"] = None

        total_entities = len(records)
        total_people = sum(len(r.people) for r in records)
        job["entity_count"] = total_entities
        job["people_count"] = total_people

        elapsed = time.time() - job["created_at"]
        elapsed_str = f"{int(elapsed // 3600)}h {int((elapsed % 3600) // 60)}m {int(elapsed % 60)}s"
        end_ts = time.strftime("%Y-%m-%d %H:%M:%S")

        ts = time.strftime("%H:%M:%S")
        job["log"].append(f"[{ts}] Complete — {total_entities} businesses, {total_people} owners")

        logger.info(
            f"\n{'═' * 72}\n"
            f"  ■ RUN COMPLETE  job={job_id[:8]}  ts={end_ts}\n"
            f"  businesses={total_entities}  owners={total_people}  elapsed={elapsed_str}\n"
            f"  output={out_path}\n"
            f"{'═' * 72}"
        )
    except Exception as e:
        logger.error(f"Job {job_id[:8]} finalization failed: {e}", exc_info=True)
        job["status"] = "failed"
        job["error"] = str(e)
        job["current_step"] = f"Failed during XLSX build: {e}"


# ══════════════════════════════════════════════════════════════
# FASTAPI APP
# ══════════════════════════════════════════════════════════════

app = FastAPI(title="Business Owner Lookup", version="6.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_static_dir = Path(__file__).parent / "static"
if _static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


@app.get("/")
async def serve_ui():
    index = _static_dir / "index.html"
    if index.exists():
        return FileResponse(str(index))
    return {"message": "FDD Lead Enrichment Agent. Visit /docs for API."}


@app.post("/process-fdd")
async def process_fdd(
    file: UploadFile = File(...),
):
    """
    Submit a Google Places business-list XLSX. Each row is looked up on the
    matching state's Secretary of State portal; owner names are extracted and
    appended to the original sheet as Owner 1..N columns.
    """
    if not GOOGLE_API_KEY:
        raise HTTPException(400, "GOOGLE_API_KEY is required for SOS agents")

    ext = Path(file.filename or "").suffix.lower()
    if ext not in {".xlsx", ".xls"}:
        raise HTTPException(400, f"Unsupported file type '{ext}'. Expected: .xlsx")

    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(400, "Uploaded file is empty")

    try:
        entities = extract_businesses_from_xlsx(file_bytes)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"Failed to read XLSX: {e}")

    if not entities:
        raise HTTPException(422, "No business rows found in spreadsheet")

    job_id = str(uuid.uuid4())
    run_ts = time.strftime("%Y-%m-%d %H:%M:%S")
    _jobs[job_id] = {
        "job_id": job_id,
        "status": "running",
        "filename": file.filename,
        # Original XLSX bytes — re-opened at finalize time so owner columns
        # are appended onto the user's exact sheet (formatting preserved).
        "input_bytes": file_bytes,
        "created_at": time.time(),
        "current_step": f"Queued — {len(entities)} businesses",
        "progress_pct": 0,
        "sos_total": len(entities),
        "sos_completed": 0,
        "records": [],
        "log": [],
        "steps": [],
        "output_path": None,
        "entity_count": 0,
        "people_count": 0,
        "error": "",
    }

    logger.info(
        f"\n{'═' * 72}\n"
        f"  ▶ RUN START  job={job_id[:8]}  ts={run_ts}\n"
        f"  file={file.filename}  businesses={len(entities)}  pool=SOS:{SOS_CONCURRENCY}\n"
        f"{'═' * 72}"
    )

    # Group by state, enqueue one batch per state
    from collections import defaultdict
    state_batches = defaultdict(list)
    for entity in entities:
        st = (entity.get("state") or "").strip().upper()
        state_batches[st or "UNKNOWN"].append(entity)

    for st, batch in state_batches.items():
        await _sos_queue.put((job_id, st, batch, GOOGLE_API_KEY))

    logger.info(
        f"  Queued {len(state_batches)} state batches: "
        + ", ".join(f"{st}({len(b)})" for st, b in sorted(state_batches.items()))
    )

    return {"job_id": job_id, "status": "running", "entity_count": len(entities)}


@app.get("/job/{job_id}")
async def get_job(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, f"Job {job_id} not found")
    return {
        "job_id": job["job_id"],
        "status": job["status"],
        "filename": job["filename"],
        "created_at": job["created_at"],
        "current_step": job["current_step"],
        "progress_pct": job["progress_pct"],
        "sos_total": job["sos_total"],
        "sos_completed": job["sos_completed"],
        "total": job["sos_total"],
        "completed": job["sos_completed"],
        "log": job["log"],
        "steps": job["steps"],
        "entity_count": job["entity_count"],
        "people_count": job["people_count"],
        "error": job.get("error", ""),
    }


@app.get("/job/{job_id}/download")
async def download_job(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, f"Job {job_id} not found")
    if job["status"] != "done":
        raise HTTPException(400, f"Job not done yet (status: {job['status']})")
    out_path = job.get("output_path")
    if not out_path or not Path(out_path).exists():
        raise HTTPException(500, "Output file not found on disk")
    stem = Path(job["filename"]).stem
    return FileResponse(
        path=out_path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=f"{stem}_results.xlsx",
    )


@app.get("/jobs")
async def list_jobs():
    return [
        {"job_id": j["job_id"], "status": j["status"], "filename": j["filename"],
         "created_at": j["created_at"], "progress_pct": j["progress_pct"]}
        for j in _jobs.values()
    ]


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "boot_id": BOOT_ID,
        "gemini_key_set": bool(GOOGLE_API_KEY),
        "pools": {"sos": SOS_CONCURRENCY, "browser_cap": GLOBAL_BROWSER_CAP},
    }


if __name__ == "__main__":
    import argparse
    import threading
    import webbrowser

    parser = argparse.ArgumentParser(description="Business Owner Lookup")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8000")))
    args = parser.parse_args()

    logger.info(f"Gemini: {'set' if GOOGLE_API_KEY else 'NOT SET'}")
    threading.Timer(1.5, lambda: webbrowser.open(f"http://localhost:{args.port}")).start()
    uvicorn.run("server:app", host="0.0.0.0", port=args.port, reload=False)
