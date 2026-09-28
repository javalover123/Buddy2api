"""解密 WorkBuddy 桌面端的 `$wbEncrypted` 凭据信封。

## 背景（2026-09-28）

WorkBuddy AI 桌面端 5.6.2 起，auth 文件里的 `accessToken` / `refreshToken`
不再是明文，而是：

    {"$wbEncrypted": 1, "envelope": "<base64>"}

于是 `auth_manager.parse_auth_file()` 读不出 token，国际版账号一个都导不进来
（国内版 `workbuddy-desktop.info` 仍是明文，所以只有国际版受影响）。

## 密钥从哪来

AES-256-GCM 的密钥不是文件，而是运行期由原生绑定下发的
`electron.workbuddyStorage.loggerGet()`，返回：

    {"version":1,
     "atRestSecretKey":"<44 字符 canonical base64，32 字节>",
     "atRestDeveloperPublicKey":{...}}

推导链（`cli/dist/codebuddy-*.js` 的 `normalizeAtRestKeyPayload` 原文，
并用信封里声明的 `keyId` 实测校验通过）：

    key   = sha256(atRestSecretKey, "utf8")   # 注意哈希的是 base64 **字符串**
    keyId = sha256(key).hexdigest()[:16]

密钥不在安装包、不在任何数据文件里（实测扫过 App 包 + 全部用户目录共 75751 个
文件，无命中），只能从运行中的客户端取。取值方式见 `read_secret_key()`。

## AAD 是怎么拼的

`buildAuthenticatedContextAad(keyId, suite, {framing:"field"})` 的逐字段复刻：

    "WB-AAD\\0" | 01 | u32len("WBEV1") | u32len("sym-v1") | u32(suite=1)
                | u32len(keyId) | 02 | 00 | 00

（末两个 00 是 optional uint64 sequence 与 optional final 的「缺失」标记。）

## 兜底

`CB_GATEWAY_WB_AT_REST_KEY` 可直接写死密钥，跳过客户端取值；
没有密钥或解密失败时，调用方按「无凭据」跳过，绝不写坏存量令牌。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
import subprocess
import sys
from pathlib import Path
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

SECRET_KEY_VAR = "CB_GATEWAY_WB_AT_REST_KEY"

# 帧类型 → (magic tag, 编号)。只有字段帧（field）会出现在 auth 文件里。
_FRAME_FIELD = ("WBEV1", 2)
_SUITE_SYM_V1 = 1
_AAD_PREFIX = b"WB-AAD\0"


class WbAtRestError(RuntimeError):
    """信封无法解密（缺密钥、AAD 不匹配、密文损坏等）。"""


def is_encrypted_field(value) -> bool:
    """是否 `{"$wbEncrypted": 1, "envelope": "..."}` 信封。"""
    if not isinstance(value, dict):
        return False
    return (
        value.get("$wbEncrypted") == 1
        and isinstance(value.get("envelope"), str)
        and bool(value["envelope"])
    )


def derive_key(secret_key: str) -> tuple[bytes, str]:
    """atRestSecretKey → (AES 密钥, keyId)。

    keyId 用来和信封自报的 keyId 对账：对不上就说明密钥不是这一份。
    """
    raw = (secret_key or "").strip()
    if not raw:
        raise WbAtRestError("at-rest secret key is empty")
    key = hashlib.sha256(raw.encode("utf-8")).digest()
    return key, hashlib.sha256(key).hexdigest()[:16]


def _length_prefixed(text: str) -> bytes:
    raw = text.encode("utf-8")
    return struct.pack(">I", len(raw)) + raw


def build_field_aad(key_id: str, suite: int = _SUITE_SYM_V1) -> bytes:
    """字段帧的 AAD，逐字段复刻官方 buildAuthenticatedContextAad。"""
    tag, frame_no = _FRAME_FIELD
    return b"".join(
        (
            _AAD_PREFIX,
            bytes([1]),
            _length_prefixed(tag),
            _length_prefixed("sym-v1"),
            struct.pack(">I", suite),
            _length_prefixed(key_id),
            bytes([frame_no]),
            bytes([0]),   # optional uint64 sequence：缺失
            bytes([0]),   # optional final：缺失
        )
    )


def decrypt_field(value, secret_key: str) -> str:
    """解开一个字段信封，返回明文。"""
    if not is_encrypted_field(value):
        raise WbAtRestError("not a $wbEncrypted field wrapper")
    key, key_id = derive_key(secret_key)
    try:
        envelope = json.loads(base64.b64decode(value["envelope"], validate=True))
    except (ValueError, json.JSONDecodeError) as exc:
        raise WbAtRestError("envelope is not canonical base64 JSON") from exc
    declared = envelope.get("keyId")
    if declared != key_id:
        raise WbAtRestError(
            f"key id mismatch: envelope says {declared}, key derives {key_id}"
        )
    try:
        nonce = base64.b64decode(envelope["nonce"], validate=True)
        ciphertext = base64.b64decode(envelope["ciphertext"], validate=True)
        auth_tag = base64.b64decode(envelope["authTag"], validate=True)
    except (KeyError, ValueError) as exc:
        raise WbAtRestError("envelope fields are malformed") from exc
    aad = build_field_aad(key_id, envelope.get("suite") or _SUITE_SYM_V1)
    try:
        plain = AESGCM(key).decrypt(nonce, ciphertext + auth_tag, aad)
    except Exception as exc:  # InvalidTag 等
        raise WbAtRestError(f"decrypt failed: {type(exc).__name__}") from exc
    return plain.decode("utf-8")


# ============================================================
# 密钥取值：先看显式配置，再从运行中的客户端取
# ============================================================

_cached_key: Optional[str] = None
_key_lookup_done = False


def reset_cache() -> None:
    """清掉进程内缓存的密钥（测试用）。"""
    global _cached_key, _key_lookup_done
    _cached_key = None
    _key_lookup_done = False


def configured_key() -> Optional[str]:
    """`CB_GATEWAY_WB_AT_REST_KEY` 显式配置的密钥。"""
    return (os.environ.get(SECRET_KEY_VAR) or "").strip() or None


def read_secret_key(*, refresh: bool = False) -> Optional[str]:
    """取 atRestSecretKey：显式配置优先，否则从运行中的客户端读。

    读不到返回 None（调用方按「无凭据」处理），不抛异常 —— 导入路径不该因为
    客户端没开就报错。
    """
    global _cached_key, _key_lookup_done
    if not refresh and _key_lookup_done:
        return _cached_key
    _key_lookup_done = True

    override = configured_key()
    if override:
        _cached_key = override
        return _cached_key

    _cached_key = _read_key_from_running_client()
    return _cached_key


def _read_key_from_running_client() -> Optional[str]:
    """从运行中的 WorkBuddy AI 客户端取密钥。

    走 Electron 的 inspector（CDP）：客户端以 `--inspect=<port>` 启动时，
    `/json/list` 给出调试目标，在它的主进程上下文里求值即可拿到
    `electron.workbuddyStorage.loggerGet()`。

    需要客户端在跑；没在跑就返回 None，由调用方提示用户。
    """
    script = Path(__file__).with_name("wb_at_rest_probe.cjs")
    if not script.exists():
        return None
    try:
        completed = subprocess.run(
            ["node", str(script)],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"[wb_at_rest] 读取客户端密钥失败: {exc}", file=sys.stderr)
        return None
    value = (completed.stdout or "").strip()
    if not value:
        return None
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return None
    secret = str(payload.get("atRestSecretKey") or "").strip()
    return secret or None
