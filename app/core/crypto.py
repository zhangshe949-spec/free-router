"""API key 加密辅助：支持 api_key 以 'enc:' 前缀时解密。

优先使用 cryptography 的 AES-GCM；若该库不可用，则退化为
hashlib+base64 的 XOR 占位实现（仅混淆，非强加密，见下方注释）。
密钥材料来自环境变量 FREE_ROUTER_ENC_KEY（缺省时用一个固定字符串派生，
仅用于本地开发；生产务必设置该环境变量）。

约定：加密后的密文以 "enc:" 开头，内容为 base64。
- AES-GCM 分支：base64( nonce(12B) || ciphertext || tag(16B) )
- XOR 占位分支：base64( utf-8(xor(plaintext, keystream)) )
绝不硬编码任何真实密钥。
"""
import base64
import hashlib
import os

try:  # 可选依赖：存在则用强加密
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    _HAS_AES = True
except Exception:  # pragma: no cover - 取决于运行环境
    _HAS_AES = False

_PREFIX = "enc:"
_DEFAULT_SECRET = "free-router-default-enc-key-do-not-use-in-prod"


def _secret_bytes() -> bytes:
    secret = os.environ.get("FREE_ROUTER_ENC_KEY") or _DEFAULT_SECRET
    # 派生 32 字节密钥（AES-256 / XOR keystream 共用）
    return hashlib.sha256(secret.encode("utf-8")).digest()


def _xor(data: bytes, key: bytes) -> bytes:
    """用 sha256(key) 生成的 keystream 做 XOR（占位实现，注释见文件头）。"""
    ks = hashlib.sha256(key + b"::keystream").digest()
    out = bytearray()
    for i, b in enumerate(data):
        out.append(b ^ ks[i % len(ks)])
    return bytes(out)


def encrypt(plaintext: str) -> str:
    """加密明文，返回 'enc:<base64>'。"""
    key = _secret_bytes()
    if _HAS_AES:
        nonce = os.urandom(12)
        ct = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
        return _PREFIX + base64.b64encode(nonce + ct).decode("ascii")
    ct = _xor(plaintext.encode("utf-8"), key)
    return _PREFIX + base64.b64encode(ct).decode("ascii")


def decrypt(token: str) -> str:
    """解密 'enc:<base64>'；非 enc: 前缀则原样返回。"""
    if not token:
        return token
    if not token.startswith(_PREFIX):
        return token
    data = base64.b64decode(token[len(_PREFIX):])
    key = _secret_bytes()
    if _HAS_AES:
        nonce, ct = data[:12], data[12:]
        return AESGCM(key).decrypt(nonce, ct, None).decode("utf-8")
    return _xor(data, key).decode("utf-8")


def maybe_decrypt(value):
    """若为 enc: 前缀则解密，否则原样返回（None/空字符串安全）。"""
    if value and isinstance(value, str) and value.startswith(_PREFIX):
        return decrypt(value)
    return value


def is_encrypted(value) -> bool:
    return bool(value) and isinstance(value, str) and value.startswith(_PREFIX)