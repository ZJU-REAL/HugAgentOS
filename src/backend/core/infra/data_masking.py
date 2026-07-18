"""Data masking utilities for sensitive information.

This module provides functions to mask sensitive data in logs, API responses,
and database queries to comply with privacy regulations and security best practices.
"""

import re
from typing import Any, Dict, List, Optional, Union


def mask_phone(phone: str) -> str:
    """
    Mask phone number, keeping first 3 and last 4 digits.

    Examples:
        13812345678 -> 138****5678
        +86 138 1234 5678 -> +86 138****5678
        1234567 -> 1234567 (too short, no masking)

    Args:
        phone: Phone number string

    Returns:
        Masked phone number
    """
    if not phone:
        return phone

    # Remove non-digit characters for processing
    digits = re.sub(r'\D', '', phone)

    if len(digits) < 7:
        # Too short, don't mask
        return phone

    if len(digits) == 11:
        # Standard Chinese mobile: 138****5678
        return f"{digits[:3]}****{digits[-4:]}"
    elif len(digits) >= 10:
        # International or landline: keep first 3 and last 4
        return f"{digits[:3]}****{digits[-4:]}"
    else:
        # 7-9 digits: keep first 2 and last 3
        return f"{digits[:2]}***{digits[-3:]}"


def mask_email(email: str) -> str:
    """
    Mask email address, keeping first 2 characters of local part and full domain.

    Examples:
        user@example.com -> us***@example.com
        admin@company.org -> ad***@company.org
        a@test.com -> a***@test.com

    Args:
        email: Email address string

    Returns:
        Masked email address
    """
    if not email or "@" not in email:
        return email

    try:
        local, domain = email.split("@", 1)

        if len(local) <= 1:
            # Single character local part
            return f"{local}***@{domain}"
        elif len(local) == 2:
            # Two character local part
            return f"{local[0]}***@{domain}"
        else:
            # Keep first 2 characters
            return f"{local[:2]}***@{domain}"
    except Exception:
        return email


def mask_id_card(id_card: str) -> str:
    """
    Mask ID card number (Chinese ID card: 18 digits).

    Examples:
        110101199001011234 -> 110101********1234
        123456789012345678 -> 123456********5678

    Args:
        id_card: ID card number

    Returns:
        Masked ID card number
    """
    if not id_card:
        return id_card

    # Remove spaces and hyphens
    id_card_clean = re.sub(r'[\s\-]', '', id_card)

    if len(id_card_clean) == 18:
        # Chinese ID: show first 6 and last 4
        return f"{id_card_clean[:6]}********{id_card_clean[-4:]}"
    elif len(id_card_clean) >= 10:
        # Other ID: show first 4 and last 4
        return f"{id_card_clean[:4]}****{id_card_clean[-4:]}"
    else:
        # Too short, mask middle
        return f"{id_card_clean[:2]}***{id_card_clean[-2:]}" if len(id_card_clean) >= 4 else id_card


def mask_bank_card(card_number: str) -> str:
    """
    Mask bank card number, showing only last 4 digits.

    Examples:
        6222021234567890 -> **** **** **** 7890
        1234567890123456 -> **** **** **** 3456

    Args:
        card_number: Bank card number

    Returns:
        Masked bank card number
    """
    if not card_number:
        return card_number

    # Remove spaces and hyphens
    card_clean = re.sub(r'[\s\-]', '', card_number)

    if len(card_clean) < 8:
        return card_number

    # Show only last 4 digits
    return f"**** **** **** {card_clean[-4:]}"


def mask_api_key(api_key: str, show_prefix: int = 4, show_suffix: int = 4) -> str:
    """
    Mask API key, showing only prefix and suffix.

    Examples:
        sk-1234567890abcdef -> sk-1234...cdef
        abcdefghijklmnop -> abcd...mnop

    Args:
        api_key: API key string
        show_prefix: Number of characters to show at start
        show_suffix: Number of characters to show at end

    Returns:
        Masked API key
    """
    if not api_key:
        return api_key

    if len(api_key) <= show_prefix + show_suffix:
        return "***"

    return f"{api_key[:show_prefix]}...{api_key[-show_suffix:]}"


def mask_password(password: str) -> str:
    """
    Completely mask password.

    Args:
        password: Password string

    Returns:
        Always returns "***"
    """
    return "***" if password else ""


def mask_token(token: str) -> str:
    """
    Mask authentication token.

    Args:
        token: Token string

    Returns:
        Masked token (shows first 8 chars)
    """
    if not token:
        return token

    if len(token) <= 8:
        return "***"

    return f"{token[:8]}..."


# Masking rules for different field types
MASKING_RULES = {
    "phone": mask_phone,
    "email": mask_email,
    "id_card": mask_id_card,
    "bank_card": mask_bank_card,
    "api_key": mask_api_key,
    "password": mask_password,
    "token": mask_token,
    "secret": mask_password,
    "access_key": mask_api_key,
    "secret_key": mask_password,
}


def mask_sensitive_data(
    data: Union[Dict, List, Any],
    rules: Optional[Dict[str, str]] = None,
    field_patterns: Optional[List[str]] = None
) -> Union[Dict, List, Any]:
    """
    Recursively mask sensitive data in dictionaries and lists.

    Args:
        data: Data to mask (dict, list, or primitive)
        rules: Custom field name to masking type mapping
            Example: {"user_phone": "phone", "user_email": "email"}
        field_patterns: List of field name patterns to mask with default rule
            Example: ["password", "token", "secret"]

    Returns:
        Data with sensitive fields masked

    Example:
        >>> data = {
        ...     "username": "john",
        ...     "phone": "13812345678",
        ...     "email": "john@example.com",
        ...     "password": "secret123"
        ... }
        >>> masked = mask_sensitive_data(data, field_patterns=["password"])
        >>> print(masked)
        {
            "username": "john",
            "phone": "138****5678",
            "email": "jo***@example.com",
            "password": "***"
        }
    """
    if rules is None:
        rules = {}

    if field_patterns is None:
        field_patterns = ["password", "token", "secret", "api_key", "access_key", "secret_key"]

    # Default field name to masking type mapping
    default_rules = {
        "phone": "phone",
        "mobile": "phone",
        "telephone": "phone",
        "email": "email",
        "mail": "email",
        "id_card": "id_card",
        "id_number": "id_card",
        "bank_card": "bank_card",
        "card_number": "bank_card",
        "api_key": "api_key",
        "password": "password",
        "token": "token",
        "secret": "secret",
        "access_key": "access_key",
        "secret_key": "secret_key",
    }

    # Merge with custom rules
    all_rules = {**default_rules, **rules}

    def _mask_value(key: str, value: Any) -> Any:
        """Mask a single value based on key."""
        if not isinstance(value, str):
            return value

        # Check exact match in rules
        key_lower = key.lower()
        if key_lower in all_rules:
            masking_type = all_rules[key_lower]
            if masking_type in MASKING_RULES:
                return MASKING_RULES[masking_type](value)

        # Check pattern match
        for pattern in field_patterns:
            if pattern.lower() in key_lower:
                return mask_password(value)

        return value

    def _mask_recursive(obj: Any) -> Any:
        """Recursively mask data structures."""
        if isinstance(obj, dict):
            return {
                k: _mask_recursive(_mask_value(k, v) if isinstance(v, str) else v)
                for k, v in obj.items()
            }
        elif isinstance(obj, list):
            return [_mask_recursive(item) for item in obj]
        else:
            return obj

    return _mask_recursive(data)


def mask_user_response(user_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Mask sensitive fields in user API response.

    Automatically masks common sensitive fields in user data.

    Args:
        user_data: User data dictionary

    Returns:
        User data with sensitive fields masked
    """
    return mask_sensitive_data(
        user_data,
        rules={
            "phone": "phone",
            "mobile": "phone",
            "email": "email",
            "id_card": "id_card",
            "id_number": "id_card",
        }
    )


def mask_log_data(log_data: Union[Dict, str]) -> Union[Dict, str]:
    """
    Mask sensitive data in log messages.

    Args:
        log_data: Log data (dict or string)

    Returns:
        Log data with sensitive information masked
    """
    if isinstance(log_data, dict):
        return mask_sensitive_data(log_data)
    elif isinstance(log_data, str):
        # Mask common patterns in strings
        patterns = [
            (re.compile(r'"(password|token|secret|api_key)"\s*:\s*"[^"]*"', re.IGNORECASE), r'"\1": "***"'),
            (re.compile(r'Bearer\s+[\w\-\.]+', re.IGNORECASE), 'Bearer ***'),
            (
                re.compile(r'(password|token|secret|api_key)\s*=\s*([^\s,;]+)', re.IGNORECASE),
                lambda m: f"{m.group(1)}=***"
            ),
            (re.compile(r'\b\d{11}\b'), lambda m: mask_phone(m.group(0))),  # Phone numbers
            (re.compile(r'\b[\w\.-]+@[\w\.-]+\.\w+\b'), lambda m: mask_email(m.group(0))),  # Email
        ]

        result = log_data
        for pattern, replacement in patterns:
            try:
                result = pattern.sub(replacement, result)
            except re.error:
                # Never let masking failures break request processing/logging.
                continue
        return result
    else:
        return log_data


# Export commonly used functions
__all__ = [
    "mask_phone",
    "mask_email",
    "mask_id_card",
    "mask_bank_card",
    "mask_api_key",
    "mask_password",
    "mask_token",
    "mask_sensitive_data",
    "mask_user_response",
    "mask_log_data",
]
