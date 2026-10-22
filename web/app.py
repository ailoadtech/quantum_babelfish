"""FastAPI backend for the qbabel web playground."""

import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from qbabel_core import SUPPORTED_LANGUAGES, emit, ir_to_json, translate  # noqa: E402

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="qbabel", description="Quantum program translation playground")

# A Bell circuit in the IR, emitted per language, gives every pane a real sample.
SAMPLE_IR = {
    "ir_version": "1.0",
    "program": {
        "name": "bell_state",
        "qubits": [{"id": "q0"}, {"id": "q1"}],
        "classical_bits": [{"id": "c0"}, {"id": "c1"}],
        "operations": [
            {"type": "gate", "name": "H", "targets": ["q0"]},
            {"type": "gate", "name": "CNOT", "controls": ["q0"], "targets": ["q1"]},
            {"type": "measure", "qubits": ["q0", "q1"], "results": ["c0", "c1"]},
        ],
    },
}

SAMPLES = {lang: emit(SAMPLE_IR, lang)["code"] for lang in SUPPORTED_LANGUAGES}


class TranslationRequest(BaseModel):
    source: str
    from_lang: str
    to_lang: str
    name: str = "circuit"


@app.get("/api/languages")
def languages():
    return {"languages": SUPPORTED_LANGUAGES}


@app.get("/api/samples")
def samples():
    return {"samples": SAMPLES}


@app.post("/api/translate")
def translate_endpoint(request: TranslationRequest):
    for lang in (request.from_lang, request.to_lang):
        if lang.lower() not in SUPPORTED_LANGUAGES:
            return JSONResponse(status_code=400,
                                content={"error": "unsupported language: %s" % lang})
    if not request.source.strip():
        return {"code": "", "ir": None, "warnings": []}
    try:
        result = translate(request.source, request.from_lang, request.to_lang,
                           name=request.name or "circuit")
    except Exception as exc:  # surface parser failures to the UI
        return JSONResponse(status_code=400,
                            content={"error": "%s: %s" % (type(exc).__name__, exc)})
    return {"code": result["code"], "ir": result["ir"],
            "warnings": result["warnings"]}


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
