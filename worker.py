"""model_1 worker: polls the site for jobs, generates locally, uploads results.

Protocol (site side is described in MODEL1_SITE_SPEC.md):
    POST <endpoint>?action=m1_next      header X-Worker-Key -> {"job": {...}} | {"job": null}
        job type "generate" (default): id, prompt, aspect_ratio, count, image_urls,
            resolution "1k" (default) | "2k" (each result is upscaled before upload)
        job type "upscale": id, source_id, index (0-based position in source urls), scale "2k"
    POST <endpoint>?action=m1_complete  multipart job_id + files[]
    POST <endpoint>?action=m1_fail      JSON {job_id, error_code}

Privacy: only generic error codes are sent to the site; images are stripped of
EXIF/XMP/C2PA/text metadata before upload. Details stay in worker.log locally.

Local 2K test without the site:
    python worker.py upscale-test <source_job_id> [index]

Config: worker_config.json next to this file:
    {"endpoint": "https://example.com/kie-proxy.php", "worker_key": "..."}
"""

import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import traceback
from pathlib import Path

import httpx

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "worker_config.json"
LOG_FILE = BASE_DIR / "worker.log"
WORK_DIR = BASE_DIR / "worker_out"
META_DIR = BASE_DIR / "worker_meta"  # tiny per-job media maps, kept after worker_out cleanup
LAUNCHER = Path(os.environ.get("M1_LAUNCHER") or BASE_DIR / "gflow_ext.py")

POLL_IDLE_SECONDS = 4
POLL_ERROR_BACKOFF = (5, 10, 30, 60)
GENERATION_TIMEOUT = int(os.environ.get("M1_TIMEOUT", "600"))
ALLOWED_ASPECTS = {"16:9", "4:3", "1:1", "3:4", "9:16"}
MODEL_ALIAS = os.environ.get("M1_MODEL", "nano-pro")  # Nano Banana Pro
KEEP_LOCAL_DAYS = 7
MAX_REFS = 10
MAX_REF_BYTES = 20 * 1024 * 1024
DEFAULT_PROMPT_SUFFIX = "Сгенерируй в модели №1"
UPSCALE_TIMEOUT = int(os.environ.get("M1_UPSCALE_TIMEOUT", "180"))
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def log(message: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
    print(line, flush=True)
    try:
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------- metadata strip

_PNG_KEEP = {b"IHDR", b"PLTE", b"IDAT", b"IEND", b"tRNS", b"gAMA", b"cHRM", b"sRGB", b"iCCP", b"sBIT", b"pHYs"}


def strip_jpeg(data: bytes) -> bytes:
    """Drop APP1..APP15 (EXIF, XMP, C2PA/JUMBF ...) and COM; keep JFIF and ICC."""
    if data[:2] != b"\xff\xd8":
        raise ValueError("not a JPEG")
    out = bytearray(b"\xff\xd8")
    i = 2
    while i < len(data):
        if data[i] != 0xFF:
            raise ValueError("bad JPEG marker")
        marker = data[i + 1]
        if marker == 0xFF:  # fill byte
            i += 1
            continue
        if marker == 0xDA:  # start of scan: copy the rest verbatim
            out += data[i:]
            return bytes(out)
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            out += data[i:i + 2]
            i += 2
            continue
        length = struct.unpack(">H", data[i + 2:i + 4])[0]
        segment = data[i:i + 2 + length]
        drop = marker == 0xFE or (0xE1 <= marker <= 0xEF and not (
            marker == 0xE2 and segment[4:16] == b"ICC_PROFILE\x00"))
        if not drop:
            out += segment
        i += 2 + length
    raise ValueError("JPEG without scan data")


def strip_png(data: bytes) -> bytes:
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    out = bytearray(data[:8])
    i = 8
    while i < len(data):
        length = struct.unpack(">I", data[i:i + 4])[0]
        ctype = data[i + 4:i + 8]
        chunk = data[i:i + 12 + length]
        if ctype in _PNG_KEEP:
            out += chunk
        i += 12 + length
        if ctype == b"IEND":
            break
    return bytes(out)


def clean_image(path: Path) -> tuple[bytes, str, str]:
    """Return (bytes, filename_ext, mime) with metadata removed."""
    data = path.read_bytes()
    if data[:2] == b"\xff\xd8":
        return strip_jpeg(data), "jpg", "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return strip_png(data), "png", "image/png"
    # Anything else (e.g. WebP): re-encode to PNG without metadata.
    from PIL import Image  # installed by worker_setup.ps1
    import io

    with Image.open(io.BytesIO(data)) as img:
        img = img.convert("RGBA") if img.mode in ("P", "LA") else img
        buf = io.BytesIO()
        img.save(buf, format="PNG")
    return buf.getvalue(), "png", "image/png"


REF_MAX_SIDE = 2048


def normalize_ref(path: Path) -> Path:
    """Re-encode a reference as a plain JPEG (no metadata, longest side <= 2048).

    The model's upload endpoint silently rejects some files (e.g. a 6.8 MB PNG
    with embedded text chunks), so every reference is normalized first."""
    from PIL import Image, ImageOps  # installed by worker_setup.ps1

    # Short name: the mention search types it key by key (ref_3.png -> r3.jpg).
    out = path.with_name("r" + path.stem.rsplit("_", 1)[-1] + ".jpg")
    with Image.open(path) as img:
        img = ImageOps.exif_transpose(img)
        if img.mode in ("RGBA", "LA", "P"):
            rgba = img.convert("RGBA")
            img = Image.new("RGB", rgba.size, (255, 255, 255))
            img.paste(rgba, mask=rgba.split()[3])
        elif img.mode != "RGB":
            img = img.convert("RGB")
        if max(img.size) > REF_MAX_SIDE:
            img.thumbnail((REF_MAX_SIDE, REF_MAX_SIDE), Image.LANCZOS)
        img.save(out, format="JPEG", quality=92, optimize=True)
    return out


# ---------------------------------------------------------------- generation

# One shared project for all jobs (no new project per request). A fresh one is
# started every PROJECT_MAX_JOBS jobs (big projects load slower) or after a
# failure that may be project related.
PROJECT_FILE = BASE_DIR / "worker_meta" / "project.json"
PROJECT_MAX_JOBS = int(os.environ.get("M1_PROJECT_MAX_JOBS", "100"))


def shared_project() -> str | None:
    try:
        data = json.loads(PROJECT_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    pid = str(data.get("id") or "")
    if not _UUID.fullmatch(pid) or int(data.get("jobs") or 0) >= PROJECT_MAX_JOBS:
        return None
    return pid


def remember_project(project_id: str | None) -> None:
    if not project_id or not _UUID.fullmatch(str(project_id)):
        return
    try:
        data = json.loads(PROJECT_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    jobs = int(data.get("jobs") or 0) + 1 if data.get("id") == project_id else 1
    PROJECT_FILE.parent.mkdir(exist_ok=True)
    PROJECT_FILE.write_text(json.dumps({"id": project_id, "jobs": jobs}), encoding="utf-8")


def forget_project() -> None:
    try:
        PROJECT_FILE.unlink()
    except OSError:
        pass


def run_generation(job_id: str, prompt: str, aspect: str, count: int,
                   ref_paths: list[Path] | None = None, upscale_2k: bool = False) -> list[Path]:
    project_id = shared_project()
    try:
        return _run_generation(job_id, prompt, aspect, count, ref_paths, project_id, upscale_2k)
    except GenerationError as exc:
        if project_id is None or exc.code in ("content_blocked", "bad_request", "quota"):
            raise
        # Maybe the shared project is the problem: retry once in a fresh one.
        log(f"job {job_id}: failed in shared project ({exc.code}), retrying in a new project")
        forget_project()
        return _run_generation(job_id, prompt, aspect, count, ref_paths, None, upscale_2k)


def is_2k_file(path: Path) -> bool:
    return path.stem.endswith("_2k")


def inline_2k(original: Path) -> Path | None:
    """The 2K copy made in the same browser run (gflow_ext GFLOW_EXT_UPSCALE)."""
    for candidate in sorted(original.parent.glob(f"{original.stem}_2k.*")):
        if candidate.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
            return candidate
    return None


def _run_generation(job_id: str, prompt: str, aspect: str, count: int,
                    ref_paths: list[Path] | None, project_id: str | None,
                    upscale_2k: bool = False) -> list[Path]:
    out_dir = WORK_DIR / job_id / "out"
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update({
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "COLUMNS": "200",
    })
    env.setdefault("GFLOW_CLI_HOME", str(BASE_DIR / "gflow_home"))
    env.setdefault("GFLOW_CLI_FLOW_HOST", "flow.google.com")
    env.setdefault("GFLOW_CLI_LEASE_WAIT_SECONDS", "180")
    env["GFLOW_EXT_UPSCALE"] = "2k" if upscale_2k else ""
    command = "i2i" if ref_paths else "t2i"
    args = [sys.executable, str(LAUNCHER), "image", command,
            "--model", MODEL_ALIAS, "--aspect", aspect, "-n", str(count)]
    for ref in ref_paths or []:
        args += ["--ref", str(ref)]
    if project_id:
        args += ["--project", project_id]
    args += ["--out", str(out_dir), "--json", "--", prompt]
    started = time.time()
    result = subprocess.run(
        args, cwd=str(BASE_DIR), env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=GENERATION_TIMEOUT,
    )
    (WORK_DIR / job_id / "run.log").write_text(
        f"exit={result.returncode} seconds={time.time() - started:.1f}\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}\n",
        encoding="utf-8",
    )
    files = sorted(p for p in out_dir.rglob("*")
                   if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"} and not is_2k_file(p))
    if not files:
        raise GenerationError(classify_error(result.stdout + "\n" + result.stderr), result.returncode)
    files = files[:count]
    # Remember which generated media each uploaded image is (needed for 2K later).
    project_id, media = parse_generation_json(result.stdout)
    remember_project(project_id)
    order = {Path(path).name: name for name, path in media}
    meta = {"project_id": project_id, "media": [order.get(p.name) or media_from_name(p.name) for p in files]}
    META_DIR.mkdir(exist_ok=True)
    (META_DIR / f"{job_id}.json").write_text(json.dumps(meta), encoding="utf-8")
    return files


def parse_generation_json(stdout: str) -> tuple[str | None, list[tuple[str, str]]]:
    """(project_id, [(media_name, local_path), ...]) from gflow --json output."""
    start, end = stdout.find("{"), stdout.rfind("}")
    if start < 0 or end <= start:
        return None, []
    try:
        data = json.loads(stdout[start:end + 1])
    except ValueError:
        return None, []
    media = [(str(i.get("media_name") or ""), str(i.get("local_path") or ""))
             for i in data.get("images") or [] if isinstance(i, dict)]
    return data.get("project_id"), media


def media_from_name(filename: str) -> str | None:
    m = _UUID.match(filename.lower())
    return m.group(0) if m else None


def load_source_meta(source_id: str) -> dict:
    """project_id + media ids (in upload order) of a finished generate job."""
    job_dir = WORK_DIR / source_id
    meta_file = META_DIR / f"{source_id}.json"
    if meta_file.exists():
        return json.loads(meta_file.read_text(encoding="utf-8"))
    # Jobs made before meta.json existed: rebuild from run.log + output files
    # (they were uploaded in sorted file-name order).
    run_log = job_dir / "run.log"
    if not run_log.exists():
        return {}
    text = run_log.read_text(encoding="utf-8", errors="replace")
    stdout = text.split("--- stdout ---", 1)[-1].split("--- stderr ---", 1)[0]
    project_id, _ = parse_generation_json(stdout)
    files = sorted(p for p in job_dir.rglob("*") if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
                   and p.parent.name != "refs" and "_2k" not in p.name)
    return {"project_id": project_id, "media": [media_from_name(p.name) for p in files]}


def run_upscale(job_id: str, source_id: str, index: int) -> Path:
    meta = load_source_meta(source_id)
    media = meta.get("media") or []
    project_id = meta.get("project_id")
    if not project_id or index < 0 or index >= len(media) or not media[index]:
        raise GenerationError("upscale_unavailable")
    out_dir = WORK_DIR / job_id / "out"
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "COLUMNS": "200"})
    env.setdefault("GFLOW_CLI_HOME", str(BASE_DIR / "gflow_home"))
    env.setdefault("GFLOW_CLI_FLOW_HOST", "flow.google.com")
    env.setdefault("GFLOW_CLI_LEASE_WAIT_SECONDS", "120")
    args = [sys.executable, str(LAUNCHER), "image", "upscale", media[index], "--scale", "2k",
            "--project", str(project_id), "--out", str(out_dir)]
    started = time.time()
    result = subprocess.run(
        args, cwd=str(BASE_DIR), env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=UPSCALE_TIMEOUT,
    )
    (WORK_DIR / job_id / "run.log").write_text(
        f"exit={result.returncode} seconds={time.time() - started:.1f}\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}\n",
        encoding="utf-8",
    )
    files = [p for p in out_dir.rglob("*") if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}]
    if not files:
        text = (result.stdout + "\n" + result.stderr).lower()
        if result.returncode == 22 or re.search(r"upscale.?unavailable|not available on (this|your) account|tile .* not found", text):
            raise GenerationError("upscale_unavailable", result.returncode)
        raise GenerationError(classify_error(result.stdout + "\n" + result.stderr), result.returncode)
    return max(files, key=lambda p: p.stat().st_mtime)


class GenerationError(Exception):
    def __init__(self, code: str, exit_code: int = -1):
        super().__init__(code)
        self.code = code
        self.exit_code = exit_code


def _error_summary(text: str) -> str:
    """class/title/detail of gflow's JSON error (its remediation hints mention
    'credits' etc. and must not drive classification)."""
    start = text.find("{")
    while start >= 0:
        end = text.find("\n}", start)
        if end < 0:
            break
        try:
            err = json.loads(text[start:end + 2]).get("error") or {}
            return " ".join(str(err.get(k) or "") for k in ("class", "title", "detail", "type"))
        except ValueError:
            start = text.find("{", start + 1)
    return ""


def classify_error(text: str) -> str:
    summary = _error_summary(text or "")
    if re.search(r"MediaUploadRejected|media-upload-rejected", summary):
        return "bad_request"
    if re.search(r"ProfileLocked", summary):
        return "generation_failed"
    low = (summary or text or "").lower()
    if re.search(r"image-to-image|not ported|unported|isn't ported|is not supported on|exit code 36|\\b36\\b.*migrat", low):
        return "refs_unsupported"
    if re.search(r"polic|safety|blocked|prohibited|violat|inappropriate|harm", low):
        return "content_blocked"
    if re.search(r"credit|quota|limit reached|rate.?limit|429|too many", low):
        return "quota"
    if re.search(r"timeout|timed out", low):
        return "timeout"
    if re.search(r"sign.?in|logged.?out|auth|401|session", low):
        return "worker_auth"
    return "generation_failed"


def cleanup_old() -> None:
    cutoff = time.time() - KEEP_LOCAL_DAYS * 86400
    if not WORK_DIR.exists():
        return
    for child in WORK_DIR.iterdir():
        try:
            if child.is_dir() and child.stat().st_mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True)
        except OSError:
            pass


# ---------------------------------------------------------------- site protocol

class Site:
    def __init__(self, endpoint: str, key: str):
        self.endpoint = endpoint
        self.client = httpx.Client(
            timeout=httpx.Timeout(60.0, connect=15.0),
            headers={"X-Worker-Key": key, "User-Agent": "m1-worker/1.0", "Accept": "application/json"},
            follow_redirects=False,
        )

    def _json(self, response: httpx.Response) -> dict:
        if response.status_code == 403:
            raise RuntimeError("site rejected worker key (403)")
        response.raise_for_status()
        try:
            return response.json()
        except ValueError:
            raise RuntimeError(f"site returned non-JSON (HTTP {response.status_code}): {response.text[:200]!r}")

    def next_job(self):
        data = self._json(self.client.post(self.endpoint, params={"action": "m1_next"}, json={}))
        return data.get("job")

    def complete(self, job_id: str, images: list[tuple[bytes, str, str]],
                 resolutions: list[str] | None = None) -> dict:
        files = [
            ("files[]", (f"image_{index + 1}.{ext}", body, mime))
            for index, (body, ext, mime) in enumerate(images)
        ]
        response = self.client.post(
            self.endpoint, params={"action": "m1_complete"},
            data={"job_id": job_id, "resolutions": ",".join(resolutions or [])}, files=files,
            timeout=httpx.Timeout(180.0, connect=15.0),
        )
        return self._json(response)

    def download_refs(self, job_id: str, urls: list) -> list[Path]:
        """Download reference images from the SAME site only."""
        origin = httpx.URL(self.endpoint)
        ref_dir = WORK_DIR / job_id / "refs"
        ref_dir.mkdir(parents=True, exist_ok=True)
        paths: list[Path] = []
        with httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0), follow_redirects=False,
                          headers={"User-Agent": "m1-worker/1.0"}) as plain:
            for index, raw in enumerate(urls[:MAX_REFS]):
                url = origin.join(str(raw))
                local_http = origin.scheme == "http" and origin.host in ("127.0.0.1", "localhost")
                if url.scheme != ("http" if local_http else "https") or url.host != origin.host or url.port != origin.port:
                    raise GenerationError("bad_request")
                response = plain.get(url)
                response.raise_for_status()
                data = response.content
                if len(data) > MAX_REF_BYTES:
                    raise GenerationError("bad_request")
                if data[:2] == b"\xff\xd8":
                    ext = "jpg"
                elif data[:8] == b"\x89PNG\r\n\x1a\n":
                    ext = "png"
                elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
                    ext = "webp"
                else:
                    raise GenerationError("bad_request")
                path = ref_dir / f"ref_{index + 1}.{ext}"
                path.write_bytes(data)
                paths.append(normalize_ref(path))
        return paths

    def fail(self, job_id: str, code: str) -> None:
        self._json(self.client.post(
            self.endpoint, params={"action": "m1_fail"}, json={"job_id": job_id, "error_code": code},
        ))


def process_upscale(site: Site, job: dict) -> None:
    job_id = str(job.get("id") or "")
    source_id = str(job.get("source_id") or "")
    try:
        index = int(job.get("index") or 0)
    except (TypeError, ValueError):
        index = -1
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,64}", source_id) or str(job.get("scale") or "2k").lower() != "2k":
        site.fail(job_id, "bad_request")
        return
    log(f"job {job_id}: upscale 2k source={source_id} index={index}")
    started = time.time()
    try:
        path = run_upscale(job_id, source_id, index)
        image = clean_image(path)
    except subprocess.TimeoutExpired:
        log(f"job {job_id}: timeout")
        site.fail(job_id, "timeout")
        return
    except GenerationError as exc:
        log(f"job {job_id}: failed code={exc.code} exit={exc.exit_code} (see worker_out/{job_id}/run.log)")
        site.fail(job_id, exc.code)
        return
    except Exception:
        log(f"job {job_id}: worker error\n{traceback.format_exc()}")
        site.fail(job_id, "generation_failed")
        return
    result = site.complete(job_id, [image], ["2k"])
    log(f"job {job_id}: done in {time.time() - started:.0f}s -> {result.get('urls') or result}")


def process(site: Site, job: dict) -> None:
    job_id = str(job.get("id") or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,64}", job_id):
        log(f"skip job with bad id: {job_id!r}")
        return
    if str(job.get("type") or "generate") == "upscale":
        process_upscale(site, job)
        return
    prompt = str(job.get("prompt") or "").strip()
    aspect = str(job.get("aspect_ratio") or "1:1")
    aspect = aspect if aspect in ALLOWED_ASPECTS else "1:1"
    try:
        count = max(1, min(4, int(job.get("count") or 1)))
    except (TypeError, ValueError):
        count = 1
    if not prompt:
        site.fail(job_id, "bad_request")
        return
    suffix = PROMPT_SUFFIX.strip()
    if suffix and suffix.lower() not in prompt.lower():
        prompt = f"{prompt.rstrip(' .')}. {suffix}"
    image_urls = job.get("image_urls") or []
    if not isinstance(image_urls, list):
        image_urls = []
    resolution = "2k" if str(job.get("resolution") or "1k").lower() == "2k" else "1k"
    log(f"job {job_id}: start aspect={aspect} count={count} refs={len(image_urls)} res={resolution}")
    started = time.time()
    shutil.rmtree(WORK_DIR / job_id, ignore_errors=True)
    resolutions: list[str] = []
    try:
        refs = site.download_refs(job_id, image_urls) if image_urls else []
        files = run_generation(job_id, prompt, aspect, count, refs, upscale_2k=resolution == "2k")
        if resolution == "2k":
            upscaled = []
            for index, path in enumerate(files):
                done = inline_2k(path)
                if done is not None:
                    upscaled.append(done)
                    resolutions.append("2k")
                    continue
                log(f"job {job_id}: inline 2k of image {index + 1} missing, separate upscale run")
                try:
                    upscaled.append(run_upscale(f"{job_id}_2k{index}", job_id, index))
                    resolutions.append("2k")
                except (GenerationError, subprocess.TimeoutExpired) as exc:
                    log(f"job {job_id}: 2k of image {index + 1} failed ({getattr(exc, 'code', 'timeout')}), sending 1k")
                    upscaled.append(path)
                    resolutions.append("1k")
            files = upscaled
        else:
            resolutions = ["1k"] * len(files)
        images = [clean_image(path) for path in files]
    except subprocess.TimeoutExpired:
        log(f"job {job_id}: timeout")
        site.fail(job_id, "timeout")
        return
    except GenerationError as exc:
        log(f"job {job_id}: failed code={exc.code} exit={exc.exit_code} (see worker_out/{job_id}/run.log)")
        site.fail(job_id, exc.code)
        return
    except Exception:
        log(f"job {job_id}: worker error\n{traceback.format_exc()}")
        site.fail(job_id, "generation_failed")
        return
    result = site.complete(job_id, images, resolutions)
    log(f"job {job_id}: done in {time.time() - started:.0f}s res={','.join(resolutions)} -> {result.get('urls') or result}")


PROMPT_SUFFIX = DEFAULT_PROMPT_SUFFIX


def load_config() -> tuple[str, str]:
    global PROMPT_SUFFIX
    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    PROMPT_SUFFIX = str(cfg.get("prompt_suffix", DEFAULT_PROMPT_SUFFIX))
    endpoint = str(cfg.get("endpoint") or "").strip()
    key = str(cfg.get("worker_key") or "").strip()
    local_http = endpoint.startswith(("http://127.0.0.1:", "http://localhost:"))
    if (not endpoint.startswith("https://") and not local_http) or "YOUR-SITE.example" in endpoint or not key:
        raise SystemExit("worker_config.json: set endpoint (https://... or local http://127.0.0.1:...) and worker_key")
    return endpoint, key


_INSTANCE_LOCK = None


def acquire_single_instance() -> None:
    """Only one worker may drive the browser profile. A second copy waits here
    until the first one exits (e.g. a second worker.ps1 window)."""
    global _INSTANCE_LOCK
    import socket
    warned = False
    while True:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows: never share the port
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            sock.bind(("127.0.0.1", 47613))
            sock.listen(1)
            _INSTANCE_LOCK = sock
            return
        except OSError:
            sock.close()
            if not warned:
                print("another worker is already running - this window waits until it stops", flush=True)
                warned = True
            time.sleep(15)


def main() -> None:
    acquire_single_instance()
    endpoint, key = load_config()
    site = Site(endpoint, key)
    log(f"worker started, polling {endpoint}")
    errors = 0
    last_cleanup = 0.0
    own_mtime = Path(__file__).stat().st_mtime
    while True:
        try:
            if Path(__file__).stat().st_mtime != own_mtime:
                log("worker.py changed on disk - exiting so the launcher restarts it")
                return
            if time.time() - last_cleanup > 3600:
                cleanup_old()
                last_cleanup = time.time()
            job = site.next_job()
            errors = 0
            if not job:
                time.sleep(POLL_IDLE_SECONDS)
                continue
            process(site, job)
        except KeyboardInterrupt:
            log("worker stopped")
            return
        except Exception as exc:
            delay = POLL_ERROR_BACKOFF[min(errors, len(POLL_ERROR_BACKOFF) - 1)]
            errors += 1
            log(f"poll error: {exc} (retry in {delay}s)")
            time.sleep(delay)


def upscale_test(source_id: str, index: int) -> None:
    """Local check of the 2K path on an already finished job (nothing is sent to the site)."""
    job_id = f"test2k_{source_id}_{index}"[:64]
    print(f"meta: {load_source_meta(source_id)}")
    started = time.time()
    path = run_upscale(job_id, source_id, index)
    body, ext, _ = clean_image(path)
    out = WORK_DIR / job_id / f"result_2k.{ext}"
    out.write_bytes(body)
    size = ""
    try:
        from PIL import Image
        with Image.open(out) as img:
            size = f" {img.width}x{img.height}"
    except Exception:
        pass
    print(f"OK in {time.time() - started:.0f}s:{size} {len(body) // 1024} KB -> {out}")


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "upscale-test":
        upscale_test(sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 0)
    else:
        main()
