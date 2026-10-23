"""dingtalk history behavior."""
from __future__ import annotations
import json
import logging
import os
import shutil
import tempfile
from typing import Any, Dict, List, Optional
import httpx
from datetime import datetime, timezone
from integrations.channels.protocol import ChannelResourceError, HistoryItem, InboundMsg
from integrations.channels.adapters.dingtalk_settings import DINGTALK_API_BASE
logger = logging.getLogger(__name__)

class DingTalkHistory:
    async def fetch_history(
        self,
        conn: Any,
        conversation_id: str,
        *,
        since_ms: int,
        limit: int,
        newest_first: bool = False,
    ) -> List[HistoryItem]:
        """`dws chat message list --group <conv> --time <t>` → normalized history items."""
        conv = (conversation_id or "").strip()
        owner = getattr(conn, "owner_user_id", "") or ""
        if not conv or not owner:
            return []
        from integrations.dingtalk.service import _run_dws

        # dws takes a local-time string, not epoch ms. `--forward` picks the direction:
        # true = messages after --time (incremental from the cursor); false = messages
        # before --time, which anchored at *now* is how the cold start gets the most recent
        # ones instead of the oldest of a week-long window.
        anchor_ms = int(datetime.now(timezone.utc).timestamp() * 1000) if newest_first else since_ms
        anchor = datetime.fromtimestamp(max(anchor_ms, 0) / 1000, tz=timezone.utc).astimezone()
        args = [
            "chat", "message", "list",
            "--group", conv,
            "--time", anchor.strftime("%Y-%m-%d %H:%M:%S"),
            "--limit", str(max(1, min(int(limit or 50), 100))),
            "--forward=false" if newest_first else "--forward=true",
            "--format", "json",
        ]
        try:
            out, err, code = await _run_dws(owner, args, timeout=self._DWS_HISTORY_TIMEOUT_S)
        except Exception:  # noqa: BLE001
            logger.warning("[dingtalk] dws 群历史拉取异常 conv=%s", conv[:24], exc_info=True)
            return []
        if code != 0 or not out.strip():
            # Not logged in / no permission / timeout all land here. Debug, not warning:
            # a tenant without the DingTalk connection set up would otherwise log on every
            # single group message.
            logger.debug(
                "[dingtalk] dws 群历史拉取未成功 code=%s err=%s", code, (err or "")[:160]
            )
            return []
        try:
            return self._parse_dws_history(json.loads(out))
        except Exception:  # noqa: BLE001
            logger.debug("[dingtalk] dws 输出解析失败: %s", out[:200], exc_info=True)
            return []

    @staticmethod
    def _dws_business_error(raw_out: str) -> Optional[str]:
        """Pull the human-readable reason out of a dws error envelope, if that's what this is.

        Worth surfacing rather than collapsing to "download failed": the messages are
        specific and actionable ("this data belongs to organization X while the tool is
        configured for organization Y"), and reading that as an expiry sends the user
        looking in entirely the wrong place.
        """
        try:
            err = (json.loads(raw_out or "{}") or {}).get("error")
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(err, dict):
            return None
        msg = str(err.get("message") or "").strip()
        return msg or None

    async def _read_doc_via_dws(self, conn: Any, node_id: str) -> Optional[bytes]:
        """`dws doc read --node <id> --content-format markdown` → markdown bytes.

        A DingTalk online document has no file to download; it is read as content. Returning
        markdown keeps it in the same artifact pipeline as everything else.
        """
        owner = getattr(conn, "owner_user_id", "") or ""
        if not owner:
            return None
        from integrations.dingtalk.service import _run_dws

        out, err, code = await _run_dws(
            owner,
            ["doc", "read", "--node", node_id, "--content-format", "markdown", "--format", "json"],
            timeout=self._DWS_DOWNLOAD_TIMEOUT_S,
        )
        reason = self._dws_business_error(out) or self._dws_business_error(err)
        if reason:
            logger.warning("[dingtalk] 钉钉文档读取被拒 node=%s: %s", node_id[:16], reason[:200])
            raise ChannelResourceError(reason)
        if code != 0 or not out.strip():
            logger.debug("[dingtalk] 钉钉文档读取失败 code=%s err=%s", code, (err or "")[:160])
            return None
        try:
            payload = json.loads(out)
        except Exception:  # noqa: BLE001
            return out.encode("utf-8")
        # Content sits under a few possible keys depending on CLI version; fall back to the
        # whole payload rather than silently returning nothing.
        for key in ("content", "markdown", "data", "result"):
            val = payload.get(key) if isinstance(payload, dict) else None
            if isinstance(val, str) and val.strip():
                return val.encode("utf-8")
            if isinstance(val, dict):
                for sub in ("content", "markdown", "text"):
                    inner = val.get(sub)
                    if isinstance(inner, str) and inner.strip():
                        return inner.encode("utf-8")
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")

    async def _download_via_dws(self, conn: Any, file_id: str) -> Optional[bytes]:
        """`dws drive download --node <fileId>` → bytes.

        The CLI only writes to a local path (it does the signed-URL dance internally), so the
        bytes are read back from a temp file that is always removed, including on failure —
        this runs on the backend host, not in a disposable sandbox.
        """
        owner = getattr(conn, "owner_user_id", "") or ""
        if not owner:
            return None
        from integrations.dingtalk.service import _run_dws

        tmpdir = tempfile.mkdtemp(prefix="dws_dl_")
        target = os.path.join(tmpdir, "download.bin")
        try:
            out, err, code = await _run_dws(
                owner,
                ["drive", "download", "--node", file_id, "--output", target, "--format", "json"],
                timeout=self._DWS_DOWNLOAD_TIMEOUT_S,
            )
            if code != 0 or not os.path.exists(target):
                logger.warning(
                    "[dingtalk] dws 文件下载失败 file_id=%s code=%s err=%s",
                    file_id[:16], code, (err or out or "")[:160],
                )
                return None
            with open(target, "rb") as fh:
                return fh.read()
        except Exception:  # noqa: BLE001
            logger.warning("[dingtalk] dws 文件下载异常 file_id=%s", file_id[:16], exc_info=True)
            return None
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    @staticmethod
    def _dws_ts_ms(value: Any) -> int:
        """dws ``createTime`` → epoch ms.

        The CLI returns a **local-time string** (``"2026-08-08 21:57:33"``), not epoch
        milliseconds. Parsing it as an int silently yielded 0 for every message, which left
        the pull cursor pinned at the cold-start window — so every @ re-fetched the same 24
        hours forever. Numeric forms are still accepted in case a CLI version returns them.
        """
        if isinstance(value, (int, float)) and value > 0:
            return int(value)
        text = str(value or "").strip()
        if not text:
            return 0
        if text.isdigit():
            return int(text)
        try:
            naive = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return 0
        # Local time, as the CLI prints it; astimezone() attaches the process zone.
        return int(naive.astimezone().timestamp() * 1000)

    @classmethod
    def _extract_dws_docs(cls, content: str) -> List[Dict[str, Any]]:
        """DingTalk Docs link cards → attachment refs (deduped by node id)."""
        nodes = {m.group("node") for m in cls._DWS_DOC_RE.finditer(content or "")}
        if not nodes:
            return []
        # The card's first non-empty line is the document title; skip the markdown image line
        # and the "创建者：" byline, neither of which names the document.
        title = ""
        for line in (content or "").splitlines():
            line = line.strip()
            if line and not line.startswith("![") and not line.startswith("[http"):
                if line.startswith("创建者"):
                    continue
                title = line
                break
        return [
            {"kind": "doc", "key": node, "name": title or "钉钉文档"}
            for node in sorted(nodes)
        ]

    @classmethod
    def _extract_dws_files(cls, content: str) -> tuple:
        """(cleaned_text, attachment_refs) for one history message body."""
        refs: List[Dict[str, Any]] = []
        for m in cls._DWS_FILE_RE.finditer(content or ""):
            refs.append({
                "kind": "image" if m.group("kind") == "图片" else "file",
                "key": m.group("fid"),
                "name": m.group("name").strip(),
            })
        doc_refs = cls._extract_dws_docs(content)
        if doc_refs:
            # The card body is title + icon + a long URL repeated twice; the attachment entry
            # carries title and handle, so keeping the raw card would just burn prompt space.
            return "", refs + doc_refs
        if not refs:
            return (content or "").strip(), []
        # Drop the matched sentences: the attachment ref already renders name + handle, and
        # the "use dws drive download" tail is instruction noise aimed at a CLI user.
        return cls._DWS_FILE_RE.sub("", content or "").strip(), refs

    @staticmethod
    def _parse_dws_history(body: Dict[str, Any]) -> List[HistoryItem]:
        """dws JSON output → HistoryItem list.

        Field names verified against live CLI output — they are **not** the ones the bot
        callback uses, which is the trap here:
          {"result": {"messages": [{"openMessageId", "sender", "content", "createTime",
                                    "openConversationId", "senderOpenDingTalkId"}], ...}}
        A bare ``{"messages": [...]}`` is accepted too, so a CLI version that drops the
        envelope does not silently yield nothing.
        """
        # Preserve the concrete adapter binding used by history parsing.
        from integrations.channels.adapters.dingtalk import DingTalkAdapter

        if not isinstance(body, dict):
            return []
        if body.get("success") is False or body.get("errorCode"):
            return []
        result = body.get("result") if isinstance(body.get("result"), dict) else body
        out: List[HistoryItem] = []
        for m in result.get("messages") or []:
            if not isinstance(m, dict):
                continue
            # Written out rather than chained: `a or b if cond else ""` binds the conditional
            # looser than the `or`, which would silently make every message look empty.
            text = m.get("content") or ""
            if not text and isinstance(m.get("text"), dict):
                text = m["text"].get("content") or ""
            if not isinstance(text, str):
                text = str(text)
            text, refs = DingTalkAdapter._extract_dws_files(text)
            if not text and not refs:
                continue
            out.append(HistoryItem(
                # openMessageId is the real id; msgId/messageId kept as fallbacks only.
                message_id=str(
                    m.get("openMessageId") or m.get("msgId") or m.get("messageId") or ""
                ),
                # `sender` is already the display name — no directory lookup needed.
                sender_name=str(
                    m.get("sender") or m.get("senderNick") or m.get("senderName") or "群成员"
                ),
                text=text,
                ts_ms=DingTalkAdapter._dws_ts_ms(m.get("createTime") or m.get("createAt")),
                attachments=refs,
                # Marks which download route this file needs: history files live on DingTalk
                # Drive and are fetched by fileId, not by the bot callback's downloadCode.
                raw={"dws_drive": True} if refs else {},
            ))
        return out

    @staticmethod
    def _extract_attachments(msgtype: str, payload: Dict[str, Any]) -> list:
        """Message → attachment list. ``key`` carries DingTalk's ``downloadCode``.

        Content layout per DingTalk's "received message types" reference:
          picture  → {pictureDownloadCode, downloadCode}
          file     → {spaceId, fileName, downloadCode, fileId}
          audio    → {downloadCode, recognition}
          video    → {duration, videoType, downloadCode}
          richText → {richText: [ ...nodes, image nodes carry downloadCode... ]}
        Everything is read defensively (``downloadCode`` looked up in more than one place)
        because these payloads are only loosely specified and vary by client version — a
        missing field must degrade to "no attachment", never raise and kill the connection.
        """
        content = payload.get("content") if isinstance(payload.get("content"), dict) else {}
        out: list = []

        def _add(kind: str, code: Any, name: str) -> None:
            if code:
                out.append({"kind": kind, "key": str(code), "name": name})

        if msgtype == "picture":
            code = content.get("downloadCode") or content.get("pictureDownloadCode")
            _add("image", code, f"dingtalk_image_{payload.get('msgId', '')[:8] or 'x'}.png")
        elif msgtype == "file":
            _add("file", content.get("downloadCode"), content.get("fileName") or "file.bin")
        elif msgtype == "audio":
            _add("file", content.get("downloadCode"), "dingtalk_audio.amr")
        elif msgtype == "video":
            _add("file", content.get("downloadCode"), "dingtalk_video.mp4")
        elif msgtype == "richText":
            for i, node in enumerate(content.get("richText") or []):
                if not isinstance(node, dict):
                    continue
                code = node.get("downloadCode") or node.get("pictureDownloadCode")
                _add("image", code, f"dingtalk_richtext_{i}.png")
        return out

    async def download_resource(
        self, conn: Any, inbound: InboundMsg, attachment: Dict[str, Any]
    ) -> Optional[bytes]:
        """Download an inbound attachment: downloadCode → temporary downloadUrl → bytes.

        Two hops, per DingTalk's design:
          POST /v1.0/robot/messageFiles/download {downloadCode, robotCode} → {downloadUrl}
          GET  downloadUrl → bytes
        The temporary URL serves the file with a ``.file`` extension; we ignore that and keep
        the filename taken from the message, so the artifact keeps its real extension.
        Download codes expire, so a deferred fetch can legitimately fail — returning None
        (rather than raising) lets the caller degrade to "attachment unavailable".
        """
        code = attachment.get("key")
        if not code:
            return None
        # Files seen through *history* are DingTalk Drive entries addressed by fileId, not
        # bot-callback attachments addressed by a short-lived downloadCode — different
        # storage, different API. The marker is set when the history item is parsed.
        if attachment.get("kind") == "doc":
            return await self._read_doc_via_dws(conn, str(code))
        if (inbound.raw or {}).get("dws_drive"):
            return await self._download_via_dws(conn, str(code))
        robot_code = (inbound.raw or {}).get("dingtalk_robot_code") or ""
        if not robot_code:
            logger.warning("[dingtalk] 缺少 robotCode，无法下载附件 code=%s", str(code)[:12])
            return None
        try:
            token = await self._access_token(conn.app_id, self._app_secret(conn))
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"{DINGTALK_API_BASE}/v1.0/robot/messageFiles/download",
                    headers={"x-acs-dingtalk-access-token": token},
                    json={"downloadCode": code, "robotCode": robot_code},
                )
                if resp.status_code != 200:
                    logger.warning(
                        "[dingtalk] 下载地址获取失败 status=%s body=%s",
                        resp.status_code, resp.text[:200],
                    )
                    return None
                url = (resp.json() or {}).get("downloadUrl")
                if not url:
                    return None
                fr = await client.get(url)
            if fr.status_code == 200:
                return fr.content
            logger.warning("[dingtalk] 附件下载失败 status=%s", fr.status_code)
        except Exception:  # noqa: BLE001
            logger.exception("[dingtalk] 附件下载异常 code=%s", str(code)[:12])
        return None
