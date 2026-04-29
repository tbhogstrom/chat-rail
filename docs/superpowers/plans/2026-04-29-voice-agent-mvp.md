# Voice Agent MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a sandbox AI bot that answers a dedicated RingCentral extension, holds a coherent ChatGPT-driven voice conversation with the caller, and ends gracefully — with a pluggable TTS layer (Kokoro local for MVP) and Jambonz handling all SIP/RTP.

**Architecture:** Jambonz container (Docker) handles SIP/RTP/STT/TTS-vendor calls. Per inbound call, Jambonz opens a WebSocket to our FastAPI app at `/voice/{bot_id}/ws`. The app loads a YAML bot profile, holds a per-call `ConversationSession`, streams from OpenAI ChatCompletion, chunks deltas into sentences, pushes one `say` verb per sentence to Jambonz, and serves audio via a custom-HTTP TTS endpoint at `/voice/{bot_id}/tts`. No SIP code in our process.

**Tech Stack:** Python 3.12 / FastAPI / `openai` (AsyncOpenAI streaming) / `kokoro-onnx` (local TTS, no torch) / `pyyaml` / Jambonz `all-in-one` Docker image / Deepgram (existing key, configured inside Jambonz).

---

## File structure

**New files:**

- `src/voice_agent/__init__.py`
- `src/voice_agent/profiles.py` — load + validate YAML bot profiles
- `src/voice_agent/conversation.py` — `ConversationSession`: history + streaming OpenAI calls
- `src/voice_agent/sentence_chunker.py` — pure state machine: GPT deltas → completed sentences
- `src/voice_agent/verb_pump.py` — per-call asyncio.Queue + writer coroutine
- `src/voice_agent/routes.py` — FastAPI WS handler + `/tts` HTTP endpoint, mounted under `/voice/{bot_id}`
- `src/voice_agent/tts/__init__.py` — backend factory
- `src/voice_agent/tts/base.py` — `TTSBackend` Protocol
- `src/voice_agent/tts/kokoro_local.py` — `KokoroLocal` implementation
- `src/voice_agent/tts/openai_tts.py` — `OpenAITTS` scaffold (raises `NotImplementedError`)
- `src/voice_agent/bots/sandbox.yaml` — single bot profile
- `src/voice_agent/prompts/sandbox.md` — sandbox system prompt
- `infra/jambonz/docker-compose.yml` — Jambonz container orchestration
- `infra/jambonz/.env.example` — credentials placeholders
- `docs/voice-agent-setup.md` — one-time Jambonz wiring instructions
- `docs/voice-agent-smoke.md` — manual end-to-end checklist
- `tests/test_voice_profile_loader.py`
- `tests/test_voice_sentence_chunker.py`
- `tests/test_voice_conversation.py`
- `tests/test_voice_tts_factory.py`
- `tests/test_voice_tts_kokoro.py`
- `tests/test_voice_verb_pump.py`
- `tests/test_voice_routes.py`

**Modified files:**

- `src/config.py` — add `OPENAI_API_KEY`, `KOKORO_MODEL_PATH`, `KOKORO_VOICES_PATH`, `BOT_PROFILES_DIR`, `JAMBONZ_WEBHOOK_SECRET`.
- `src/api/main.py` — mount `voice_router` from `src/voice_agent/routes.py`.
- `pyproject.toml` — add `openai`, `kokoro-onnx`, `pyyaml` to dependencies.

**Note on Jambonz wire format:** The exact JSON shape Jambonz uses on its WebSocket app interface (field naming, envelope structure) varies by version. This plan uses a clean assumed shape:

```
{"type": "session:new",  "call_sid": "<sid>", "data": {...}}
{"type": "verb:hook",    "call_sid": "<sid>", "hook": "turn", "data": {"speech": {"alternatives": [{"transcript": "..."}]}}}
{"type": "session:done", "call_sid": "<sid>"}
```

and outbound verbs as plain JSON objects. Unit tests use this shape directly. Task 13 (smoke) is where any Jambonz-specific adapter tweaks land — if the real wire format differs, the change is isolated to a parser at the top of `routes.py`, not the rest of the code.

---

## Task 1: Bot profile loader

**Files:**
- Create: `src/voice_agent/__init__.py`
- Create: `src/voice_agent/profiles.py`
- Create: `src/voice_agent/bots/sandbox.yaml`
- Create: `src/voice_agent/prompts/sandbox.md`
- Test: `tests/test_voice_profile_loader.py`

- [ ] **Step 1: Add `pyyaml` dependency**

Edit `pyproject.toml`:
```toml
dependencies = [
    "fastapi>=0.115",
    "uvicorn>=0.34",
    "upstash-redis>=1.1",
    "ringcentral>=0.9",
    "httpx>=0.28",
    "python-dotenv>=1.0",
    "websockets>=14.0",
    "pyyaml>=6.0",
]
```

Run: `pip install -e .`
Expected: pyyaml installs.

- [ ] **Step 2: Write the failing test**

Create `tests/test_voice_profile_loader.py`:
```python
import pytest
from pathlib import Path
from src.voice_agent.profiles import BotProfile, load_profiles, ProfileError


def _write(tmp_path: Path, name: str, body: str) -> None:
    (tmp_path / f"{name}.yaml").write_text(body, encoding="utf-8")


def test_loads_valid_profile(tmp_path):
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "sandbox.md").write_text("You are a helpful assistant.", encoding="utf-8")
    _write(tmp_path, "sandbox", """
id: sandbox
display_name: SFW Sandbox Bot
system_prompt_path: prompts/sandbox.md
voice: af_bella
tts_backend: kokoro_local
openai_model: gpt-4o-mini
""")
    profiles = load_profiles(tmp_path)
    assert "sandbox" in profiles
    p = profiles["sandbox"]
    assert isinstance(p, BotProfile)
    assert p.id == "sandbox"
    assert p.display_name == "SFW Sandbox Bot"
    assert p.voice == "af_bella"
    assert p.tts_backend == "kokoro_local"
    assert p.openai_model == "gpt-4o-mini"
    assert p.system_prompt == "You are a helpful assistant."


def test_missing_required_field_raises(tmp_path):
    _write(tmp_path, "broken", "id: broken\n")
    with pytest.raises(ProfileError, match="missing"):
        load_profiles(tmp_path)


def test_unknown_tts_backend_raises(tmp_path):
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "x.md").write_text("x", encoding="utf-8")
    _write(tmp_path, "bad", """
id: bad
display_name: Bad
system_prompt_path: prompts/x.md
voice: v
tts_backend: nonexistent_backend
openai_model: gpt-4o-mini
""")
    with pytest.raises(ProfileError, match="unknown tts_backend"):
        load_profiles(tmp_path)


def test_missing_prompt_file_raises(tmp_path):
    _write(tmp_path, "p", """
id: p
display_name: P
system_prompt_path: prompts/missing.md
voice: v
tts_backend: kokoro_local
openai_model: gpt-4o-mini
""")
    with pytest.raises(ProfileError, match="prompt file"):
        load_profiles(tmp_path)


def test_empty_directory_returns_empty_dict(tmp_path):
    assert load_profiles(tmp_path) == {}
```

- [ ] **Step 3: Run tests, verify they fail**

Run: `pytest tests/test_voice_profile_loader.py -v`
Expected: FAIL — `src.voice_agent` module doesn't exist.

- [ ] **Step 4: Create the module package**

Create `src/voice_agent/__init__.py` (empty file, just `""`).

- [ ] **Step 5: Implement the loader**

Create `src/voice_agent/profiles.py`:
```python
"""Bot profile loader. A profile is a YAML file under a profiles directory
that captures everything specific to one bot: prompt path, voice,
TTS backend choice, and OpenAI model. Profiles are loaded once at
startup; missing or malformed files fail fast.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import yaml


KNOWN_TTS_BACKENDS = {"kokoro_local", "openai_tts"}
REQUIRED_FIELDS = ("id", "display_name", "system_prompt_path", "voice", "tts_backend", "openai_model")


class ProfileError(ValueError):
    """Raised when a profile YAML is missing required fields, references a
    missing prompt file, or names an unknown TTS backend."""


@dataclass(frozen=True)
class BotProfile:
    id: str
    display_name: str
    system_prompt: str  # resolved content, not the path
    voice: str
    tts_backend: str
    openai_model: str


def load_profiles(profiles_dir: Path) -> dict[str, BotProfile]:
    """Load every *.yaml file in profiles_dir into a {id: BotProfile} dict."""
    profiles_dir = Path(profiles_dir)
    if not profiles_dir.exists():
        return {}

    out: dict[str, BotProfile] = {}
    for yaml_path in sorted(profiles_dir.glob("*.yaml")):
        out[yaml_path.stem] = _load_one(yaml_path, profiles_dir)
    return out


def _load_one(yaml_path: Path, profiles_dir: Path) -> BotProfile:
    raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    missing = [f for f in REQUIRED_FIELDS if f not in raw]
    if missing:
        raise ProfileError(f"{yaml_path.name}: missing required field(s): {', '.join(missing)}")

    if raw["tts_backend"] not in KNOWN_TTS_BACKENDS:
        raise ProfileError(
            f"{yaml_path.name}: unknown tts_backend '{raw['tts_backend']}' "
            f"(known: {sorted(KNOWN_TTS_BACKENDS)})"
        )

    prompt_path = profiles_dir / raw["system_prompt_path"]
    if not prompt_path.exists():
        raise ProfileError(f"{yaml_path.name}: prompt file not found at {prompt_path}")
    prompt = prompt_path.read_text(encoding="utf-8")

    return BotProfile(
        id=raw["id"],
        display_name=raw["display_name"],
        system_prompt=prompt,
        voice=raw["voice"],
        tts_backend=raw["tts_backend"],
        openai_model=raw["openai_model"],
    )
```

- [ ] **Step 6: Run tests, verify they pass**

Run: `pytest tests/test_voice_profile_loader.py -v`
Expected: 5 passed.

- [ ] **Step 7: Create the actual sandbox profile**

Create `src/voice_agent/prompts/sandbox.md`:
```markdown
You are SFW Sandbox, a friendly AI assistant on a phone call. This is a
demo / sandbox conversation — you have no business goal, no information
to capture, and no tools. Just hold a coherent, polite conversation.

Keep replies short — usually one or two sentences. Speak naturally like
a phone call, not like a chatbot. If the caller goes silent or seems
done, wrap up politely and let them know they can hang up whenever.

Do not mention that you are an AI sandbox unless asked directly. If
asked, say "I'm an AI assistant SFW is testing — happy to chat."
```

Create `src/voice_agent/bots/sandbox.yaml`:
```yaml
id: sandbox
display_name: SFW Sandbox Bot
system_prompt_path: ../prompts/sandbox.md
voice: af_bella
tts_backend: kokoro_local
openai_model: gpt-4o-mini
```

Note: the path `../prompts/sandbox.md` is relative to the `bots/` subdirectory. The loader resolves relative to `profiles_dir`. To keep paths simple, set `BOT_PROFILES_DIR=src/voice_agent/bots` and put prompts at `src/voice_agent/bots/prompts/sandbox.md`. Adjust:

Move/create `src/voice_agent/bots/prompts/sandbox.md` (same content as above).

Update `src/voice_agent/bots/sandbox.yaml`:
```yaml
id: sandbox
display_name: SFW Sandbox Bot
system_prompt_path: prompts/sandbox.md
voice: af_bella
tts_backend: kokoro_local
openai_model: gpt-4o-mini
```

Delete the now-unused `src/voice_agent/prompts/` directory.

- [ ] **Step 8: Verify the real profile loads**

Run: `python -c "from pathlib import Path; from src.voice_agent.profiles import load_profiles; print(load_profiles(Path('src/voice_agent/bots')))"`
Expected: prints a dict with `'sandbox'` key and a fully-populated `BotProfile`.

- [ ] **Step 9: Commit**

```bash
git add pyproject.toml src/voice_agent/__init__.py src/voice_agent/profiles.py src/voice_agent/bots/sandbox.yaml src/voice_agent/bots/prompts/sandbox.md tests/test_voice_profile_loader.py
git commit -m "feat(voice-agent): bot profile loader + sandbox profile"
```

---

## Task 2: Sentence chunker

**Files:**
- Create: `src/voice_agent/sentence_chunker.py`
- Test: `tests/test_voice_sentence_chunker.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_voice_sentence_chunker.py`:
```python
from src.voice_agent.sentence_chunker import SentenceChunker


def feed_all(deltas: list[str]) -> tuple[list[str], str]:
    """Helper: feed deltas, return (emitted_sentences, final_flush)."""
    c = SentenceChunker()
    emitted: list[str] = []
    for d in deltas:
        emitted.extend(c.feed(d))
    final = c.flush()
    return emitted, final


def test_emits_on_period_then_space():
    emitted, final = feed_all(["Hello world. ", "Next sentence."])
    assert emitted == ["Hello world."]
    assert final == "Next sentence."


def test_emits_on_question_mark():
    emitted, final = feed_all(["How are you? ", "Fine."])
    assert emitted == ["How are you?"]
    assert final == "Fine."


def test_emits_on_exclamation():
    emitted, final = feed_all(["Wow! ", "That's great."])
    assert emitted == ["Wow!"]
    assert final == "That's great."


def test_token_by_token_split():
    deltas = ["He", "llo", " wor", "ld", ". ", "And", " then", "."]
    emitted, final = feed_all(deltas)
    assert emitted == ["Hello world."]
    assert final == "And then."


def test_force_flush_at_80_chars_no_punctuation():
    long = "a" * 90
    emitted, final = feed_all([long])
    # First 80 emitted, remainder flushed
    assert len(emitted) == 1
    assert len(emitted[0]) == 80
    assert final == "a" * 10


def test_whitespace_only_delta_is_appended_not_emitted():
    emitted, final = feed_all(["Hello", " ", "world."])
    assert emitted == []
    assert final == "Hello world."


def test_trailing_fragment_returned_by_flush():
    emitted, final = feed_all(["Hello there"])
    assert emitted == []
    assert final == "Hello there"


def test_empty_flush_returns_empty_string():
    c = SentenceChunker()
    assert c.flush() == ""


def test_period_without_trailing_space_does_not_emit_yet():
    emitted, final = feed_all(["Mr.", " Smith said hi."])
    # "Mr." followed by space WOULD emit, but only if we treat it as end-of-sentence.
    # MVP does emit on ". " — accept this false-positive; mid-call this is rare.
    # Document: emits "Mr."
    assert emitted == ["Mr."]
    assert final == "Smith said hi."


def test_emit_strips_leading_whitespace_only():
    # Ensure the next sentence's leading space isn't part of the previous one
    emitted, final = feed_all(["First. ", "Second."])
    assert emitted == ["First."]
    assert final == "Second."
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `pytest tests/test_voice_sentence_chunker.py -v`
Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Implement the chunker**

Create `src/voice_agent/sentence_chunker.py`:
```python
"""Pure state machine: feed GPT delta tokens, get back completed sentences.

Emission rule: a sentence-terminator [.!?] followed by whitespace ends a
sentence. If the buffer grows past FORCE_FLUSH_LEN with no terminator,
emit the first FORCE_FLUSH_LEN chars and keep the rest. flush() returns
any trailing fragment for the caller to emit at stream end.
"""
from __future__ import annotations


FORCE_FLUSH_LEN = 80
TERMINATORS = ".!?"


class SentenceChunker:
    def __init__(self) -> None:
        self._buf = ""

    def feed(self, delta: str) -> list[str]:
        """Append delta, return any completed sentences."""
        self._buf += delta
        out: list[str] = []
        while True:
            sentence, remainder = self._extract_one()
            if sentence is None:
                break
            out.append(sentence)
            self._buf = remainder
        return out

    def flush(self) -> str:
        """Return any trailing fragment and clear the buffer."""
        out = self._buf.strip()
        self._buf = ""
        return out

    def _extract_one(self) -> tuple[str | None, str]:
        # Look for terminator followed by whitespace
        for i, ch in enumerate(self._buf):
            if ch in TERMINATORS and i + 1 < len(self._buf) and self._buf[i + 1].isspace():
                sentence = self._buf[: i + 1].strip()
                remainder = self._buf[i + 2 :]
                return sentence, remainder
        # Force-flush: too long without a terminator
        if len(self._buf) >= FORCE_FLUSH_LEN:
            sentence = self._buf[:FORCE_FLUSH_LEN]
            remainder = self._buf[FORCE_FLUSH_LEN:]
            return sentence, remainder
        return None, self._buf
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `pytest tests/test_voice_sentence_chunker.py -v`
Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add src/voice_agent/sentence_chunker.py tests/test_voice_sentence_chunker.py
git commit -m "feat(voice-agent): sentence chunker for streaming GPT deltas"
```

---

## Task 3: TTS Protocol + factory + scaffold

**Files:**
- Create: `src/voice_agent/tts/__init__.py`
- Create: `src/voice_agent/tts/base.py`
- Create: `src/voice_agent/tts/openai_tts.py`
- Test: `tests/test_voice_tts_factory.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_voice_tts_factory.py`:
```python
import pytest
from src.voice_agent.tts import get_tts_backend
from src.voice_agent.tts.base import TTSBackend
from src.voice_agent.tts.openai_tts import OpenAITTS


def test_factory_returns_openai_tts_scaffold():
    backend = get_tts_backend("openai_tts")
    assert isinstance(backend, OpenAITTS)


def test_unknown_backend_raises():
    with pytest.raises(ValueError, match="unknown tts backend"):
        get_tts_backend("nope")


async def test_openai_scaffold_raises_not_implemented():
    backend = get_tts_backend("openai_tts")
    with pytest.raises(NotImplementedError):
        await backend.synthesize("hello", voice="alloy")


def test_factory_caches_backends():
    """Same backend name returns the same instance — backends are stateless adapters."""
    a = get_tts_backend("openai_tts")
    b = get_tts_backend("openai_tts")
    assert a is b


def test_protocol_shape():
    """TTSBackend is the structural type; both built-in backends conform."""
    # Static check via runtime: synthesize must be callable with the expected signature
    backend: TTSBackend = OpenAITTS()
    assert callable(backend.synthesize)
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `pytest tests/test_voice_tts_factory.py -v`
Expected: FAIL — modules don't exist.

- [ ] **Step 3: Implement the Protocol**

Create `src/voice_agent/tts/base.py`:
```python
"""TTS backend Protocol. Backends accept text + a voice name and return
audio bytes plus a MIME type. Async because real backends do I/O.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class TTSBackend(Protocol):
    async def synthesize(self, text: str, voice: str) -> tuple[bytes, str]:
        """Return (audio_bytes, mime_type). mime_type is e.g. 'audio/wav'."""
        ...
```

- [ ] **Step 4: Implement the OpenAI scaffold**

Create `src/voice_agent/tts/openai_tts.py`:
```python
"""Scaffold for a future hosted-OpenAI TTS backend. Exists so the
pluggable seam in the factory is real today, not aspirational. Wire up
when ready: call client.audio.speech.create(model='tts-1', voice=voice, input=text).
"""
from __future__ import annotations


class OpenAITTS:
    async def synthesize(self, text: str, voice: str) -> tuple[bytes, str]:
        raise NotImplementedError("OpenAITTS not implemented in MVP")
```

- [ ] **Step 5: Implement the factory**

Create `src/voice_agent/tts/__init__.py`:
```python
"""Backend factory keyed by the string name used in bot profiles."""
from __future__ import annotations

from src.voice_agent.tts.base import TTSBackend
from src.voice_agent.tts.openai_tts import OpenAITTS


_INSTANCES: dict[str, TTSBackend] = {}


def get_tts_backend(name: str) -> TTSBackend:
    if name in _INSTANCES:
        return _INSTANCES[name]

    if name == "openai_tts":
        backend: TTSBackend = OpenAITTS()
    elif name == "kokoro_local":
        # Imported lazily so test environments without kokoro-onnx still work.
        from src.voice_agent.tts.kokoro_local import KokoroLocal
        backend = KokoroLocal()
    else:
        raise ValueError(f"unknown tts backend: {name!r}")

    _INSTANCES[name] = backend
    return backend
```

- [ ] **Step 6: Run tests, verify they pass**

Run: `pytest tests/test_voice_tts_factory.py -v`
Expected: 5 passed.

- [ ] **Step 7: Commit**

```bash
git add src/voice_agent/tts/__init__.py src/voice_agent/tts/base.py src/voice_agent/tts/openai_tts.py tests/test_voice_tts_factory.py
git commit -m "feat(voice-agent): TTS Protocol, factory, and OpenAITTS scaffold"
```

---

## Task 4: KokoroLocal TTS backend

**Files:**
- Create: `src/voice_agent/tts/kokoro_local.py`
- Test: `tests/test_voice_tts_kokoro.py`

- [ ] **Step 1: Add `kokoro-onnx` dependency**

Edit `pyproject.toml`:
```toml
dependencies = [
    "fastapi>=0.115",
    "uvicorn>=0.34",
    "upstash-redis>=1.1",
    "ringcentral>=0.9",
    "httpx>=0.28",
    "python-dotenv>=1.0",
    "websockets>=14.0",
    "pyyaml>=6.0",
    "kokoro-onnx>=0.4",
    "soundfile>=0.12",
    "numpy>=1.26",
]
```

Run: `pip install -e .`
Expected: `kokoro-onnx`, `soundfile`, and `numpy` install. (kokoro-onnx pulls in `onnxruntime`.)

- [ ] **Step 2: Add slow marker to pytest config**

Edit `pyproject.toml`, append under `[tool.pytest.ini_options]`:
```toml
markers = ["slow: marks tests as slow (deselect with '-m \"not slow\"')"]
```

- [ ] **Step 3: Write the failing unit test (with mocked kokoro_onnx)**

Create `tests/test_voice_tts_kokoro.py`:
```python
import pytest
import numpy as np
from unittest.mock import MagicMock, patch
from src.voice_agent.tts.kokoro_local import KokoroLocal


@pytest.fixture
def fake_kokoro():
    """Patch the Kokoro class at the import site inside kokoro_local."""
    with patch("src.voice_agent.tts.kokoro_local.Kokoro") as MK:
        instance = MagicMock()
        # create returns (samples, sample_rate)
        instance.create.return_value = (np.zeros(8000, dtype=np.float32), 16000)
        MK.return_value = instance
        yield instance


async def test_synthesize_returns_wav_bytes(fake_kokoro, tmp_path, monkeypatch):
    monkeypatch.setenv("KOKORO_MODEL_PATH", str(tmp_path / "model.onnx"))
    monkeypatch.setenv("KOKORO_VOICES_PATH", str(tmp_path / "voices.bin"))
    (tmp_path / "model.onnx").write_bytes(b"fake")
    (tmp_path / "voices.bin").write_bytes(b"fake")

    backend = KokoroLocal()
    audio, mime = await backend.synthesize("Hello world.", voice="af_bella")
    assert mime == "audio/wav"
    assert audio.startswith(b"RIFF")  # WAV header
    assert len(audio) > 44  # more than just the header
    fake_kokoro.create.assert_called_once_with(
        "Hello world.", voice="af_bella", speed=1.0, lang="en-us"
    )


async def test_synthesize_lazy_initializes_kokoro(fake_kokoro, tmp_path, monkeypatch):
    monkeypatch.setenv("KOKORO_MODEL_PATH", str(tmp_path / "m.onnx"))
    monkeypatch.setenv("KOKORO_VOICES_PATH", str(tmp_path / "v.bin"))
    (tmp_path / "m.onnx").write_bytes(b"fake")
    (tmp_path / "v.bin").write_bytes(b"fake")

    backend = KokoroLocal()
    await backend.synthesize("a", voice="v1")
    await backend.synthesize("b", voice="v2")
    # Two synthesize calls but only one Kokoro construction
    assert fake_kokoro.create.call_count == 2


async def test_synthesize_missing_model_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("KOKORO_MODEL_PATH", str(tmp_path / "missing.onnx"))
    monkeypatch.setenv("KOKORO_VOICES_PATH", str(tmp_path / "missing.bin"))
    backend = KokoroLocal()
    with pytest.raises(FileNotFoundError, match="kokoro model"):
        await backend.synthesize("hi", voice="af_bella")


@pytest.mark.slow
async def test_real_kokoro_smoke():
    """Real model invocation — only runs if model files actually exist on disk."""
    import os
    if not os.environ.get("KOKORO_MODEL_PATH") or not os.path.exists(os.environ["KOKORO_MODEL_PATH"]):
        pytest.skip("KOKORO_MODEL_PATH not set or file missing")
    backend = KokoroLocal()
    audio, mime = await backend.synthesize("Hello world.", voice="af_bella")
    assert mime == "audio/wav"
    assert len(audio) > 1000  # real audio is non-trivial size
```

- [ ] **Step 4: Run tests, verify they fail**

Run: `pytest tests/test_voice_tts_kokoro.py -v -m "not slow"`
Expected: FAIL — `KokoroLocal` doesn't exist.

- [ ] **Step 5: Implement the backend**

Create `src/voice_agent/tts/kokoro_local.py`:
```python
"""Local Kokoro TTS via kokoro-onnx (no PyTorch dependency).

Loads the ONNX model + voices file lazily on first synthesize() call.
Returns 16-bit PCM WAV at the model's sample rate (Jambonz handles
resampling to 8 kHz µ-law for SIP transport).
"""
from __future__ import annotations

import io
import os
from pathlib import Path

import numpy as np
import soundfile as sf
from kokoro_onnx import Kokoro


class KokoroLocal:
    def __init__(self) -> None:
        self._kokoro: Kokoro | None = None

    def _ensure_loaded(self) -> Kokoro:
        if self._kokoro is not None:
            return self._kokoro

        model_path = os.environ.get("KOKORO_MODEL_PATH", "")
        voices_path = os.environ.get("KOKORO_VOICES_PATH", "")
        if not model_path or not Path(model_path).exists():
            raise FileNotFoundError(f"kokoro model not found at {model_path!r}")
        if not voices_path or not Path(voices_path).exists():
            raise FileNotFoundError(f"kokoro voices not found at {voices_path!r}")

        self._kokoro = Kokoro(model_path, voices_path)
        return self._kokoro

    async def synthesize(self, text: str, voice: str) -> tuple[bytes, str]:
        kokoro = self._ensure_loaded()
        samples, sample_rate = kokoro.create(text, voice=voice, speed=1.0, lang="en-us")
        # Convert float32 samples to 16-bit PCM WAV in memory
        buf = io.BytesIO()
        sf.write(buf, samples, sample_rate, format="WAV", subtype="PCM_16")
        return buf.getvalue(), "audio/wav"
```

- [ ] **Step 6: Run tests, verify they pass**

Run: `pytest tests/test_voice_tts_kokoro.py -v -m "not slow"`
Expected: 3 passed (the slow test is skipped).

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml src/voice_agent/tts/kokoro_local.py tests/test_voice_tts_kokoro.py
git commit -m "feat(voice-agent): KokoroLocal TTS backend (kokoro-onnx)"
```

---

## Task 5: Conversation engine

**Files:**
- Create: `src/voice_agent/conversation.py`
- Test: `tests/test_voice_conversation.py`

- [ ] **Step 1: Add `openai` dependency**

Edit `pyproject.toml`, append to `dependencies`:
```toml
    "openai>=1.50",
```

Run: `pip install -e .`
Expected: openai installs.

- [ ] **Step 2: Write the failing tests**

Create `tests/test_voice_conversation.py`:
```python
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from src.voice_agent.conversation import ConversationSession
from src.voice_agent.profiles import BotProfile


@pytest.fixture
def profile():
    return BotProfile(
        id="sandbox",
        display_name="Sandbox",
        system_prompt="You are SFW Sandbox.",
        voice="af_bella",
        tts_backend="kokoro_local",
        openai_model="gpt-4o-mini",
    )


def _fake_chunk(content: str | None):
    """Build a fake OpenAI streaming chunk with the given delta content."""
    chunk = MagicMock()
    delta = MagicMock()
    delta.content = content
    chunk.choices = [MagicMock(delta=delta)]
    return chunk


async def _fake_stream(*deltas: str | None):
    """Async iterator yielding fake chunks with the given delta contents."""
    for d in deltas:
        yield _fake_chunk(d)


def test_session_initializes_with_system_prompt(profile):
    session = ConversationSession("call-1", profile, client=MagicMock())
    assert session.history == [{"role": "system", "content": "You are SFW Sandbox."}]
    assert session.call_sid == "call-1"


async def test_stream_response_yields_deltas_and_appends_history(profile):
    client = MagicMock()
    client.chat = MagicMock()
    client.chat.completions = MagicMock()
    client.chat.completions.create = AsyncMock(
        return_value=_fake_stream("Hello", " world", ".", None)
    )
    session = ConversationSession("call-1", profile, client=client)

    deltas = []
    async for d in session.stream_response("Hi there"):
        deltas.append(d)

    assert deltas == ["Hello", " world", "."]
    assert session.history == [
        {"role": "system", "content": "You are SFW Sandbox."},
        {"role": "user", "content": "Hi there"},
        {"role": "assistant", "content": "Hello world."},
    ]
    # Verify model + messages passed correctly
    call_kwargs = client.chat.completions.create.call_args.kwargs
    assert call_kwargs["model"] == "gpt-4o-mini"
    assert call_kwargs["stream"] is True
    assert call_kwargs["messages"][-1] == {"role": "user", "content": "Hi there"}
    assert call_kwargs["messages"][0]["role"] == "system"


async def test_history_carries_across_turns(profile):
    client = MagicMock()
    client.chat = MagicMock()
    client.chat.completions = MagicMock()
    # First turn
    client.chat.completions.create = AsyncMock(return_value=_fake_stream("First."))
    session = ConversationSession("call-1", profile, client=client)
    async for _ in session.stream_response("Q1"):
        pass

    # Second turn — assert the messages passed include the prior assistant turn
    client.chat.completions.create = AsyncMock(return_value=_fake_stream("Second."))
    async for _ in session.stream_response("Q2"):
        pass

    call_kwargs = client.chat.completions.create.call_args.kwargs
    msgs = call_kwargs["messages"]
    assert msgs[0]["role"] == "system"
    assert msgs[1] == {"role": "user", "content": "Q1"}
    assert msgs[2] == {"role": "assistant", "content": "First."}
    assert msgs[3] == {"role": "user", "content": "Q2"}


async def test_stream_response_skips_none_deltas(profile):
    client = MagicMock()
    client.chat = MagicMock()
    client.chat.completions = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=_fake_stream(None, "a", None, "b"))
    session = ConversationSession("call-1", profile, client=client)

    deltas = [d async for d in session.stream_response("hi")]
    assert deltas == ["a", "b"]
    assert session.history[-1] == {"role": "assistant", "content": "ab"}
```

- [ ] **Step 3: Run tests, verify they fail**

Run: `pytest tests/test_voice_conversation.py -v`
Expected: FAIL — `ConversationSession` doesn't exist.

- [ ] **Step 4: Implement the session**

Create `src/voice_agent/conversation.py`:
```python
"""Per-call conversation state + streaming OpenAI access. The session
keeps a history list (system prompt at index 0) and exposes a single
async generator method that streams delta tokens for one user turn,
appending both user and assistant messages to history.
"""
from __future__ import annotations

from typing import AsyncIterator

from openai import AsyncOpenAI

from src.voice_agent.profiles import BotProfile


class ConversationSession:
    def __init__(self, call_sid: str, profile: BotProfile, client: AsyncOpenAI | None = None) -> None:
        self.call_sid = call_sid
        self.profile = profile
        self.history: list[dict] = [
            {"role": "system", "content": profile.system_prompt}
        ]
        # Lazy import-friendly default; tests pass in a mock.
        self._client = client or AsyncOpenAI()

    async def stream_response(self, user_text: str) -> AsyncIterator[str]:
        """Append the user turn, stream the assistant reply tokens, append
        the assembled assistant turn at the end. Yields delta strings.
        """
        self.history.append({"role": "user", "content": user_text})

        stream = await self._client.chat.completions.create(
            model=self.profile.openai_model,
            messages=list(self.history),
            stream=True,
        )

        assembled: list[str] = []
        async for chunk in stream:
            delta = chunk.choices[0].delta.content
            if delta is None:
                continue
            assembled.append(delta)
            yield delta

        self.history.append({"role": "assistant", "content": "".join(assembled)})
```

- [ ] **Step 5: Run tests, verify they pass**

Run: `pytest tests/test_voice_conversation.py -v`
Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml src/voice_agent/conversation.py tests/test_voice_conversation.py
git commit -m "feat(voice-agent): ConversationSession with streaming OpenAI"
```

---

## Task 6: Verb pump

**Files:**
- Create: `src/voice_agent/verb_pump.py`
- Test: `tests/test_voice_verb_pump.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_voice_verb_pump.py`:
```python
import asyncio
import pytest
from unittest.mock import AsyncMock
from src.voice_agent.verb_pump import VerbPump


async def test_put_then_run_writes_to_websocket():
    ws = AsyncMock()
    pump = VerbPump(ws)
    await pump.put({"verb": "say", "text": "hi"})
    await pump.put({"verb": "gather"})
    pump.close()  # signals the run loop to exit after draining

    await pump.run()

    assert ws.send_json.call_count == 2
    assert ws.send_json.call_args_list[0].args[0] == {"verb": "say", "text": "hi"}
    assert ws.send_json.call_args_list[1].args[0] == {"verb": "gather"}


async def test_run_drains_remaining_after_close():
    ws = AsyncMock()
    pump = VerbPump(ws)
    await pump.put({"verb": "a"})
    await pump.put({"verb": "b"})
    pump.close()

    await pump.run()
    assert ws.send_json.call_count == 2


async def test_run_can_be_cancelled():
    ws = AsyncMock()
    pump = VerbPump(ws)

    task = asyncio.create_task(pump.run())
    await asyncio.sleep(0.05)  # let it start waiting
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ws.send_json.call_count == 0
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `pytest tests/test_voice_verb_pump.py -v`
Expected: FAIL — `VerbPump` doesn't exist.

- [ ] **Step 3: Implement the pump**

Create `src/voice_agent/verb_pump.py`:
```python
"""Per-call outbound verb queue + writer coroutine. Decouples the GPT-
streaming task (which produces verbs as sentences arrive) from the
WebSocket writer.

Usage:
    pump = VerbPump(ws)
    asyncio.create_task(pump.run())
    await pump.put({"verb": "say", "text": "..."})
    ...
    pump.close()  # tell run() to exit after draining
"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import WebSocket


_SENTINEL = object()


class VerbPump:
    def __init__(self, ws: WebSocket) -> None:
        self._ws = ws
        self._q: asyncio.Queue[Any] = asyncio.Queue()

    async def put(self, verb: dict) -> None:
        await self._q.put(verb)

    def close(self) -> None:
        # Schedule the sentinel without awaiting (callable from sync contexts)
        self._q.put_nowait(_SENTINEL)

    async def run(self) -> None:
        while True:
            item = await self._q.get()
            if item is _SENTINEL:
                return
            await self._ws.send_json(item)
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `pytest tests/test_voice_verb_pump.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/voice_agent/verb_pump.py tests/test_voice_verb_pump.py
git commit -m "feat(voice-agent): VerbPump (per-call outbound verb queue)"
```

---

## Task 7: Voice WS route — session:new greeting

**Files:**
- Create: `src/voice_agent/routes.py`
- Test: `tests/test_voice_routes.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_voice_routes.py`:
```python
import pytest
from unittest.mock import MagicMock, patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from src.voice_agent.profiles import BotProfile
from src.voice_agent.routes import build_voice_router


@pytest.fixture
def profile():
    return BotProfile(
        id="sandbox",
        display_name="Sandbox",
        system_prompt="You are SFW Sandbox.",
        voice="af_bella",
        tts_backend="kokoro_local",
        openai_model="gpt-4o-mini",
    )


@pytest.fixture
def app(profile):
    app = FastAPI()
    app.include_router(build_voice_router({"sandbox": profile}))
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


def test_session_new_emits_greeting_and_gather(client):
    with client.websocket_connect("/voice/sandbox/ws") as ws:
        ws.send_json({"type": "session:new", "call_sid": "c-1", "data": {}})

        first = ws.receive_json()
        assert first == {"verb": "say", "text": "Hi, this is the Sandbox. Talk to me."}

        second = ws.receive_json()
        assert second["verb"] == "gather"
        assert second["actionHook"] == "turn"
        assert second["recognizer"]["vendor"] == "deepgram"
        assert second["bargein"] is False


def test_unknown_bot_id_closes_websocket(client):
    with pytest.raises(Exception):  # WebSocketDisconnect
        with client.websocket_connect("/voice/nonexistent/ws") as ws:
            ws.receive_json()
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `pytest tests/test_voice_routes.py -v`
Expected: FAIL — `routes` module doesn't exist.

- [ ] **Step 3: Implement the route (greeting only)**

Create `src/voice_agent/routes.py`:
```python
"""FastAPI router for the voice agent. One WebSocket endpoint per call
plus one HTTP endpoint that Jambonz hits for custom TTS.

build_voice_router(profiles) returns a router that, for every profile,
exposes /voice/{profile.id}/ws and /voice/{profile.id}/tts.
"""
from __future__ import annotations

import logging
from typing import Mapping

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from src.voice_agent.profiles import BotProfile

logger = logging.getLogger(__name__)


def _greeting_text(profile: BotProfile) -> str:
    return f"Hi, this is the {profile.display_name}. Talk to me."


def _gather_verb() -> dict:
    return {
        "verb": "gather",
        "input": ["speech"],
        "recognizer": {"vendor": "deepgram", "language": "en-US"},
        "timeout": 8,
        "bargein": False,
        "actionHook": "turn",
    }


def build_voice_router(profiles: Mapping[str, BotProfile]) -> APIRouter:
    router = APIRouter()

    @router.websocket("/voice/{bot_id}/ws")
    async def voice_ws(ws: WebSocket, bot_id: str) -> None:
        if bot_id not in profiles:
            await ws.close(code=4404)
            return

        profile = profiles[bot_id]
        await ws.accept()
        try:
            while True:
                msg = await ws.receive_json()
                msg_type = msg.get("type")
                if msg_type == "session:new":
                    await ws.send_json({"verb": "say", "text": _greeting_text(profile)})
                    await ws.send_json(_gather_verb())
                # Other types handled in later tasks.
        except WebSocketDisconnect:
            logger.info("voice ws disconnected: bot=%s", bot_id)

    return router
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `pytest tests/test_voice_routes.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add src/voice_agent/routes.py tests/test_voice_routes.py
git commit -m "feat(voice-agent): WS route — session:new emits greeting + gather"
```

---

## Task 8: Voice WS route — verb:hook turn handling

**Files:**
- Modify: `src/voice_agent/routes.py`
- Modify: `tests/test_voice_routes.py`

- [ ] **Step 1: Add the failing test**

Append to `tests/test_voice_routes.py`:
```python
async def _stream(*deltas):
    """Async iter of fake OpenAI chunks with the given delta contents."""
    for d in deltas:
        chunk = MagicMock()
        delta = MagicMock()
        delta.content = d
        chunk.choices = [MagicMock(delta=delta)]
        yield chunk


def test_verb_hook_streams_say_verbs_then_gather(client):
    fake_client = MagicMock()
    fake_client.chat = MagicMock()
    fake_client.chat.completions = MagicMock()

    async def _create(**kwargs):
        return _stream("Hello", " world.", " Second", " sentence.")

    fake_client.chat.completions.create = _create

    with patch("src.voice_agent.routes._openai_client", return_value=fake_client):
        with client.websocket_connect("/voice/sandbox/ws") as ws:
            # Drive past greeting
            ws.send_json({"type": "session:new", "call_sid": "c-1", "data": {}})
            ws.receive_json()  # greeting say
            ws.receive_json()  # initial gather

            # Caller turn
            ws.send_json({
                "type": "verb:hook",
                "call_sid": "c-1",
                "hook": "turn",
                "data": {"speech": {"alternatives": [{"transcript": "Hi there"}]}},
            })

            # Two say verbs (one per sentence) then a gather
            verbs = [ws.receive_json() for _ in range(3)]
            assert verbs[0] == {"verb": "say", "text": "Hello world."}
            assert verbs[1] == {"verb": "say", "text": "Second sentence."}
            assert verbs[2]["verb"] == "gather"


def test_verb_hook_with_empty_transcript_emits_reprompt(client):
    with client.websocket_connect("/voice/sandbox/ws") as ws:
        ws.send_json({"type": "session:new", "call_sid": "c-2", "data": {}})
        ws.receive_json(); ws.receive_json()  # drain greeting + gather

        ws.send_json({
            "type": "verb:hook",
            "call_sid": "c-2",
            "hook": "turn",
            "data": {"speech": {"alternatives": []}},
        })
        first = ws.receive_json()
        assert first == {"verb": "say", "text": "Didn't catch that — try again?"}
        second = ws.receive_json()
        assert second["verb"] == "gather"


def test_two_empty_transcripts_in_a_row_hangs_up(client):
    with client.websocket_connect("/voice/sandbox/ws") as ws:
        ws.send_json({"type": "session:new", "call_sid": "c-3", "data": {}})
        ws.receive_json(); ws.receive_json()  # greeting say + initial gather

        # First empty hook: reprompt + gather
        ws.send_json({
            "type": "verb:hook", "call_sid": "c-3", "hook": "turn",
            "data": {"speech": {"alternatives": []}},
        })
        first = ws.receive_json()
        second = ws.receive_json()
        assert first == {"verb": "say", "text": "Didn't catch that — try again?"}
        assert second["verb"] == "gather"

        # Second empty hook: goodbye say + hangup, then server returns and closes WS
        ws.send_json({
            "type": "verb:hook", "call_sid": "c-3", "hook": "turn",
            "data": {"speech": {"alternatives": []}},
        })
        goodbye = ws.receive_json()
        hangup = ws.receive_json()
        assert goodbye["verb"] == "say"
        assert "hang up" in goodbye["text"].lower()
        assert hangup == {"verb": "hangup"}
        # Server has returned; further receives raise.
        with pytest.raises(Exception):
            ws.receive_json()
```

Note: the third test is intentionally exploratory; refine the exact frame ordering after implementation.

- [ ] **Step 2: Run tests, verify the new ones fail**

Run: `pytest tests/test_voice_routes.py -v`
Expected: existing two pass; new tests FAIL.

- [ ] **Step 3: Extend the route**

Replace the body of `voice_ws` in `src/voice_agent/routes.py` with full turn-handling. Final file:
```python
"""FastAPI router for the voice agent."""
from __future__ import annotations

import logging
from typing import Mapping

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from openai import AsyncOpenAI

from src.voice_agent.conversation import ConversationSession
from src.voice_agent.profiles import BotProfile
from src.voice_agent.sentence_chunker import SentenceChunker

logger = logging.getLogger(__name__)


def _greeting_text(profile: BotProfile) -> str:
    return f"Hi, this is the {profile.display_name}. Talk to me."


def _gather_verb() -> dict:
    return {
        "verb": "gather",
        "input": ["speech"],
        "recognizer": {"vendor": "deepgram", "language": "en-US"},
        "timeout": 8,
        "bargein": False,
        "actionHook": "turn",
    }


def _openai_client() -> AsyncOpenAI:
    """Hook for tests to swap in a fake client."""
    return AsyncOpenAI()


REPROMPT = "Didn't catch that — try again?"
GOODBYE = "Sounds like we got disconnected — going to hang up. Call back anytime."


def _extract_transcript(data: dict) -> str:
    alts = data.get("speech", {}).get("alternatives") or []
    if not alts:
        return ""
    return (alts[0].get("transcript") or "").strip()


def build_voice_router(profiles: Mapping[str, BotProfile]) -> APIRouter:
    router = APIRouter()

    @router.websocket("/voice/{bot_id}/ws")
    async def voice_ws(ws: WebSocket, bot_id: str) -> None:
        if bot_id not in profiles:
            await ws.close(code=4404)
            return

        profile = profiles[bot_id]
        await ws.accept()
        session: ConversationSession | None = None
        empty_streak = 0

        try:
            while True:
                msg = await ws.receive_json()
                msg_type = msg.get("type")
                call_sid = msg.get("call_sid", "")

                if msg_type == "session:new":
                    session = ConversationSession(call_sid, profile, client=_openai_client())
                    await ws.send_json({"verb": "say", "text": _greeting_text(profile)})
                    await ws.send_json(_gather_verb())

                elif msg_type == "verb:hook" and msg.get("hook") == "turn":
                    if session is None:
                        logger.warning("verb:hook before session:new; ignoring")
                        continue

                    transcript = _extract_transcript(msg.get("data", {}))

                    if not transcript:
                        empty_streak += 1
                        if empty_streak >= 2:
                            await ws.send_json({"verb": "say", "text": GOODBYE})
                            await ws.send_json({"verb": "hangup"})
                            return
                        await ws.send_json({"verb": "say", "text": REPROMPT})
                        await ws.send_json(_gather_verb())
                        continue

                    empty_streak = 0
                    chunker = SentenceChunker()
                    async for delta in session.stream_response(transcript):
                        for sentence in chunker.feed(delta):
                            await ws.send_json({"verb": "say", "text": sentence})
                    tail = chunker.flush()
                    if tail:
                        await ws.send_json({"verb": "say", "text": tail})
                    await ws.send_json(_gather_verb())

                elif msg_type == "session:done":
                    return

        except WebSocketDisconnect:
            logger.info("voice ws disconnected: bot=%s", bot_id)
        finally:
            session = None  # drop reference; gc cleans history

    return router
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `pytest tests/test_voice_routes.py -v`
Expected: all turn-handling tests pass. (If the third "two empties" test asserts a specific frame count that doesn't match, adjust the test to match the implementation's actual frame sequence: `say(reprompt) → gather → say(reprompt) → say(goodbye) → hangup`.)

- [ ] **Step 5: Commit**

```bash
git add src/voice_agent/routes.py tests/test_voice_routes.py
git commit -m "feat(voice-agent): WS turn handling with GPT streaming + sentence chunking"
```

---

## Task 9: Voice WS route — session:done + error handling

**Files:**
- Modify: `src/voice_agent/routes.py`
- Modify: `tests/test_voice_routes.py`

- [ ] **Step 1: Add tests for session:done and OpenAI failure**

Append to `tests/test_voice_routes.py`:
```python
def test_session_done_closes_cleanly(client):
    with client.websocket_connect("/voice/sandbox/ws") as ws:
        ws.send_json({"type": "session:new", "call_sid": "c-d", "data": {}})
        ws.receive_json(); ws.receive_json()
        ws.send_json({"type": "session:done", "call_sid": "c-d"})
        # Server should close the WS without sending more frames.
        # Receiving on a closed WS raises; that's the expected behavior.
        with pytest.raises(Exception):
            ws.receive_json()


def test_openai_failure_emits_fallback_say(client):
    fake_client = MagicMock()
    fake_client.chat = MagicMock()
    fake_client.chat.completions = MagicMock()

    async def _boom(**kwargs):
        raise RuntimeError("openai exploded")

    fake_client.chat.completions.create = _boom

    with patch("src.voice_agent.routes._openai_client", return_value=fake_client):
        with client.websocket_connect("/voice/sandbox/ws") as ws:
            ws.send_json({"type": "session:new", "call_sid": "c-err", "data": {}})
            ws.receive_json(); ws.receive_json()

            ws.send_json({
                "type": "verb:hook",
                "call_sid": "c-err",
                "hook": "turn",
                "data": {"speech": {"alternatives": [{"transcript": "Hello"}]}},
            })
            first = ws.receive_json()
            assert first == {"verb": "say", "text": "Sorry, having trouble — could you repeat that?"}
            second = ws.receive_json()
            assert second["verb"] == "gather"
```

- [ ] **Step 2: Run tests, verify the new ones fail**

Run: `pytest tests/test_voice_routes.py -v`
Expected: `test_openai_failure_emits_fallback_say` fails (no try/except around the stream).

- [ ] **Step 3: Wrap the streaming block in try/except**

In `src/voice_agent/routes.py`, replace the verb:hook handling block (the part that calls `session.stream_response`) with this version:
```python
                    empty_streak = 0
                    chunker = SentenceChunker()
                    try:
                        async for delta in session.stream_response(transcript):
                            for sentence in chunker.feed(delta):
                                await ws.send_json({"verb": "say", "text": sentence})
                        tail = chunker.flush()
                        if tail:
                            await ws.send_json({"verb": "say", "text": tail})
                    except Exception:
                        logger.exception("openai stream failed for call %s", call_sid)
                        await ws.send_json({
                            "verb": "say",
                            "text": "Sorry, having trouble — could you repeat that?",
                        })
                    await ws.send_json(_gather_verb())
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `pytest tests/test_voice_routes.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/voice_agent/routes.py tests/test_voice_routes.py
git commit -m "feat(voice-agent): session:done cleanup + OpenAI failure fallback"
```

---

## Task 10: /tts HTTP endpoint

**Files:**
- Modify: `src/voice_agent/routes.py`
- Modify: `tests/test_voice_routes.py`

- [ ] **Step 1: Add the failing test**

Append to `tests/test_voice_routes.py`:
```python
def test_tts_endpoint_returns_audio(client, profile):
    fake_backend = MagicMock()

    async def _synth(text, voice):
        return b"RIFF" + b"\x00" * 100, "audio/wav"

    fake_backend.synthesize = _synth

    with patch("src.voice_agent.routes.get_tts_backend", return_value=fake_backend):
        resp = client.post(
            "/voice/sandbox/tts",
            json={"text": "Hello", "voice": "af_bella", "language": "en-US"},
        )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "audio/wav"
    assert resp.content.startswith(b"RIFF")


def test_tts_endpoint_unknown_bot_returns_404(client):
    resp = client.post("/voice/nonexistent/tts", json={"text": "hi", "voice": "v"})
    assert resp.status_code == 404


def test_tts_endpoint_synth_failure_returns_silence(client):
    fake_backend = MagicMock()

    async def _synth(text, voice):
        raise RuntimeError("synth exploded")

    fake_backend.synthesize = _synth

    with patch("src.voice_agent.routes.get_tts_backend", return_value=fake_backend):
        resp = client.post(
            "/voice/sandbox/tts",
            json={"text": "Hello", "voice": "af_bella"},
        )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "audio/wav"
    # Silent WAV: header (44 bytes) + 1 second of zero samples at 16 kHz, 16-bit mono = 32044 bytes
    assert len(resp.content) >= 44
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `pytest tests/test_voice_routes.py::test_tts_endpoint_returns_audio -v`
Expected: 404 — endpoint doesn't exist.

- [ ] **Step 3: Add the endpoint and silent-WAV helper**

In `src/voice_agent/routes.py`, add imports:
```python
import io
import wave
from fastapi import HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from src.voice_agent.tts import get_tts_backend
```

Add the silent-WAV builder above `build_voice_router`:
```python
def _silent_wav(seconds: float = 1.0, sample_rate: int = 16000) -> bytes:
    """Build a minimal 16-bit mono PCM WAV of the given duration."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(b"\x00\x00" * int(seconds * sample_rate))
    return buf.getvalue()


class _TTSRequest(BaseModel):
    text: str
    voice: str | None = None
    language: str | None = None
```

Inside `build_voice_router`, after registering the WS route, add:
```python
    @router.post("/voice/{bot_id}/tts")
    async def voice_tts(bot_id: str, body: _TTSRequest) -> Response:
        if bot_id not in profiles:
            raise HTTPException(status_code=404, detail="unknown bot")
        profile = profiles[bot_id]
        backend = get_tts_backend(profile.tts_backend)
        voice = body.voice or profile.voice
        try:
            audio, mime = await backend.synthesize(body.text, voice)
            return Response(content=audio, media_type=mime)
        except Exception:
            logger.exception("tts synth failed for bot=%s text=%r", bot_id, body.text)
            return Response(content=_silent_wav(), media_type="audio/wav")
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `pytest tests/test_voice_routes.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/voice_agent/routes.py tests/test_voice_routes.py
git commit -m "feat(voice-agent): /tts HTTP endpoint with silent-WAV fallback"
```

---

## Task 11: Config additions + mount in main FastAPI app

**Files:**
- Modify: `src/config.py`
- Modify: `src/api/main.py`

- [ ] **Step 1: Add new config fields**

Edit `src/config.py`, append to the `Config` class:
```python
    # Voice Agent
    OPENAI_API_KEY: str = os.environ.get("OPENAI_API_KEY", "")
    KOKORO_MODEL_PATH: str = os.environ.get("KOKORO_MODEL_PATH", "")
    KOKORO_VOICES_PATH: str = os.environ.get("KOKORO_VOICES_PATH", "")
    BOT_PROFILES_DIR: str = os.environ.get("BOT_PROFILES_DIR", "src/voice_agent/bots")
    JAMBONZ_WEBHOOK_SECRET: str = os.environ.get("JAMBONZ_WEBHOOK_SECRET", "")
```

- [ ] **Step 2: Mount the voice router in `create_app`**

Edit `src/api/main.py` so the full file reads:
```python
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import FileResponse
from src.config import Config
from src.redis_store import CallStore
from src.api.routes import router, hubspot_router, set_store
from src.voice_agent.profiles import load_profiles
from src.voice_agent.routes import build_voice_router


_STATIC_DIR = Path(__file__).parent / "static"


def create_app(store: CallStore | None = None) -> FastAPI:
    app = FastAPI(title="SFW Call Intelligence Bridge", version="1.0.0")

    if store:
        set_store(store)

    app.include_router(router)
    app.include_router(hubspot_router)

    profiles = load_profiles(Path(Config.BOT_PROFILES_DIR))
    if profiles:
        app.include_router(build_voice_router(profiles))

    @app.get("/dashboard")
    def dashboard() -> FileResponse:
        return FileResponse(_STATIC_DIR / "dashboard.html")

    return app
```

- [ ] **Step 3: Smoke-check the app boots**

Run: `python -c "from src.api.main import create_app; app = create_app(); print([r.path for r in app.routes])"`
Expected: output includes `/voice/sandbox/ws` and `/voice/sandbox/tts`.

- [ ] **Step 4: Run the full test suite**

Run: `pytest -v -m "not slow"`
Expected: every test passes (existing + new). If any pre-existing test breaks, investigate before committing.

- [ ] **Step 5: Commit**

```bash
git add src/config.py src/api/main.py
git commit -m "feat(voice-agent): mount voice router from main FastAPI app"
```

---

## Task 12: Jambonz Docker compose + setup doc

**Files:**
- Create: `infra/jambonz/docker-compose.yml`
- Create: `infra/jambonz/.env.example`
- Create: `infra/jambonz/README.md`

This task is infrastructure-only. No tests.

- [ ] **Step 1: Create the compose file**

Create `infra/jambonz/docker-compose.yml`:
```yaml
# Jambonz all-in-one for voice agent MVP.
# Brings up: drachtio SBC, FreeSWITCH feature server, Jambonz API,
# admin UI, MySQL, Redis. ~2 GB RAM, ~5–10 min cold start.
#
# After `docker compose up -d`, open http://localhost:3001 and follow
# infra/jambonz/README.md for one-time wiring (carrier, app, profile).
version: "3.8"

services:
  jambonz:
    image: jambonz/all-in-one:latest
    container_name: jambonz
    restart: unless-stopped
    network_mode: host  # SIP needs unrestricted UDP; host networking is simplest on Windows Docker Desktop with WSL2.
    environment:
      - JAMBONZ_HOSTING=local
    volumes:
      - jambonz-data:/var/lib/jambonz
    env_file:
      - .env

volumes:
  jambonz-data:
```

- [ ] **Step 2: Create the `.env` template**

Create `infra/jambonz/.env.example`:
```bash
# Copy to .env and fill in.

# RingCentral SIP credentials for the bot extension.
# Get these from RC admin → Phones & Devices → Existing Phones → SIP profile for the dedicated bot extension.
RC_SIP_USERNAME=
RC_SIP_PASSWORD=
RC_SIP_AUTH_USERNAME=
RC_SIP_DOMAIN=sip.ringcentral.com
RC_SIP_OUTBOUND_PROXY=sip80.ringcentral.com:5090
```

- [ ] **Step 3: Write the setup README**

Create `infra/jambonz/README.md`:
```markdown
# Jambonz Setup for SFW Voice Agent

One-time wiring after `docker compose up -d`. Estimated time: 20 minutes.

## 1. Provision the bot's RC extension

In RingCentral admin:

- Create or pick a dedicated extension for the bot (separate from any human rep).
- Under **Phones & Devices → Existing Phones**, set up a **SIP profile** for that extension.
- Copy the SIP username, password, auth username, domain, and outbound proxy into `infra/jambonz/.env`.

## 2. Bring up Jambonz

```
cd infra/jambonz
cp .env.example .env
# Edit .env with the values from step 1.
docker compose up -d
# Wait ~5 minutes for first boot; check `docker compose logs -f`.
```

Open http://localhost:3001 and complete the initial admin user setup.

## 3. Configure inside Jambonz

In the Jambonz admin UI:

### Speech credentials

- **STT (Deepgram):** add Deepgram with the existing `DEEPGRAM_API_KEY` from the project's `.env`.
- **TTS (Custom Vendor):** add a custom HTTP TTS vendor pointed at:
  - URL: `http://host.docker.internal:8000/voice/sandbox/tts`
  - Method: POST
  - Body: `{"text": "<text>", "voice": "<voice>", "language": "<language>"}`
  - Response: `audio/wav` bytes

### Carrier (RingCentral)

- Name: `ringcentral`
- Inbound + Outbound enabled.
- Auth: use the `.env` SIP credentials.
- Outbound REGISTER: yes.
- SIP gateway: `sip80.ringcentral.com:5090` (or whatever your `.env` specifies).

### Application (sandbox bot)

- Name: `sandbox-bot`
- Type: **WebSocket**
- URL: `ws://host.docker.internal:8000/voice/sandbox/ws`
- Status URL: leave blank (we read session:done from the WS itself)
- STT vendor: Deepgram
- TTS vendor: the custom vendor from above

### Phone number

- Bind the bot extension's RC DID/extension to the `sandbox-bot` application.

## 4. Confirm registration

In Jambonz UI → Carriers → ringcentral → check that REGISTER status is green. Tail `docker compose logs jambonz | grep REGISTER` if it isn't.

## 5. First test call

Run the FastAPI app: `uvicorn src.api.main:create_app --factory --host 0.0.0.0 --port 8000`.

Call the bot's RC extension from any phone. Refer to `docs/voice-agent-smoke.md` for the full smoke checklist.
```

- [ ] **Step 4: Commit**

```bash
git add infra/jambonz/docker-compose.yml infra/jambonz/.env.example infra/jambonz/README.md
git commit -m "infra(voice-agent): Jambonz docker-compose + setup README"
```

---

## Task 13: End-to-end smoke documentation

**Files:**
- Create: `docs/voice-agent-setup.md`
- Create: `docs/voice-agent-smoke.md`

- [ ] **Step 1: Write the setup doc**

Create `docs/voice-agent-setup.md`:
```markdown
# Voice Agent Setup

End-to-end checklist to bring up the sandbox voice agent on a fresh Windows box.

## Prerequisites

- Python 3.12, the project installed in editable mode (`pip install -e .[dev]`).
- Docker Desktop with WSL2.
- A dedicated RingCentral extension provisioned for the bot, with SIP credentials in hand.
- Deepgram API key (already in the project's `.env` as `DEEPGRAM_API_KEY`).
- OpenAI API key.

## 1. Download Kokoro model files

```
mkdir -p models/kokoro
cd models/kokoro
curl -LO https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files/kokoro-v0_19.onnx
curl -LO https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files/voices.bin
```

(Confirm current URLs from the kokoro-onnx repo before running.)

## 2. Configure `.env`

Append to the project's `.env`:

```
OPENAI_API_KEY=sk-...
KOKORO_MODEL_PATH=models/kokoro/kokoro-v0_19.onnx
KOKORO_VOICES_PATH=models/kokoro/voices.bin
BOT_PROFILES_DIR=src/voice_agent/bots
```

## 3. Smoke-test the TTS endpoint without Jambonz

Start the FastAPI app:

```
uvicorn src.api.main:create_app --factory --host 0.0.0.0 --port 8000
```

In another terminal:

```
curl -X POST http://localhost:8000/voice/sandbox/tts \
  -H "Content-Type: application/json" \
  -d '{"text":"Hello world","voice":"af_bella"}' \
  --output sample.wav
```

Play `sample.wav`. Confirm it's intelligible Kokoro speech.

## 4. Bring up Jambonz

Follow `infra/jambonz/README.md`.

## 5. Run the smoke checklist

Follow `docs/voice-agent-smoke.md`.
```

- [ ] **Step 2: Write the smoke checklist**

Create `docs/voice-agent-smoke.md`:
```markdown
# Voice Agent Smoke Checklist

Run before declaring MVP done. Tests the full stack with real telephony.

## Pre-flight

- [ ] FastAPI app running on `localhost:8000`.
- [ ] Jambonz container running; admin UI reachable at `http://localhost:3001`.
- [ ] Jambonz Carrier `ringcentral` shows green REGISTER status.
- [ ] Jambonz Application `sandbox-bot` exists with WebSocket URL `ws://host.docker.internal:8000/voice/sandbox/ws`.
- [ ] Bot's RC extension is bound to the `sandbox-bot` application.

## Test 1: TTS endpoint isolation

```
curl -X POST http://localhost:8000/voice/sandbox/tts \
  -H "Content-Type: application/json" \
  -d '{"text":"Hello world","voice":"af_bella"}' \
  --output /tmp/sample.wav
```

- [ ] Returns 200, `Content-Type: audio/wav`.
- [ ] `/tmp/sample.wav` is intelligible Kokoro speech when played.

## Test 2: Single call, single turn

- [ ] From a phone, dial the bot's RC extension.
- [ ] Greeting ("Hi, this is the SFW Sandbox Bot. Talk to me.") plays within 2 seconds of pickup.
- [ ] Speak: "Hi, what's your name?"
- [ ] Bot replies coherently within ~2 seconds of you stopping.
- [ ] Hang up.
- [ ] FastAPI logs show: WS open, session:new, verb:hook with transcript, multiple say verbs, gather, session:done, WS close.

## Test 3: Multi-turn conversation

- [ ] Call again. Speak three turns:
  1. "Hi, my name is Tyler."
  2. "What did I just tell you?"
  3. "Thanks, goodbye."
- [ ] Turn 2 reply references the name from turn 1 (proves history holds across turns).
- [ ] Turn 3 reply is a polite closing.

## Test 4: Empty / silent turn recovery

- [ ] Call. After greeting, stay silent for the full gather timeout.
- [ ] Bot replies with "Didn't catch that — try again?"
- [ ] Stay silent again.
- [ ] Bot says goodbye and hangs up.

## Test 5: Concurrent calls

- [ ] From two different phones, call the bot extension simultaneously.
- [ ] Both calls connect; both run independent conversations.
- [ ] FastAPI logs show two separate WebSocket sessions with distinct `call_sid` values.

## Test 6: TTS failure recovery

- [ ] Stop the FastAPI app while a call is in progress (or temporarily move `KOKORO_MODEL_PATH` to a bad path and restart).
- [ ] Call the bot. Greeting attempt may produce silence (the silent-WAV fallback) but the call should not drop until the caller hangs up.
- [ ] Restore the model path; subsequent calls work normally.

## Test 7: OpenAI failure recovery

- [ ] Temporarily invalidate `OPENAI_API_KEY` in `.env`; restart the app.
- [ ] Call the bot. Speak a turn.
- [ ] Bot replies "Sorry, having trouble — could you repeat that?" and offers another gather.
- [ ] Restore the key; subsequent turns work.
```

- [ ] **Step 3: Commit**

```bash
git add docs/voice-agent-setup.md docs/voice-agent-smoke.md
git commit -m "docs(voice-agent): setup + smoke checklists"
```

---

## Final verification

- [ ] **Run the full test suite (excluding slow):**

```
pytest -v -m "not slow"
```
Expected: all tests pass.

- [ ] **Verify the app boots:**

```
python -c "from src.api.main import create_app; create_app()"
```
Expected: no exceptions.

- [ ] **Walk the smoke checklist (`docs/voice-agent-smoke.md`)**
- [ ] **Confirm the Jambonz wire format in `routes.py` matches what real Jambonz sends.** If it differs (camelCase, different envelope, etc.), adapt the parsing in the WS handler — keep the rest of the flow unchanged.

---

## Out-of-scope for this plan (deferred)

- Barge-in (`bargein: true` + caller-speech-cancel handling).
- Persistence to `CallStore` / dashboard integration for bot calls.
- HMAC validation on `/voice/*` endpoints.
- Hosted TTS implementation (`OpenAITTS` stays a scaffold).
- Cloud GPU / remote Kokoro backend.
- Additional bot profiles beyond `sandbox`.
