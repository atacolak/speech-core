#!/usr/bin/env python3
"""Isolated AuK runtime: occupancy, pin gate, precision, lease. No GPU, no weights.

The pin directory is faked with sparse files of the exact published byte sizes,
so the fail-closed weight gate is exercised without 8 GiB of checkpoints and the
worker is an in-process double that never touches CUDA.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO))

from tts.auk.pin import AUK_PIN_COMPONENTS, AUK_PIN_SIZES, resolve_pin
from tts.lab.backend.app import create_app
from tts.lab.backend.runtime.manager import E2RuntimeManager
from tts.lab.backend.runtime.processors import ProcessorLease
from tts.lab.backend.runtime.vram import FixedVramProbe
from tts.lab.backend.runtime.worker import CountingWorkerFactory, FakeAukWorkerHandle

GIB = 1024 ** 3
SOURCE_URL = "https://example.invalid/ata.wav"


def _seed_pin(root: Path, *precisions: str) -> Path:
    """Sparse files at the exact published sizes: present, but no bytes read."""
    for precision in precisions or ("bf16",):
        for name in AUK_PIN_COMPONENTS[precision].values():
            path = root / "weights" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("wb") as handle:
                handle.truncate(AUK_PIN_SIZES[name])
    tokenizer = root / "assets" / "qwen2.5-omni-3b"
    tokenizer.mkdir(parents=True, exist_ok=True)
    (tokenizer / "config.json").write_text("{}", encoding="utf-8")
    arch = root / "assets" / "auk_base"
    arch.mkdir(parents=True, exist_ok=True)
    (arch / "config.yaml").write_text("{}\n", encoding="utf-8")
    return root


def _e2_manager() -> E2RuntimeManager:
    return E2RuntimeManager(
        vram=FixedVramProbe(free=12 * GIB, used=1 * GIB),
        worker_factory=CountingWorkerFactory(),
        required_vram_bytes=1 * GIB,
        vram_margin_bytes=0,
        load_timeout_s=2.0,
        unload_timeout_s=1.0,
        synth_timeout_s=2.0,
    )


def _app(
    root: Path,
    *,
    pin: Path,
    e2: E2RuntimeManager | None = None,
    worker_factory=None,
    **kwargs,
):
    from tts.lab.backend.runtime.auk import AukRuntimeManager

    e2 = e2 or _e2_manager()
    auk = AukRuntimeManager(
        lease=ProcessorLease(e2),
        pin_root=pin,
        vram=FixedVramProbe(free=12 * GIB, used=1 * GIB),
        worker_factory=worker_factory
        or CountingWorkerFactory(handle_factory=FakeAukWorkerHandle),
        load_timeout_s=2.0,
        generate_timeout_s=2.0,
        unload_timeout_s=1.0,
    )
    return create_app(root=root, leftover_parked=False, e2_runtime=e2, auk_runtime=auk, **kwargs)


class AukRuntimeHttp(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.pin = _seed_pin(self.root / "pin")
        self.e2 = _e2_manager()
        self.app = _app(self.root / "lab", pin=self.pin, e2=self.e2)
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.client.close()
        self.tmp.cleanup()

    @property
    def lab(self):
        return self.app.state.lab

    def _load(self, body: dict | None = None):
        return self.client.post("/api/auk/runtime/load", json=body if body is not None else {})

    def _wait_ready(self, timeout: float = 5.0) -> dict:
        status = self.lab.auk.wait_until_not("loading", timeout=timeout)
        self.assertEqual(status["state"], "ready", status)
        return status

    def test_status_reports_unloaded_pin_and_no_occupant(self) -> None:
        payload = self.client.get("/api/auk/runtime").json()
        self.assertEqual(payload["state"], "unloaded")
        self.assertEqual(payload["status"], "unloaded")
        self.assertIsNone(payload["occupant"])
        self.assertEqual(payload["precision"], "bf16")
        self.assertEqual(payload["pin"], "auk_base_bf16")
        self.assertTrue(payload["weights_present"])
        self.assertFalse(payload["live_call_active"])
        self.assertEqual(payload["loaded"], {"encoder": False, "model": False, "vae": False})

    def test_load_without_weights_fails_closed_501(self) -> None:
        empty = self.root / "empty-pin"
        empty.mkdir()
        app = _app(self.root / "lab2", pin=empty)
        with TestClient(app) as client:
            response = client.post("/api/auk/runtime/load", json={"precision": "bf16"})
            self.assertEqual(response.status_code, 501, response.text)
            self.assertEqual(response.json()["detail"]["code"], "weights_missing")
            self.assertIn("auk_base_bf16.safetensors", response.json()["detail"]["missing"])
            status = client.get("/api/auk/runtime").json()
            self.assertEqual(status["state"], "unloaded")
            self.assertIsNone(status["occupant"])
            self.assertFalse(status["weights_present"])
            self.assertEqual(app.state.lab.auk.spawn_count, 0)

    def test_truncated_weight_is_not_present(self) -> None:
        name = AUK_PIN_COMPONENTS["bf16"]["diffusion"]
        with (self.pin / "weights" / name).open("r+b") as handle:
            handle.truncate(1024)
        self.assertEqual(resolve_pin("bf16", self.pin).missing(), [name])

    def test_incomplete_bundle_fails_closed_501(self) -> None:
        # No arch config: the pin is incomplete, so nothing is spawned to crash.
        (self.pin / "assets" / "auk_base" / "config.yaml").unlink()
        response = self._load({"precision": "bf16"})
        self.assertEqual(response.status_code, 501, response.text)
        self.assertEqual(
            response.json()["detail"]["missing"], ["assets/auk_base/config.yaml"]
        )
        self.assertEqual(self.lab.auk.status()["state"], "unloaded")
        self.assertEqual(self.lab.auk.spawn_count, 0)

    def test_load_unknown_precision_400(self) -> None:
        response = self._load({"precision": "fp8"})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["detail"]["code"], "unknown_precision")
        self.assertEqual(self.lab.auk.status()["state"], "unloaded")

    def test_load_defaults_to_bf16_never_int8(self) -> None:
        # int8 is downloaded but must never be selected by omission.
        _seed_pin(self.pin, "int8")
        response = self._load({})
        self.assertEqual(response.status_code, 200, response.text)
        status = self._wait_ready()
        self.assertEqual(status["precision"], "bf16")
        self.assertEqual(status["pin"], "auk_base_bf16")
        self.assertFalse(status["int8_selected"])

    def test_explicit_int8_loads_int8_pin(self) -> None:
        _seed_pin(self.pin, "int8")
        response = self._load({"precision": "int8"})
        self.assertEqual(response.status_code, 200, response.text)
        status = self._wait_ready()
        self.assertEqual(status["precision"], "int8")
        self.assertEqual(status["pin"], "auk_base_int8")
        self.assertTrue(status["int8_selected"])

    def test_int8_absent_does_not_fall_back_to_bf16(self) -> None:
        response = self._load({"precision": "int8"})
        self.assertEqual(response.status_code, 501, response.text)
        self.assertEqual(response.json()["detail"]["code"], "weights_missing")
        self.assertIn("auk_base_int8.safetensors", response.json()["detail"]["missing"])
        self.assertEqual(self.lab.auk.status()["state"], "unloaded")

    def test_load_takes_lease_unloads_e2_without_restoring_leftover(self) -> None:
        self.e2.load()
        self.assertTrue(self.e2.status().leftover_parked)
        restores = self.e2._leftover.restore_calls  # noqa: SLF001 - test double probe

        self._load({})
        status = self._wait_ready()
        self.assertEqual(status["occupant"], "auk")
        self.assertEqual([status["loaded"][k] for k in ("encoder", "model", "vae")], [True] * 3)
        self.assertEqual(self.e2.status().state, "unloaded")
        self.assertIsNone(self.e2.status().worker_pid)
        self.assertEqual(self.e2._leftover.restore_calls, restores)  # noqa: SLF001
        self.assertTrue(self.e2.status().leftover_parked)

    def test_load_blocked_by_live_call_409_and_leaves_e2_ready(self) -> None:
        self.e2.load()
        self.e2.refresh_live_call_lease(ttl_s=60)
        response = self._load({})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"]["code"], "live_call_active")
        status = self.e2.status()
        self.assertEqual(status.state, "ready")
        self.assertIsNone(status.processor)
        self.assertEqual(self.lab.auk.status()["state"], "unloaded")
        self.assertEqual(self.lab.auk.spawn_count, 0)

    def test_second_load_while_ready_is_a_noop(self) -> None:
        self._load({})
        self._wait_ready()
        self._load({})
        self.assertEqual(self.lab.auk.spawn_count, 1)

    def test_unload_releases_lease_and_stays_unloaded(self) -> None:
        self.e2.load()
        restores = self.e2._leftover.restore_calls  # noqa: SLF001
        self._load({})
        self._wait_ready()
        response = self.client.post("/api/auk/runtime/unload")
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["state"], "unloaded")
        self.assertIsNone(payload["occupant"])
        self.assertEqual(payload["loaded"], {"encoder": False, "model": False, "vae": False})
        self.assertFalse(self.lab.auk.worker_alive())
        self.assertEqual(self.e2.status().state, "unloaded")
        self.assertEqual(self.e2._leftover.restore_calls, restores)  # noqa: SLF001
        self.assertTrue(self.e2.status().leftover_parked)

    def test_components_drop_independently(self) -> None:
        self._load({})
        self._wait_ready()
        response = self.client.post("/api/auk/runtime/components/offload", json={"component": "vae"})
        self.assertEqual(response.status_code, 200, response.text)
        loaded = response.json()["loaded"]
        self.assertFalse(loaded["vae"])
        self.assertTrue(loaded["encoder"])
        self.assertTrue(loaded["model"])
        self.assertEqual(response.json()["state"], "ready")

    def test_offload_unknown_component_400(self) -> None:
        self._load({})
        self._wait_ready()
        response = self.client.post(
            "/api/auk/runtime/components/offload", json={"component": "talker"}
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["detail"]["code"], "unknown_component")

    def test_offload_while_unloaded_409(self) -> None:
        response = self.client.post(
            "/api/auk/runtime/components/offload", json={"component": "vae"}
        )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"]["code"], "runtime_unloaded")


class AukGenerateHttp(unittest.TestCase):
    """POST /api/voices/{id}/auk: candidate provenance, fail-closed generate."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.pin = _seed_pin(self.root / "pin")
        self.e2 = _e2_manager()
        self.app = _app(self.root / "lab", pin=self.pin, e2=self.e2)
        self.client = TestClient(self.app)
        self.voice = self._create_voice()
        self.source_bytes = self.store.get(self.voice["source_audio_artifact_id"]).path.read_bytes()

    def tearDown(self) -> None:
        self.client.close()
        self.tmp.cleanup()

    @property
    def lab(self):
        return self.app.state.lab

    @property
    def store(self):
        return self.app.state.lab.store

    def _create_voice(self, name: str = "ata", client=None) -> dict:
        import numpy as np

        from tts.wav import write_wav

        client = client or self.client
        wav = self.root / f"{name}.wav"
        write_wav(wav, 24000, 0.1 * np.sin(2 * np.pi * 440.0 * np.arange(2400) / 24000))
        with wav.open("rb") as handle:
            response = client.post(
                "/api/voices",
                data={"name": name, "transcript": "hello from ata", "tags": '["lab"]'},
                files={"audio": ("ata.wav", handle, "audio/wav")},
            )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _generate(self, **body):
        return self.client.post(
            f"/api/voices/{self.voice['id']}/auk",
            json={
                "auk_task": "enhance",
                "instruction": "Preserve all speakers and remove noise.",
                "seed": 7,
                "auk_precision": "bf16",
                "settings": {"nfe": 32, "cfg": 2.0, "sway": -1.0},
                **body,
            },
        )

    def test_generate_before_load_409(self) -> None:
        response = self._generate()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"]["code"], "runtime_unloaded")

    def test_generate_reports_unavailable_engine_501(self) -> None:
        # Weights resident, sampler not ported: a 501, never synthesized audio.
        app = _app(
            self.root / "lab-engine-pending",
            pin=self.pin,
            worker_factory=CountingWorkerFactory(
                handle_factory=lambda: FakeAukWorkerHandle(
                    generate_reply={
                        "ok": False,
                        "code": "engine_unavailable",
                        "message": "sampler pending",
                    }
                )
            ),
        )
        with TestClient(app) as client:
            voice = self._create_voice(client=client)
            client.post("/api/auk/runtime/load", json={"precision": "bf16"})
            app.state.lab.auk.wait_until_not("loading", timeout=5.0)
            response = client.post(
                f"/api/voices/{voice['id']}/auk",
                json={"auk_task": "enhance", "instruction": "Preserve all speakers."},
            )
            variants = app.state.lab.store.execute(
                "SELECT kind FROM reference_variants WHERE voice_id = ?", (voice["id"],)
            ).fetchall()
        self.assertEqual(response.status_code, 501, response.text)
        self.assertEqual(response.json()["detail"]["code"], "engine_unavailable")
        self.assertFalse(any(row["kind"] == "auk" for row in variants))

    def test_generate_records_provenance_without_touching_source(self) -> None:
        self.client.post("/api/auk/runtime/load", json={"precision": "bf16"})
        self.lab.auk.wait_until_not("loading", timeout=5.0)
        response = self._generate()
        self.assertEqual(response.status_code, 200, response.text)
        voice = response.json()
        candidate = next(item for item in voice["variants"] if item["kind"] == "auk")
        self.assertEqual(candidate["auk_task"], "enhance")
        self.assertEqual(candidate["instruction"], "Preserve all speakers and remove noise.")
        self.assertEqual(candidate["auk_precision"], "bf16")
        self.assertEqual(candidate["encoder_precision"], "w4a8")
        self.assertEqual(candidate["seed"], 7)
        self.assertEqual(candidate["settings"], {"nfe": 32, "cfg": 2.0, "sway": -1.0})
        self.assertEqual(
            self.store.get(self.voice["source_audio_artifact_id"]).path.read_bytes(),
            self.source_bytes,
        )

    def test_generate_precision_must_match_loaded_pin(self) -> None:
        self.client.post("/api/auk/runtime/load", json={"precision": "bf16"})
        self.lab.auk.wait_until_not("loading", timeout=5.0)
        response = self._generate(auk_precision="int8")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"]["code"], "precision_mismatch")

    def test_generate_unknown_voice_404(self) -> None:
        self.client.post("/api/auk/runtime/load", json={"precision": "bf16"})
        self.lab.auk.wait_until_not("loading", timeout=5.0)
        response = self.client.post(
            "/api/voices/nope/auk",
            json={"auk_task": "enhance", "instruction": "x"},
        )
        self.assertEqual(response.status_code, 404, response.text)


if __name__ == "__main__":
    unittest.main()
