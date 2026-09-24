"""TREX MES Cloud ile yerel Ollama arasında anahtar korumalı, salt amaçlı köprü."""
from __future__ import annotations

import os

import requests
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse


app = FastAPI(title="TREX Ollama Bridge", docs_url=None, redoc_url=None)
LOCAL_OLLAMA_URL = os.environ.get("OLLAMA_LOCAL_URL", "http://127.0.0.1:11434").rstrip("/")
BRIDGE_TOKEN = os.environ.get("TREX_OLLAMA_TOKEN", "").strip()


def require_token(authorization: str = Header(default="")):
    if not BRIDGE_TOKEN:
        raise HTTPException(status_code=503, detail="TREX_OLLAMA_TOKEN tanımlı değil")
    if authorization != f"Bearer {BRIDGE_TOKEN}":
        raise HTTPException(status_code=401, detail="Geçersiz köprü anahtarı")


def forward(method: str, path: str, payload=None):
    try:
        response = requests.request(method, f"{LOCAL_OLLAMA_URL}{path}", json=payload, timeout=125)
    except requests.RequestException as exc:
        raise HTTPException(status_code=503, detail="Yerel Ollama erişilemiyor") from exc
    try:
        content = response.json()
    except ValueError:
        content = {"detail": response.text[:500]}
    return JSONResponse(status_code=response.status_code, content=content)


@app.get("/health")
def health(_: None = Depends(require_token)):
    response = requests.get(f"{LOCAL_OLLAMA_URL}/api/tags", timeout=4)
    return {"status": "ok", "ollama": response.ok}


@app.get("/api/tags")
def tags(_: None = Depends(require_token)):
    return forward("GET", "/api/tags")


@app.post("/api/chat")
def chat(payload: dict, _: None = Depends(require_token)):
    payload["stream"] = False
    return forward("POST", "/api/chat", payload)


