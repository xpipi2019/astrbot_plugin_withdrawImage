"""群聊中按屏蔽规则自动撤回图片或 QQ 表情（Face），仅适用于 OneBot v11（aiocqhttp）。

屏蔽列表使用 SQLite 持久化，按群号分表维护，存储于 AstrBot 数据目录下的 plugin_data。
"""

from __future__ import annotations

import asyncio
import os
import re
import sqlite3
from collections.abc import Callable
from typing import Any

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import Face, Image, Reply
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path


class WithdrawImagePlugin(Star):
    """群聊图片 / QQ 表情屏蔽并自动撤回（OneBot v11）。

    仅在群聊中生效。每个群有独立的屏蔽列表（SQLite 按 group_id 区分）。

    指令（需 AstrBot 管理员，且仅在群内）：/imgblk …
    - face <id>：按 QQ 表情 ID 屏蔽（与消息段 Face 的 id 一致）
    - img <片段>：按子串匹配图片的 file / url / file_unique（不区分大小写）
    - img（无参数）：引用回复一条带图的消息并发送 /imgblk img，从该图中自动写入规则
    - list：列出本群规则
    - del <序号>：按 list 序号删除
    - clear：清空本群列表

    协议端需支持 delete_msg，且机器人在群内通常需有撤回他人消息权限。
    """

    _DB_NAME = "withdraw_blocklist.db"

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self._db_lock = asyncio.Lock()
        self._db_path: str | None = None
        self.config = config or {}

    def _preview_enabled(self) -> bool:
        return bool(self.config.get("enable_list_preview", True))

    async def _set_preview_enabled(self, enabled: bool) -> None:
        self.config["enable_list_preview"] = enabled
        save_fn = getattr(self.config, "save_config", None)
        if callable(save_fn):
            try:
                save_fn()
            except Exception as e:
                logger.warning("withdraw_image: 保存配置失败: %s", e)

    async def initialize(self) -> None:
        base = get_astrbot_plugin_data_path()
        safe_name = (getattr(self, "name", None) or "withdraw_image").replace("/", "_")
        sub = os.path.join(base, safe_name)
        os.makedirs(sub, exist_ok=True)
        self._db_path = os.path.join(sub, self._DB_NAME)
        await self._run_db(self._init_schema_sync)
        logger.info("withdraw_image: SQLite 已就绪: %s", self._db_path)

    async def terminate(self) -> None:
        self._db_path = None

    async def _run_db(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        """在锁内于线程中执行同步 SQLite 操作。"""
        path = self._db_path
        if not path:
            raise RuntimeError("withdraw_image: 数据库路径未初始化")

        async with self._db_lock:

            def _work() -> Any:
                conn = sqlite3.connect(path)
                conn.row_factory = sqlite3.Row
                try:
                    return fn(conn)
                finally:
                    conn.close()

            return await asyncio.to_thread(_work)

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
        conn.commit()

    async def _list_rules(self, group_id: str) -> list[dict[str, Any]]:
        def _q(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            cur = conn.execute(
                "SELECT id, kind, value FROM block_rules WHERE group_id = ? ORDER BY id ASC",
                (group_id,),
            )
            rows = cur.fetchall()
            return [{"id": r["id"], "kind": r["kind"], "value": r["value"]} for r in rows]

        return await self._run_db(_q)

    async def _add_rule(self, group_id: str, kind: str, value: str) -> bool:
        """返回 True 表示新插入一行，False 表示已存在（UNIQUE）。"""

        def _ins(conn: sqlite3.Connection) -> bool:
            cur = conn.execute(
                "INSERT OR IGNORE INTO block_rules (group_id, kind, value) VALUES (?, ?, ?)",
                (group_id, kind, value),
            )
            conn.commit()
            return cur.rowcount > 0

        return await self._run_db(_ins)

    async def _delete_rule_by_index(self, group_id: str, index_1: int) -> dict[str, Any] | None:
        """按当前 list 序号删除一条；无效序号返回 None。"""

        def _del(conn: sqlite3.Connection) -> dict[str, Any] | None:
            cur = conn.execute(
                "SELECT id FROM block_rules WHERE group_id = ? ORDER BY id ASC",
                (group_id,),
            )
            ids = [r[0] for r in cur.fetchall()]
            if index_1 < 1 or index_1 > len(ids):
                return None
            target = ids[index_1 - 1]
            cur = conn.execute(
                "DELETE FROM block_rules WHERE id = ? AND group_id = ?",
                (target, group_id),
            )
            conn.commit()
            if cur.rowcount:
                return {"id": target}
            return None

        return await self._run_db(_del)

    async def _clear_group(self, group_id: str) -> None:
        def _clr(conn: sqlite3.Connection) -> None:
            conn.execute("DELETE FROM block_rules WHERE group_id = ?", (group_id,))
            conn.commit()

        await self._run_db(_clr)

    @staticmethod
    def _split_face_image(entries: list[dict[str, Any]]):
        faces: list[int] = []
        images: list[str] = []
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
        return faces, images

    @staticmethod
    def _best_pattern_from_image(img: Image) -> str | None:
        """从图片段中取用于入库匹配的字符串（优先 file_unique，其次 file，再次 url）。"""
        fu = (getattr(img, "file_unique", None) or "").strip()
        if fu:
            return fu
        f = (getattr(img, "file", None) or "").strip()
        if f and not f.startswith("base64://"):
            return f
        u = (getattr(img, "url", None) or "").strip()
        if u:
            return u
        if f:
            return f
        return None

    @staticmethod
    def _images_from_onebot_segments(segments: Any) -> list[Image]:
        """解析 OneBot get_msg 返回的 message 段列表。"""
        out: list[Image] = []
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
    ) -> bool:
        if not face_ids and not image_patterns:
            return False
        face_set = set(face_ids)
        for comp in chain:
            if face_set and isinstance(comp, Face):
                if comp.id in face_set:
                    return True
            if image_patterns and isinstance(comp, Image):
                parts: list[str] = []
                for attr in ("file", "url", "file_unique"):
                    v = getattr(comp, attr, None)
                    if v:
                        parts.append(str(v).lower())
                if not parts:
                    continue
                blob = "\n".join(parts)
                for p in image_patterns:
                    if p.lower() in blob:
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

    async def _ensure_group_admin_or_owner(self, event: AstrMessageEvent) -> tuple[bool, str]:
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
        entries = await self._list_rules(gid)
        face_ids, image_patterns = self._split_face_image(entries)
        chain = event.get_messages()
        if not self._message_should_recall(chain, face_ids, image_patterns):
            return
        await self._try_delete(event)
        event.stop_event()
        event.should_call_llm(False)

    @filter.command_group("imgblk")
    def imgblk(self):
        """管理本群图片与表情的屏蔽规则（仅群主/群管理员可用）。"""

    @imgblk.command("face")
    async def imgblk_face(self, event: AstrMessageEvent):
        """添加 QQ 表情 ID 到本群屏蔽列表。支持 /imgblk face 177 或 /imgblk face [表情:177]"""
        ok, reason = await self._ensure_group_admin_or_owner(event)
        if not ok:
            yield event.plain_result(reason)
            return
        gid = event.get_group_id()
        if not gid:
            yield event.plain_result("此指令仅可在群聊中使用。")
            return
        face_id = self._extract_face_id_from_event(event)
        if face_id is None:
            yield event.plain_result(
                "用法：/imgblk face <表情ID>，或直接发送 /imgblk face [表情:5]。"
            )
            return
        added = await self._add_rule(gid, "face", str(face_id))
        n = len(await self._list_rules(gid))
        if added:
            yield event.plain_result(f"本群已添加表情屏蔽：id={face_id}，当前共 {n} 条规则。")
        else:
            yield event.plain_result(f"本群已存在相同规则（表情 id={face_id}），当前共 {n} 条。")

    @imgblk.command("img")
    async def imgblk_img(self, event: AstrMessageEvent):
        """按子串匹配图片 file/url/file_unique；或引用回复带图消息后发送 /imgblk img（无额外文字）自动入库。"""
        ok, reason = await self._ensure_group_admin_or_owner(event)
        if not ok:
            yield event.plain_result(reason)
            return
        gid = event.get_group_id()
        if not gid:
            yield event.plain_result("此指令仅可在群聊中使用。")
            return
        raw = event.message_str.strip()
        m = re.match(r"^[/\s#＃!]*imgblk\s+img\s*(.*)$", raw, re.DOTALL | re.IGNORECASE)
        if m:
            manual_pattern = m.group(1).strip()
        else:
            manual_pattern = raw.strip()

        if manual_pattern:
            added = await self._add_rule(gid, "image", manual_pattern)
            n = len(await self._list_rules(gid))
            if added:
                yield event.plain_result(f"本群已添加图片匹配规则，当前共 {n} 条。")
            else:
                yield event.plain_result(f"本群已存在相同匹配规则，当前共 {n} 条。")
            return

        reply_seg: Reply | None = None
        for comp in event.get_messages():
            if isinstance(comp, Reply):
                reply_seg = comp
                break
        if reply_seg is None:
            yield event.plain_result(
                "用法：\n"
                "1）/imgblk img <匹配片段> — 按 file/url/file_unique 子串匹配（不区分大小写）；\n"
                "2）引用回复一条带图的消息，再发送 /imgblk img（可不带其它文字），从该图自动添加规则。"
            )
            return

        images = await self._resolve_images_from_reply(event, reply_seg)
        if not images:
            yield event.plain_result(
                "未在引用消息中解析到图片。请引用包含图片的消息；若仍失败，请确认协议端支持 get_msg。"
            )
            return

        patterns: list[str] = []
        for im in images:
            p = self._best_pattern_from_image(im)
            if p:
                patterns.append(p)
        if not patterns:
            yield event.plain_result("无法从图片中提取 file/url/file_unique 标识，请改用文本手动指定匹配片段。")
            return

        added_n = 0
        dup_n = 0
        for p in patterns:
            if await self._add_rule(gid, "image", p):
                added_n += 1
            else:
                dup_n += 1
        n = len(await self._list_rules(gid))
        if added_n and dup_n:
            yield event.plain_result(
                f"本群已从引用消息添加 {added_n} 条规则，{dup_n} 条已存在；当前共 {n} 条。"
            )
        elif added_n:
            yield event.plain_result(f"本群已从引用消息添加 {added_n} 条规则，当前共 {n} 条。")
        else:
            yield event.plain_result(f"引用中的图片规则均已存在，当前共 {n} 条。")

    @imgblk.command("list")
    async def imgblk_list(self, event: AstrMessageEvent):
        """列出本群所有屏蔽规则。"""
        ok, reason = await self._ensure_group_admin_or_owner(event)
        if not ok:
            yield event.plain_result(reason)
            return
        gid = event.get_group_id()
        if not gid:
            yield event.plain_result("此指令仅可在群聊中使用。")
            return
        entries = await self._list_rules(gid)
        if not entries:
            yield event.plain_result("本群屏蔽列表为空。")
            return
        lines: list[str] = [f"群 {gid} 屏蔽规则："]
        for i, e in enumerate(entries, start=1):
            kind = e.get("kind", "?")
            val = e.get("value", "")
            lines.append(f"{i}. [{kind}] {val}")
        yield event.plain_result("\n".join(lines))
        if not self._preview_enabled():
            return
        for i, e in enumerate(entries, start=1):
            kind = str(e.get("kind", ""))
            val = str(e.get("value", ""))
            if kind == "face":
                try:
                    face_id = int(val)
                    yield event.chain_result([Face(id=face_id),])
                except (TypeError, ValueError):
                    continue
            elif kind == "image":
                # 优先尝试作为 URL 发送；若不是 URL，则回退为文本提示
                if val.startswith("http://") or val.startswith("https://"):
                    yield event.chain_result([Image(file=val, url=val)])
                else:
                    yield event.plain_result(f"{i}. 图片规则预览：{val}")

    @imgblk.command("preview")
    async def imgblk_preview(self, event: AstrMessageEvent):
        """快捷切换 list 预览开关。用法：/imgblk preview on|off"""
        ok, reason = await self._ensure_group_admin_or_owner(event)
        if not ok:
            yield event.plain_result(reason)
            return
        gid = event.get_group_id()
        if not gid:
            yield event.plain_result("此指令仅可在群聊中使用。")
            return

        raw = event.message_str.strip()
        m = re.match(
            r"^[/\s#＃!]*imgblk\s+preview\s*(.*)$",
            raw,
            re.DOTALL | re.IGNORECASE,
        )
        arg = (m.group(1).strip() if m else raw).lower()
        if not arg:
            state = "开启" if self._preview_enabled() else "关闭"
            yield event.plain_result(
                f"当前 list 预览：{state}\n"
                "用法：/imgblk preview on|off"
            )
            return

        if arg in {"on", "true", "1", "开启", "开"}:
            await self._set_preview_enabled(True)
            yield event.plain_result("已开启 list 预览发送。")
            return
        if arg in {"off", "false", "0", "关闭", "关"}:
            await self._set_preview_enabled(False)
            yield event.plain_result("已关闭 list 预览发送。")
            return
        yield event.plain_result("参数无效。用法：/imgblk preview on|off")

    @imgblk.command("del")
    async def imgblk_del(self, event: AstrMessageEvent, index: int):
        """按序号删除本群规则（序号见 /imgblk list）。"""
        ok, reason = await self._ensure_group_admin_or_owner(event)
        if not ok:
            yield event.plain_result(reason)
            return
        gid = event.get_group_id()
        if not gid:
            yield event.plain_result("此指令仅可在群聊中使用。")
            return
        n = len(await self._list_rules(gid))
        if index < 1 or index > n:
            yield event.plain_result(f"序号无效，本群当前共 {n} 条。")
            return
        removed = await self._delete_rule_by_index(gid, index)
        if removed:
            yield event.plain_result(
                f"本群已删除第 {index} 条，剩余 {len(await self._list_rules(gid))} 条。"
            )
        else:
            yield event.plain_result("删除失败，请重试。")

    @imgblk.command("clear")
    async def imgblk_clear(self, event: AstrMessageEvent):
        """清空本群屏蔽列表。"""
        ok, reason = await self._ensure_group_admin_or_owner(event)
        if not ok:
            yield event.plain_result(reason)
            return
        gid = event.get_group_id()
        if not gid:
            yield event.plain_result("此指令仅可在群聊中使用。")
            return
        await self._clear_group(gid)
        yield event.plain_result("已清空本群屏蔽列表。")
