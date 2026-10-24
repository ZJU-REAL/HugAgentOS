"""Settings for the external file-parsing service."""
from __future__ import annotations
import os

def svc_get(key: str, default: str = "") -> str:
    """Read from SystemConfigService (DB-first), fall back to env."""
    try:
        from core.config.store.system_config import SystemConfigService
        val = SystemConfigService.get_instance().get(key)
        if val is not None:
            return val.strip()
    except Exception as fallback_error:
        from core.infra.failures import record_best_effort_failure
        record_best_effort_failure('core.files.content.parser_config:svc_get', fallback_error)
    return (os.getenv(ENV_MAP.get(key, ""), default) or default).strip()


def cfg_api_url() -> str:
    return svc_get("file_parser.api_url")


def cfg_timeout() -> int:
    try:
        return int(svc_get("file_parser.timeout", "60"))
    except ValueError:
        return 60


def cfg_parse_params() -> dict:
    return {
        "lang_list": svc_get("file_parser.lang_list", "ch"),
        "backend": svc_get("file_parser.backend", "pipeline"),
        "parse_method": svc_get("file_parser.parse_method", "auto"),
        "formula_enable": svc_get("file_parser.formula_enable", "true"),
        "table_enable": svc_get("file_parser.table_enable", "true"),
    }


ENV_MAP = {
    "file_parser.api_url": "FILE_PARSER_API_URL",
    "file_parser.timeout": "FILE_PARSER_TIMEOUT",
    "file_parser.lang_list": "FILE_PARSER_LANG_LIST",
    "file_parser.backend": "FILE_PARSER_BACKEND",
    "file_parser.parse_method": "FILE_PARSER_PARSE_METHOD",
    "file_parser.formula_enable": "FILE_PARSER_FORMULA_ENABLE",
    "file_parser.table_enable": "FILE_PARSER_TABLE_ENABLE",
}
