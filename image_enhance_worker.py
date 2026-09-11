"""Private photo enhancement worker for passport/4x6/name-date photos.

This deliberately uses conservative, identity-preserving processing. It does not
invent facial details or perform face replacement/restoration.
"""
import base64, io, os, json, urllib.request, urllib.error
from flask import Flask, jsonify, request
from PIL import Image, ImageOps
import cv2
import numpy as np

app = Flask(__name__)
MAX_BYTES = int(os.getenv("MAX_IMAGE_BYTES", "12000000"))
API_KEY = os.getenv("ENHANCE_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_IMAGE_MODEL = os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-2.5-sunburst").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_IMAGE_MODEL = os.getenv("GEMINI_IMAGE_MODEL", "gemini-3.1-flash-image").strip()

def _authorized():
    return bool(API_KEY) and request.headers.get("X-Enhance-Key", "") == API_KEY

def _process(raw: bytes, level: int) -> tuple[bytes, str]:
    original = ImageOps.exif_transpose(Image.open(io.BytesIO(raw)))
    has_alpha = "A" in original.getbands()
    src = original.convert("RGBA" if has_alpha else "RGB")
    alpha = np.asarray(src)[:, :, 3] if has_alpha else None
    bgr = cv2.cvtColor(np.asarray(src), cv2.COLOR_RGBA2BGR if has_alpha else cv2.COLOR_RGB2BGR)
    # Gentle denoise, local contrast and unsharp mask. No facial synthesis.
    strength = max(0.0, min(1.0, level / 100.0))
    if strength:
        bgr = cv2.fastNlMeansDenoisingColored(bgr, None, 2 + int(3 * strength), 2 + int(3 * strength), 7, 21)
        lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
        l, a, b = cv2.split(lab)
        clahe = cv2.createCLAHE(clipLimit=1.0 + 0.7 * strength, tileGridSize=(8, 8))
        lab = cv2.merge((clahe.apply(l), a, b))
        bgr = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)
        blur = cv2.GaussianBlur(bgr, (0, 0), 1.0)
        bgr = cv2.addWeighted(bgr, 1.0 + 0.35 * strength, blur, -0.35 * strength, 0)
    out = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    buf = io.BytesIO()
    if has_alpha:
        rgba = np.dstack((out, alpha))
        Image.fromarray(rgba, "RGBA").save(buf, format="PNG", optimize=True)
        return buf.getvalue(), "image/png"
    Image.fromarray(out, "RGB").save(buf, format="JPEG", quality=96, optimize=True)
    return buf.getvalue(), "image/jpeg"

@app.get("/health")
def health():
    return jsonify(success=True, service="image-enhance", mode="conservative")

@app.post("/enhance")
def enhance():
    if not _authorized():
        return jsonify(success=False, error="Unauthorized"), 401
    raw = request.files.get("image")
    if raw and raw.filename:
        data = raw.read(MAX_BYTES + 1)
    elif request.is_json and request.json.get("imageBase64"):
        try:
            data = base64.b64decode(request.json["imageBase64"], validate=True)
        except Exception:
            return jsonify(success=False, error="invalid imageBase64"), 400
    else:
        return jsonify(success=False, error="image file is required"), 400
    if len(data) > MAX_BYTES:
        return jsonify(success=False, error="image is too large"), 413
    try:
        level = int((request.form.get("level") if not request.is_json else request.json.get("level", 35)) or 35)
        result, mime = _process(data, level)
        return jsonify(success=True, imageBase64=base64.b64encode(result).decode("ascii"), mimeType=mime)
    except Exception:
        app.logger.exception("enhancement failed")
        return jsonify(success=False, error="Image enhancement failed"), 422

def _multipart_form(fields, file_field, filename, content, mime_type):
    boundary = "----SkilloPassportBoundary" + base64.b16encode(os.urandom(12)).decode("ascii")
    chunks = []
    for key, value in fields.items():
        chunks.extend([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode(),
            str(value).encode("utf-8"),
            b"\r\n",
        ])
    chunks.extend([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'.encode(),
        f"Content-Type: {mime_type}\r\n\r\n".encode(),
        content,
        b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ])
    return b"".join(chunks), boundary

@app.post("/clean-passport")
def clean_passport():
    """Prompt-based passport edit; provider keys stay on this private worker."""
    if not _authorized():
        return jsonify(success=False, error="Unauthorized"), 401
    if not GEMINI_API_KEY and not OPENAI_API_KEY:
        return jsonify(success=False, configured=False, error="Prompt AI service is not configured."), 503
    body = request.get_json(silent=True) or {}
    encoded = str(body.get("imageBase64", ""))
    encoded = encoded.split(",", 1)[1] if "," in encoded else encoded
    if not encoded or len(encoded) > 17000000:
        return jsonify(success=False, error="Valid passport image is required."), 400
    try:
        image_bytes = base64.b64decode(encoded, validate=True)
    except Exception:
        return jsonify(success=False, error="Invalid passport image encoding."), 400
    prompt = str(body.get("prompt", "")).strip()
    if not prompt or len(prompt) > 5000:
        return jsonify(success=False, error="A valid passport prompt is required."), 400
    if GEMINI_API_KEY:
        # Use Google's documented generateContent REST shape for image editing:
        # text and the source image are sent as parts of one user content block.
        # Keep the key server-side and request image-only output.
        contents = [{
            "parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": "image/png", "data": encoded}},
            ]
        }]
        # Some Gemini projects reject optional imageConfig fields even though the
        # model supports them. Retry once with Google's minimal REST payload.
        gemini_payloads = [
            {
                "contents": contents,
                "generationConfig": {
                    "responseModalities": ["IMAGE"],
                    "responseFormat": {
                        "image": {"aspectRatio": "4:5", "imageSize": "1K"}
                    },
                },
            },
            {"contents": contents},
        ]
        result = None
        last_code = 502
        last_detail = "request rejected"
        for attempt, gemini_payload in enumerate(gemini_payloads):
            req = urllib.request.Request(
                "https://generativelanguage.googleapis.com/v1/models/"
                + GEMINI_IMAGE_MODEL
                + ":generateContent",
                data=json.dumps(gemini_payload).encode("utf-8"),
                headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=120) as response:
                    result = json.loads(response.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as exc:
                last_code = exc.code
                raw_detail = exc.read().decode("utf-8", errors="replace")[:500]
                try:
                    parsed_detail = json.loads(raw_detail)
                    last_detail = str((parsed_detail.get("error") or {}).get("message") or raw_detail)
                except Exception:
                    last_detail = raw_detail
                app.logger.warning("Gemini passport edit failed (HTTP %s, attempt %s): %s", exc.code, attempt + 1, last_detail)
                if exc.code == 400 and attempt == 0:
                    continue
                safe_detail = " ".join(last_detail.split())[:180]
                return jsonify(success=False, error=f"Gemini rejected the request (HTTP {last_code}): {safe_detail}"), 502
            except Exception:
                app.logger.exception("Gemini passport edit unavailable")
                return jsonify(success=False, error="Gemini prompt service is temporarily unavailable."), 502
        if result is None:
            safe_detail = " ".join(last_detail.split())[:180]
            return jsonify(success=False, error=f"Gemini rejected the request (HTTP {last_code}): {safe_detail}"), 502
        image_data = None
        for candidate in result.get("candidates", []) or []:
            content = candidate.get("content") or {}
            for block in content.get("parts", []) or []:
                inline = block.get("inlineData") or block.get("inline_data") or {}
                if inline.get("data"):
                    image_data = inline["data"]
                    break
            if image_data:
                break
        if not image_data:
            return jsonify(success=False, error="Gemini prompt service returned no image."), 502
        return jsonify(success=True, mode="prompt-ai", provider="gemini", model=GEMINI_IMAGE_MODEL, imageBase64=image_data, mimeType="image/png")

    fields = {"model": OPENAI_IMAGE_MODEL, "prompt": prompt, "size": "1024x1536", "quality": "medium"}
    payload, boundary = _multipart_form(fields, "image[]", "passport-source.png", image_bytes, "image/png")
    req = urllib.request.Request(
        "https://api.openai.com/v1/images/edits",
        data=payload,
        headers={"Authorization": "Bearer " + OPENAI_API_KEY, "Content-Type": "multipart/form-data; boundary=" + boundary},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        app.logger.warning("OpenAI passport edit failed: %s", detail)
        return jsonify(success=False, error="Prompt AI service rejected the image."), 502
    except Exception:
        app.logger.exception("OpenAI passport edit unavailable")
        return jsonify(success=False, error="Prompt AI service is temporarily unavailable."), 502
    image_data = ((result.get("data") or [{}])[0]).get("b64_json")
    if not image_data:
        return jsonify(success=False, error="Prompt AI returned no image."), 502
    return jsonify(success=True, mode="prompt-ai", provider="openai", model=OPENAI_IMAGE_MODEL, imageBase64=image_data, mimeType="image/png")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
