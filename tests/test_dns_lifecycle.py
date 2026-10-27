"""The DNS side of a domain's life: baseline at PLAN, the gate, CREATE, rollback.

* PLAN resolves every custom host the sources serve and journals the answer
  (hosts, addresses, TTL, nameserver names — never a secret).
* The gate blocks a host on neither server (ELSEWHERE) for every provider, and
  one that resolved at plan time but no longer does (VANISHED), until the
  operator accepts that host by name; ``--accept-dns`` answers a cutover only.
* CREATE re-checks, while the source still serves, that DNS still sends its
  custom hosts to it — and refuses with zero downtime when not.
* A rollback re-resolves at its end and lists the hosts to point back.

Every lookup here is injected: the autouse ``_offline_dns`` fixture keeps the
real resolver out of unit tests.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

import httpx
import pytest
import respx

from bg_coolify_migrate.api.client import CoolifyClient
from bg_coolify_migrate.dns import managed
from bg_coolify_migrate.dns.extract import Hostname, HostnameOrigin
from bg_coolify_migrate.dns.gate import Resolution
from bg_coolify_migrate.domain import domains
from bg_coolify_migrate.domain.kinds import ResourceKind
from bg_coolify_migrate.domain.plan import (
    HostBaseline,
    MigrationPlan,
    ResourcePlan,
    ResourceSnapshot,
    ServerRef,
    Strategy,
)
from bg_coolify_migrate.domain.statemachine import Outcome, State
from bg_coolify_migrate.engine import planner, runner, steps
from bg_coolify_migrate.engine.context import MigrationContext, dns_baseline_of
from bg_coolify_migrate.engine.executor import RunResult
from bg_coolify_migrate.errors import DnsGateBlocked, PreflightError
from bg_coolify_migrate.journal.store import Journal
from bg_coolify_migrate.settings.base import Settings
from tests.test_rollback_domains_engine import _host, _mock_create_routes, _mock_rest_of_create

HOST = "https://coolify.example.com"
BASE = f"{HOST}/api/v1"
SHOP = "https://shop.example.com"
SOURCE_IP = "10.0.0.1"
TARGET_IP = "10.0.0.2"
SRC_WC = "app.src.example"
TGT_WC = "app.tgt.example"


def _app() -> ResourceSnapshot:
    return ResourceSnapshot(
        uuid="app1", name="shop", collection="applications", kind=ResourceKind.APP_DOCKERIMAGE
    )


def _plan(*baseline: HostBaseline, **servers: ServerRef) -> MigrationPlan:
    return MigrationPlan(
        project="shop",
        environment="production",
        source_server=servers.get("source", ServerRef(uuid="s1", name="old", ip=SOURCE_IP)),
        target_server=servers.get("target", ServerRef(uuid="s2", name="new", ip=TARGET_IP)),
        resources=(ResourcePlan(snapshot=_app(), strategy=Strategy.COPY_DATA),),
        dns_baseline=baseline,
    )


@pytest.fixture
async def ctx(tmp_path: Path):  # type: ignore[no-untyped-def]
    api = CoolifyClient(HOST, "tok", max_retries=0)
    context = MigrationContext(
        api=api,
        settings=Settings(_env_file=None, state_dir=tmp_path),
        plan=_plan(),
        journal=Journal.create(tmp_path, "m1"),
        migration_id="m1",
        source_host=_host(),  # type: ignore[arg-type]
        target_host=_host(),  # type: ignore[arg-type]
    )
    yield context
    await api.aclose()


def _answers(monkeypatch: pytest.MonkeyPatch, answers: dict[str, object]) -> list[str]:
    """Every lookup answers from ``answers`` (an address, or an error string)."""
    asked: list[str] = []

    async def one(hostname: Hostname, config: object = None) -> Resolution:
        asked.append(hostname.host)
        answer = answers.get(hostname.host)
        if isinstance(answer, tuple):
            return Resolution(hostname, answer)
        if isinstance(answer, str) and answer[:1].isdigit():
            return Resolution(hostname, (answer,), ttl=300)
        return Resolution(hostname, (), error=str(answer or "NXDOMAIN"))

    async def many(hostnames: Iterable[Hostname], config: object = None) -> list[Resolution]:
        return [await one(h) for h in hostnames]

    monkeypatch.setattr("bg_coolify_migrate.dns.resolve.resolve_one", one)
    monkeypatch.setattr("bg_coolify_migrate.dns.resolve.resolve_all", many)
    return asked


def _target_serves(respx_mock: respx.Router, fqdn: str = SHOP) -> None:
    respx_mock.get(f"{BASE}/applications/appT").mock(
        return_value=httpx.Response(200, json={"uuid": "appT", "fqdn": fqdn})
    )
    respx_mock.get(f"{BASE}/applications/appT/envs").mock(return_value=httpx.Response(200, json=[]))


# ── pure: where a custom host lives, whose zone it is ───────────────────────


class TestSourceCustomHosts:
    def test_an_application(self) -> None:
        full = {"fqdn": f"{SHOP},https://x.{SRC_WC},http://a.1.2.3.4.sslip.io"}
        assert domains.source_custom_hosts(_app(), full, source_wildcard=SRC_WC) == [
            "shop.example.com"
        ]

    def test_a_service_with_a_route_key_of_its_own_image(self) -> None:
        snapshot = ResourceSnapshot(
            uuid="s", name="s3", collection="services", kind=ResourceKind.SERVICE_TEMPLATE
        )
        full = {
            "applications": [
                {"name": "minio", "image": "pgsty/silo:1", "fqdn": None},
                {"name": "web", "url": "https://w.example.com"},
                "junk",
            ]
        }
        envs = [
            {"key": "MINIO_SERVER_URL", "value": "https://s3.example.com"},
            {"key": "LOGTO_ENDPOINT", "value": "https://auth.example.com"},
        ]
        assert domains.source_custom_hosts(snapshot, full, envs, source_wildcard=None) == [
            "w.example.com",
            "s3.example.com",
        ]

    def test_a_database_serves_none(self) -> None:
        snapshot = ResourceSnapshot(
            uuid="d", name="pg", collection="databases", kind=ResourceKind.DATABASE
        )
        assert domains.source_custom_hosts(snapshot, {"fqdn": SHOP}, source_wildcard=None) == []


class TestManaged:
    @pytest.mark.parametrize(
        ("ns", "cname", "expected"),
        [
            (("ada.ns.cloudflare.com",), (), True),
            (("BOB.NS.CLOUDFLARE.COM.",), (), True),
            ((), ("shop.example.com.cdn.cloudflare.net",), True),
            (("ns1.hetzner.com",), (), False),
            ((), (), False),
        ],
    )
    def test_on_cloudflare(self, ns: tuple[str, ...], cname: tuple[str, ...], expected: bool) -> None:
        assert managed.on_cloudflare(ns, cname) is expected

    def test_coolify_manages_from_4_4_and_fails_toward_it(self) -> None:
        cf = ("ada.ns.cloudflare.com",)
        assert managed.coolify_may_manage(cf, version=(4, 4, 0))
        assert not managed.coolify_may_manage(cf, version=(4, 3, 24))
        assert managed.coolify_may_manage(cf, version=None)
        assert not managed.coolify_may_manage(("ns1.x.com",), version=(4, 5, 0))


# ── PLAN: the baseline, journalled ──────────────────────────────────────────


class TestBaseline:
    async def test_the_planner_resolves_every_custom_host_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def resolved(hosts: list[str]) -> dict[str, Resolution]:
            return {
                h: Resolution(
                    Hostname(h, HostnameOrigin.FQDN, False),
                    (SOURCE_IP,),
                    ttl=60,
                    nameservers=("ada.ns.cloudflare.com",),
                )
                for h in hosts
            }

        monkeypatch.setattr("bg_coolify_migrate.dns.resolve.resolve_hosts", resolved)
        baseline = await planner.dns_baseline(
            [(_app(), {"fqdn": f"{SHOP},https://x.{SRC_WC}"}, [])], source_wildcard=SRC_WC
        )
        assert baseline == (
            HostBaseline(
                host="shop.example.com",
                resource="shop",
                addresses=(SOURCE_IP,),
                ttl=60,
                nameservers=("ada.ns.cloudflare.com",),
            ),
        )

    async def test_no_custom_host_resolves_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        asked = _answers(monkeypatch, {})
        assert await planner.dns_baseline([(_app(), {}, [])], source_wildcard=None) == ()
        assert asked == []

    async def test_a_resolver_failure_is_no_baseline_not_a_refusal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def broken(hosts: list[str]) -> dict[str, Resolution]:
            raise RuntimeError("no network")

        monkeypatch.setattr("bg_coolify_migrate.dns.resolve.resolve_hosts", broken)
        assert await planner.dns_baseline([(_app(), {"fqdn": SHOP}, [])], source_wildcard=None) == ()

    async def test_plan_journals_it_without_secrets(self, ctx: MigrationContext) -> None:
        ctx.plan = _plan(HostBaseline(host="shop.example.com", addresses=(SOURCE_IP,), ttl=60))
        detail = await steps.step_plan(ctx)
        ctx.journal.append("step_completed", state="plan", detail=detail)
        assert detail["dns_baseline"]["shop.example.com"]["addresses"] == [SOURCE_IP]
        assert dns_baseline_of(ctx.journal, _plan())["shop.example.com"].addresses == (SOURCE_IP,)

    def test_the_journal_wins_and_junk_is_skipped(self, ctx: MigrationContext) -> None:
        ctx.journal.append(
            "step_completed",
            state="plan",
            detail={
                "dns_baseline": {
                    "a.example.com": {"addresses": ["1.1.1.1"], "ttl": "x"},
                    "b.example.com": "junk",
                }
            },
        )
        fallback = _plan(HostBaseline(host="c.example.com"))
        out = dns_baseline_of(ctx.journal, fallback)
        assert list(out) == ["a.example.com"] and out["a.example.com"].ttl is None

    def test_without_a_journalled_baseline_the_plan_answers(self, ctx: MigrationContext) -> None:
        plan = _plan(HostBaseline(host="c.example.com"))
        assert list(dns_baseline_of(ctx.journal, plan)) == ["c.example.com"]


# ── the gate ─────────────────────────────────────────────────────────────────


class TestGate:
    async def test_elsewhere_blocks_even_with_accept_dns(
        self, ctx: MigrationContext, respx_mock: respx.Router, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx.target_uuids["app1"] = "appT"
        ctx.accept_dns = True
        _target_serves(respx_mock)
        _answers(monkeypatch, {"shop.example.com": "104.21.1.1"})
        with pytest.raises(DnsGateBlocked) as exc:
            await steps.step_dns_gate(ctx)
        assert "shop.example.com (elsewhere)" in str(exc.value)
        assert "--accept-dns-host shop.example.com" in (exc.value.hint or "")

    async def test_an_accepted_host_passes_and_is_journalled(
        self, ctx: MigrationContext, respx_mock: respx.Router, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx.target_uuids["app1"] = "appT"
        ctx.accepted_hosts = frozenset({"shop.example.com"})
        _target_serves(respx_mock)
        _answers(monkeypatch, {"shop.example.com": "104.21.1.1"})
        detail = await steps.step_dns_gate(ctx)
        assert detail["accepted"] == ["shop.example.com"]
        assert detail["ambiguous"] == ["shop.example.com"]
        assert detail["cutover_accepted"] == []

    async def test_a_vanished_host_blocks(
        self, ctx: MigrationContext, respx_mock: respx.Router, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx.target_uuids["app1"] = "appT"
        ctx.plan = _plan(HostBaseline(host="shop.example.com", addresses=(SOURCE_IP,)))
        ctx.accept_dns = True
        _target_serves(respx_mock)
        _answers(monkeypatch, {"shop.example.com": "NXDOMAIN"})
        with pytest.raises(DnsGateBlocked, match=r"shop\.example\.com \(vanished\)") as exc:
            await steps.step_dns_gate(ctx)
        assert f"resolved to {SOURCE_IP} at plan time" in (exc.value.hint or "")

    async def test_a_lookup_that_times_out_blocks_without_calling_it_deleted(
        self, ctx: MigrationContext, respx_mock: respx.Router, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx.target_uuids["app1"] = "appT"
        ctx.plan = _plan(HostBaseline(host="shop.example.com", addresses=(SOURCE_IP,)))
        ctx.accept_dns = True
        _target_serves(respx_mock)
        _answers(monkeypatch, {"shop.example.com": "LifetimeTimeout"})
        with pytest.raises(DnsGateBlocked, match=r"shop\.example\.com \(unanswered\)") as exc:
            await steps.step_dns_gate(ctx)
        hint = exc.value.hint or ""
        assert "could not be asked" in hint and "recreate" not in hint

    async def test_a_host_unresolved_at_plan_time_too_passes(
        self, ctx: MigrationContext, respx_mock: respx.Router, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx.target_uuids["app1"] = "appT"
        _target_serves(respx_mock)
        _answers(monkeypatch, {"shop.example.com": "NXDOMAIN"})
        detail = await steps.step_dns_gate(ctx)
        assert detail["vanished"] == [] and detail["hostnames"] == 1

    async def test_the_wildcard_base_counts_as_the_server(
        self, ctx: MigrationContext, respx_mock: respx.Router, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The server's `ip` is private; its wildcard base resolves to its PUBLIC
        # address, which is what the custom record points at.
        ctx.plan = _plan(
            source=ServerRef(uuid="s1", name="old", ip="192.168.1.5", wildcard_domain=SRC_WC),
            target=ServerRef(uuid="s2", name="new", ip="192.168.1.6", wildcard_domain=TGT_WC),
        )
        ctx.target_uuids["app1"] = "appT"
        _target_serves(respx_mock)
        _answers(
            monkeypatch,
            {
                "shop.example.com": "203.0.113.5",
                f"bg-coolify-migrate-probe.{SRC_WC}": "203.0.113.5",
                f"bg-coolify-migrate-probe.{TGT_WC}": "203.0.113.6",
            },
        )
        with pytest.raises(DnsGateBlocked, match=r"shop\.example\.com \(cutover_needed\)"):
            await steps.step_dns_gate(ctx)

    async def test_a_wildcard_both_servers_share_proves_neither(
        self, ctx: MigrationContext, respx_mock: respx.Router, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Both wildcards resolve to the source's address. Counted for the target
        # too, a custom host still on the source read as READY and passed.
        ctx.plan = _plan(
            source=ServerRef(uuid="s1", name="old", ip="203.0.113.5", wildcard_domain=SRC_WC),
            target=ServerRef(uuid="s2", name="new", ip="198.51.100.6", wildcard_domain=TGT_WC),
        )
        ctx.target_uuids["app1"] = "appT"
        ctx.accept_dns = True
        _target_serves(respx_mock)
        _answers(
            monkeypatch,
            {
                "shop.example.com": "203.0.113.5",
                f"bg-coolify-migrate-probe.{SRC_WC}": "203.0.113.5",
                f"bg-coolify-migrate-probe.{TGT_WC}": "203.0.113.5",
            },
        )
        with pytest.raises(DnsGateBlocked, match=r"shop\.example\.com \(elsewhere\)") as exc:
            await steps.step_dns_gate(ctx)
        assert "both servers answer as" in (exc.value.hint or "")
        assert "is 198.51.100.6" in (exc.value.hint or "")

    async def test_a_cloudflare_zone_gets_the_cloudflare_line_from_4_4(
        self, ctx: MigrationContext, respx_mock: respx.Router, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx.target_uuids["app1"] = "appT"
        ctx.journal.append("step_completed", state="preflight", detail={"coolify_version": "4.4.2"})
        ctx.plan = _plan(
            HostBaseline(
                host="shop.example.com",
                addresses=(SOURCE_IP,),
                nameservers=("ada.ns.cloudflare.com",),
            )
        )
        _target_serves(respx_mock)
        _answers(monkeypatch, {"shop.example.com": SOURCE_IP})
        with pytest.raises(DnsGateBlocked) as exc:
            await steps.step_dns_gate(ctx)
        assert "Cloudflare zone: change the record at Cloudflare" in (exc.value.hint or "")


# ── CREATE: DNS must still send the custom hosts to the serving source ──────


class TestCreateRecheck:
    def _baseline(self, ctx: MigrationContext) -> None:
        ctx.plan = _plan(HostBaseline(host="shop.example.com", addresses=(SOURCE_IP,)))

    async def test_a_host_still_on_the_source_passes(
        self, ctx: MigrationContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._baseline(ctx)
        _answers(monkeypatch, {"shop.example.com": SOURCE_IP})
        await steps._refuse_moved_dns(ctx)

    @pytest.mark.parametrize("now", ["104.21.1.1", TARGET_IP, "NXDOMAIN"])
    async def test_a_host_that_moved_or_vanished_refuses(
        self, ctx: MigrationContext, monkeypatch: pytest.MonkeyPatch, now: str
    ) -> None:
        self._baseline(ctx)
        _answers(monkeypatch, {"shop.example.com": now})
        with pytest.raises(PreflightError, match=r"shop\.example\.com") as exc:
            await steps._refuse_moved_dns(ctx)
        assert "never stopped" in (exc.value.hint or "")

    async def test_a_timeout_proves_nothing(
        self, ctx: MigrationContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._baseline(ctx)
        _answers(monkeypatch, {"shop.example.com": "LifetimeTimeout"})
        await steps._refuse_moved_dns(ctx)

    async def test_a_host_that_was_never_on_the_source_is_not_checked(
        self, ctx: MigrationContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx.plan = _plan(HostBaseline(host="cdn.example.com", addresses=("104.21.1.1",)))
        asked = _answers(monkeypatch, {})
        await steps._refuse_moved_dns(ctx)
        assert "cdn.example.com" not in asked

    async def test_create_refuses_and_deletes_its_target(
        self, ctx: MigrationContext, respx_mock: respx.Router, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._baseline(ctx)
        _answers(monkeypatch, {"shop.example.com": "NXDOMAIN"})
        _mock_create_routes(respx_mock)
        _mock_rest_of_create(respx_mock)
        respx_mock.post(f"{BASE}/applications/dockerimage").mock(
            return_value=httpx.Response(201, json={"uuid": "appT"})
        )
        respx_mock.patch(f"{BASE}/applications/appT").mock(return_value=httpx.Response(200, json={}))
        respx_mock.get(f"{BASE}/applications/appT").mock(
            return_value=httpx.Response(200, json={"uuid": "appT", "fqdn": SHOP})
        )
        delete = respx_mock.delete(f"{BASE}/applications/appT").mock(
            return_value=httpx.Response(200, json={})
        )
        with pytest.raises(PreflightError, match="no longer sends"):
            await steps.step_create_target(ctx)
        assert delete.called


# ── the end of every rollback ────────────────────────────────────────────────


class TestRollbackRecheck:
    async def test_hosts_now_on_the_target_and_vanished_ones_are_listed(
        self, ctx: MigrationContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx.plan = _plan(
            HostBaseline(host="shop.example.com", addresses=(SOURCE_IP,)),
            HostBaseline(host="gone.example.com", addresses=(SOURCE_IP,)),
            HostBaseline(host="still.example.com", addresses=(SOURCE_IP,)),
        )
        _answers(
            monkeypatch,
            {
                "shop.example.com": TARGET_IP,
                "gone.example.com": "NXDOMAIN",
                "still.example.com": SOURCE_IP,
            },
        )
        result = await runner._with_rollback_notes(
            RunResult(outcome=Outcome.ROLLED_BACK, reached=State.INIT), ctx
        )
        (note,) = result.notes
        assert f"point DNS for shop.example.com back to {SOURCE_IP}" in note
        assert f"recreate the DNS record of gone.example.com -> {SOURCE_IP}" in note
        assert "still.example.com" not in note

    async def test_an_address_both_servers_share_is_no_reason_to_flip_back(
        self, ctx: MigrationContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx.plan = _plan(
            HostBaseline(host="shop.example.com", addresses=("104.21.1.1",)),
            source=ServerRef(uuid="s1", name="old", ip=SOURCE_IP, wildcard_domain=SRC_WC),
            target=ServerRef(uuid="s2", name="new", ip=TARGET_IP, wildcard_domain=TGT_WC),
        )
        _answers(
            monkeypatch,
            {
                "shop.example.com": "104.21.1.1",
                f"bg-coolify-migrate-probe.{SRC_WC}": "104.21.1.1",
                f"bg-coolify-migrate-probe.{TGT_WC}": "104.21.1.1",
            },
        )
        result = await runner._with_rollback_notes(
            RunResult(outcome=Outcome.ROLLED_BACK, reached=State.INIT), ctx
        )
        assert not any("shop.example.com" in note for note in result.notes)

    async def test_dns_that_cannot_be_asked_adds_nothing(
        self, ctx: MigrationContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx.plan = _plan(HostBaseline(host="shop.example.com", addresses=(SOURCE_IP,)))

        async def broken(hosts: list[str]) -> dict[str, Resolution]:
            raise RuntimeError("resolver down")

        monkeypatch.setattr("bg_coolify_migrate.dns.resolve.resolve_hosts", broken)
        result = await runner._with_rollback_notes(
            RunResult(outcome=Outcome.ROLLED_BACK, reached=State.INIT), ctx
        )
        assert result.notes == []

    async def test_an_accepted_host_is_flipped_back_too(
        self, ctx: MigrationContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx.journal.append(
            "step_completed", state="dns_gate", detail={"accepted": ["cdn.example.com"]}
        )
        _answers(monkeypatch, {"cdn.example.com": "104.21.1.1"})
        result = await runner._with_rollback_notes(
            RunResult(outcome=Outcome.ROLLBACK_FAILED, reached=State.INIT), ctx
        )
        assert "cdn.example.com" in result.notes[0]


# ── CLI: per-host acceptance ─────────────────────────────────────────────────


class TestAcceptDnsHost:
    def test_values_are_normalised(self) -> None:
        from bg_coolify_migrate.cli import _accepted_hosts

        assert _accepted_hosts(["https://Shop.Example.com/", "cdn.example.com"]) == frozenset(
            {"shop.example.com", "cdn.example.com"}
        )
        assert _accepted_hosts(None) == frozenset()

    def test_a_non_host_is_refused_before_anything_runs(self) -> None:
        import typer

        from bg_coolify_migrate.cli import _accepted_hosts

        with pytest.raises(typer.BadParameter):
            _accepted_hosts(["not a host"])


def test_journal_detail_of_the_baseline_is_plain_json() -> None:
    from bg_coolify_migrate.engine.context import serialise_dns_baseline

    out = serialise_dns_baseline((HostBaseline(host="a.b.c", addresses=("1.2.3.4",)),))
    assert json.loads(json.dumps(out))["a.b.c"]["addresses"] == ["1.2.3.4"]
