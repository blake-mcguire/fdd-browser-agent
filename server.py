"""
FDD Lead Enrichment Agent — Server v5
Three-pool architecture: SOS → Company Enrichment + Person Search → XLSX output.
"""

import asyncio
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

load_dotenv()

from config import (
    GOOGLE_API_KEY, BROWSER_USE_API_KEY,
    SOS_CONCURRENCY, COMPANY_CONCURRENCY,
    PERSON_CONCURRENCY_MAX, GLOBAL_BROWSER_CAP,
)
from models import EntityRecord, PersonEntry, CompanyResult, PersonResult
from extraction import extract_entities_from_pdf, extract_entities_from_xlsx, dedup_entities
from sos_agent import sos_lookup, sos_lookup_batch, build_people_list
from company_agent import company_enrichment
from person_agent import person_search
from xlsx_builder import build_xlsx, write_audit_trail
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
    f"═══ SERVER BOOT ═══  ts={_server_start_ts}  boot_id={BOOT_ID}  "
    f"sos={SOS_CONCURRENCY}  company={COMPANY_CONCURRENCY}  person_max={PERSON_CONCURRENCY_MAX}"
)


# ── Global Queues ─────────────────────────────────────────────
_sos_queue: asyncio.Queue = asyncio.Queue()          # (job_id, state, [entity_dicts], api_key)
_company_queue: asyncio.Queue = asyncio.Queue()       # (job_id, entity_idx, api_key)
_person_queue: asyncio.Queue = asyncio.Queue()        # (job_id, entity_idx, person_entry, api_key)
_dispatcher_tasks: list = []

# In-memory job registry
_jobs: dict[str, dict] = {}


# ── Lifespan ──────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Pool 1: SOS dispatchers
    for i in range(SOS_CONCURRENCY):
        t = asyncio.create_task(_sos_dispatcher(i + 1))
        _dispatcher_tasks.append(t)
    # Pool 2: Company enrichment dispatchers
    for i in range(COMPANY_CONCURRENCY):
        t = asyncio.create_task(_company_dispatcher(i + 1))
        _dispatcher_tasks.append(t)
    # Pool 3: Person search dispatchers (dynamic, capped)
    for i in range(PERSON_CONCURRENCY_MAX):
        t = asyncio.create_task(_person_dispatcher(i + 1))
        _dispatcher_tasks.append(t)

    logger.info(
        f"Started {SOS_CONCURRENCY} SOS + {COMPANY_CONCURRENCY} company + "
        f"{PERSON_CONCURRENCY_MAX} person dispatchers"
    )
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

            entity_idx = len(job["records"])
            rec = EntityRecord(
                entity_name=entity_name,
                state=state,
                num_locations=entity_dict.get("num_locations", 1),
                all_states=entity_dict.get("all_states", state),
                address=entity_dict.get("address", ""),
                original_notes=entity_dict.get("notes", ""),
                source_file=job.get("filename", ""),
                franchisor=job.get("franchisor", ""),
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

            # Queue company enrichment (uses Browser Use Cloud key, not Gemini)
            await _company_queue.put((job_id, entity_idx, BROWSER_USE_API_KEY))
            job["company_total"] += 1

            # Queue person search for each human officer/agent
            for person in people:
                await _person_queue.put((job_id, entity_idx, person, BROWSER_USE_API_KEY))
                job["person_total"] += 1

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
                    num_locations=entity_dict.get("num_locations", 1),
                    all_states=entity_dict.get("all_states", state),
                    original_notes=entity_dict.get("notes", ""),
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
# POOL 2: COMPANY ENRICHMENT DISPATCHERS
# ══════════════════════════════════════════════════════════════

async def _company_dispatcher(worker_id: int):
    """Pull from company queue, run web search for company intelligence."""
    logger.info(f"Company Dispatcher {worker_id} ready")

    while True:
        try:
            job_id, entity_idx, api_key = await _company_queue.get()
        except asyncio.CancelledError:
            break

        job = _jobs.get(job_id)
        if not job:
            _company_queue.task_done()
            continue

        # Fail-fast: skip work if API key is dead
        if _fail_job_if_key_dead(job, api_key):
            job["company_completed"] += 1
            _company_queue.task_done()
            _maybe_finalize(job_id)
            continue

        rec = job["records"][entity_idx]
        entity_name = rec.entity_name
        step_id = f"company:{entity_name}"

        ts = time.strftime("%H:%M:%S")
        job["log"].append(f"[{ts}] COMPANY: {entity_name}")
        job["current_step"] = f"Company: {entity_name}"
        _add_step(job, step_id, f"Company Intel — {entity_name}", "company")

        logger.info(f"── COMPANY START  [W{worker_id}] job={job_id[:8]}  entity=\"{entity_name}\"")
        comp_start = time.time()

        try:
            result = await company_enrichment(
                entity_name=entity_name,
                state=rec.state,
                franchisor=rec.franchisor,
                api_key=api_key,
            )
            rec.company = result
            elapsed = round(time.time() - comp_start, 1)

            # Update step with result details
            if result.error:
                _update_step(job, step_id, "failed", result.error[:80])
            else:
                parts = []
                if result.website:
                    parts.append(f"Website found")
                if result.recent_news_summary:
                    parts.append(f"News found")
                if result.key_developments:
                    parts.append(f"{len(result.key_developments)} development{'s' if len(result.key_developments) != 1 else ''}")
                _update_step(job, step_id, "success", ", ".join(parts) if parts else "No info found")

            logger.info(
                f"── COMPANY DONE   [W{worker_id}] job={job_id[:8]}  "
                f"entity=\"{entity_name}\"  website={'found' if result.website else 'none'}  "
                f"elapsed={elapsed}s"
            )

        except Exception as e:
            elapsed = round(time.time() - comp_start, 1)
            logger.error(
                f"── COMPANY FAIL   [W{worker_id}] job={job_id[:8]}  "
                f"entity=\"{entity_name}\"  elapsed={elapsed}s  error={e}"
            )
            rec.company = CompanyResult(entity_name=entity_name, error=str(e)[:500])
            _update_step(job, step_id, "failed", str(e)[:80])

        finally:
            job["company_completed"] += 1
            _company_queue.task_done()
            _update_progress(job)
            _maybe_finalize(job_id)


# ══════════════════════════════════════════════════════════════
# POOL 3: PERSON SEARCH DISPATCHERS
# ══════════════════════════════════════════════════════════════

async def _person_dispatcher(worker_id: int):
    """Pull from person queue, run web search for personal contact info."""
    logger.info(f"Person Dispatcher {worker_id} ready")

    while True:
        try:
            job_id, entity_idx, person_entry, api_key = await _person_queue.get()
        except asyncio.CancelledError:
            break

        job = _jobs.get(job_id)
        if not job:
            _person_queue.task_done()
            continue

        # Fail-fast: skip work if API key is dead
        if _fail_job_if_key_dead(job, api_key):
            job["person_completed"] += 1
            _person_queue.task_done()
            _maybe_finalize(job_id)
            continue

        rec = job["records"][entity_idx]
        person_name = person_entry.name
        entity_name = rec.entity_name
        step_id = f"person:{person_name}@{entity_name}"

        ts = time.strftime("%H:%M:%S")
        job["log"].append(f"[{ts}] PERSON: {person_name} @ {entity_name}")
        job["current_step"] = f"Person: {person_name} @ {entity_name}"
        _add_step(job, step_id, f"Person Search — {person_name} @ {entity_name}", "person")

        logger.info(
            f"── PERSON START  [W{worker_id}] job={job_id[:8]}  "
            f"person=\"{person_name}\"  entity=\"{entity_name}\""
        )
        person_start = time.time()

        try:
            result = await person_search(
                person=person_entry,
                entity_name=entity_name,
                state=rec.state,
                api_key=api_key,
            )
            rec.person_results.append(result)
            elapsed = round(time.time() - person_start, 1)

            # Update step with result details
            if result.error:
                _update_step(job, step_id, "failed", result.error[:80])
            else:
                parts = []
                if result.linkedin_url:
                    parts.append("LinkedIn found")
                if result.linkedin_location:
                    parts.append(f"Location: {result.linkedin_location[:30]}")
                if result.email:
                    parts.append("Email found")
                if result.personal_phone:
                    parts.append("Phone found")
                _update_step(job, step_id, "success", ", ".join(parts) if parts else "No info found")

            logger.info(
                f"── PERSON DONE   [W{worker_id}] job={job_id[:8]}  "
                f"person=\"{person_name}\"  linkedin={'found' if result.linkedin_url else 'none'}  "
                f"elapsed={elapsed}s"
            )

        except Exception as e:
            elapsed = round(time.time() - person_start, 1)
            logger.error(
                f"── PERSON FAIL   [W{worker_id}] job={job_id[:8]}  "
                f"person=\"{person_name}\"  elapsed={elapsed}s  error={e}"
            )
            rec.person_results.append(PersonResult(
                entity_name=entity_name,
                person_name=person_name,
                title=person_entry.title,
                sos_address=person_entry.address if person_entry.address != "UNKNOWN" else "",
                error=str(e)[:500],
            ))
            _update_step(job, step_id, "failed", str(e)[:80])

        finally:
            job["person_completed"] += 1
            _person_queue.task_done()
            _update_progress(job)
            _maybe_finalize(job_id)


# ══════════════════════════════════════════════════════════════
# PROGRESS + FINALIZATION
# ══════════════════════════════════════════════════════════════

def _update_progress(job: dict):
    """Recalculate progress_pct across all three phases."""
    total = job["sos_total"] + job["company_total"] + job["person_total"]
    done = job["sos_completed"] + job["company_completed"] + job["person_completed"]
    if total > 0:
        job["progress_pct"] = min(95, int(done / total * 100))


def _maybe_finalize(job_id: str):
    """Check if all work is done and trigger XLSX build."""
    job = _jobs.get(job_id)
    if not job or job["status"] != "running":
        return
    sos_done = job["sos_completed"] >= job["sos_total"]
    company_done = job["company_completed"] >= job["company_total"]
    person_done = job["person_completed"] >= job["person_total"]
    if sos_done and company_done and person_done:
        asyncio.create_task(_finalize_job(job_id))


async def _finalize_job(job_id: str):
    """Build XLSX + audit trail from all collected results and mark job done."""
    job = _jobs.get(job_id)
    if not job or job["status"] != "running":
        return
    try:
        job["current_step"] = "Building output spreadsheet…"
        records = job["records"]

        xlsx_bytes = build_xlsx(records)
        out_path = JOBS_DIR / f"{job_id}.xlsx"
        out_path.write_bytes(xlsx_bytes)

        # Write audit trail
        write_audit_trail(records, JOBS_DIR, job_id)

        job["status"] = "done"
        job["progress_pct"] = 100
        job["current_step"] = "Complete"
        job["output_path"] = str(out_path)

        total_entities = len(records)
        total_people = sum(len(r.person_results) for r in records)
        job["entity_count"] = total_entities
        job["people_count"] = total_people

        elapsed = time.time() - job["created_at"]
        elapsed_str = f"{int(elapsed // 3600)}h {int((elapsed % 3600) // 60)}m {int(elapsed % 60)}s"
        end_ts = time.strftime("%Y-%m-%d %H:%M:%S")

        ts = time.strftime("%H:%M:%S")
        job["log"].append(f"[{ts}] Complete — {total_entities} entities, {total_people} people")

        logger.info(
            f"\n{'═' * 72}\n"
            f"  ■ RUN COMPLETE  job={job_id[:8]}  ts={end_ts}\n"
            f"  entities={total_entities}  people={total_people}  elapsed={elapsed_str}\n"
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

app = FastAPI(title="FDD Lead Enrichment Agent", version="5.0.0", lifespan=lifespan)
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
    gemini_key_override: str = Form(default=""),
    browser_use_key_override: str = Form(default=""),
):
    """
    Submit a file for processing.
    Extracts entities from PDF/XLSX, creates a job, and enqueues to the SOS pool.
    """
    gemini_key = gemini_key_override or GOOGLE_API_KEY
    bu_key = browser_use_key_override or BROWSER_USE_API_KEY
    if not gemini_key:
        raise HTTPException(400, "GOOGLE_API_KEY is required for PDF extraction and SOS agents")
    if not bu_key:
        raise HTTPException(400, "BROWSER_USE_API_KEY is required for company/person agents")

    allowed = {".pdf", ".xlsx", ".xls", ".csv"}
    ext = Path(file.filename or "").suffix.lower()
    if ext not in allowed:
        raise HTTPException(400, f"Unsupported file type '{ext}'. Allowed: {allowed}")

    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(400, "Uploaded file is empty")

    # ── Extract entities (uses Gemini for PDF/XLSX parsing) ──
    try:
        if ext == ".pdf":
            entities = await extract_entities_from_pdf(file_bytes, gemini_key)
        else:
            entities = await extract_entities_from_xlsx(file_bytes, gemini_key)
        entities = dedup_entities(entities)
    except GeminiOverloadedError as e:
        logger.error(f"Extraction failed — Gemini overloaded: {e}")
        raise HTTPException(503, str(e))
    except GeminiRateLimitError as e:
        logger.error(f"Extraction failed — Gemini rate limited: {e}")
        raise HTTPException(429, str(e))
    except APIKeyDeadError as e:
        logger.error(f"Extraction failed — API key dead: {e}")
        raise HTTPException(401, str(e))
    except Exception as e:
        raise HTTPException(500, f"Entity extraction failed: {e}")

    if not entities:
        raise HTTPException(422, "No franchisee entities found in document")

    # ── Create job ──
    job_id = str(uuid.uuid4())
    run_ts = time.strftime("%Y-%m-%d %H:%M:%S")
    _jobs[job_id] = {
        "job_id": job_id,
        "status": "running",
        "filename": file.filename,
        "franchisor": "",  # could be detected from PDF page 1
        "created_at": time.time(),
        "current_step": f"Queued — {len(entities)} entities",
        "progress_pct": 0,
        # SOS phase
        "sos_total": len(entities),
        "sos_completed": 0,
        # Company enrichment phase
        "company_total": 0,
        "company_completed": 0,
        # Person search phase
        "person_total": 0,
        "person_completed": 0,
        # Results
        "records": [],  # list[EntityRecord]
        "log": [],
        "steps": [],   # structured step tracker: [{id, label, phase, status, detail, ts}]
        "output_path": None,
        "entity_count": 0,
        "people_count": 0,
        "error": "",
    }

    logger.info(
        f"\n{'═' * 72}\n"
        f"  ▶ RUN START  job={job_id[:8]}  ts={run_ts}\n"
        f"  file={file.filename}  entities={len(entities)}  "
        f"pools=SOS:{SOS_CONCURRENCY}/CO:{COMPANY_CONCURRENCY}/P:{PERSON_CONCURRENCY_MAX}\n"
        f"{'═' * 72}"
    )

    # ── Group entities by state, enqueue as batches ──
    from collections import defaultdict
    state_batches = defaultdict(list)
    for entity in entities:
        st = (entity.get("state") or "").strip().upper().split(",")[0].strip()
        state_batches[st or "UNKNOWN"].append(entity)

    for st, batch in state_batches.items():
        await _sos_queue.put((job_id, st, batch, gemini_key))

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
        "company_total": job["company_total"],
        "company_completed": job["company_completed"],
        "person_total": job["person_total"],
        "person_completed": job["person_completed"],
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
        "browser_use_key_set": bool(BROWSER_USE_API_KEY),
        "pools": {
            "sos": SOS_CONCURRENCY,
            "company": COMPANY_CONCURRENCY,
            "person_max": PERSON_CONCURRENCY_MAX,
            "browser_cap": GLOBAL_BROWSER_CAP,
        },
    }


if __name__ == "__main__":
    import argparse
    import threading
    import webbrowser

    parser = argparse.ArgumentParser(description="FDD Lead Enrichment Agent")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8000")))
    args = parser.parse_args()

    logger.info(f"Gemini: {'set' if GOOGLE_API_KEY else 'NOT SET'}  BrowserUse: {'set' if BROWSER_USE_API_KEY else 'NOT SET'}")
    threading.Timer(1.5, lambda: webbrowser.open(f"http://localhost:{args.port}")).start()
    uvicorn.run("server:app", host="0.0.0.0", port=args.port, reload=False)
