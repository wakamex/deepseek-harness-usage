#!/usr/bin/env python3
"""DeepSeek Harness account balance monitor."""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import signal
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit

import yaml

__version__ = "0.1.0"

BALANCE_URL = "https://api.deepseek.com/user/balance"
CACHE_MAX_AGE = 300
FAILURE_BACKOFF = 30
DAEMON_INTERVAL = 300
MAX_RESPONSE_BYTES = 1_048_576
SCHEMA_VERSION = 1
PROVIDER = "deepseek"
CLIENT = "deepseek_harness"
SOURCE = "deepseek_user_balance_api"
CREDENTIAL_REF = "DEEPSEEK_API_KEY"
EXPLICIT_KEY_ENV = "DEEPSEEK_HARNESS_USAGE_API_KEY"
USAGE_FILE_ENV = "DEEPSEEK_HARNESS_USAGE_FILE"

_CREDENTIAL_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_LEGAL_API_KEY = re.compile(r"^[\x21-\x7e]+$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_DECIMAL = re.compile(r"^(?:0|[1-9]\d*)(?:\.\d+)?$")
_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")


class _DuplicateYamlKey(yaml.YAMLError):
    pass


class _UniqueSafeLoader(yaml.SafeLoader):
    pass


_UniqueSafeLoader.yaml_implicit_resolvers = {
    key: list(resolvers)
    for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
for _resolver_key, _resolvers in _UniqueSafeLoader.yaml_implicit_resolvers.items():
    _UniqueSafeLoader.yaml_implicit_resolvers[_resolver_key] = [
        resolver for resolver in _resolvers if resolver[0] != "tag:yaml.org,2002:bool"
    ]
_UniqueSafeLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)


def _construct_unique_mapping(loader, node, deep=False):
    if not isinstance(node, yaml.MappingNode):
        raise _DuplicateYamlKey
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise _DuplicateYamlKey from exc
        if duplicate:
            raise _DuplicateYamlKey
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


class UsageError(RuntimeError):
    """A safe, user-facing balance collection error."""


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    """Keep the authorization header pinned to the configured origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def _expand_harness_home(path: str) -> str:
    """Mirror Harness's deliberately narrow tilde expansion."""
    if path == "~":
        return str(Path.home())
    if path.startswith(("~/", "~\\")):
        return os.path.join(Path.home(), path[2:])
    return path


def _absolute_lexical(path: str) -> Path:
    """Make a path absolute without following its final symlink."""
    return Path(os.path.abspath(_expand_harness_home(path)))


def get_dsh_home() -> Path:
    """Resolve the default Harness home from the launching environment."""
    configured = os.environ.get("DSH_HOME", "")
    return _absolute_lexical(configured) if configured.strip() else Path.home() / ".dsh"


def get_usage_file() -> Path:
    """Return the cache path, honoring the package-specific override."""
    configured = os.environ.get(USAGE_FILE_ENV, "")
    return (
        _absolute_lexical(configured)
        if configured.strip()
        else get_dsh_home() / "usage-limits.json"
    )


def _read_text_if_present(path: Path, label: str) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError) as exc:
        raise UsageError(f"Could not read {label} at {path}") from exc


def _trim_node_env(value: str) -> str:
    return value.strip(" \t\n")


def _parse_node_env(text: str) -> dict[str, str]:
    """Mirror the Node 22 parser used by Harness's launch environment."""
    content = _trim_node_env(text.replace("\r", ""))
    values: dict[str, str] = {}
    while content:
        if content[0] in "\n#":
            newline = content.find("\n")
            content = content[newline + 1 :] if newline >= 0 else ""
            continue

        equals = content.find("=")
        newline = content.find("\n")
        delimiter = (
            min(index for index in (equals, newline) if index >= 0)
            if (equals >= 0 or newline >= 0)
            else -1
        )
        if delimiter < 0 or (newline >= 0 and delimiter == newline):
            content = _trim_node_env(content[delimiter + 1 :]) if delimiter >= 0 else ""
            continue

        key = _trim_node_env(content[:delimiter])
        content = content[delimiter + 1 :]
        if not content or content[0] == "\n":
            values[key] = ""
            continue
        content = _trim_node_env(content)
        if not key:
            continue
        if key.startswith("export "):
            key = _trim_node_env(key[7:])
        if not content:
            values[key] = ""
            break

        if content[0] == '"':
            closing = content.find('"', 1)
            if closing >= 0:
                values[key] = content[1:closing].replace("\\n", "\n")
                newline = content.find("\n", closing + 1)
                content = content[newline + 1 :] if newline >= 0 else ""
                continue

        if content[0] in "'\"`":
            closing = content.find(content[0], 1)
            if closing < 0:
                newline = content.find("\n")
                if newline >= 0:
                    values[key] = content[:newline]
                    content = content[newline + 1 :]
                else:
                    values[key] = content
                    break
            else:
                values[key] = content[1:closing]
                newline = content.find("\n", closing + 1)
                content = content[newline + 1 :] if newline >= 0 else ""
                continue
        else:
            newline = content.find("\n")
            if newline >= 0:
                value = content[:newline]
                comment = value.find("#")
                if comment >= 0:
                    value = value[:comment]
                values[key] = _trim_node_env(value)
                content = content[newline + 1 :]
            else:
                comment = content.find("#")
                value = content[:comment] if comment >= 0 else content
                values[key] = _trim_node_env(value)
                content = ""
        content = _trim_node_env(content)
    return values


def _dotenv_lookup(path: Path, name: str) -> tuple[bool, str | None]:
    text = _read_text_if_present(path, ".env credential source")
    if text is None:
        return False, None
    values = _parse_node_env(text)
    return name in values, values.get(name)


def _dotenv_value(path: Path, name: str) -> str | None:
    found, value = _dotenv_lookup(path, name)
    return value if found and value else None


def _managed_credential(path: Path, name: str) -> str | None:
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise UsageError(
            f"Could not inspect Harness credential store at {path}"
        ) from exc
    if os.name != "nt" and stat.S_IMODE(mode) & 0o077:
        raise UsageError(
            f"Harness credential store {path} is readable beyond its owner; "
            f'run "chmod 600 {path}" before using it'
        )
    text = _read_text_if_present(path, "Harness credential store")
    if text is None:  # The file may disappear between stat and read.
        return None
    try:
        document = yaml.load(text, Loader=_UniqueSafeLoader)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        location = (
            f" at line {mark.line + 1}, column {mark.column + 1}"
            if mark is not None
            else ""
        )
        raise UsageError(
            f"Harness credential store {path} is invalid{location}"
        ) from exc
    if document is None:
        document = {}
    if not isinstance(document, dict):
        raise UsageError(f"Harness credential store {path} must be a mapping")
    for key, value in document.items():
        if not isinstance(key, str) or _CREDENTIAL_NAME.fullmatch(key) is None:
            raise UsageError(
                f"Harness credential store {path} has an invalid credential reference"
            )
        if not isinstance(value, str):
            raise UsageError(f"The value for {key} in {path} is not a string")
        if not value:
            raise UsageError(
                f"The value for {key} in {path} is empty; remove it instead"
            )
    return document.get(name)


def _usable_api_key(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise UsageError(
            "The configured DEEPSEEK_API_KEY is blank; set it to the raw key"
        )
    if _LEGAL_API_KEY.fullmatch(normalized) is None:
        raise UsageError(
            "The configured DEEPSEEK_API_KEY contains characters an HTTP header cannot carry"
        )
    return normalized


def _is_official_base_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname == "api.deepseek.com"
        and port in (None, 443)
        and parsed.username is None
        and parsed.password is None
        and parsed.path.rstrip("/") in ("", "/v1")
        and not parsed.query
        and not parsed.fragment
    )


def _assert_compatible_harness_route(cwd: Path) -> None:
    """Refuse known custom routes before reading their possible credential."""
    dsh_home = get_dsh_home()
    bases: list[tuple[bool, str | None]] = [
        (
            "DEEPSEEK_BASE_URL" in os.environ,
            os.environ.get("DEEPSEEK_BASE_URL"),
        )
    ]
    bases.append(_dotenv_lookup(cwd / ".env", "DEEPSEEK_BASE_URL"))
    if cwd != dsh_home:
        bases.append(_dotenv_lookup(dsh_home / ".env", "DEEPSEEK_BASE_URL"))

    settings_text = _read_text_if_present(
        dsh_home / "settings.yaml", "Harness settings"
    )
    if settings_text is not None:
        try:
            settings = yaml.load(settings_text, Loader=_UniqueSafeLoader)
        except yaml.YAMLError as exc:
            raise UsageError(
                "Could not verify the DeepSeek route because Harness settings are invalid"
            ) from exc
        if settings is not None and not isinstance(settings, dict):
            raise UsageError(
                "Could not verify the DeepSeek route because Harness settings are invalid"
            )
        section = (settings or {}).get("llm-deepseek")
        if section is not None and not isinstance(section, dict):
            raise UsageError(
                "Could not verify the DeepSeek route because llm-deepseek settings are invalid"
            )
        if isinstance(section, dict):
            has_base_url = "baseURL" in section
            base_url = section.get("baseURL")
            if base_url is not None and not isinstance(base_url, str):
                raise UsageError(
                    "Could not verify the DeepSeek route because its baseURL is invalid"
                )
            bases.append((has_base_url, base_url))
            credential_ref = section.get("apiKeyEnv")
            if credential_ref not in (None, CREDENTIAL_REF):
                raise UsageError(
                    "Harness uses a custom DeepSeek credential reference; set "
                    f"{EXPLICIT_KEY_ENV} to the official DeepSeek account key for this tool"
                )

    if any(
        present and (not isinstance(base, str) or not _is_official_base_url(base))
        for present, base in bases
    ):
        raise UsageError(
            "Harness uses a custom DeepSeek endpoint; set "
            f"{EXPLICIT_KEY_ENV} to the official DeepSeek account key for this tool"
        )


def get_api_key(cwd: Path | None = None) -> str:
    """Resolve the default DeepSeek key using Harness credential precedence."""
    explicit = os.environ.get(EXPLICIT_KEY_ENV)
    if explicit is not None:
        return _usable_api_key(explicit)

    project_root = _absolute_lexical(str(cwd or Path.cwd()))
    _assert_compatible_harness_route(project_root)

    inherited = os.environ.get(CREDENTIAL_REF)
    if inherited:
        return _usable_api_key(inherited)

    dsh_home = get_dsh_home()
    stored = _managed_credential(dsh_home / ".credentials.yaml", CREDENTIAL_REF)
    if stored:
        return _usable_api_key(stored)

    project = _dotenv_value(project_root / ".env", CREDENTIAL_REF)
    if project:
        return _usable_api_key(project)
    if project_root != dsh_home:
        user = _dotenv_value(dsh_home / ".env", CREDENTIAL_REF)
        if user:
            return _usable_api_key(user)
    raise UsageError(
        "DeepSeek Harness has no DEEPSEEK_API_KEY; configure it in the Models page, "
        "export it, or add it to a Harness credential source"
    )


def fetch_balance(api_key: str | None = None) -> dict:
    """Fetch the supported account-balance response with the current key."""
    api_key = api_key or get_api_key()
    request = urllib.request.Request(
        BALANCE_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": f"deepseek-harness-usage/{__version__}",
        },
    )
    try:
        opener = urllib.request.build_opener(_RejectRedirects())
        with opener.open(request, timeout=15) as response:
            payload_bytes = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise UsageError(
                "DeepSeek no longer accepts the configured API key; update it in DeepSeek Harness"
            ) from exc
        raise UsageError(f"DeepSeek balance API returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise UsageError("DeepSeek balance API is unavailable") from exc
    if len(payload_bytes) > MAX_RESPONSE_BYTES:
        raise UsageError("DeepSeek balance API returned an oversized response")
    try:
        payload = json.loads(payload_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UsageError("DeepSeek balance API returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise UsageError("DeepSeek balance API returned an unexpected response")
    return payload


def _money(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise UsageError(f"DeepSeek balance response omitted {field}")
    if _DECIMAL.fullmatch(value) is None:
        raise UsageError(f"DeepSeek balance response has invalid {field}")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise UsageError(f"DeepSeek balance response has invalid {field}") from exc
    if not parsed.is_finite():
        raise UsageError(f"DeepSeek balance response has invalid {field}")
    return value


def normalize_balance(payload: dict) -> dict:
    """Validate and normalize DeepSeek's balance response."""
    available = payload.get("is_available")
    raw_balances = payload.get("balance_infos")
    if not isinstance(available, bool) or not isinstance(raw_balances, list):
        raise UsageError("DeepSeek balance API returned an unexpected response")
    balances: list[dict[str, str]] = []
    currencies: set[str] = set()
    for raw in raw_balances:
        if not isinstance(raw, dict):
            raise UsageError(
                "DeepSeek balance API returned an unexpected balance entry"
            )
        currency = raw.get("currency")
        if not isinstance(currency, str) or _CURRENCY.fullmatch(currency) is None:
            raise UsageError("DeepSeek balance response omitted currency")
        if currency in currencies:
            raise UsageError(f"DeepSeek balance response repeats currency {currency}")
        currencies.add(currency)
        balances.append(
            {
                "currency": currency,
                "total": _money(raw.get("total_balance"), "total_balance"),
                "granted": _money(raw.get("granted_balance"), "granted_balance"),
                "topped_up": _money(raw.get("topped_up_balance"), "topped_up_balance"),
            }
        )
    balances.sort(key=lambda item: item["currency"])
    return {"available": available, "balances": balances}


def _unavailable_snapshot(error: str, retrieved_at: str | None = None) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "provider": PROVIDER,
        "client": CLIENT,
        "source": SOURCE,
        "retrieved_at": retrieved_at or _iso_now(),
        "status": "unavailable",
        "error": error,
    }


def build_usage_json(api_key: str | None = None) -> dict:
    """Build one live or unavailable normalized snapshot."""
    retrieved_at = _iso_now()
    try:
        account_balance = normalize_balance(fetch_balance(api_key))
    except UsageError as exc:
        return _unavailable_snapshot(str(exc), retrieved_at)
    except Exception:
        return _unavailable_snapshot(
            "Unexpected error while checking DeepSeek balance", retrieved_at
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "provider": PROVIDER,
        "client": CLIENT,
        "source": SOURCE,
        "retrieved_at": retrieved_at,
        "status": "live",
        "account_balance": account_balance,
    }


def _cache_is_valid(data: object) -> bool:
    if not isinstance(data, dict):
        return False
    if (
        data.get("schema_version") != SCHEMA_VERSION
        or data.get("provider") != PROVIDER
        or data.get("client") != CLIENT
        or data.get("source") != SOURCE
        or _parse_iso(data.get("retrieved_at")) is None
        or not isinstance(data.get("credential_fingerprint"), str)
        or _FINGERPRINT.fullmatch(data["credential_fingerprint"]) is None
        or _parse_iso(data.get("last_attempt_at")) is None
        or data.get("status") not in {"live", "stale", "unavailable"}
    ):
        return False
    if data["status"] == "unavailable":
        return isinstance(data.get("error"), str) and bool(data["error"])
    balance = data.get("account_balance")
    if not isinstance(balance, dict) or not isinstance(balance.get("available"), bool):
        return False
    balances = balance.get("balances")
    if not isinstance(balances, list):
        return False
    currencies: set[str] = set()
    for item in balances:
        if not isinstance(item, dict):
            return False
        currency = item.get("currency")
        if (
            not isinstance(currency, str)
            or _CURRENCY.fullmatch(currency) is None
            or currency in currencies
        ):
            return False
        currencies.add(currency)
        if any(
            not isinstance(item.get(field), str)
            or _DECIMAL.fullmatch(item[field]) is None
            for field in ("total", "granted", "topped_up")
        ):
            return False
    return True


def _mkdir_private_parents(path: Path) -> None:
    missing: list[Path] = []
    current = path
    while not current.exists():
        missing.append(current)
        parent = current.parent
        if parent == current:
            break
        current = parent
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            if not directory.is_dir():
                raise


@contextmanager
def _cache_lock():
    """Claim the cache refresh/write role without waiting on another process."""
    path = get_usage_file()
    try:
        _mkdir_private_parents(path.parent)
        lock_path = path.with_name(f"{path.name}.lock")
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        raise UsageError(f"Could not lock balance cache at {path}") from exc
    try:
        if os.name == "nt":
            import msvcrt

            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            try:
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    os.close(descriptor)
                    descriptor = -1
                    yield False
                    return
                raise
        else:
            import fcntl

            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(descriptor)
                descriptor = -1
                yield False
                return
        os.ftruncate(descriptor, 0)
        os.write(descriptor, f"{os.getpid()}\n".encode("ascii"))
        yield True
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _write_usage_file_unlocked(data: dict) -> None:
    path = get_usage_file()
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", dir=path.parent
        )
    except OSError as exc:
        raise UsageError(f"Could not prepare balance cache at {path}") from exc
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise UsageError(f"Could not update balance cache at {path}") from exc


def write_usage_file(data: dict) -> None:
    """Atomically replace the credential-free private balance cache."""
    with _cache_lock() as acquired:
        if not acquired:
            raise UsageError(f"Balance cache at {get_usage_file()} is busy")
        _write_usage_file_unlocked(data)


def _read_cache() -> dict | None:
    try:
        with get_usage_file().open("rb") as handle:
            payload = handle.read(MAX_RESPONSE_BYTES + 1)
        if len(payload) > MAX_RESPONSE_BYTES:
            return None
        data = json.loads(payload)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if _cache_is_valid(data) else None


def _credential_fingerprint(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("ascii")).hexdigest()


def _bound_cache_record(data: dict, fingerprint: str, attempt_at: str) -> dict:
    return {
        **data,
        "credential_fingerprint": fingerprint,
        "last_attempt_at": attempt_at,
    }


def _without_cache_binding(data: dict) -> dict:
    return {
        key: value
        for key, value in data.items()
        if key not in {"credential_fingerprint", "last_attempt_at"}
    }


def _write_unlocked_or_annotate(data: dict) -> dict:
    try:
        _write_usage_file_unlocked(data)
    except UsageError as exc:
        return {**data, "cache_error": str(exc)}
    return data


def _matching_cache(fingerprint: str) -> dict | None:
    cached = _read_cache()
    if cached is None or cached["credential_fingerprint"] != fingerprint:
        return None
    return cached


def _ready_cached_result(
    cached: dict | None, now: datetime, max_age: int
) -> dict | None:
    if cached is None:
        return None
    last_attempt = _parse_iso(cached["last_attempt_at"])
    attempt_age = (
        (now - last_attempt).total_seconds() if last_attempt is not None else -1
    )
    if cached["status"] in {"stale", "unavailable"} and (
        0 <= attempt_age < FAILURE_BACKOFF
    ):
        return _without_cache_binding(cached)
    retrieved = _parse_iso(cached["retrieved_at"])
    age = (now - retrieved).total_seconds() if retrieved is not None else -1
    if cached["status"] == "live" and 0 <= age < max_age:
        return _without_cache_binding({**cached, "status": "cached"})
    return None


def _refresh_in_progress_result(
    cached: dict | None, now: datetime, max_age: int
) -> dict:
    if cached is None:
        return _unavailable_snapshot("Another balance refresh is already in progress")
    if "account_balance" not in cached:
        return _without_cache_binding(cached)
    retrieved = _parse_iso(cached["retrieved_at"])
    age = (now - retrieved).total_seconds() if retrieved is not None else -1
    status = "cached" if cached["status"] == "live" and 0 <= age < max_age else "stale"
    result = {**cached, "status": status}
    if status == "stale":
        result["refresh_error"] = "Another balance refresh is already in progress"
    return _without_cache_binding(result)


def get_cached_usage(max_age: int = CACHE_MAX_AGE, force_refresh: bool = False) -> dict:
    """Read a fresh cache, refresh it, or return an explicitly stale snapshot."""
    try:
        api_key = get_api_key()
    except UsageError as exc:
        return _unavailable_snapshot(str(exc))
    fingerprint = _credential_fingerprint(api_key)
    cached = _matching_cache(fingerprint)
    now = datetime.now(UTC)
    if not force_refresh:
        ready = _ready_cached_result(cached, now, max_age)
        if ready is not None:
            return ready

    with _cache_lock() as acquired:
        if not acquired:
            return _refresh_in_progress_result(
                _matching_cache(fingerprint), datetime.now(UTC), max_age
            )
        cached = _matching_cache(fingerprint)
        if not force_refresh:
            ready = _ready_cached_result(cached, datetime.now(UTC), max_age)
            if ready is not None:
                return ready

        fresh = build_usage_json(api_key)
        attempt_at = fresh["retrieved_at"]
        if fresh["status"] == "live":
            stored = _write_unlocked_or_annotate(
                _bound_cache_record(fresh, fingerprint, attempt_at)
            )
            return _without_cache_binding(stored)
        if cached is not None and "account_balance" in cached:
            stale = {
                **cached,
                "status": "stale",
                "last_attempt_at": attempt_at,
                "refresh_error": fresh.get("error", "Refresh failed"),
            }
            return _without_cache_binding(_write_unlocked_or_annotate(stale))
        unavailable = _bound_cache_record(fresh, fingerprint, attempt_at)
        return _without_cache_binding(_write_unlocked_or_annotate(unavailable))


def _statusline_text(data: dict) -> str:
    account = data.get("account_balance")
    if not isinstance(account, dict):
        return "dsh:bal:unavailable"
    balances = account.get("balances")
    if not isinstance(balances, list) or not balances:
        summary = "none"
    else:
        summary = (
            ",".join(
                f"{item['currency']}{item['total']}"
                for item in balances
                if isinstance(item, dict)
                and isinstance(item.get("currency"), str)
                and isinstance(item.get("total"), str)
            )
            or "none"
        )
    parts = [f"dsh:bal:{summary}"]
    if account.get("available") is False:
        parts.append("api-unavailable")
    if data.get("status") in {"cached", "stale"}:
        parts.append(str(data["status"]))
    return " ".join(parts)


def _print_status(data: dict) -> None:
    print("DeepSeek Harness balance")
    print(f"Status: {data['status']}")
    account = data.get("account_balance")
    if isinstance(account, dict):
        print(
            f"API access: {'available' if account.get('available') else 'unavailable'}"
        )
        balances = account.get("balances")
        if isinstance(balances, list) and balances:
            for item in balances:
                print(
                    f"{item['currency']}: {item['total']} total "
                    f"({item['granted']} granted + {item['topped_up']} topped up)"
                )
        else:
            print("Balances: none reported")
    if data.get("error"):
        print(f"Error: {data['error']}")
    if data.get("refresh_error"):
        print(f"Refresh error: {data['refresh_error']}")
    if data.get("cache_error"):
        print(f"Cache error: {data['cache_error']}")


def _refresh_and_cache() -> dict:
    return get_cached_usage(max_age=0, force_refresh=True)


def cmd_status(_args: argparse.Namespace) -> int:
    data = build_usage_json()
    _print_status(data)
    return 0 if data["status"] == "live" else 1


def cmd_json(_args: argparse.Namespace) -> int:
    data = build_usage_json()
    print(json.dumps(data, indent=2))
    return 0 if data["status"] == "live" else 1


def cmd_statusline(args: argparse.Namespace) -> int:
    print(_statusline_text(get_cached_usage(args.max_age, args.refresh)))
    return 0


def cmd_refresh(_args: argparse.Namespace) -> int:
    data = _refresh_and_cache()
    _print_status(data)
    return 0 if data["status"] == "live" else 1


def cmd_daemon(args: argparse.Namespace) -> int:
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    print(f"deepseek-harness-usage daemon started (refreshing every {args.interval}s)")
    print(f"Writing to {get_usage_file()}")
    while True:
        data = _refresh_and_cache()
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {_statusline_text(data)}")
        if data.get("error"):
            print(f"Error: {data['error']}", file=sys.stderr)
        if data.get("cache_error"):
            print(f"Cache error: {data['cache_error']}", file=sys.stderr)
        time.sleep(args.interval)


def cmd_install(_args: argparse.Namespace) -> int:
    print(
        "Install with:\n  uv tool install deepseek-harness-usage\n\n"
        "Then run:\n  deepseek-harness-usage"
    )
    return 0


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="DeepSeek Harness account balance monitor"
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="status",
        choices=["status", "json", "daemon", "statusline", "refresh", "install"],
    )
    parser.add_argument("-i", "--interval", type=_positive_int, default=DAEMON_INTERVAL)
    parser.add_argument("--max-age", type=_nonnegative_int, default=CACHE_MAX_AGE)
    parser.add_argument("--refresh", action="store_true")
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    commands = {
        "status": cmd_status,
        "json": cmd_json,
        "daemon": cmd_daemon,
        "statusline": cmd_statusline,
        "refresh": cmd_refresh,
        "install": cmd_install,
    }
    return commands[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
