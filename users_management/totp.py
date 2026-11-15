"""RFC 6238 TOTP (2FA) & single-use recovery code helpers for Admin Panel."""
import base64
import hashlib
import hmac
import io
import secrets
import struct
import time
from urllib.parse import quote

import qrcode
import qrcode.image.svg

TOTP_PERIOD = 30
TOTP_DIGITS = 6
BASE32_ALPHABET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567'
RECOVERY_ALPHABET = '23456789ABCDEFGHJKLMNPQRSTUVWXYZ'


def generate_totp_secret(length=32):
    """Generate a cryptographically random Base32 TOTP secret (160 bits when length=32)."""
    return ''.join(secrets.choice(BASE32_ALPHABET) for _ in range(length))


def _normalize_secret(secret):
    cleaned = ''.join(str(secret or '').strip().upper().split())
    pad = (-len(cleaned)) % 8
    return base64.b32decode(cleaned + ('=' * pad), casefold=True)


def hotp_code(secret, counter, digits=TOTP_DIGITS):
    """Compute an RFC 4226 HOTP numeric string for a secret and counter."""
    key = _normalize_secret(secret)
    msg = struct.pack('>Q', int(counter))
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code_int = struct.unpack('>I', digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code_int % (10 ** digits)).zfill(digits)


def get_totp_code(secret, for_time=None, step=TOTP_PERIOD, digits=TOTP_DIGITS):
    """Compute the current RFC 6238 TOTP code for a given timestamp."""
    ts = int(time.time() if for_time is None else for_time)
    return hotp_code(secret, ts // step, digits=digits)


def verify_totp_code(secret, code, window=1, for_time=None):
    """Verify a 6-digit TOTP code allowing +/- `window` 30s steps."""
    if not secret:
        return False
    normalized = ''.join(ch for ch in str(code or '').strip() if ch.isdigit())
    if len(normalized) != TOTP_DIGITS:
        return False

    ts = int(time.time() if for_time is None else for_time)
    current_step = ts // TOTP_PERIOD
    try:
        for delta in range(-window, window + 1):
            expected = hotp_code(secret, current_step + delta)
            if hmac.compare_digest(expected, normalized):
                return True
    except Exception:
        return False
    return False


def build_otpauth_uri(secret, username, issuer='ENTN Admin'):
    """Build standard otpauth:// URI for Google Authenticator / Authy."""
    label = quote(f'{issuer}:{username}')
    issuer_param = quote(issuer)
    return f'otpauth://totp/{label}?secret={secret}&issuer={issuer_param}&algorithm=SHA1&digits={TOTP_DIGITS}&period={TOTP_PERIOD}'


def render_qr_svg_data_uri(uri):
    """Generate a self-contained SVG QR data URI offline without leaking secrets to external APIs."""
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=2,
    )
    qr.add_data(uri)
    qr.make(fit=True)
    img = qr.make_image(image_factory=qrcode.image.svg.SvgPathImage)
    buf = io.BytesIO()
    img.save(buf)
    svg_bytes = buf.getvalue()
    encoded = base64.b64encode(svg_bytes).decode('ascii')
    return f'data:image/svg+xml;base64,{encoded}'


def _normalize_recovery_input(raw_code):
    return ''.join(ch for ch in str(raw_code or '').upper() if ch.isalnum())


def hash_recovery_code(raw_code):
    """Hash a single-use recovery code with a random 16-byte salt."""
    salt = secrets.token_hex(16)
    norm = _normalize_recovery_input(raw_code)
    digest = hashlib.sha256(f'{salt}:{norm}'.encode('utf-8')).hexdigest()
    return f'{salt}${digest}'


def generate_backup_codes(count=8):
    """Return `(plaintext_codes, hashed_codes)` for one-time 2FA recovery."""
    plain = []
    hashed = []
    for _ in range(count):
        part1 = ''.join(secrets.choice(RECOVERY_ALPHABET) for _ in range(4))
        part2 = ''.join(secrets.choice(RECOVERY_ALPHABET) for _ in range(4))
        code = f'{part1}-{part2}'
        plain.append(code)
        hashed.append(hash_recovery_code(code))
    return plain, hashed


def verify_and_consume_backup_code(profile, submitted_code):
    """If submitted_code matches a stored backup hash on AdminProfile, consume and persist it."""
    norm = _normalize_recovery_input(submitted_code)
    if len(norm) < 6 or not isinstance(profile.totp_backup_codes, list):
        return False

    for idx, stored in enumerate(profile.totp_backup_codes):
        if not isinstance(stored, str) or '$' not in stored:
            continue
        salt, expected_digest = stored.split('$', 1)
        candidate = hashlib.sha256(f'{salt}:{norm}'.encode('utf-8')).hexdigest()
        if hmac.compare_digest(candidate, expected_digest):
            remaining = list(profile.totp_backup_codes)
            remaining.pop(idx)
            profile.totp_backup_codes = remaining
            profile.save(update_fields=['totp_backup_codes', 'updated_at'])
            return True
    return False

