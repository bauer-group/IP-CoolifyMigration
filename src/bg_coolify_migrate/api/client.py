"""Coolify REST API client.

IO shell: keep logic out of here; it belongs in ``domain/``.

Three non-obvious behaviours this client exists to handle:

1. **The token scope trap.** Coolify's ``ApiSensitiveData`` middleware computes
   ``can_read_sensitive = token->can('root') || token->can('read:sensitive')``.
   Without it, controllers call ``makeHidden(['value', 'real_value', ...])`` and
   the keys simply **vanish** from the JSON — no error, no redaction marker, HTTP
   200. A migration driven by a plain ``read`` token would happily recreate every
   environment variable with no value at all. So we probe eagerly at startup and
   fail closed. This is the single most important thing in this module.

2. **``$allowedFields`` is enforced.** Unknown fields are a 422 per field. Every
   write goes through a whitelist from :mod:`.fields`; we never round-trip a GET
   into a POST.

3. **Deploy endpoints rate-limit.** 429 is expected under normal operation, not
   exceptional, so it is retried with backoff rather than surfaced.
"""

from __future__ import annotations

import asyncio
import random
from types import TracebackType
from typing import Any, Self

import httpx
import structlog

from bg_coolify_migrate.errors import CoolifyApiError, InsufficientTokenScope

log = structlog.get_logger(__name__)

#: Methods that are safe to retry blindly. POST is deliberately absent: a
#: retried create can produce two resources, and cleaning that up by hand is
#: exactly the kind of half-state this tool exists to avoid.
_IDEMPOTENT = frozenset({"GET", "HEAD", "PUT", "PATCH", "DELETE"})

_RETRY_STATUS = frozenset({429, 502, 503, 504})


class CoolifyClient:
    """Async client for one Coolify instance.

    Usage::

        async with CoolifyClient(base_url, token) as api:
            await api.assert_can_read_sensitive()
            servers = await api.list_servers()
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 30.0,
        max_retries: int = 4,
        verify: bool = True,
    ) -> None:
        self._base = base_url.rstrip("/")
        if not self._base.endswith("/api/v1"):
            self._base = f"{self._base}/api/v1"
        self._token = token
        self._max_retries = max_retries
        self._client = httpx.AsyncClient(
            base_url=self._base,
            timeout=httpx.Timeout(timeout, connect=10.0),
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            verify=verify,
            follow_redirects=False,
        )
        self._can_read_sensitive: bool | None = None
        self._probe_reason: str = "unprobed"

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # ── transport ────────────────────────────────────────────────────────────

    async def _raw(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Perform the request with retries; return the response undecoded.

        Exists because ``/version`` answers in plain text while everything else
        answers in JSON.
        """
        attempt = 0
        while True:
            attempt += 1
            try:
                response = await self._client.request(method, path, json=json, params=params)
            except httpx.TimeoutException as exc:
                if method in _IDEMPOTENT and attempt <= self._max_retries:
                    await self._backoff(attempt, reason="timeout")
                    continue
                raise CoolifyApiError(
                    f"{method} {path} timed out after {attempt} attempt(s)",
                    hint="Check COOLIFY_URL and that the instance is reachable.",
                ) from exc
            except httpx.HTTPError as exc:
                raise CoolifyApiError(f"{method} {path} failed: {exc}") from exc

            if response.status_code in _RETRY_STATUS and attempt <= self._max_retries:
                retry_after = _parse_retry_after(response)
                await self._backoff(attempt, reason=str(response.status_code), floor=retry_after)
                continue

            if response.status_code >= 400:
                raise _to_error(method, path, response)

            return response

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Perform the request and decode its JSON body."""
        response = await self._raw(method, path, json=json, params=params)
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise CoolifyApiError(
                f"{method} {path} returned non-JSON body",
                status_code=response.status_code,
                body=response.text[:200],
            ) from exc

    async def _backoff(self, attempt: int, *, reason: str, floor: float | None = None) -> None:
        # Full jitter: a fleet of retries must not synchronise into a thundering
        # herd against an instance that is already struggling.
        delay = min(2 ** (attempt - 1), 30.0)
        delay = random.uniform(0, delay)
        if floor is not None:
            delay = max(delay, floor)
        log.debug("api.retry", attempt=attempt, reason=reason, delay=round(delay, 2))
        await asyncio.sleep(delay)

    async def get(self, path: str, **params: Any) -> Any:
        return await self._request("GET", path, params=params or None)

    async def post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        return await self._request("POST", path, json=body)

    async def patch(self, path: str, body: dict[str, Any]) -> Any:
        return await self._request("PATCH", path, json=body)

    async def delete(self, path: str, **params: Any) -> Any:
        return await self._request("DELETE", path, params=params or None)

    # ── capability probe ─────────────────────────────────────────────────────

    async def probe_sensitive(self) -> tuple[bool, str]:
        """Probe the token's ability to read secrets. Returns ``(ok, reason)``.

        We cannot introspect a token's abilities through the API, so we observe
        the middleware's behaviour instead: ask for security keys and see whether
        ``private_key`` survives the sanitizer.

        THREE outcomes, not two — and conflating the last two is a lie that costs
        someone half an hour:

        * a key with ``private_key``      -> the token can read secrets
        * a key without it               -> the token definitely cannot
        * no keys at all                 -> we could not tell

        The last is still fail-closed, because an indeterminate probe must never
        read as a pass. But the *reason* has to be the true one, or the operator
        goes and re-issues a token that was never the problem.
        """
        if self._can_read_sensitive is not None:
            return self._can_read_sensitive, self._probe_reason

        keys = await self.get("/security/keys")
        if not isinstance(keys, list) or not keys:
            self._can_read_sensitive = False
            self._probe_reason = "indeterminate"
            log.warning("api.probe.indeterminate", reason="no ssh keys exist to probe against")
            return False, "indeterminate"

        ok = any("private_key" in k for k in keys if isinstance(k, dict))
        self._can_read_sensitive = ok
        self._probe_reason = "confirmed" if ok else "denied"
        return ok, self._probe_reason

    async def can_read_sensitive(self) -> bool:
        """Whether the token carries ``root`` or ``read:sensitive``.

        Fails closed on an indeterminate probe. See :meth:`probe_sensitive` for
        why the distinction matters.
        """
        ok, _ = await self.probe_sensitive()
        return ok

    async def assert_can_read_sensitive(self) -> None:
        """Fail closed unless the token can read secrets.

        Raises:
            InsufficientTokenScope: Always, if the probe fails. Never a warning:
                without this scope the API returns HTTP 200 with the secret keys
                simply absent, so the migration would silently produce a target
                whose environment variables are all empty and whose compose is
                missing entirely.
        """
        if await self.can_read_sensitive():
            return
        raise InsufficientTokenScope(
            "the Coolify API token cannot read sensitive data",
            hint=(
                "Create a token with `root` or `read:sensitive` in Coolify under "
                "Keys & Tokens > API tokens.\n"
                "Without it Coolify silently OMITS environment variable values, "
                "docker_compose_raw and database passwords from its responses — "
                "HTTP 200, no error, keys simply absent. Migrating with such a "
                "token would recreate every resource with empty secrets."
            ),
        )

    # ── reads ────────────────────────────────────────────────────────────────

    async def version(self) -> str:
        """The instance's Coolify version.

        Special-cased because ``GET /v1/version`` returns a BARE STRING —
        ``4.1.2``, not ``{"version": "4.1.2"}`` and not even a quoted JSON
        string. Every other endpoint returns JSON, so this one cannot go through
        the normal decode path.

        (This was found by the e2e rig against a real instance. The unit test
        mocked it as JSON — an assumption checked against itself.)
        """
        response = await self._raw("GET", "/version")
        text = response.text.strip().strip('"')
        if text.startswith("{"):
            # Tolerate a future version that returns a JSON object.
            try:
                parsed = response.json()
            except ValueError:
                return text
            if isinstance(parsed, dict):
                return str(parsed.get("version", text))
        return text

    async def list_servers(self) -> list[dict[str, Any]]:
        return _as_list(await self.get("/servers"))

    async def get_server(self, uuid: str) -> dict[str, Any]:
        return _as_dict(await self.get(f"/servers/{uuid}"))

    @staticmethod
    def server_is_reachable(server: dict[str, Any]) -> bool | None:
        """Whether Coolify can SSH to this server.

        NOT a top-level field: it lives under ``settings.is_reachable``. Servers
        are the one endpoint that eager-loads its settings relation
        (``server_by_uuid`` does ``$server->load(['settings'])``), and reading it
        from the top level silently yields None forever.

        Returns None when the key is absent — "we do not know" is not "no".
        """
        settings = server.get("settings")
        if isinstance(settings, dict) and "is_reachable" in settings:
            return bool(settings["is_reachable"])
        return None

    @staticmethod
    def server_is_coolify_host(server: dict[str, Any]) -> bool:
        """Whether Coolify itself runs on this server. Used by F2."""
        return bool(server.get("is_coolify_host"))

    async def list_projects(self) -> list[dict[str, Any]]:
        return _as_list(await self.get("/projects"))

    async def get_project(self, uuid: str) -> dict[str, Any]:
        return _as_dict(await self.get(f"/projects/{uuid}"))

    async def list_resources(self) -> list[dict[str, Any]]:
        return _as_list(await self.get("/resources"))

    async def get_resource(self, collection: str, uuid: str) -> dict[str, Any]:
        return _as_dict(await self.get(f"/{collection}/{uuid}"))

    async def get_storages(self, collection: str, uuid: str) -> dict[str, Any]:
        """``{"persistent_storages": [...], "file_storages": [...]}``.

        Note the shape: a two-key object, not a flat array.
        """
        return _as_dict(await self.get(f"/{collection}/{uuid}/storages"))

    async def get_envs(self, collection: str, uuid: str) -> list[dict[str, Any]]:
        """Environment variables — **requires** ``read:sensitive``.

        With a plain ``read`` token this returns entries whose ``value`` key is
        absent rather than empty. Callers must have called
        :meth:`assert_can_read_sensitive` first.
        """
        return _as_list(await self.get(f"/{collection}/{uuid}/envs"))

    # ── writes ───────────────────────────────────────────────────────────────

    async def set_envs_bulk(self, collection: str, uuid: str, entries: list[dict[str, Any]]) -> Any:
        """Upsert environment variables, matched by ``key``.

        The body key is literally ``data`` (``$request->get('data')``); anything
        else is a 400 "Bulk data is required.".
        """
        return await self.patch(f"/{collection}/{uuid}/envs/bulk", {"data": entries})

    async def create_storage(self, collection: str, uuid: str, body: dict[str, Any]) -> Any:
        return await self.post(f"/{collection}/{uuid}/storages", body)

    async def start(self, collection: str, uuid: str) -> Any:
        return await self.post(f"/{collection}/{uuid}/start")

    async def restart(self, collection: str, uuid: str) -> Any:
        """Bring a resource up, whatever Coolify believes its state is.

        Use this, not ``start``, to recover a source after a failed migration.
        ``/start`` guards on the status column::

            if (str($database->status)->contains('running')) {
                return response()->json(['message' => 'Database is already running.'], 400);
            }

        and that column is advanced by a background job, so it lags the daemon.
        After QUIESCE — which is ``docker stop`` then ``docker rm -f`` — the
        container is gone but the column can still read "running", and ``/start``
        then refuses to act while the source is in fact down. Swallowing that 400
        would be the worst possible move: it reports the source recovered while
        leaving it dead.

        ``/restart`` (``action_restart``) carries no such guard — it dispatches
        Restart* unconditionally, which for a removed container simply deploys
        it. This is why the rollback path that ends the outage uses restart.
        """
        return await self.post(f"/{collection}/{uuid}/restart")

    async def stop(self, collection: str, uuid: str) -> bool:
        """Request a stop. Returns whether Coolify actually dispatched one.

        A False return is not a failure — it is Coolify declining to act because
        it believes the resource is already down. The caller must decide whether
        to believe it (see the note below); it is never grounds to proceed as if
        the stack were quiesced.

        **This returns before anything has stopped** — every stop endpoint is a
        `dispatch(...)`. Worse, for applications it does NOT stop preview
        containers (``StopApplication`` filters ``pullRequestId=0``), and for
        services it stops containers named from DB records rather than by label,
        so a compose container Coolify never parsed is never stopped.

        Callers MUST verify with the label-based quiesce gate in
        ``discovery.quiesce``; never trust this call's return.

        The False case is a 400, and it deserves precision because treating it as
        an error and treating it as success are both wrong. Coolify decides it
        from a database column::

            if (str($database->status)->contains('stopped') ||
                str($database->status)->contains('exited')) {
                return response()->json(['message' => 'Database is already stopped.'], 400);
            }
            StopDatabase::dispatch($database, $dockerCleanup);   // never reached

        The column defaults to 'exited' and is advanced by a background job, so
        it lags the daemon — right after a deploy it reads "exited" about a
        container that is serving traffic. Raising here aborts a migration over a
        stale row.

        But note where the `return` sits: **no stop is dispatched**. So a caller
        that shrugs this off and waits for the containers to exit waits for
        something nobody asked for, and burns its whole gate timeout doing it.
        Hence bool rather than a swallowed exception — the caller has to look.
        """
        try:
            await self.post(f"/{collection}/{uuid}/stop")
            return True
        except CoolifyApiError as exc:
            # Matched on the body, not the message: the wording differs per kind
            # ("Database is already stopped.", "Service is already stopped.", ...)
            # but all of them carry this phrase.
            if exc.status_code == 400 and "already stopped" in str(exc.body).lower():
                log.debug("api.stop.refused_as_stopped", collection=collection, uuid=uuid)
                return False
            raise

    async def delete_resource(
        self, collection: str, uuid: str, *, delete_volumes: bool = False
    ) -> Any:
        return await self.delete(
            f"/{collection}/{uuid}",
            deleteVolumes="true" if delete_volumes else "false",
        )

    async def update_resource(self, collection: str, uuid: str, body: dict[str, Any]) -> Any:
        return await self.patch(f"/{collection}/{uuid}", body)


def _parse_retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _to_error(method: str, path: str, response: httpx.Response) -> CoolifyApiError:
    try:
        body = response.json()
    except ValueError:
        body = response.text[:400]

    hint: str | None = None
    message = str(body.get("message", "")) if isinstance(body, dict) else str(body)

    if response.status_code == 401:
        hint = "Check COOLIFY_TOKEN. Coolify tokens are instance-specific."
    elif response.status_code == 403:
        # Coolify returns 403 for BOTH "your token cannot do this" and "the API
        # is switched off instance-wide", and the two need opposite fixes.
        # Guessing sends the operator to re-issue a token that was never the
        # problem. It tells us which in the body — read it.
        if "API is disabled" in message:
            hint = (
                "The instance has its API switched off. Enable it in Coolify under "
                "Settings > API, or call GET /api/v1/enable with a write token.\n"
                "This is instance-wide and off by default; your token is fine."
            )
        else:
            hint = "The token lacks the ability for this call (needs write/deploy, or root)."
    elif response.status_code == 422:
        # Coolify names the offending field, which is the single most useful
        # thing when a whitelist in api/fields.py has drifted from upstream.
        hint = (
            "Coolify rejected a field. If this says 'This field is not allowed.', "
            "our request whitelist in api/fields.py has drifted from upstream — "
            "please open an issue with the field name."
        )
    elif response.status_code == 404:
        hint = "Resource not found, or the API is disabled for this instance."

    return CoolifyApiError(
        f"{method} {path} failed",
        status_code=response.status_code,
        body=body,
        hint=hint,
    )


def _as_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [v for v in value if isinstance(v, dict)]
    raise CoolifyApiError(f"expected a JSON array, got {type(value).__name__}")


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    raise CoolifyApiError(f"expected a JSON object, got {type(value).__name__}")
