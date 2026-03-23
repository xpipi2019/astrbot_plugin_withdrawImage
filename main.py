"""群聊中按屏蔽规则自动撤回图片或 QQ 表情（Face），仅适用于 OneBot v11（aiocqhttp）。

屏蔽列表使用 SQLite 持久化，按群号分表维护，存储于 AstrBot 数据目录下的 plugin_data。
"""

from __future__ import annotations

import asyncio
import base64
import io
import os
import re
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Face, Image, Plain, Reply
from astrbot.api.star import Context, Star, StarTools

try:
    from PIL import Image as PILImage
except Exception:
    PILImage = None


class WithdrawImagePlugin(Star):
    """群聊图片 / QQ 表情屏蔽并自动撤回（OneBot v11）。

    仅在群聊中生效。每个群有独立的屏蔽列表（SQLite 按 group_id 区分）。

    指令（需 AstrBot 管理员，且仅在群内）：/imgblk …
    - face <id>：按 QQ 表情 ID 屏蔽（与消息段 Face 的 id 一致）
    - emoji <字符>：按 Emoji 字符屏蔽（如 😀）
    - img：引用回复一条带图的消息并发送 /imgblk img，从该图中自动写入规则
    - list：列出本群规则
    - del <序号>：按 list 序号删除
    - clear：清空本群列表

    协议端需支持 delete_msg，且机器人在群内通常需有撤回他人消息权限。
    """

    _DB_NAME = "withdraw_blocklist.db"
    _LIST_PAGE_SIZE = 10
    _LIST_ARG_RE = re.compile(
        r"^[/\s#＃!]*imgblk\s+list\s*(.*)$", re.DOTALL | re.IGNORECASE
    )
    _IMG_ARG_RE = re.compile(
        r"^[/\s#＃!]*imgblk\s+img\s*(.*)$", re.DOTALL | re.IGNORECASE
    )
    _EMOJI_ARG_RE = re.compile(
        r"^[/\s#＃!]*imgblk\s+emoji\s*(.*)$", re.DOTALL | re.IGNORECASE
    )
    _MIN_IMAGE_PATTERN_LEN = 3
    _IMG_ASSET_DIR = "IMG_ASSET"
    _MAX_PREVIEW_SIDE = 199
    _MAX_IMAGE_DOWNLOAD_BYTES = 5 * 1024 * 1024

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self._db_lock = asyncio.Lock()
        self._db_path: str | None = None
        self._asset_dir_path: str | None = None
        _ = config
        self._group_rule_cache: dict[str, tuple[list[int], list[str], list[str]]] = {}

    @staticmethod
    def _extract_subcommand_arg(raw: str, pattern: re.Pattern[str]) -> str:
        """从完整消息中提取子命令参数；若未匹配到命令前缀，则回退为原文本。"""
        text = (raw or "").strip()
        m = pattern.match(text)
        if m:
            return m.group(1).strip()
        return text

    async def _ensure_cmd_access(
        self, event: AstrMessageEvent
    ) -> tuple[str | None, str | None]:
        """统一命令前置校验：权限 + 群聊。成功返回 (gid, None)。"""
        ok, reason = await self._ensure_group_admin_or_owner(event)
        if not ok:
            return None, reason
        gid = event.get_group_id()
        if not gid:
            return None, "此指令仅可在群聊中使用。"
        return gid, None

    async def _add_image_rules_from_reply(
        self, event: AstrMessageEvent, gid: str
    ) -> str:
        """处理 /imgblk img 的引用图片添加分支。"""
        reply_seg: Reply | None = None
        for comp in event.get_messages():
            if isinstance(comp, Reply):
                reply_seg = comp
                break
        if reply_seg is None:
            return (
                "用法：\n"
                "引用回复一条带图的消息，再发送 /imgblk img（可不带其它文字），从该图自动添加规则。"
            )

        images = await self._resolve_images_from_reply(event, reply_seg)
        if not images:
            return "未在引用消息中解析到图片。请引用包含图片的消息；若仍失败，请确认协议端支持 get_msg。"

        image_pattern_pairs = [
            (im, p) for im in images if (p := self._best_pattern_from_image(im))
        ]
        if not image_pattern_pairs:
            return "无法从图片中提取 file/url/file_unique 标识。"

        added_n = 0
        dup_n = 0
        preview_ok = 0
        setter_id = str(event.get_sender_id() or "unknown")
        for im, p in image_pattern_pairs:
            rule = self._normalize_image_rule(p)
            if len(rule) < self._MIN_IMAGE_PATTERN_LEN:
                continue
            saved = await self._save_image_asset(gid, setter_id, im)
            if saved:
                new_path, origin_name = saved
                inserted, old_path = await self._upsert_image_asset_for_rule(
                    gid, rule, new_path, origin_name, setter_id
                )
                if inserted:
                    added_n += 1
                else:
                    dup_n += 1
                if old_path and old_path != new_path:
                    await self._delete_local_file(old_path)
                preview_ok += 1
                continue
            if await self._add_rule(gid, "image", rule):
                added_n += 1
            else:
                dup_n += 1
        self._invalidate_group_cache(gid)
        n = len(await self._list_rules(gid))
        if added_n and dup_n:
            return (
                f"本群已从引用消息添加 {added_n} 条规则，{dup_n} 条已存在；"
                f"已保存 {preview_ok} 张本地预览图；当前共 {n} 条。"
            )
        if added_n:
            return (
                f"本群已从引用消息添加 {added_n} 条规则；"
                f"已保存 {preview_ok} 张本地预览图；当前共 {n} 条。"
            )
        return f"引用中的图片规则均已存在；已更新/保存 {preview_ok} 张本地预览图；当前共 {n} 条。"

    async def initialize(self) -> None:
        data_dir = StarTools.get_data_dir()
        os.makedirs(data_dir, exist_ok=True)
        self._db_path = str(data_dir / self._DB_NAME)
        asset_dir = data_dir / self._IMG_ASSET_DIR
        os.makedirs(asset_dir, exist_ok=True)
        self._asset_dir_path = str(asset_dir)
        await self._run_db_write(self._init_schema_sync)
        self._group_rule_cache.clear()
        logger.info("withdraw_image: SQLite 已就绪: %s", self._db_path)
        if PILImage is None:
            logger.warning(
                "withdraw_image: 未安装 Pillow，引用图片时将无法生成本地预览图。"
            )

    async def terminate(self) -> None:
        self._group_rule_cache.clear()
        self._db_path = None
        self._asset_dir_path = None

    async def _run_db_read(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        """读操作：不加全局锁，提升并发吞吐。"""
        path = self._db_path
        if not path:
            raise RuntimeError("withdraw_image: 数据库路径未初始化")

        async def _run_once() -> Any:
            def _work() -> Any:
                conn = sqlite3.connect(path, timeout=5.0)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA busy_timeout = 5000")
                conn.execute("PRAGMA foreign_keys = ON")
                try:
                    return fn(conn)
                finally:
                    conn.close()

            return await asyncio.to_thread(_work)

        retries = 3
        for i in range(retries + 1):
            try:
                return await _run_once()
            except sqlite3.OperationalError as e:
                if "locked" in str(e).lower() and i < retries:
                    await asyncio.sleep(0.05 * (i + 1))
                    continue
                raise

    async def _run_db_write(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        """写操作：串行化，避免 SQLite 写竞争。"""
        async with self._db_lock:
            path = self._db_path
            if not path:
                raise RuntimeError("withdraw_image: 数据库路径未初始化")

            async def _run_once() -> Any:
                def _work() -> Any:
                    conn = sqlite3.connect(path, timeout=5.0)
                    conn.row_factory = sqlite3.Row
                    conn.execute("PRAGMA busy_timeout = 5000")
                    conn.execute("PRAGMA journal_mode = WAL")
                    conn.execute("PRAGMA foreign_keys = ON")
                    try:
                        return fn(conn)
                    finally:
                        conn.close()

                return await asyncio.to_thread(_work)

            retries = 3
            for i in range(retries + 1):
                try:
                    return await _run_once()
                except sqlite3.OperationalError as e:
                    if "locked" in str(e).lower() and i < retries:
                        await asyncio.sleep(0.05 * (i + 1))
                        continue
                    raise

    def _init_schema_sync(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS block_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                value TEXT NOT NULL,
                UNIQUE(group_id, kind, value)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_block_rules_group ON block_rules(group_id)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rule_assets (
                rule_id INTEGER PRIMARY KEY,
                group_id TEXT NOT NULL,
                local_path TEXT NOT NULL,
                origin_name TEXT NOT NULL,
                created_by TEXT,
                FOREIGN KEY(rule_id) REFERENCES block_rules(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_rule_assets_group ON rule_assets(group_id)"
        )
        conn.commit()

    async def _list_rules(self, group_id: str) -> list[dict[str, Any]]:
        def _q(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            cur = conn.execute(
                "SELECT id, kind, value FROM block_rules WHERE group_id = ? ORDER BY id ASC",
                (group_id,),
            )
            rows = cur.fetchall()
            return [
                {"id": r["id"], "kind": r["kind"], "value": r["value"]} for r in rows
            ]

        return await self._run_db_read(_q)

    async def _add_rule(self, group_id: str, kind: str, value: str) -> bool:
        """返回 True 表示新插入一行，False 表示已存在（UNIQUE）。"""

        def _ins(conn: sqlite3.Connection) -> bool:
            cur = conn.execute(
                "INSERT OR IGNORE INTO block_rules (group_id, kind, value) VALUES (?, ?, ?)",
                (group_id, kind, value),
            )
            conn.commit()
            return cur.rowcount > 0

        return await self._run_db_write(_ins)

    async def _upsert_image_asset_for_rule(
        self,
        group_id: str,
        value: str,
        local_path: str,
        origin_name: str,
        created_by: str,
    ) -> tuple[bool, str | None]:
        """绑定 image 规则到预览图，返回 (是否新规则, 旧文件路径)。"""

        def _upsert(conn: sqlite3.Connection) -> tuple[bool, str | None]:
            ins = conn.execute(
                "INSERT OR IGNORE INTO block_rules (group_id, kind, value) VALUES (?, ?, ?)",
                (group_id, "image", value),
            )
            cur = conn.execute(
                "SELECT id FROM block_rules WHERE group_id = ? AND kind = 'image' AND value = ?",
                (group_id, value),
            )
            row = cur.fetchone()
            if not row:
                conn.rollback()
                raise RuntimeError("withdraw_image: 无法获取 image rule_id")
            rule_id = int(row["id"])
            old_cur = conn.execute(
                "SELECT local_path FROM rule_assets WHERE rule_id = ?", (rule_id,)
            )
            old_row = old_cur.fetchone()
            old_path = str(old_row["local_path"]) if old_row else None
            conn.execute(
                """
                INSERT INTO rule_assets (rule_id, group_id, local_path, origin_name, created_by)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(rule_id) DO UPDATE SET
                    group_id=excluded.group_id,
                    local_path=excluded.local_path,
                    origin_name=excluded.origin_name,
                    created_by=excluded.created_by
                """,
                (rule_id, group_id, local_path, origin_name, created_by),
            )
            conn.commit()
            return ins.rowcount > 0, old_path

        return await self._run_db_write(_upsert)

    async def _list_rule_assets(self, group_id: str) -> dict[int, str]:
        def _q(conn: sqlite3.Connection) -> dict[int, str]:
            cur = conn.execute(
                "SELECT rule_id, local_path FROM rule_assets WHERE group_id = ?",
                (group_id,),
            )
            rows = cur.fetchall()
            out: dict[int, str] = {}
            for r in rows:
                try:
                    out[int(r["rule_id"])] = str(r["local_path"])
                except Exception:
                    continue
            return out

        return await self._run_db_read(_q)

    async def _delete_rule_by_index(
        self, group_id: str, index_1: int
    ) -> dict[str, Any] | None:
        """按当前 list 序号删除一条；无效序号返回 None。"""

        def _del(conn: sqlite3.Connection) -> dict[str, Any] | None:
            cur = conn.execute(
                "SELECT id, kind, value FROM block_rules WHERE group_id = ? ORDER BY id ASC",
                (group_id,),
            )
            rows = cur.fetchall()
            if index_1 < 1 or index_1 > len(rows):
                return None
            row = rows[index_1 - 1]
            target = int(row["id"])
            asset_cur = conn.execute(
                "SELECT local_path FROM rule_assets WHERE rule_id = ?", (target,)
            )
            asset_row = asset_cur.fetchone()
            local_path = str(asset_row["local_path"]) if asset_row else None
            cur = conn.execute(
                "DELETE FROM block_rules WHERE id = ? AND group_id = ?",
                (target, group_id),
            )
            conn.commit()
            if cur.rowcount:
                return {
                    "id": target,
                    "kind": str(row["kind"]),
                    "value": str(row["value"]),
                    "local_path": local_path,
                }
            return None

        return await self._run_db_write(_del)

    async def _clear_group(self, group_id: str) -> list[str]:
        def _clr(conn: sqlite3.Connection) -> list[str]:
            cur = conn.execute(
                "SELECT local_path FROM rule_assets WHERE group_id = ?", (group_id,)
            )
            paths = [str(r["local_path"]) for r in cur.fetchall()]
            conn.execute("DELETE FROM block_rules WHERE group_id = ?", (group_id,))
            conn.commit()
            return paths

        return await self._run_db_write(_clr)

    @staticmethod
    def _split_rules(entries: list[dict[str, Any]]):
        faces: list[int] = []
        images: list[str] = []
        emojis: list[str] = []
        for e in entries:
            if not isinstance(e, dict):
                continue
            kind = str(e.get("kind", "")).lower()
            val = e.get("value")
            if kind == "face" and val is not None:
                try:
                    faces.append(int(val))
                except (TypeError, ValueError):
                    continue
            elif kind == "image" and isinstance(val, str) and val.strip():
                images.append(val.strip())
            elif kind == "emoji" and isinstance(val, str) and val.strip():
                emojis.append(val.strip())
        return faces, images, emojis

    @staticmethod
    def _normalize_patterns(patterns: list[str]) -> list[str]:
        return [p.lower().strip() for p in patterns if p and p.strip()]

    async def _get_group_rules_cached(
        self, gid: str
    ) -> tuple[list[int], list[str], list[str]]:
        cached = self._group_rule_cache.get(gid)
        if cached is not None:
            return cached
        entries = await self._list_rules(gid)
        face_ids, image_patterns, emoji_patterns = self._split_rules(entries)
        normalized = self._normalize_patterns(image_patterns)
        cached = (
            face_ids,
            normalized,
            [e.strip() for e in emoji_patterns if e.strip()],
        )
        self._group_rule_cache[gid] = cached
        return cached

    def _invalidate_group_cache(self, gid: str) -> None:
        self._group_rule_cache.pop(gid, None)

    @staticmethod
    def _normalize_image_rule(value: str) -> str:
        """图片规则规范化（去首尾空白并转小写），用于去重与匹配一致。"""
        return (value or "").strip().lower()

    @staticmethod
    def _safe_filename(name: str) -> str:
        cleaned = re.sub(r'[\\/:*?"<>|\r\n]+', "_", (name or "").strip())
        cleaned = cleaned.strip(" .")
        return cleaned or "image"

    @staticmethod
    def _file_name_from_image(img: Image) -> str:
        file_name = str(getattr(img, "file", "") or "").strip()
        if file_name and not file_name.startswith("base64://"):
            try:
                parsed = urlparse(file_name)
                if parsed.scheme in ("http", "https", "file"):
                    base = os.path.basename(parsed.path)
                else:
                    base = os.path.basename(file_name)
            except Exception:
                base = os.path.basename(file_name)
            if base:
                return base
        url = str(getattr(img, "url", "") or "").strip()
        if url:
            try:
                base = os.path.basename(urlparse(url).path)
                if base:
                    return base
            except Exception:
                pass
        file_unique = str(getattr(img, "file_unique", "") or "").strip()
        if file_unique:
            return f"{file_unique}.jpg"
        return "image.jpg"

    async def _download_image_bytes(self, img: Image) -> bytes | None:
        file_field = str(getattr(img, "file", "") or "").strip()
        if file_field.startswith("base64://"):
            try:
                data = base64.b64decode(file_field[len("base64://") :], validate=False)
                if len(data) > self._MAX_IMAGE_DOWNLOAD_BYTES:
                    logger.warning(
                        "withdraw_image: base64 图片过大，已跳过 bytes=%s limit=%s",
                        len(data),
                        self._MAX_IMAGE_DOWNLOAD_BYTES,
                    )
                    return None
                return data
            except Exception:
                return None

        url = str(getattr(img, "url", "") or "").strip()
        if not url and file_field.startswith(("http://", "https://")):
            url = file_field
        if not url:
            return None

        def _fetch() -> bytes | None:
            req = Request(
                url=url,
                headers={"User-Agent": "astrbot-plugin-withdraw-image/1.0"},
            )
            with urlopen(req, timeout=12) as resp:
                cl = resp.headers.get("Content-Length")
                if cl:
                    try:
                        cl_val = int(cl)
                        if cl_val > self._MAX_IMAGE_DOWNLOAD_BYTES:
                            logger.warning(
                                "withdraw_image: 图片 content-length 超限 url=%s bytes=%s limit=%s",
                                url,
                                cl_val,
                                self._MAX_IMAGE_DOWNLOAD_BYTES,
                            )
                            return None
                    except (TypeError, ValueError):
                        pass
                chunks: list[bytes] = []
                total = 0
                chunk_size = 64 * 1024
                while True:
                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > self._MAX_IMAGE_DOWNLOAD_BYTES:
                        logger.warning(
                            "withdraw_image: 下载图片超限中止 url=%s bytes>%s",
                            url,
                            self._MAX_IMAGE_DOWNLOAD_BYTES,
                        )
                        return None
                    chunks.append(chunk)
                return b"".join(chunks)

        try:
            return await asyncio.to_thread(_fetch)
        except Exception as e:
            logger.warning("withdraw_image: 下载图片失败 url=%s err=%s", url, e)
            return None

    async def _resample_preview_image(self, raw: bytes) -> tuple[bytes, str] | None:
        pil_mod = PILImage
        if not raw or pil_mod is None:
            return None

        def _work() -> tuple[bytes, str] | None:
            with pil_mod.open(io.BytesIO(raw)) as im:
                im.load()
                w, h = im.size
                if w <= 0 or h <= 0:
                    return None
                ratio = min(self._MAX_PREVIEW_SIDE / w, self._MAX_PREVIEW_SIDE / h, 1.0)
                new_size = (max(1, int(w * ratio)), max(1, int(h * ratio)))
                if new_size != (w, h):
                    resampling = getattr(pil_mod, "Resampling", None)
                    lanczos = getattr(
                        resampling, "LANCZOS", getattr(pil_mod, "LANCZOS", 1)
                    )
                    im = im.resize(new_size, lanczos)
                has_alpha = im.mode in ("RGBA", "LA") or (
                    im.mode == "P" and "transparency" in im.info
                )
                buf = io.BytesIO()
                if has_alpha:
                    im.save(buf, format="PNG", optimize=True)
                    return buf.getvalue(), ".png"
                if im.mode not in ("RGB", "L"):
                    im = im.convert("RGB")
                im.save(buf, format="JPEG", quality=88, optimize=True)
                return buf.getvalue(), ".jpg"

        try:
            return await asyncio.to_thread(_work)
        except Exception as e:
            logger.warning("withdraw_image: 重采样失败: %s", e)
            return None

    async def _save_image_asset(
        self, gid: str, setter_id: str, image: Image
    ) -> tuple[str, str] | None:
        asset_dir = self._asset_dir_path
        if not asset_dir:
            return None
        raw = await self._download_image_bytes(image)
        if not raw:
            return None
        sampled = await self._resample_preview_image(raw)
        if not sampled:
            return None
        sampled_bytes, ext = sampled
        src_name = self._file_name_from_image(image)
        src_stem = Path(src_name).stem or "image"
        safe_base = self._safe_filename(f"{gid}_{setter_id}_{src_stem}")[:120]
        final_name = f"{safe_base}{ext}"
        full_path = os.path.join(asset_dir, final_name)

        def _write() -> None:
            with open(full_path, "wb") as f:
                f.write(sampled_bytes)

        try:
            await asyncio.to_thread(_write)
            return full_path, final_name
        except Exception as e:
            logger.warning(
                "withdraw_image: 写入本地预览图失败 path=%s err=%s", full_path, e
            )
            return None

    @staticmethod
    async def _delete_local_file(path: str | None) -> None:
        if not path:
            return

        def _rm() -> None:
            try:
                if os.path.isfile(path):
                    os.remove(path)
            except Exception:
                return

        await asyncio.to_thread(_rm)

    @classmethod
    def _best_pattern_from_image(cls, img: Image) -> str | None:
        """从图片段中取用于入库匹配的字符串（优先 file，其次 file_unique，再次 url）。"""
        file_name = (getattr(img, "file", None) or "").strip()
        if file_name and not file_name.startswith(
            ("http://", "https://", "base64://", "file:///")
        ):
            return file_name
        fu = (getattr(img, "file_unique", None) or "").strip()
        if fu:
            return fu
        u = (getattr(img, "url", None) or "").strip()
        if u:
            return u
        return None

    @staticmethod
    def _images_from_onebot_segments(segments: Any) -> list[Image]:
        """解析 OneBot get_msg 返回的 message 段列表。"""
        out: list[Image] = []
        if isinstance(segments, str):
            for m in re.finditer(r"\[CQ:image,([^\]]+)\]", segments):
                payload = m.group(1)
                fields: dict[str, str] = {}
                for part in payload.split(","):
                    if "=" not in part:
                        continue
                    k, v = part.split("=", 1)
                    fields[k.strip()] = v.strip()
                out.append(
                    Image(
                        file=fields.get("file", ""),
                        url=fields.get("url", ""),
                        file_unique=fields.get("file_unique", ""),
                    )
                )
            return out
        if not isinstance(segments, list):
            return out
        for seg in segments:
            if not isinstance(seg, dict):
                continue
            if seg.get("type") != "image":
                continue
            d = seg.get("data") or {}
            out.append(
                Image(
                    file=d.get("file") or "",
                    url=d.get("url") or "",
                    file_unique=d.get("file_unique") or "",
                )
            )
        return out

    async def _resolve_images_from_reply(
        self,
        event: AstrMessageEvent,
        reply: Reply,
    ) -> list[Image]:
        """优先从 Reply.chain 取 Image；若无则对 reply.id 调用 get_msg（仅 aiocqhttp）。"""
        chain = getattr(reply, "chain", None) or []
        found: list[Image] = [c for c in chain if isinstance(c, Image)]
        if found:
            return found
        mid = getattr(reply, "id", None)
        if mid is None:
            return []
        if event.get_platform_name() != "aiocqhttp":
            return []
        try:
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
                AiocqhttpMessageEvent,
            )
        except ImportError:
            return []
        if not isinstance(event, AiocqhttpMessageEvent):
            return []
        try:
            mid_int = int(mid)
        except (TypeError, ValueError):
            return []
        try:
            ret = await event.bot.call_action("get_msg", message_id=mid_int)
        except Exception as e:
            logger.warning("withdraw_image: get_msg(%s) 失败: %s", mid, e)
            return []
        if not isinstance(ret, dict):
            return []
        msg = ret.get("message")
        return self._images_from_onebot_segments(msg)

    def _message_should_recall(
        self,
        chain: list,
        face_ids: list[int],
        image_patterns: list[str],
        emoji_patterns: list[str],
    ) -> bool:
        if not face_ids and not image_patterns and not emoji_patterns:
            return False
        face_set = set(face_ids)
        text_blob_parts: list[str] = []
        for comp in chain:
            if face_set and isinstance(comp, Face):
                if comp.id in face_set:
                    return True
            if emoji_patterns:
                text = getattr(comp, "text", None)
                if text:
                    text_blob_parts.append(str(text))
            if image_patterns and isinstance(comp, Image):
                file_name = str(getattr(comp, "file", "") or "").lower()
                parts: list[str] = []
                for attr in ("file", "url", "file_unique"):
                    v = getattr(comp, attr, None)
                    if v:
                        parts.append(str(v).lower())
                if not parts:
                    continue
                blob = "\n".join(parts)
                for p in image_patterns:
                    if file_name and p == file_name:
                        return True
                    if p in blob:
                        return True
        if emoji_patterns and text_blob_parts:
            text_blob = "\n".join(text_blob_parts)
            for e in emoji_patterns:
                if e and e in text_blob:
                    return True
        return False

    @staticmethod
    def _extract_face_id_from_event(event: AstrMessageEvent) -> int | None:
        """从消息链或文本中提取表情 ID。"""
        for comp in event.get_messages():
            if isinstance(comp, Face):
                try:
                    return int(comp.id)
                except (TypeError, ValueError):
                    continue

        raw = event.message_str.strip()
        m = re.search(r"\[表情:(\d+)\]", raw)
        if m:
            return int(m.group(1))
        m = re.search(r"\b(\d+)\b", raw)
        if m:
            return int(m.group(1))
        return None

    def _extract_emoji_from_event(self, event: AstrMessageEvent) -> str | None:
        """从命令中提取 emoji 字符，支持 /imgblk emoji 😀。"""
        raw = self._extract_subcommand_arg(
            event.message_str, self._EMOJI_ARG_RE
        ).strip()
        if not raw:
            return None
        if raw.startswith("[") and raw.endswith("]"):
            return None
        return raw[:16]

    async def _try_delete(self, event: AstrMessageEvent) -> bool:
        if event.get_platform_name() != "aiocqhttp":
            return False
        try:
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
                AiocqhttpMessageEvent,
            )
        except ImportError:
            logger.warning("withdraw_image: 无法导入 AiocqhttpMessageEvent")
            return False
        if not isinstance(event, AiocqhttpMessageEvent):
            return False
        mid = event.message_obj.message_id
        try:
            mid_int = int(mid)
        except (TypeError, ValueError):
            logger.warning("withdraw_image: 无法解析 message_id: %s", mid)
            return False
        try:
            ret = await event.bot.call_action("delete_msg", message_id=mid_int)
            logger.info("withdraw_image: delete_msg(%s) => %s", mid_int, ret)
            return True
        except Exception as e:
            logger.warning("withdraw_image: delete_msg 失败: %s", e)
            return False

    async def _ensure_group_admin_or_owner(
        self, event: AstrMessageEvent
    ) -> tuple[bool, str]:
        """仅允许群管理员、群主或 AstrBot 超级用户操作。"""
        gid = event.get_group_id()
        if not gid:
            return False, "此指令仅可在群聊中使用。"
        # 兼容 AstrBot 超级用户越权
        if event.is_admin():
            return True, ""
        try:
            group = await event.get_group(gid)
        except Exception as e:
            logger.warning("withdraw_image: 获取群信息失败: %s", e)
            return False, "无法获取群权限信息，请稍后再试。"
        if not group:
            return False, "无法获取群权限信息，请稍后再试。"

        sender = str(event.get_sender_id() or "").strip()
        if not sender:
            return False, "无法识别发送者身份。"
        owner = str(getattr(group, "group_owner", "") or "").strip()
        admins = {
            str(i).strip()
            for i in (getattr(group, "group_admins", None) or [])
            if str(i).strip()
        }
        if sender == owner or sender in admins:
            return True, ""

        return False, "仅群主、群管理员或 AstrBot 超级用户可以使用该命令。"

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(
        filter.EventMessageType.GROUP_MESSAGE,
        priority=5,
    )
    async def on_group_image(self, event: AstrMessageEvent):
        """匹配群消息中的图片 / 表情并撤回（仅本群规则）。"""
        if event.get_sender_id() and event.get_sender_id() == event.get_self_id():
            return
        gid = event.get_group_id()
        if not gid:
            return
        face_ids, image_patterns, emoji_patterns = await self._get_group_rules_cached(
            gid
        )
        chain = event.get_messages()
        if not self._message_should_recall(
            chain, face_ids, image_patterns, emoji_patterns
        ):
            return
        deleted = await self._try_delete(event)
        if deleted:
            logger.info(
                "withdraw_image: 命中规则并撤回成功 group_id=%s sender_id=%s message_id=%s",
                gid,
                event.get_sender_id(),
                event.message_obj.message_id,
            )
        if deleted:
            event.stop_event()
            event.should_call_llm(False)

    @filter.command_group("imgblk")
    def imgblk(self, event: AstrMessageEvent):
        """管理本群图片与表情的屏蔽规则（群主/群管理员/AstrBot 超级用户可用）。"""

    @imgblk.command("face")
    async def imgblk_face(self, event: AstrMessageEvent):
        """添加 QQ 表情 ID 到本群屏蔽列表。支持 /imgblk face 314 或 /imgblk face [表情:314]"""
        try:
            gid, err = await self._ensure_cmd_access(event)
            if err or not gid:
                yield event.plain_result(err or "权限校验失败。")
                return
            face_id = self._extract_face_id_from_event(event)
            if face_id is None:
                yield event.chain_result(
                    [
                        Plain(
                            "用法：/imgblk face <表情ID>，或直接发送 /imgblk face \u200b"
                        ),
                        Face(id=314),
                    ]
                )
                return
            added = await self._add_rule(gid, "face", str(face_id))
            self._invalidate_group_cache(gid)
            n = len(await self._list_rules(gid))
            if added:
                yield event.plain_result(
                    f"本群已添加表情屏蔽：id={face_id}，当前共 {n} 条规则。"
                )
            else:
                yield event.plain_result(
                    f"本群已存在相同规则（表情 id={face_id}），当前共 {n} 条。"
                )
        except Exception as e:
            logger.warning("withdraw_image: imgblk_face 失败: %s", e)
            yield event.plain_result("操作失败，请稍后重试。")

    @imgblk.command("emoji")
    async def imgblk_emoji(self, event: AstrMessageEvent):
        """添加 Emoji 字符到本群屏蔽列表。示例：/imgblk emoji 😀"""
        try:
            gid, err = await self._ensure_cmd_access(event)
            if err or not gid:
                yield event.plain_result(err or "权限校验失败。")
                return
            emoji_text = self._extract_emoji_from_event(event)
            if not emoji_text:
                yield event.plain_result(
                    "用法：/imgblk emoji <Emoji字符>，例如 /imgblk emoji 😀"
                )
                return
            added = await self._add_rule(gid, "emoji", emoji_text)
            self._invalidate_group_cache(gid)
            n = len(await self._list_rules(gid))
            if added:
                yield event.plain_result(
                    f"本群已添加 Emoji 屏蔽：{emoji_text}，当前共 {n} 条规则。"
                )
            else:
                yield event.plain_result(
                    f"本群已存在相同规则（Emoji {emoji_text}），当前共 {n} 条。"
                )
        except Exception as e:
            logger.warning("withdraw_image: imgblk_emoji 失败: %s", e)
            yield event.plain_result("操作失败，请稍后重试。")

    @imgblk.command("img")
    async def imgblk_img(self, event: AstrMessageEvent):
        """仅支持引用回复带图消息后发送 /imgblk img（无额外文字）自动入库。"""
        try:
            gid, err = await self._ensure_cmd_access(event)
            if err or not gid:
                yield event.plain_result(err or "权限校验失败。")
                return
            extra = self._extract_subcommand_arg(event.message_str, self._IMG_ARG_RE)
            if extra:
                yield event.plain_result(
                    "`/imgblk img` 仅支持引用添加，请引用一条带图消息后再发送该命令。"
                )
                return
            msg = await self._add_image_rules_from_reply(event, gid)
            yield event.plain_result(msg)
        except Exception as e:
            logger.warning("withdraw_image: imgblk_img 失败: %s", e)
            yield event.plain_result("操作失败，请稍后重试。")

    @imgblk.command("list")
    async def imgblk_list(self, event: AstrMessageEvent):
        """列出本群所有屏蔽规则。"""
        try:
            gid, err = await self._ensure_cmd_access(event)
            if err or not gid:
                yield event.plain_result(err or "权限校验失败。")
                return
            entries = await self._list_rules(gid)
            asset_map = await self._list_rule_assets(gid)
            if not entries:
                yield event.plain_result("本群屏蔽列表为空。")
                return
            raw_page = self._extract_subcommand_arg(
                event.message_str, self._LIST_ARG_RE
            )
            if raw_page:
                try:
                    page = int(raw_page)
                except (TypeError, ValueError):
                    yield event.plain_result("参数无效。用法：/imgblk list [页码]")
                    return
            else:
                page = 1
            if page < 1:
                yield event.plain_result("页码必须 >= 1。")
                return

            total = len(entries)
            total_pages = (total + self._LIST_PAGE_SIZE - 1) // self._LIST_PAGE_SIZE
            if page > total_pages:
                yield event.plain_result(f"页码超出范围。当前共 {total_pages} 页。")
                return

            start = (page - 1) * self._LIST_PAGE_SIZE
            end = min(start + self._LIST_PAGE_SIZE, total)
            current = entries[start:end]

            zwsp = "\u200b"
            chain = [
                Plain(
                    f"{zwsp}群 {gid} 屏蔽规则（第 {page}/{total_pages} 页，共 {total} 条）："
                )
            ]
            for idx, e in enumerate(current, start=start + 1):
                kind = str(e.get("kind", ""))
                val = str(e.get("value", ""))
                if kind == "face":
                    try:
                        chain.extend(
                            [Plain(f"{zwsp}\n{idx}. [QQ表情] "), Face(id=int(val))]
                        )
                    except (TypeError, ValueError):
                        chain.append(Plain(f"{zwsp}\n{idx}. [QQ表情] {val}"))
                elif kind == "emoji":
                    chain.append(Plain(f"{zwsp}\n{idx}. [Emoji] {val}"))
                elif kind == "image":
                    chain.append(Plain(f"{zwsp}\n{idx}. [图片] {val}"))
                    rid = int(e.get("id", 0) or 0)
                    preview_path = asset_map.get(rid)
                    if preview_path and os.path.isfile(preview_path):
                        chain.append(Image.fromFileSystem(preview_path))
                else:
                    chain.append(Plain(f"{zwsp}\n{idx}. [{kind}] {val}"))
            yield event.chain_result(chain)

            if total_pages > 1:
                yield event.plain_result("翻页用法：/imgblk list <页码>")
        except Exception as e:
            logger.warning("withdraw_image: imgblk_list 失败: %s", e)
            yield event.plain_result("操作失败，请稍后重试。")

    @imgblk.command("del")
    async def imgblk_del(self, event: AstrMessageEvent, index: int):
        """按序号删除本群规则（序号见 /imgblk list）。"""
        try:
            gid, err = await self._ensure_cmd_access(event)
            if err or not gid:
                yield event.plain_result(err or "权限校验失败。")
                return
            try:
                idx = int(str(index).strip())
            except (TypeError, ValueError):
                yield event.plain_result("参数无效。用法：/imgblk del <序号>")
                return
            n = len(await self._list_rules(gid))
            if idx < 1 or idx > n:
                yield event.plain_result(f"序号无效，本群当前共 {n} 条。")
                return
            removed = await self._delete_rule_by_index(gid, idx)
            if removed:
                await self._delete_local_file(removed.get("local_path"))
                self._invalidate_group_cache(gid)
                yield event.plain_result(
                    f"本群已删除第 {idx} 条，剩余 {len(await self._list_rules(gid))} 条。"
                )
            else:
                yield event.plain_result("删除失败，请重试。")
        except Exception as e:
            logger.warning("withdraw_image: imgblk_del 失败: %s", e)
            yield event.plain_result("操作失败，请稍后重试。")

    @imgblk.command("clear")
    async def imgblk_clear(self, event: AstrMessageEvent):
        """清空本群屏蔽列表。"""
        try:
            gid, err = await self._ensure_cmd_access(event)
            if err or not gid:
                yield event.plain_result(err or "权限校验失败。")
                return
            deleted_paths = await self._clear_group(gid)
            for p in deleted_paths:
                await self._delete_local_file(p)
            self._invalidate_group_cache(gid)
            yield event.plain_result("已清空本群屏蔽列表。")
        except Exception as e:
            logger.warning("withdraw_image: imgblk_clear 失败: %s", e)
            yield event.plain_result("操作失败，请稍后重试。")
