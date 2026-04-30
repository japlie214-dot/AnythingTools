# utils/vision_utils.py
import os
import base64
import io
import threading
import shutil
from datetime import datetime, timezone
from PIL import Image, ImageStat

from botasaurus.browser import Driver

from utils.logger import get_dual_logger
from utils.id_generator import ULID

log = get_dual_logger(__name__)

SCREENSHOT_LOG_DIR = "logs/screenshots"
os.makedirs(SCREENSHOT_LOG_DIR, exist_ok=True)

_IMAGE_PAYLOAD_LIMIT_BYTES = 20 * 1024 * 1024  # 20 MB hard ceiling
_vision_lock = threading.Lock()
# Hard ceiling before PIL.open() is attempted; prevents single-file OOM.
IMAGE_OPEN_MAX_BYTES: int = 500 * 1024 * 1024   # 500 MB
# Grayscale pixel variance below this value → slice classified as empty.
_SLICE_VARIANCE_THRESHOLD: float = 5.0


def _optimize_image_for_api(image_path: str, target_mb: int = 10) -> str | None:
    """
    Iteratively compress an image to fit within LLM payload limits.
    Returns a Base64-encoded JPEG string, or None on failure.
    """
    try:
        from PIL import Image
        target_bytes = target_mb * 1024 * 1024
        with Image.open(image_path) as img:
            if img.mode != "RGB":
                img = img.convert("RGB")
            max_dim = 2048
            if max(img.size) > max_dim:
                img.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            quality = 85
            while True:
                buf.seek(0)
                buf.truncate(0)
                img.save(buf, format="JPEG", quality=quality)
                size = buf.tell()
                if size < target_bytes or quality <= 20:
                    break
                quality -= 10
            log.dual_log(
                tag="Vision:Optimize",
                message=f"Image optimized. Final size: {size / 1024 / 1024:.2f}MB",
                payload={"final_size_bytes": size},
            )
            return base64.b64encode(buf.getvalue()).decode("utf-8")
    except Exception as e:
        log.dual_log(
            tag="Vision:Optimize",
            message="Image optimization failed.",
            level="ERROR",
            exc_info=e,
            payload={"error": str(e)},
        )
        return None


def capture_and_optimize(driver: Driver, step_index: int) -> list[dict]:
    """
    Capture a screenshot, slice it if it exceeds 2048×2048, compress each slice,
    persist all slices to SCREENSHOT_LOG_DIR for audit, and return a list of
    image metadata dicts ready for LLM submission.

    All dicts share a single event_id (ULID) generated once per call.
    Each dict has keys: b64, path, mime, status, event_id.
      status == "OK"                     → safe to submit; b64 is populated.
      status == "EMPTY_SLICE_DISCARDED"  → low-variance slice; b64 is None.
      status == "IMAGE_TOO_LARGE_SKIPPED"→ pre-open guard fired; b64 is None.
      status == "Screenshot Analysis Unavailable" → capture failed; b64 is None.

    Never raises — returns a typed status list on every failure path.
    Audit writes always precede any return path (Golden Rule 2 compliance).
    """
    event_id = ULID.generate()
    ts       = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    raw_path = os.path.join(
        SCREENSHOT_LOG_DIR, f"step_{step_index:02d}_{ts}_{event_id}.png"
    )

    # ── Capture ───────────────────────────────────────────────────────────────
    try:
        driver.save_screenshot(raw_path)
    except Exception as exc:
        log.dual_log(
            tag="Vision:Capture",
            message=f"save_screenshot failed: {exc}",
            level="ERROR",
            exc_info=exc,
            payload={"error": str(exc)},
        )
        return [{"b64": None, "path": None, "mime": None,
                 "status": "Screenshot Analysis Unavailable", "event_id": event_id}]

    # ── Pre-open file-size guard (executed WITHOUT the lock) ──────────────────
    raw_size = os.path.getsize(raw_path)
    if raw_size > IMAGE_OPEN_MAX_BYTES:
        log.dual_log(
            tag="Vision:Guard",
            message=f"Raw PNG exceeds {IMAGE_OPEN_MAX_BYTES // (1024**2)} MB pre-open limit. Skipping PIL.",
            level="WARNING",
            payload={"bytes": raw_size},
        )
        # Audit file (raw_path) remains on disk per Golden Rule 2.
        return [{"b64": None, "path": raw_path, "mime": "image/png",
                 "status": "IMAGE_TOO_LARGE_SKIPPED", "event_id": event_id}]

    # ── Serialised PIL section ────────────────────────────────────────────────
    with _vision_lock:
        orig_max            = Image.MAX_IMAGE_PIXELS
        Image.MAX_IMAGE_PIXELS = None          # Safe bypass; restored in finally.
        try:
            results: list[dict] = []

            from utils.budget import calculate_image_cost
            with Image.open(raw_path) as img:
                if img.mode != "RGB":
                    img = img.convert("RGB")
                w, h = img.size
                
                total_char_cost = calculate_image_cost(w, h)

                if w <= 2048 and h <= 2048:
                    # ── Single-slice path (small screenshot) ─────────────────
                    jpg_path = os.path.join(
                        SCREENSHOT_LOG_DIR,
                        f"step_{step_index:02d}_{ts}_{event_id}.jpg",
                    )
                    buf     = io.BytesIO()
                    quality = 85
                    while True:
                        buf.seek(0); buf.truncate(0)
                        img.save(buf, format="JPEG", quality=quality)
                        if buf.tell() < 10 * 1024 * 1024 or quality <= 20:
                            break
                        quality -= 10
                    jpeg_bytes = buf.getvalue()
                    with open(jpg_path, "wb") as fh:
                        fh.write(jpeg_bytes)
                    if os.path.exists(raw_path):
                        os.remove(raw_path)
                    if len(jpeg_bytes) > _IMAGE_PAYLOAD_LIMIT_BYTES:
                        log.dual_log(
                            tag="Vision:Guard",
                            message="Single-slice JPEG exceeds 20 MB payload limit. HTML-only.",
                            level="WARNING",
                            payload={"bytes": len(jpeg_bytes)},
                        )
                        return [{"b64": None, "path": jpg_path, "mime": "image/jpeg",
                                 "status": "Screenshot Analysis Unavailable",
                                 "event_id": event_id}]
                    b64 = base64.b64encode(jpeg_bytes).decode("utf-8")
                    return [{"b64": b64, "path": jpg_path, "mime": "image/jpeg",
                             "status": "OK", "event_id": event_id, "total_char_cost": total_char_cost}]

                # ── Multi-slice path (large screenshot) ───────────────────────
                # Orientation: slice along the longer dimension.
                is_vertical = h > w
                dim_length  = h if is_vertical else w
                SLICE_SIZE  = 2048
                OVERLAP     = 205      # 10% of 2048, integer-truncated
                STRIDE      = SLICE_SIZE - OVERLAP    # 1843 pixels

                slice_boxes: list[tuple] = []
                offset = 0
                while offset < dim_length:
                    end = min(offset + SLICE_SIZE, dim_length)
                    box = (0, offset, w, end) if is_vertical else (offset, 0, end, h)
                    slice_boxes.append(box)
                    if end == dim_length:
                        break
                    offset += STRIDE

                for idx, box in enumerate(slice_boxes):
                    slice_img = img.crop(box)
                    if max(slice_img.size) > 2048:
                        slice_img.thumbnail((2048, 2048), Image.Resampling.LANCZOS)

                    # Variance filter — discard blank/solid-color slices.
                    stat = ImageStat.Stat(slice_img.convert("L"))
                    if stat.var[0] < _SLICE_VARIANCE_THRESHOLD:
                        results.append({
                            "b64":      None,
                            "path":     None,
                            "mime":     None,
                            "status":   "EMPTY_SLICE_DISCARDED",
                            "event_id": event_id,
                        })
                        continue

                    # Compression loop (85 → 20 in steps of 10).
                    buf     = io.BytesIO()
                    quality = 85
                    while True:
                        buf.seek(0); buf.truncate(0)
                        slice_img.save(buf, format="JPEG", quality=quality)
                        if buf.tell() < 10 * 1024 * 1024 or quality <= 20:
                            break
                        quality -= 10
                    slice_bytes = buf.getvalue()

                    if len(slice_bytes) > _IMAGE_PAYLOAD_LIMIT_BYTES:
                        results.append({
                            "b64":      None,
                            "path":     None,
                            "mime":     None,
                            "status":   "Screenshot Analysis Unavailable",
                            "event_id": event_id,
                        })
                        continue

                    slice_path = os.path.join(
                        SCREENSHOT_LOG_DIR,
                        f"step_{step_index:02d}_{ts}_{event_id}_slice_{idx}.jpg",
                    )
                    with open(slice_path, "wb") as fh:
                        fh.write(slice_bytes)
                    b64_slice = base64.b64encode(slice_bytes).decode("utf-8")
                    slice_cost = calculate_image_cost(slice_img.width, slice_img.height)
                    results.append({
                        "b64":      b64_slice,
                        "path":     slice_path,
                        "mime":     "image/jpeg",
                        "status":   "OK",
                        "event_id": event_id,
                        "total_char_cost": slice_cost,
                    })

            if os.path.exists(raw_path):
                try:
                    os.remove(raw_path)
                except OSError:
                    pass
            return results

        finally:
            Image.MAX_IMAGE_PIXELS = orig_max    # Always restored.
