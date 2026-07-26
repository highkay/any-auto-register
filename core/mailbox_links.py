"""Magic-link / raw-mail helpers that do NOT strip URLs.

``BaseMailbox.wait_for_code`` runs ``_safe_extract`` which removes all
``https?://`` URLs before OTP matching. Platforms that need magic links
(Claude, Cerebras, Z.ai, etc.) must use this module instead.

P0 does **not** claim every ``create_mailbox`` provider works. Only
providers that expose a list/detail hook reachable by the adapters below
are supported. Use ``supports_magic_link`` / ``UnsupportedMailboxForLinksError``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import html
import inspect
import re
import time
from typing import Any, Callable, Iterable, Pattern


# Product allowlist (documentation + UI hints). Runtime support is still
# determined by adapter hit, not by this set alone.
P0_MAGIC_LINK_PROVIDER_ALLOWLIST: frozenset[str] = frozenset(
    {
        "cfworker",
        "outlookemail",
        "maliapi",
        "gptmail",
        "applemail",
        "microsoft",
        "outlook",
        "skymail",
        "cloudmail",
        "edumail",
        "opentrashmail",
    }
)

# Hard gate for first-slice acceptance (must have working adapters + tests).
P0_MAGIC_LINK_HARD_GATE: frozenset[str] = frozenset(
    {
        "cfworker",
        "maliapi",
        "gptmail",
    }
)

CLAUDE_MAGIC_LINK_REGEX = re.compile(
    r"https://claude\.ai/magic-link#[A-Za-z0-9_\-:=+/]+",
    re.IGNORECASE,
)


class UnsupportedMailboxForLinksError(RuntimeError):
    """Raised when no message adapter can list raw mail bodies for links."""

    def __init__(self, provider_hint: str = "", detail: str = ""):
        self.provider_hint = str(provider_hint or "").strip() or "unknown"
        self.detail = str(detail or "").strip()
        allow = ", ".join(sorted(P0_MAGIC_LINK_PROVIDER_ALLOWLIST))
        msg = (
            f"当前邮箱 provider={self.provider_hint!r} 不支持 magic-link 原始正文拉取。"
            f" 请切换到白名单 provider（例如: {allow}）。"
        )
        if self.detail:
            msg = f"{msg} 详情: {self.detail}"
        super().__init__(msg)


@dataclass
class MailMessageView:
    id: str
    subject: str = ""
    texts: list[str] = field(default_factory=list)


def _provider_hint(mailbox) -> str:
    for attr in ("provider", "provider_name", "name", "type"):
        value = getattr(mailbox, attr, None)
        if value:
            return str(value).strip().lower()
    return type(mailbox).__name__


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        try:
            return value.decode("utf-8", errors="ignore")
        except Exception:
            return ""
    return str(value)


def _normalize_text(value: str) -> str:
    text = html.unescape(_as_text(value))
    # Light cleanup only — keep URLs intact.
    return text.replace("\r\n", "\n")


def _collect_text_parts(message: dict, decoded_raw: str = "") -> list[str]:
    parts: list[str] = []
    if not isinstance(message, dict):
        return parts
    for key in (
        "subject",
        "from",
        "from_addr",
        "sender",
        "body",
        "text",
        "html",
        "content",
        "raw",
        "raw_content",
        "snippet",
        "preview",
    ):
        val = message.get(key)
        if val:
            parts.append(_normalize_text(_as_text(val)))
    if decoded_raw:
        parts.append(_normalize_text(decoded_raw))
    # Deduplicate while preserving order.
    seen: set[str] = set()
    unique: list[str] = []
    for part in parts:
        if part and part not in seen:
            seen.add(part)
            unique.append(part)
    return unique


def _message_id(message: dict, fallback_index: int) -> str:
    for key in ("id", "message_id", "uid", "mail_id", "msg_id"):
        value = message.get(key)
        if value not in (None, ""):
            return str(value)
    return f"msg-{fallback_index}"


def _message_subject(message: dict) -> str:
    return _as_text(message.get("subject") or message.get("title") or "")


def _decode_raw_if_possible(mailbox, raw: Any) -> str:
    if raw in (None, ""):
        return ""
    decoder = getattr(mailbox, "_decode_raw_content", None)
    if callable(decoder):
        try:
            return _as_text(decoder(raw))
        except Exception:
            return _as_text(raw)
    return _as_text(raw)


def _enrich_with_detail(mailbox, message: dict) -> dict:
    """Best-effort full-body enrichment for list stubs."""
    if not isinstance(message, dict):
        return {}
    merged = dict(message)
    mid = _message_id(message, 0)

    raw_fn = getattr(mailbox, "_fetch_message_raw", None)
    if callable(raw_fn):
        try:
            raw = raw_fn(message) if _accepts_message_arg(raw_fn) else raw_fn(mid)
            if raw:
                merged["raw"] = raw
                merged["_decoded_raw"] = _decode_raw_if_possible(mailbox, raw)
        except Exception:
            pass

    for name in ("_fetch_message_detail", "_get_message_detail", "_message_detail_text"):
        fn = getattr(mailbox, name, None)
        if not callable(fn):
            continue
        try:
            detail = fn(message) if _accepts_message_arg(fn) else fn(mid)
        except TypeError:
            try:
                detail = fn(mid)
            except Exception:
                continue
        except Exception:
            continue
        if isinstance(detail, dict):
            for key, value in detail.items():
                if value not in (None, "") and key not in merged:
                    merged[key] = value
                elif key in ("body", "text", "html", "content", "raw") and value:
                    merged[key] = value
        elif detail:
            merged.setdefault("body", _as_text(detail))
    return merged


def _accepts_message_arg(fn: Callable) -> bool:
    try:
        params = list(inspect.signature(fn).parameters.values())
    except (TypeError, ValueError):
        return False
    # skip self
    names = [p.name for p in params if p.name != "self"]
    if not names:
        return False
    first = names[0]
    return first in {"message", "msg", "item", "mail", "account"}


def _views_from_messages(mailbox, messages: Iterable[Any], *, limit: int) -> list[MailMessageView]:
    views: list[MailMessageView] = []
    for index, raw_msg in enumerate(messages or []):
        if len(views) >= limit:
            break
        if not isinstance(raw_msg, dict):
            continue
        enriched = _enrich_with_detail(mailbox, raw_msg)
        decoded = _as_text(enriched.pop("_decoded_raw", "") or "")
        if not decoded and enriched.get("raw"):
            decoded = _decode_raw_if_possible(mailbox, enriched.get("raw"))
        texts = _collect_text_parts(enriched, decoded_raw=decoded)
        views.append(
            MailMessageView(
                id=_message_id(enriched, index),
                subject=_message_subject(enriched),
                texts=texts,
            )
        )
    return views


def _account_email(account) -> str:
    if account is None:
        return ""
    if isinstance(account, str):
        return account
    for attr in ("email", "address", "account_id"):
        value = getattr(account, attr, None)
        if value:
            return str(value)
    return str(account)


def iter_mail_message_views(
    mailbox,
    account,
    *,
    limit: int = 20,
) -> list[MailMessageView]:
    """Pull message views via ordered duck-type adapters.

    Never calls ``wait_for_code`` / ``_safe_extract``.
    """
    if mailbox is None:
        raise UnsupportedMailboxForLinksError("none", "mailbox is None")

    limit = max(1, int(limit or 20))
    email = _account_email(account)
    hint = _provider_hint(mailbox)

    # Hook F: explicit provider implementation wins.
    hook_views = getattr(mailbox, "iter_mail_message_views", None)
    if callable(hook_views) and hook_views is not iter_mail_message_views:
        try:
            result = hook_views(account)
            if result is not None:
                views = list(result)
                if views and not isinstance(views[0], MailMessageView):
                    # Allow list[dict] from provider hook.
                    return _views_from_messages(mailbox, views, limit=limit)[:limit]
                return list(views)[:limit]
        except Exception as exc:
            raise UnsupportedMailboxForLinksError(hint, f"hook iter_mail_message_views failed: {exc}") from exc

    hook_texts = getattr(mailbox, "iter_message_texts", None)
    if callable(hook_texts):
        try:
            views: list[MailMessageView] = []
            for index, item in enumerate(hook_texts(account)):
                if len(views) >= limit:
                    break
                if isinstance(item, MailMessageView):
                    views.append(item)
                elif isinstance(item, dict):
                    views.extend(_views_from_messages(mailbox, [item], limit=1))
                else:
                    text = _normalize_text(_as_text(item))
                    if text:
                        views.append(MailMessageView(id=f"text-{index}", texts=[text]))
            if views:
                return views
        except Exception as exc:
            raise UnsupportedMailboxForLinksError(hint, f"hook iter_message_texts failed: {exc}") from exc

    # Adapter A: CFWorker-style _get_mails(email)
    get_mails = getattr(mailbox, "_get_mails", None)
    if callable(get_mails):
        try:
            mails = get_mails(email) if email else get_mails()
        except TypeError:
            mails = get_mails(email)
        views = _views_from_messages(mailbox, mails or [], limit=limit)
        if views is not None:
            # Even empty list means adapter hit — return empty rather than fall through
            # only when the call succeeded.
            return views

    # Adapter B: _list_mails(email)
    list_mails = getattr(mailbox, "_list_mails", None)
    if callable(list_mails):
        try:
            mails = list_mails(email)
        except TypeError:
            try:
                mails = list_mails(getattr(account, "account_id", None) or email)
            except Exception as exc:
                raise UnsupportedMailboxForLinksError(hint, f"_list_mails failed: {exc}") from exc
        return _views_from_messages(mailbox, mails or [], limit=limit)

    # Adapter C / D: _list_messages(...)
    list_messages = getattr(mailbox, "_list_messages", None)
    if callable(list_messages):
        mails: list = []
        try:
            # AppleMail: _list_messages(account, mailbox_name)
            params = list(inspect.signature(list_messages).parameters.values())
            param_names = [p.name for p in params if p.name != "self"]
        except (TypeError, ValueError):
            param_names = []

        try:
            if len(param_names) >= 2 and param_names[0] in {"account", "acc"}:
                # C-apple: try common folder names
                folders = ["INBOX", "Junk", "Junk Email", "Spam"]
                resolve = getattr(mailbox, "_resolve_mailboxes_for_account", None)
                if callable(resolve):
                    try:
                        folders = list(resolve(account)) or folders
                    except Exception:
                        pass
                seen_ids: set[str] = set()
                for folder in folders:
                    try:
                        batch = list_messages(account, folder)
                    except TypeError:
                        batch = list_messages(account, mailbox=folder)
                    for item in batch or []:
                        mid = _message_id(item, len(mails)) if isinstance(item, dict) else ""
                        if mid and mid in seen_ids:
                            continue
                        if mid:
                            seen_ids.add(mid)
                        mails.append(item)
                        if len(mails) >= limit * 2:
                            break
                    if len(mails) >= limit * 2:
                        break
            elif param_names and param_names[0] in {"account", "acc"}:
                mails = list(list_messages(account) or [])
            else:
                # D: email: str
                mails = list(list_messages(email) or [])
        except Exception as exc:
            raise UnsupportedMailboxForLinksError(hint, f"_list_messages failed: {exc}") from exc
        return _views_from_messages(mailbox, mails, limit=limit)

    # Adapter E: _get_client().get_messages(email)
    get_client = getattr(mailbox, "_get_client", None)
    if callable(get_client):
        try:
            client = get_client()
            get_messages = getattr(client, "get_messages", None) if client is not None else None
            if callable(get_messages):
                mails = list(get_messages(email) or [])
                return _views_from_messages(mailbox, mails, limit=limit)
        except Exception as exc:
            raise UnsupportedMailboxForLinksError(hint, f"_get_client.get_messages failed: {exc}") from exc

    raise UnsupportedMailboxForLinksError(hint, "no list/detail adapter matched")


def supports_magic_link(mailbox) -> bool:
    """Best-effort probe: True if an adapter can be selected without error.

    Does not require non-empty inbox — only that a list hook exists / succeeds.
    """
    if mailbox is None:
        return False
    # Fast path: known hooks without network.
    if any(
        callable(getattr(mailbox, name, None))
        for name in (
            "iter_mail_message_views",
            "iter_message_texts",
            "_get_mails",
            "_list_mails",
            "_list_messages",
        )
    ):
        # Exclude our own function bound accidentally.
        hook = getattr(mailbox, "iter_mail_message_views", None)
        if hook is iter_mail_message_views:
            pass
        else:
            return True
    get_client = getattr(mailbox, "_get_client", None)
    if callable(get_client):
        return True
    return False


def _compile_regex(link_regex: Pattern[str] | str) -> Pattern[str]:
    if isinstance(link_regex, re.Pattern):
        return link_regex
    return re.compile(str(link_regex), re.IGNORECASE)


def find_magic_link_in_texts(
    texts: Iterable[str],
    link_regex: Pattern[str] | str,
    *,
    must_contain: str = "",
) -> str | None:
    pattern = _compile_regex(link_regex)
    needle = str(must_contain or "").strip().lower()
    for text in texts:
        blob = _as_text(text)
        if needle and needle not in blob.lower():
            continue
        match = pattern.search(blob)
        if match:
            return match.group(0)
    return None


def wait_for_magic_link(
    mailbox,
    account,
    *,
    link_regex: Pattern[str] | str,
    timeout: int,
    before_ids: set | None = None,
    poll_interval: float = 3.0,
    task_control=None,
    must_contain: str = "",
    log=print,
    limit: int = 20,
) -> str:
    """Poll raw message bodies until *link_regex* matches.

    Raises ``UnsupportedMailboxForLinksError`` immediately if no adapter works.
    Honors ``task_control.checkpoint()`` when provided.
    """
    pattern = _compile_regex(link_regex)
    seen = set(before_ids or set())
    # Prime: if before_ids not given, snapshot current ids so we prefer new mail,
    # but still scan all texts (some providers rewrite ids).
    try:
        initial = iter_mail_message_views(mailbox, account, limit=limit)
    except UnsupportedMailboxForLinksError:
        raise
    if before_ids is None:
        for view in initial:
            seen.add(view.id)

    deadline = time.monotonic() + max(1, int(timeout))
    last_error: Exception | None = None

    while time.monotonic() < deadline:
        if task_control is not None:
            checkpoint = getattr(task_control, "checkpoint", None)
            if callable(checkpoint):
                checkpoint()

        try:
            views = iter_mail_message_views(mailbox, account, limit=limit)
        except UnsupportedMailboxForLinksError:
            raise
        except Exception as exc:  # transient list errors
            last_error = exc
            if log:
                log(f"[mailbox_links] list error: {exc}")
            views = []

        # Prefer new ids, then fall back to full scan.
        candidates = [v for v in views if v.id not in seen] or list(views)
        for view in candidates:
            corpus = list(view.texts)
            if view.subject:
                corpus.insert(0, view.subject)
            if must_contain:
                joined = "\n".join(corpus).lower()
                if str(must_contain).lower() not in joined:
                    continue
            link = find_magic_link_in_texts(corpus, pattern)
            if link:
                if log:
                    log(f"[mailbox_links] magic link found in message {view.id}")
                return link
            seen.add(view.id)

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        sleep_for = min(max(0.2, float(poll_interval)), remaining)
        # Interruptible sleep
        end = time.monotonic() + sleep_for
        while time.monotonic() < end:
            if task_control is not None:
                checkpoint = getattr(task_control, "checkpoint", None)
                if callable(checkpoint):
                    checkpoint()
            time.sleep(min(0.5, end - time.monotonic()))

    detail = f" last_error={last_error}" if last_error else ""
    raise TimeoutError(f"等待 magic link 超时（{timeout}s）{detail}")
