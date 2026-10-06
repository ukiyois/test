from __future__ import annotations

import keyring
from keyring.errors import KeyringError


SERVICE_NAME = "LLMmodol"


def set_secret(reference: str, value: str) -> None:
    try:
        keyring.set_password(SERVICE_NAME, reference, value)
    except KeyringError as exc:
        raise RuntimeError(
            "Windows 凭据管理器不可用，API 密钥没有保存。请先检查 keyring 后端。"
        ) from exc


def get_secret(reference: str) -> str | None:
    try:
        return keyring.get_password(SERVICE_NAME, reference)
    except KeyringError as exc:
        raise RuntimeError("读取系统凭据失败。") from exc


def delete_secret(reference: str) -> None:
    try:
        keyring.delete_password(SERVICE_NAME, reference)
    except keyring.errors.PasswordDeleteError:
        return
    except KeyringError as exc:
        raise RuntimeError("删除系统凭据失败。") from exc

