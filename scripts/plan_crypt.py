# -*- coding: utf-8 -*-





import base64
import hashlib
import hmac
import json

PLAN_ENC_ALG = "hmac-sha256-ctr"
PLAN_ENC_SALT = "plan-enc"


class PlanCryptError(Exception):
    pass


def _hex2bytes(value, name: str) -> bytes:
    try:
        raw = bytes.fromhex(str(value or ""))
    except ValueError:
        raise PlanCryptError(f"{name} 不是合法的十六进制串")
    if not raw:
        raise PlanCryptError(f"缺少 {name}")
    return raw


def keystream(key_hex: str, nonce_hex: str, size: int) -> bytes:

    key = _hex2bytes(key_hex, "处理凭据")
    nonce = _hex2bytes(nonce_hex, "随机数")
    out = bytearray()
    n = 0
    while len(out) < size:
        out.extend(hmac.new(key, nonce + n.to_bytes(8, "big"),
                            hashlib.sha256).digest())
        n += 1
    return bytes(out[:size])


def xor_bytes(data: bytes, key_hex: str, nonce_hex: str) -> bytes:
    ks = keystream(key_hex, nonce_hex, len(data))
    return bytes(a ^ b for a, b in zip(data, ks))


def compute_tag(key_hex: str, nonce_hex: str, ct: bytes) -> str:
    return hmac.new(_hex2bytes(key_hex, "处理凭据"),
                    _hex2bytes(nonce_hex, "随机数") + ct,
                    hashlib.sha256).hexdigest()


def is_blob(obj) -> bool:

    return (isinstance(obj, dict) and "ct" in obj and "nonce" in obj
            and "tag" in obj)


def decrypt_blob(blob, key_hex: str) -> dict:

    if not is_blob(blob):
        raise PlanCryptError("不是可处理的数据块结构")
    alg = str(blob.get("alg") or "")
    if alg != PLAN_ENC_ALG:
        raise PlanCryptError(f"不支持的数据格式: {alg!r}")
    if int(blob.get("enc_version") or 0) != 1:
        raise PlanCryptError(f"不支持的数据版本: {blob.get('enc_version')!r}")
    try:
        ct = base64.b64decode(str(blob.get("ct") or ""), validate=True)
    except Exception:
        raise PlanCryptError("数据块不是合法的 base64")
    if not ct:
        raise PlanCryptError("数据块为空")

    nonce = str(blob.get("nonce") or "")
    want = compute_tag(key_hex, nonce, ct)
    if not hmac.compare_digest(want, str(blob.get("tag") or "")):
        raise PlanCryptError("数据块校验失败（凭据不匹配或内容被改动）")

    plain = xor_bytes(ct, key_hex, nonce)
    if not hmac.compare_digest(hashlib.sha256(plain).hexdigest(),
                               str(blob.get("plain_sha256") or "")):
        raise PlanCryptError("还原内容摘要与声明不符")

    try:
        plan = json.loads(plain.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise PlanCryptError("还原结果不是合法 JSON")
    if not isinstance(plan, dict):
        raise PlanCryptError("还原结果不是期望的数据结构")
    stated = str(blob.get("plan_version") or "")
    if stated and str(plan.get("plan_version") or "") != stated:
        raise PlanCryptError("数据块声明的版本与内容不一致")
    return plan