"""A stub OpenAI-compatible model server for setup runs without a real one.

    uv run python tools/testdata/model_stub.py --port 8900

Answers GET /v1/models (the stub's and make.py's model ids), POST /v1/chat/completions
(streamed or not, one canned sentence), POST /v1/embeddings (fixed small vectors),
POST /v1/rerank (scores by length), POST /v1/audio/transcriptions (one canned line),
POST /v1/audio/speech (a short tone as WAV) and GET /v1/voices. Standard library only;
one request at a time.
"""
from __future__ import annotations

import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

MODELS = [{"id": m, "object": "model"} for m in (
    "stub-chat", "stub-embed", "stub-rerank",
    "qwen-chat", "qwen-small", "embed", "rerank", "whisper", "kokoro")]     # make.py's
HEARD = "This is the stub model. It heard this line."


def tone(seconds: float = 0.6, hz: int = 440, rate: int = 16000) -> bytes:
    """A sine tone as 16-bit mono WAV."""
    import math
    import struct
    n = int(seconds * rate)
    pcm = b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * hz * i / rate)))
                   for i in range(n))
    return (b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt "
            + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
            + b"data" + struct.pack("<I", len(pcm)) + pcm)
REPLY = "This is the stub model. Every answer is this sentence."


class Handler(BaseHTTPRequestHandler):
    def _json(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") in ("/v1/models", "/models"):
            self._json(200, {"object": "list", "data": MODELS})
        elif self.path.rstrip("/").endswith("/voices"):
            self._json(200, {"voices": ["alloy", "nova", "shimmer"]})
        elif self.path.rstrip("/") in ("/health", "/v1/health", ""):
            self._json(200, {"status": "ok"})
        else:
            self._json(404, {"error": {"message": f"no route {self.path}"}})

    def do_POST(self) -> None:  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        path = self.path.rstrip("/")
        if path.endswith("/audio/transcriptions"):         # multipart: the body is not read
            self._json(200, {"text": HEARD})
            return
        body = json.loads(raw or b"{}")
        if path.endswith("/audio/speech"):
            wav = tone()
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(wav)))
            self.end_headers()
            self.wfile.write(wav)
        elif path.endswith("/chat/completions"):
            model = body.get("model", "stub-chat")
            if body.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for word in REPLY.split(" "):
                    chunk = {"id": "stub", "object": "chat.completion.chunk", "model": model,
                             "choices": [{"index": 0, "delta": {"content": word + " "},
                                          "finish_reason": None}]}
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                    self.wfile.flush()
                    time.sleep(0.02)
                done = {"id": "stub", "object": "chat.completion.chunk", "model": model,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
                self.wfile.write(f"data: {json.dumps(done)}\n\ndata: [DONE]\n\n".encode())
                return
            self._json(200, {"id": "stub", "object": "chat.completion", "model": model,
                             "choices": [{"index": 0, "finish_reason": "stop",
                                          "message": {"role": "assistant", "content": REPLY}}],
                             "usage": {"prompt_tokens": 1, "completion_tokens": 10,
                                       "total_tokens": 11}})
        elif path.endswith("/embeddings"):
            inputs = body.get("input") or []
            if isinstance(inputs, str):
                inputs = [inputs]
            data = [{"object": "embedding", "index": i,
                     "embedding": [(len(t) % 7) / 7.0, 0.5, 0.25, 0.125]}
                    for i, t in enumerate(inputs)]
            self._json(200, {"object": "list", "data": data, "model": "stub-embed",
                             "usage": {"prompt_tokens": 0, "total_tokens": 0}})
        elif path.endswith("/rerank"):
            docs = body.get("documents") or []
            self._json(200, {"model": "stub-rerank",
                             "results": [{"index": i, "relevance_score": 1.0 / (1 + len(d))}
                                         for i, d in enumerate(docs)]})
        else:
            self._json(404, {"error": {"message": f"no route {self.path}"}})

    def log_message(self, fmt, *args) -> None:  # quiet
        pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8900)
    args = ap.parse_args()
    print(f"model stub on http://127.0.0.1:{args.port}/v1", flush=True)
    HTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
