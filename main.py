import os
from datetime import datetime

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# Optional database usage (metadata), will not fail if db not configured
try:
    from database import db, create_document, get_documents  # type: ignore
except Exception:
    db = None
    def create_document(*args, **kwargs):
        return None
    def get_documents(*args, **kwargs):
        return []

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Ensure videos directory exists and is served statically
VIDEOS_DIR = os.path.abspath("videos")
os.makedirs(VIDEOS_DIR, exist_ok=True)
app.mount("/videos", StaticFiles(directory=VIDEOS_DIR), name="videos")


class GenerateRequest(BaseModel):
    text: str = Field(..., min_length=1, description="Teks yang akan dijadikan video")
    duration: int = Field(60, ge=60, le=600, description="Durasi video dalam detik (min 60, max 600)")
    width: int = Field(1280, ge=320, le=1920)
    height: int = Field(720, ge=240, le=1080)
    fps: int = Field(24, ge=10, le=60)
    background: str = Field("#0f172a", description="Warna latar belakang (hex)")
    text_color: str = Field("#e2e8f0", description="Warna teks (hex)")


@app.get("/")
def read_root():
    return {"message": "Text-to-Video API is running", "videos_path": "/videos"}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/hello")
def hello():
    return {"message": "Hello from the backend API!"}


@app.get("/test")
def test_database():
    response = {
        "backend": "✅ Running",
        "database": "❌ Not Available" if db is None else "✅ Available",
    }
    return response


def hex_to_rgb(hex_color: str):
    hex_color = hex_color.lstrip('#')
    lv = len(hex_color)
    if lv not in (3, 6):
        return (15, 23, 42)
    if lv == 3:
        hex_color = ''.join([c*2 for c in hex_color])
    return tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))


def wrap_text(text: str, draw, font, max_width: int):
    # Simple word wrap based on pixel width
    words = text.split()
    lines = []
    line = ""
    for w in words:
        test = (line + " " + w).strip()
        bbox = draw.textbbox((0, 0), test, font=font)
        width = bbox[2] - bbox[0]
        if width <= max_width or not line:
            line = test
        else:
            lines.append(line)
            line = w
    if line:
        lines.append(line)
    return lines


def generate_scrolling_text_frames(text: str, width: int, height: int, fps: int, duration: int, bg_color: tuple, text_color: tuple):
    # Lazy imports to avoid startup failures if optional libs missing
    try:
        import numpy as np
        from PIL import Image, ImageDraw, ImageFont
    except ModuleNotFoundError as e:
        raise HTTPException(status_code=500, detail=f"Dependency missing: {e.name}. Please try again in a moment.")

    # Load a default font
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", size=int(height * 0.05))
    except Exception:
        font = ImageFont.load_default()

    # Pre-render lines and compute total height
    img = Image.new("RGB", (width, height), bg_color)
    draw = ImageDraw.Draw(img)
    margin_x = int(width * 0.08)
    max_text_width = width - 2 * margin_x
    lines = wrap_text(text, draw, font, max_text_width)
    line_spacing = int((draw.textbbox((0,0), "Ay", font=font)[3]) * 1.4)

    total_text_height = max(len(lines) * line_spacing, height) + height  # add extra height for full scroll through

    # Scroll from bottom to top across duration
    total_frames = duration * fps
    start_y = height
    end_y = -total_text_height

    for frame_idx in range(total_frames):
        t = frame_idx / (total_frames - 1 if total_frames > 1 else 1)
        current_offset = int(start_y + (end_y - start_y) * t)
        frame = Image.new("RGB", (width, height), bg_color)
        draw_f = ImageDraw.Draw(frame)
        y = current_offset
        for line in lines:
            draw_f.text((margin_x, y), line, font=font, fill=text_color)
            y += line_spacing
        yield np.array(frame)


def render_text_video(text: str, width: int, height: int, fps: int, duration: int, background: str, text_color: str, out_path: str):
    # Lazy import imageio to avoid startup failures
    try:
        import imageio
    except ModuleNotFoundError as e:
        raise HTTPException(status_code=500, detail=f"Dependency missing: {e.name}. Please try again in a moment.")

    bg_rgb = hex_to_rgb(background)
    text_rgb = hex_to_rgb(text_color)
    writer = imageio.get_writer(out_path, fps=fps, codec='libx264', quality=8)
    try:
        for frame in generate_scrolling_text_frames(text, width, height, fps, duration, bg_rgb, text_rgb):
            writer.append_data(frame)
    finally:
        writer.close()


@app.post("/api/generate")
def generate_video(req: GenerateRequest):
    text = req.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Teks tidak boleh kosong")
    duration = max(60, int(req.duration))

    filename = f"video_{datetime.utcnow().strftime('%Y%m%d_%H%M%S_%f')}.mp4"
    out_path = os.path.join(VIDEOS_DIR, filename)

    try:
        render_text_video(
            text=text,
            width=req.width,
            height=req.height,
            fps=req.fps,
            duration=duration,
            background=req.background,
            text_color=req.text_color,
            out_path=out_path,
        )
    except HTTPException:
        # re-raise
        raise
    except Exception as e:
        # Clean up partial file
        if os.path.exists(out_path):
            try:
                os.remove(out_path)
            except Exception:
                pass
        raise HTTPException(status_code=500, detail=f"Gagal membuat video: {str(e)[:200]}")

    # Save metadata if db available
    try:
        meta = {
            "filename": filename,
            "path": out_path,
            "duration": duration,
            "width": req.width,
            "height": req.height,
            "fps": req.fps,
            "created_at": datetime.utcnow().isoformat(),
            "text_length": len(text),
        }
        create_document("video", meta)
    except Exception:
        pass

    return {
        "status": "ok",
        "filename": filename,
        "url": f"/videos/{filename}",
        "duration": duration,
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
