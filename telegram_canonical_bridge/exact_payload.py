"""逐字資料面的 UTF-8 artifact 契約。

Bridge 的任務追蹤文字屬於控制面，不得被拼進需要逐字交付的公開正文。
本模組只做一件事：把 Controller 明確提供的 exact text 原封不動寫成
content-addressed artifact，並以 task-specific binding 供狀態查詢回讀。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .task_model import normalize_task_id


CONTRACT_VERSION = 1
EXACT_TEXT_KIND = "exact_text"
MAX_EXACT_PAYLOAD_BYTES = 1024 * 1024
_BODY_FILENAME = "body.utf8.txt"
_MANIFEST_FILENAME = "manifest.json"


class ExactPayloadError(ValueError):
    """exact payload 無法安全建立或驗證。"""


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temp = tempfile.mkstemp(prefix=".tcb-", dir=path.parent)
    temp = Path(raw_temp)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        with contextlib.suppress(PermissionError):
            path.chmod(0o600)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _storage_root(plugin_data_root: Path | str) -> Path:
    return Path(plugin_data_root).resolve() / "exact-payloads"


def _binding_path(plugin_data_root: Path | str, task_id: str) -> Path:
    normalized = normalize_task_id(task_id)
    if not normalized:
        raise ExactPayloadError("task_id 格式無效，拒絕建立 payload binding。")
    return _storage_root(plugin_data_root) / "bindings" / f"{normalized}.json"


def _core_contract(storage: Path, digest: str, byte_length: int) -> dict[str, Any]:
    artifact = storage / "blobs" / "sha256" / digest / _BODY_FILENAME
    return {
        "contract_version": CONTRACT_VERSION,
        "kind": EXACT_TEXT_KIND,
        "encoding": "utf-8",
        "media_type": "text/plain; charset=utf-8",
        "artifact_path": str(artifact.resolve()),
        "sha256": digest,
        "byte_length": byte_length,
    }


def _view(contract: Mapping[str, Any]) -> dict[str, Any]:
    artifact = Path(str(contract["artifact_path"])).resolve()
    return {
        **dict(contract),
        "manifest_path": str((artifact.parent / _MANIFEST_FILENAME).resolve()),
        "verified": True,
    }


def create_exact_text_payload(
    plugin_data_root: Path | str,
    task_id: str,
    value: Any,
) -> dict[str, Any] | None:
    """建立逐字 payload；``None`` 表示本 task 沒有逐字資料面。"""

    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ExactPayloadError("exact_payload 必須是包含 text 的物件。")
    unknown = set(value) - {"kind", "text"}
    if unknown:
        raise ExactPayloadError(
            "exact_payload 含未支援欄位：" + ", ".join(sorted(map(str, unknown)))
        )
    kind = value.get("kind", EXACT_TEXT_KIND)
    if kind != EXACT_TEXT_KIND:
        raise ExactPayloadError("exact_payload.kind 目前只支援 exact_text。")
    text = value.get("text")
    if not isinstance(text, str):
        raise ExactPayloadError("exact_payload.text 必須是字串。")
    content = text.encode("utf-8")
    if not content:
        raise ExactPayloadError("exact_payload.text 不得為空字串。")
    if len(content) > MAX_EXACT_PAYLOAD_BYTES:
        raise ExactPayloadError(
            f"exact_payload UTF-8 大小不得超過 {MAX_EXACT_PAYLOAD_BYTES} bytes。"
        )

    storage = _storage_root(plugin_data_root)
    digest = hashlib.sha256(content).hexdigest()
    contract = _core_contract(storage, digest, len(content))
    binding = _binding_path(plugin_data_root, task_id)
    if binding.is_file():
        try:
            existing = json.loads(binding.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ExactPayloadError("既有 payload binding 無法讀取。") from exc
        if existing != contract:
            raise ExactPayloadError("同一 task 已綁定不同的 exact payload，拒絕覆寫。")
        return read_exact_text_payload(plugin_data_root, task_id)

    artifact = Path(contract["artifact_path"])
    manifest = artifact.parent / _MANIFEST_FILENAME
    _atomic_write(artifact, content)
    _atomic_write(manifest, _json_bytes(contract))
    _atomic_write(binding, _json_bytes(contract))
    return read_exact_text_payload(plugin_data_root, task_id)


def read_exact_text_payload(
    plugin_data_root: Path | str,
    task_id: str,
) -> dict[str, Any] | None:
    """由 task binding 回讀並重新驗證 manifest、bytes、長度與 digest。"""

    binding = _binding_path(plugin_data_root, task_id)
    if not binding.is_file():
        return None
    try:
        contract = json.loads(binding.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ExactPayloadError("payload binding 無法解析。") from exc
    if not isinstance(contract, dict):
        raise ExactPayloadError("payload binding 格式無效。")

    expected_fields = {
        "contract_version",
        "kind",
        "encoding",
        "media_type",
        "artifact_path",
        "sha256",
        "byte_length",
    }
    if set(contract) != expected_fields:
        raise ExactPayloadError("payload contract 欄位不完整或含未知欄位。")
    if (
        contract.get("contract_version") != CONTRACT_VERSION
        or contract.get("kind") != EXACT_TEXT_KIND
        or contract.get("encoding") != "utf-8"
        or contract.get("media_type") != "text/plain; charset=utf-8"
    ):
        raise ExactPayloadError("payload contract 版本或型別不受支援。")

    artifact = Path(str(contract["artifact_path"])).resolve()
    blobs = (_storage_root(plugin_data_root) / "blobs").resolve()
    try:
        artifact.relative_to(blobs)
    except ValueError as exc:
        raise ExactPayloadError("payload artifact 超出 plugin data root。") from exc
    if artifact.name != _BODY_FILENAME:
        raise ExactPayloadError("payload artifact 名稱不符合契約。")

    manifest = artifact.parent / _MANIFEST_FILENAME
    try:
        manifest_contract = json.loads(manifest.read_text(encoding="utf-8"))
        content = artifact.read_bytes()
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ExactPayloadError("payload artifact 或 manifest 無法回讀。") from exc
    if manifest_contract != contract:
        raise ExactPayloadError("payload manifest 與 task binding 不一致。")
    digest = hashlib.sha256(content).hexdigest()
    if digest != contract.get("sha256") or len(content) != contract.get("byte_length"):
        raise ExactPayloadError("payload bytes 與 SHA-256／長度契約不一致。")
    try:
        content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ExactPayloadError("payload artifact 不是有效 UTF-8。") from exc
    return _view(contract)


__all__ = [
    "CONTRACT_VERSION",
    "EXACT_TEXT_KIND",
    "MAX_EXACT_PAYLOAD_BYTES",
    "ExactPayloadError",
    "create_exact_text_payload",
    "read_exact_text_payload",
]
