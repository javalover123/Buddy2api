"""`$wbEncrypted` 凭据信封解密。

回归（2026-09-28）：WorkBuddy AI 桌面端 5.6.2 把 auth 文件里的
accessToken/refreshToken 换成了加密信封，国际版账号一个都导不进来。

这里钉死四件事：
1. 密钥推导（sha256(secret_utf8) → key，sha256(key)[:16] → keyId）与信封自报的
   keyId 对账 —— 这是唯一能在离线状态下判断「密钥对不对」的依据；
2. AAD 逐字节正确（错一个字节 GCM 就 InvalidTag）；
3. keyId 不匹配时必须明确报错，绝不能拿错密钥硬解；
4. 拿不到密钥时返回空串而不是抛异常 —— 导入路径不该因为客户端没开就崩。
"""

import base64
import hashlib
import json
import struct

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import buddy2api.wb_at_rest as wb_at_rest

# 实测取自本机 WorkBuddy AI 5.6.2 的 workbuddyStorage.loggerGet()
SECRET = "Sik9U5aXhCdwTVEwsEySDOmDoB9r9ntFxHF1fst9LQI="
EXPECTED_KEY_ID = "9127dea1b44020a7"
PLAINTEXT = "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.fake-token-payload.signature"


def _seal(plaintext: str, secret: str = SECRET, key_id_override=None) -> dict:
    """按官方格式造一个信封，供解密侧验证。"""
    key, key_id = wb_at_rest.derive_key(secret)
    key_id = key_id_override or key_id
    nonce = bytes(range(12))
    aad = wb_at_rest.build_field_aad(key_id)
    blob = AESGCM(key).encrypt(nonce, plaintext.encode(), aad)
    ciphertext, auth_tag = blob[:-16], blob[-16:]
    envelope = {
        "suite": 1,
        "keyId": key_id,
        "nonce": base64.b64encode(nonce).decode(),
        "authTag": base64.b64encode(auth_tag).decode(),
        "ciphertext": base64.b64encode(ciphertext).decode(),
    }
    return {
        "$wbEncrypted": 1,
        "envelope": base64.b64encode(json.dumps(envelope).encode()).decode(),
    }


@pytest.fixture(autouse=True)
def _clear_cache(monkeypatch):
    monkeypatch.delenv(wb_at_rest.SECRET_KEY_VAR, raising=False)
    wb_at_rest.reset_cache()
    yield
    wb_at_rest.reset_cache()


def test_derived_key_id_matches_envelope_declared_id():
    """密钥推导必须与信封自报的 keyId 一致 —— 这是离线校验密钥的唯一依据。"""
    key, key_id = wb_at_rest.derive_key(SECRET)

    assert len(key) == 32
    assert key_id == EXPECTED_KEY_ID
    assert hashlib.sha256(key).hexdigest()[:16] == key_id


def test_roundtrip_decrypts_to_plaintext():
    assert wb_at_rest.decrypt_field(_seal(PLAINTEXT), SECRET) == PLAINTEXT


def test_aad_is_byte_exact():
    """AAD 是手写复刻的，错一个字节 GCM 就解不开 —— 逐字节钉住。"""
    aad = wb_at_rest.build_field_aad("9127dea1b44020a7")

    def lp(text):
        raw = text.encode()
        return struct.pack(">I", len(raw)) + raw

    assert aad == (
        b"WB-AAD\0" + bytes([1]) + lp("WBEV1") + lp("sym-v1")
        + struct.pack(">I", 1) + lp("9127dea1b44020a7") + bytes([2]) + bytes([0, 0])
    )


def test_wrong_key_is_rejected_before_decrypting():
    """密钥不对必须报 keyId 不匹配，不能拿错密钥硬解。"""
    other = base64.b64encode(b"x" * 32).decode()
    wrapper = _seal(PLAINTEXT)  # 用真密钥封的

    with pytest.raises(wb_at_rest.WbAtRestError, match="key id mismatch"):
        wb_at_rest.decrypt_field(wrapper, other)


def test_tampered_ciphertext_is_rejected():
    wrapper = _seal(PLAINTEXT)
    envelope = json.loads(base64.b64decode(wrapper["envelope"]))
    ciphertext = bytearray(base64.b64decode(envelope["ciphertext"]))
    ciphertext[0] ^= 0xFF
    envelope["ciphertext"] = base64.b64encode(bytes(ciphertext)).decode()
    wrapper["envelope"] = base64.b64encode(json.dumps(envelope).encode()).decode()

    with pytest.raises(wb_at_rest.WbAtRestError, match="decrypt failed"):
        wb_at_rest.decrypt_field(wrapper, SECRET)


def test_plain_string_is_not_an_envelope():
    assert wb_at_rest.is_encrypted_field("eyJhbGci") is False
    assert wb_at_rest.is_encrypted_field(None) is False
    assert wb_at_rest.is_encrypted_field({}) is False
    assert wb_at_rest.is_encrypted_field({"$wbEncrypted": 1}) is False


def test_envelope_detection_accepts_official_shape():
    assert wb_at_rest.is_encrypted_field(_seal(PLAINTEXT)) is True


def test_configured_key_wins_and_is_cached(monkeypatch):
    monkeypatch.setenv(wb_at_rest.SECRET_KEY_VAR, SECRET)
    wb_at_rest.reset_cache()

    assert wb_at_rest.read_secret_key() == SECRET
    assert wb_at_rest.read_secret_key() == SECRET


def test_missing_client_yields_none_not_raise(monkeypatch):
    """客户端没在跑时返回 None —— 导入路径不能因此抛异常。"""
    monkeypatch.setattr(wb_at_rest, "_read_key_from_running_client", lambda: None)
    wb_at_rest.reset_cache()

    assert wb_at_rest.read_secret_key() is None


def test_parse_auth_file_decrypts_encrypted_tokens(tmp_path, monkeypatch):
    """端到端：加密信封的 auth 文件现在能解析出明文 token。"""
    import buddy2api.auth_manager as auth_manager

    monkeypatch.setenv(wb_at_rest.SECRET_KEY_VAR, SECRET)
    wb_at_rest.reset_cache()

    document = {
        "account": {"uid": "u-1", "nickname": "tester", "type": "personal"},
        "auth": {
            "accessToken": _seal("ACCESS-PLAIN"),
            "refreshToken": _seal("REFRESH-PLAIN"),
            "expiresAt": 123,
            "domain": "www.workbuddy.ai",
            "sessionState": "s-1",
        },
    }
    path = tmp_path / "workbuddy-desktop-ai.info"
    path.write_text(json.dumps(document), encoding="utf-8")

    parsed = auth_manager.parse_auth_file(path)

    assert parsed is not None
    assert parsed["access_token"] == "ACCESS-PLAIN"
    assert parsed["refresh_token"] == "REFRESH-PLAIN"
    assert parsed["uid"] == "u-1"


def test_parse_auth_file_skips_when_key_unavailable(tmp_path, monkeypatch):
    """没有密钥时按无凭据跳过，绝不把信封 dict 透传下去。"""
    import buddy2api.auth_manager as auth_manager

    monkeypatch.setattr(wb_at_rest, "_read_key_from_running_client", lambda: None)
    wb_at_rest.reset_cache()

    document = {
        "account": {"uid": "u-2"},
        "auth": {"accessToken": _seal("ACCESS-PLAIN"), "domain": "www.workbuddy.ai"},
    }
    path = tmp_path / "workbuddy-desktop-ai.info"
    path.write_text(json.dumps(document), encoding="utf-8")

    assert auth_manager.parse_auth_file(path) is None
