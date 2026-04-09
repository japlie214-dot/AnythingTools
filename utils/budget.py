# utils/budget.py
"""Budgeting engine for accurate character cost calculation of attachments."""
import math
import statistics
import fitz


def calculate_image_cost(width: int, height: int) -> int:
    """OpenAI high-res tile logic mapped to character cost (* 4)."""
    if max(width, height) > 2048:
        scale = 2048 / max(width, height)
        width, height = int(width * scale), int(height * scale)
    if min(width, height) > 768:
        scale = 768 / min(width, height)
        width, height = int(width * scale), int(height * scale)
    
    tiles_w = math.ceil(width / 512)
    tiles_h = math.ceil(height / 512)
    total_tiles = tiles_w * tiles_h
    total_tokens = (total_tiles * 170) + 85
    return total_tokens * 4


def calculate_pdf_cost(file_path: str) -> dict:
    """Evaluates PDF for textual or visual density to assign an accurate character budget."""
    try:
        doc = fitz.open(file_path)
        try:
            if doc.needs_pass or doc.is_encrypted:
                return {"cost": 0, "status": "REJECTED_PASSWORD", "reason": "Password protected"}
            
            total_pages = len(doc)
            if total_pages == 0:
                return {"cost": 0, "status": "REJECTED_CORRUPTED", "reason": "Zero pages"}

            s_size = max(2, min(10, int(2 + math.log(total_pages))))
            step = max(1, total_pages // s_size)
            samples = [i for i in range(0, total_pages, step)][:s_size]

            chars_per_page = [len(doc[i].get_text()) for i in samples]
            avg_chars = sum(chars_per_page) / len(chars_per_page)

            if len(chars_per_page) > 1:
                mean = statistics.mean(chars_per_page)
                if mean > 0:
                    cv = statistics.stdev(chars_per_page) / mean
                    if cv > 0.3 and s_size < total_pages:
                        s_size = min(total_pages, s_size * 2)
                        step = max(1, total_pages // s_size)
                        samples = [i for i in range(0, total_pages, step)][:s_size]
                        chars_per_page = [len(doc[i].get_text()) for i in samples]
                        avg_chars = sum(chars_per_page) / len(chars_per_page)

            if avg_chars >= 100:
                return {"cost": int(avg_chars * total_pages * 1.1), "status": "OK", "type": "DIGITAL"}

            from paddleocr import PaddleOCR
            import io
            from PIL import Image
            import numpy as np

            ocr = PaddleOCR(use_angle_cls=True, lang='en', use_gpu=False, show_log=False)
            ocr_chars = []

            for i in samples:
                pix = doc[i].get_pixmap(dpi=150)
                img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
                # PaddleOCR expects numpy arrays in BGR format
                img_bgr = np.array(img)[:, :, ::-1]
                result = ocr.ocr(img_bgr, cls=True)
                page_text = ""
                if result and result[0]:
                    for line in result[0]:
                        page_text += line[1][0] + " "
                ocr_chars.append(len(page_text))

            ocr_avg = sum(ocr_chars) / len(ocr_chars)
            return {"cost": int(ocr_avg * total_pages * 1.1), "status": "OK", "type": "SCANNED"}
        finally:
            doc.close()

    except Exception as e:
        return {"cost": 0, "status": "REJECTED_CORRUPTED", "reason": str(e)}
