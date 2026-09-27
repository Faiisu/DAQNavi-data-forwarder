"""Operator access control, session lifecycle, and secret redaction for DAQ Navi."""

import os
import re
import time
import hmac
import hashlib
import secrets
import urllib.parse
from urllib.parse import urlparse
from typing import Optional, Dict, Any, Tuple

COOKIE_NAME = "daq_session_id"
DEFAULT_SESSION_TTL_SECONDS = 3600  # 1 hour
REDACTED_PLACEHOLDER = "********"
CLEAR_SECRET = "__CLEAR__"
SECRET_FIELDS = ("DB_PASSWORD", "POSTGRES_PASSWORD", "INFLUX_TOKEN", "MQTT_PASSWORD")


def hash_password(password: str, salt: Optional[bytes] = None, iterations: int = 100_000) -> str:
    """Generate a salted PBKDF2-HMAC-SHA256 hash string."""
    if not salt:
        salt = secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256:{iterations}:{salt.hex()}:{derived.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    """Verify password against a stored salted hash using constant-time comparison."""
    if not stored_hash or not password:
        return False
    parts = stored_hash.split(":")
    if len(parts) != 4 or parts[0] != "pbkdf2_sha256":
        return False
    try:
        iterations = int(parts[1])
        salt = bytes.fromhex(parts[2])
        expected = bytes.fromhex(parts[3])
    except (ValueError, TypeError):
        return False

    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(derived, expected)


def load_auth_secrets(env: Optional[Dict[str, str]] = None) -> Tuple[Dict[str, str], str]:
    """
    Load operator credentials and session signing key from environment or secret files.
    Fails closed if either is missing or empty.
    Returns: ({username: salted_hash}, session_signing_key)
    """
    if env is None:
        env = os.environ

    # 1. Load password hash: check hash_file first if defined/exists, else fallback to env
    password_hash = ""
    hash_file = env.get("DAQ_OPERATOR_HASH_FILE", "").strip()
    if hash_file and os.path.exists(hash_file):
        try:
            with open(hash_file, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if content:
                    password_hash = content
        except Exception:
            password_hash = ""

    if not password_hash:
        password_hash = env.get("DAQ_OPERATOR_HASH", "").strip()


    # 2. Load session signing key
    session_key = env.get("DAQ_SESSION_KEY", "").strip()
    key_file = env.get("DAQ_SESSION_KEY_FILE", "").strip()
    if not session_key and key_file and os.path.exists(key_file):
        try:
            with open(key_file, "r", encoding="utf-8") as f:
                session_key = f.read().strip()
        except Exception:
            session_key = ""

    # Fail-closed check
    if not password_hash:
        raise RuntimeError("Fail-closed: Missing or empty DAQ operator password hash secret.")
    if not session_key:
        raise RuntimeError("Fail-closed: Missing or empty DAQ session signing key secret.")

    username = env.get("DAQ_OPERATOR_USER", "operator").strip() or "operator"
    return ({username: password_hash}, session_key)


def save_operator_hash(hash_str: str, file_path: Optional[str] = None) -> str:
    """
    Atomically save password hash to a persistent file.
    If file_path is omitted, uses DAQ_OPERATOR_HASH_FILE or default persistent spool location.
    """
    if not file_path:
        file_path = os.environ.get("DAQ_OPERATOR_HASH_FILE", "").strip()
    if not file_path:
        spool_dir = "/var/lib/daq_navi/spool"
        if os.path.isdir(spool_dir):
            file_path = os.path.join(spool_dir, "operator_hash")
        else:
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            file_path = os.path.join(base_dir, ".operator_hash")

    resolved_path = os.path.abspath(file_path)
    os.makedirs(os.path.dirname(resolved_path), exist_ok=True)
    temp_path = f"{resolved_path}.tmp.{secrets.token_hex(4)}"
    with open(temp_path, "w", encoding="utf-8") as f:
        f.write(hash_str.strip() + "\n")
    os.replace(temp_path, resolved_path)
    return resolved_path


class SessionStore:
    """Manages active operator sessions and signed cookies."""

    def __init__(self, signing_key: str, ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS):
        self.signing_key = signing_key.encode("utf-8") if isinstance(signing_key, str) else signing_key
        self.ttl_seconds = ttl_seconds
        self._active_sessions: Dict[str, Dict[str, Any]] = {}

    def _sign(self, session_id: str, timestamp: int) -> str:
        data = f"{session_id}.{timestamp}".encode("utf-8")
        return hmac.new(self.signing_key, data, hashlib.sha256).hexdigest()

    def create_session(self, username: str) -> Tuple[str, str]:
        """Creates a session and returns (session_id, signed_cookie_value)."""
        session_id = secrets.token_hex(16)
        now = time.time()
        expires_at = now + self.ttl_seconds
        self._active_sessions[session_id] = {
            "username": username,
            "created_at": now,
            "expires_at": expires_at,
        }
        sig = self._sign(session_id, int(now))
        cookie_val = f"{session_id}.{int(now)}.{sig}"
        return session_id, cookie_val

    def validate_cookie(self, cookie_val: Optional[str]) -> Optional[Dict[str, Any]]:
        """Validate signed cookie and verify session is active and not expired."""
        if not cookie_val:
            return None
        parts = cookie_val.split(".")
        if len(parts) != 3:
            return None
        session_id, ts_str, signature = parts
        try:
            timestamp = int(ts_str)
        except ValueError:
            return None

        # Verify HMAC
        expected_sig = self._sign(session_id, timestamp)
        if not hmac.compare_digest(signature, expected_sig):
            return None

        # Check server-side session
        session = self._active_sessions.get(session_id)
        if not session:
            return None

        # Check expiration
        now = time.time()
        if now >= session.get("expires_at", 0):
            self.invalidate_session(cookie_val)
            return None

        return session

    def invalidate_session(self, cookie_val: Optional[str]) -> None:
        """Invalidate and remove session from active store."""
        if not cookie_val:
            return
        parts = cookie_val.split(".")
        if len(parts) >= 1:
            session_id = parts[0]
            self._active_sessions.pop(session_id, None)

    def is_valid_session(self, cookie_val: Optional[str]) -> bool:
        return self.validate_cookie(cookie_val) is not None


def redact_dsn_password(dsn: Optional[str]) -> str:
    """Mask password embedded inside a DSN connection string (URL or keyword)."""
    if not dsn:
        return ""
    # Matches :password@ in connection strings like postgresql://user:pass@host:port/db
    masked = re.sub(r":([^/@:\s]+)@", f":{REDACTED_PLACEHOLDER}@", dsn)
    # Matches password=... or password='...' in keyword DSNs
    masked = re.sub(r"(password\s*=\s*)([^\s'\"]+|'[^']*'|\"[^\"]*\")", rf"\g<1>{REDACTED_PLACEHOLDER}", masked, flags=re.IGNORECASE)
    return masked


def redact_config_secrets(config: Dict[str, Any]) -> Dict[str, Any]:
    """Return a deep copy of config with all sensitive secrets masked."""
    result = dict(config)
    for key in SECRET_FIELDS:
        if key in result and result[key]:
            result[key] = REDACTED_PLACEHOLDER

    if "DB_DSN" in result and result["DB_DSN"]:
        result["DB_DSN"] = redact_dsn_password(result["DB_DSN"])

    return result


def redact_error_message(message: str, *configs: Dict[str, Any]) -> str:
    """Sanitize secrets embedded inside exception or error strings."""
    if not message:
        return ""
    cleaned = str(message)
    secrets_to_hide = {
        str(config[key])
        for config in configs if isinstance(config, dict)
        for key in SECRET_FIELDS if config.get(key)
    }
    for secret in sorted(secrets_to_hide, key=len, reverse=True):
        for form in (secret, urllib.parse.quote(secret, safe="")):
            cleaned = cleaned.replace(form, REDACTED_PLACEHOLDER)
    return redact_dsn_password(cleaned)


def infer_db_connection_mode(config: Dict[str, Any]) -> str:
    """
    Deterministic migration rule for existing configurations without DB_CONNECTION_MODE.
    If DB_DSN equals the standard DSN derived from DB_HOST, DB_PORT, DB_NAME, DB_USER,
    and DB_PASSWORD, it is 'fields' mode. Otherwise it is preserved as custom 'dsn' mode.
    """
    explicit = config.get("DB_CONNECTION_MODE")
    if explicit in ("fields", "dsn"):
        return explicit

    saved_dsn = str(config.get("DB_DSN", "")).strip()
    if not saved_dsn:
        return "fields"

    user = urllib.parse.quote(str(config.get("DB_USER", "admin")), safe="")
    host = str(config.get("DB_HOST", "localhost"))
    port = str(config.get("DB_PORT", "5432"))
    dbname = urllib.parse.quote(str(config.get("DB_NAME", "daq_db")), safe="")

    pw_candidates = [
        str(config.get("DB_PASSWORD", "")),
        str(config.get("POSTGRES_PASSWORD", "")),
        "admin"
    ]
    for pw in pw_candidates:
        if not pw:
            continue
        p = urllib.parse.quote(pw, safe="")
        if saved_dsn == f"postgresql://{user}:{p}@{host}:{port}/{dbname}":
            return "fields"

    return "dsn"


def merge_preserved_secrets(new_config: Dict[str, Any], old_config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Merge new configuration with existing saved configuration.
    If a secret is omitted or contains the placeholder, preserve the old value.
    If explicitly replaced with a new value, use the new value.
    If explicitly marked with CLEAR_SECRET or cleared with CLEAR_<field>, clear it.
    """
    merged = dict(new_config)

    for key in SECRET_FIELDS:
        val = merged.get(key)
        old_val = old_config.get(key, "")
        if val == CLEAR_SECRET or (val == "" and merged.get(f"CLEAR_{key}") is True):
            merged[key] = ""
        elif val is None or val == "" or val == REDACTED_PLACEHOLDER:
            # Preserve old value if present
            if old_val:
                merged[key] = old_val
            elif key in merged and val == "":
                pass
        else:
            merged[key] = val

    # Synchronize legacy POSTGRES_PASSWORD with DB_PASSWORD if present
    if "DB_PASSWORD" in merged and "POSTGRES_PASSWORD" not in new_config:
        merged["POSTGRES_PASSWORD"] = merged["DB_PASSWORD"]
    elif "POSTGRES_PASSWORD" in merged and "DB_PASSWORD" not in new_config:
        merged["DB_PASSWORD"] = merged["POSTGRES_PASSWORD"]

    # Handle DB_DSN password preservation
    if "DB_DSN" in merged:
        new_dsn = merged["DB_DSN"]
        old_dsn = old_config.get("DB_DSN", "")
        if REDACTED_PLACEHOLDER in new_dsn:
            # Check for URL DSN password
            old_match = re.search(r":([^/@:\s]+)@", old_dsn)
            effective_pw = old_match.group(1) if old_match else old_config.get("DB_PASSWORD", "")
            if effective_pw:
                merged["DB_DSN"] = new_dsn.replace(f":{REDACTED_PLACEHOLDER}@", f":{effective_pw}@")
            # Check for keyword DSN password
            kw_match = re.search(r"password\s*=\s*([^\s]+)", old_dsn, flags=re.IGNORECASE)
            kw_pw = kw_match.group(1) if kw_match else old_config.get("DB_PASSWORD", "")
            if kw_pw:
                merged["DB_DSN"] = re.sub(
                    rf"(password\s*=\s*){re.escape(REDACTED_PLACEHOLDER)}",
                    rf"\g<1>{kw_pw}",
                    merged["DB_DSN"],
                    flags=re.IGNORECASE
                )

    return merged


def is_allowed_origin(origin_or_referer: Optional[str], allowed_origins: list) -> bool:
    """Validate request Origin or Referer against allowed origins."""
    if not origin_or_referer:
        return True  # Direct non-browser clients or same-origin without header
    clean = origin_or_referer.strip().rstrip("/")
    for allowed in allowed_origins:
        if allowed == "*":
            return True
        if clean == allowed.rstrip("/"):
            return True
    return False


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="DAQ Navi operator credential utility")
    parser.add_argument("--hash", help="Generate PBKDF2 hash for the given plain text password")
    parser.add_argument("--set-file", help="File path to save the hash to")
    parser.add_argument("--password", help="Password to hash and write when used with --set-file")
    args = parser.parse_args()

    if args.hash:
        print(hash_password(args.hash))
    elif args.set_file and args.password:
        h = hash_password(args.password)
        saved_to = save_operator_hash(h, args.set_file)
        print(f"Password hash saved to {saved_to}")
    else:
        parser.print_help()
