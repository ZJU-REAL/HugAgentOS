"""Effective management fields for paired gateway and device observations.

Join once on the authenticated account/device/object identity. SQL filters and
aggregates use the same field precedence as the serialized management record.
"""

from features.desktop.models import DesktopRecord
from sqlalchemy import case, func
from sqlalchemy.orm import aliased

desktop_counterpart = aliased(DesktopRecord, name="desktop_counterpart")
gateway_counterpart = aliased(DesktopRecord, name="gateway_counterpart")


def counterpart_identity(counterpart, observation):
    return (
        (counterpart.user_id == DesktopRecord.user_id)
        & (counterpart.device_id == DesktopRecord.device_id)
        & (counterpart.kind == DesktopRecord.kind)
        & (counterpart.object_id == DesktopRecord.object_id)
        & (counterpart.observation == observation)
    )


def payload_value(field, nested=None):
    own = DesktopRecord.payload[field]
    local = desktop_counterpart.payload[field]
    if nested:
        own, local = own[nested], local[nested]
    value = func.coalesce(own.as_string(), local.as_string())
    if field == "usage":
        # Unknown gateway usage must not replace the device's measured usage,
        # including when the gateway explicitly reports zero token counts.
        use_local = ~func.coalesce(
            DesktopRecord.payload["usage_known"].as_boolean(), False
        ) & desktop_counterpart.payload["usage"].as_string().isnot(None)
        value = case(
            (use_local, local.as_string()),
            (DesktopRecord.payload["usage"].as_string().isnot(None), own.as_string()),
            else_=local.as_string(),
        )
    return value
