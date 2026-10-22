"""DingTalk CLI response parsing and QR presentation."""
import json
import logging
import re
from typing import Any, Dict, Optional, Tuple
logger = logging.getLogger(__name__)

_URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+")

_CODE_RES = (
    re.compile(r'user[_]?code["\s:=]+([A-Za-z0-9][A-Za-z0-9-]{3,15})', re.I),
    re.compile(r'(?:验证码|授权码|码)[：:\s]+([A-Za-z0-9][A-Za-z0-9-]{3,15})'),
)

def make_qr_data_uri(url: Optional[str]) -> Optional[str]:
    """Render the verification URL as an SVG QR-code data-URI (for scan-to-bind).
    segno is pure Python with zero extra dependencies; if the library is
    missing or url is empty, return None and the front end falls back to
    "click the link + copy the user code"."""
    if not url:
        return None
    try:
        import segno

        return segno.make(url, error="m").svg_data_uri(scale=5, border=2)
    except Exception as exc:  # noqa: BLE001
        logger.info("[dingtalk] qr render failed: %s", exc)
        return None

def parse_device_login_output(
    text: str,
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Extract (verification URL, user_code, complete URL) from the output of ``dws auth login --device``.

    dws device-code box format (``dfPrintDeviceCodeBox``), with Chinese labels verbatim:
        链接: <verificationUri>                    (link)
        授权码: <userCode>                          (user code)
        或者直接打开以下链接：                       (or open this link directly:)
          <verificationUriComplete>     ← code already embedded, one scan goes straight through
    The third item, ``verification_uri_complete``, is the QR-code target for
    **scan-to-bind** (preferred over the plain URL). Returns None for any
    item that cannot be extracted; the caller still returns the raw output
    to the front end as a fallback.
    """
    if not text:
        return None, None, None
    urls = [u.rstrip(".,;") for u in _URL_RE.findall(text)]
    url = urls[0] if urls else None
    code: Optional[str] = None
    for rx in _CODE_RES:
        m = rx.search(text)
        if m:
            code = m.group(1)
            break
    # Complete URL: prefer the one with the user_code embedded; otherwise take the second one (in the box, complete comes after plain)
    complete: Optional[str] = None
    if code:
        for u in urls:
            if code in u or "user_code=" in u or "userCode=" in u:
                complete = u
                break
    if complete is None and len(urls) >= 2:
        complete = urls[1]
    return url, code, complete

def parse_auth_status(stdout: str) -> bool:
    """``dws auth status -f json`` → whether authenticated. Any parse failure counts as not authenticated."""
    try:
        data = json.loads((stdout or "").strip() or "{}")
    except (ValueError, TypeError):
        return False
    return isinstance(data, dict) and bool(data.get("authenticated"))

def parse_get_self(stdout: str) -> Dict[str, Optional[str]]:
    """``dws contact user get-self -f json`` → {dingtalk_user_id, dingtalk_name, corp_id}。

    Best effort: extract what it can when the structure changes, leave None
    when unavailable, never raise.
    """
    out: Dict[str, Optional[str]] = {"dingtalk_user_id": None, "dingtalk_name": None, "corp_id": None}
    try:
        data = json.loads((stdout or "").strip() or "{}")
    except (ValueError, TypeError):
        return out

    def _walk(obj: Any) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                lk = k.lower()
                if out["dingtalk_user_id"] is None and lk in ("userid", "user_id", "unionid"):
                    if isinstance(v, (str, int)):
                        out["dingtalk_user_id"] = str(v)
                if out["dingtalk_name"] is None and lk in ("orgusername", "name", "nick", "username"):
                    if isinstance(v, str):
                        out["dingtalk_name"] = v
                if out["corp_id"] is None and lk in ("corpid", "corp_id"):
                    if isinstance(v, (str, int)):
                        out["corp_id"] = str(v)
                _walk(v)
        elif isinstance(obj, list):
            for it in obj:
                _walk(it)

    _walk(data)
    return out
