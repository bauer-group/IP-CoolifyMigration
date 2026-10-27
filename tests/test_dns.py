"""Tests for hostname extraction and the DNS gate.

Extraction has to find hostnames in five different places; a missed one means the
gate passes while a live domain still points at the old server.
"""

from __future__ import annotations

import json

import pytest

from bg_coolify_migrate.dns.extract import (
    Hostname,
    HostnameOrigin,
    collect,
    from_compose_domains,
    from_env,
    from_fqdn,
    from_labels,
    from_traefik_rule,
    is_generated,
    normalise_host,
    real_hostnames,
)
from bg_coolify_migrate.dns.gate import (
    HostVerdict,
    Resolution,
    Verdict,
    build_report,
    classify,
    explain_why_blocking_matters,
)

SOURCE = frozenset({"10.0.0.1"})
TARGET = frozenset({"10.0.0.2"})


def _h(host: str, *, generated: bool = False) -> Hostname:
    return Hostname(host=host, origin=HostnameOrigin.FQDN, is_generated=generated)


class TestNormaliseHost:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("a.example.com", "a.example.com"),
            ("https://a.example.com", "a.example.com"),
            ("http://a.example.com/path", "a.example.com"),
            ("https://a.example.com:8443/x?y=1", "a.example.com"),
            ("A.EXAMPLE.COM", "a.example.com"),
            ("a.example.com.", "a.example.com"),
            ("  a.example.com  ", "a.example.com"),
        ],
    )
    def test_normalises(self, raw: str, expected: str) -> None:
        assert normalise_host(raw) == expected

    @pytest.mark.parametrize("raw", ["", "   ", "*.example.com", "/just/a/path", "localhost", "nodot"])
    def test_rejects_unusable(self, raw: str) -> None:
        assert normalise_host(raw) is None


class TestIsGenerated:
    @pytest.mark.parametrize(
        "host", ["1-2-3-4.sslip.io", "app.10-0-0-1.nip.io", "x.traefik.me"]
    )
    def test_generated_hostnames(self, host: str) -> None:
        # These encode the server IP and follow the server, so they must never
        # gate a cutover.
        assert is_generated(host) is True

    def test_real_hostname(self) -> None:
        assert is_generated("shop.example.com") is False


class TestFromFqdn:
    def test_single(self) -> None:
        (host,) = from_fqdn("https://shop.example.com")
        assert host.host == "shop.example.com"
        assert host.origin is HostnameOrigin.FQDN

    def test_comma_separated(self) -> None:
        hosts = from_fqdn("https://a.example.com,https://b.example.com")
        assert [h.host for h in hosts] == ["a.example.com", "b.example.com"]

    def test_none_and_empty(self) -> None:
        assert from_fqdn(None) == []
        assert from_fqdn("") == []


class TestFromComposeDomains:
    def test_nested_json_shape(self) -> None:
        raw = json.dumps({"web": {"domain": "https://a.example.com,https://b.example.com"}})
        hosts = from_compose_domains(raw)
        assert {h.host for h in hosts} == {"a.example.com", "b.example.com"}
        assert all(h.origin is HostnameOrigin.COMPOSE_DOMAINS for h in hosts)

    def test_list_shape(self) -> None:
        raw = json.dumps([{"domain": "https://a.example.com"}])
        assert [h.host for h in from_compose_domains(raw)] == ["a.example.com"]

    def test_malformed_json_yields_nothing_rather_than_crashing(self) -> None:
        assert from_compose_domains("{not json") == []

    def test_none(self) -> None:
        assert from_compose_domains(None) == []


class TestFromEnv:
    def test_service_fqdn_var(self) -> None:
        # `services` has no fqdn column; SERVICE_FQDN_* is often the ONLY record
        # of a service's domain.
        hosts = from_env([{"key": "SERVICE_FQDN_MINIO", "value": "https://s3.example.com"}])
        assert [h.host for h in hosts] == ["s3.example.com"]
        assert hosts[0].origin is HostnameOrigin.SERVICE_ENV

    def test_service_url_var(self) -> None:
        hosts = from_env([{"key": "SERVICE_URL_APP", "value": "https://app.example.com"}])
        assert [h.host for h in hosts] == ["app.example.com"]

    def test_ignores_unrelated_vars(self) -> None:
        assert from_env([{"key": "DATABASE_URL", "value": "postgres://x/y"}]) == []

    def test_falls_back_to_real_value(self) -> None:
        # real_value resolves Coolify's magic variables.
        hosts = from_env([{"key": "SERVICE_FQDN_X", "real_value": "https://x.example.com"}])
        assert [h.host for h in hosts] == ["x.example.com"]

    def test_ignores_non_string_values(self) -> None:
        assert from_env([{"key": "SERVICE_FQDN_X", "value": None}]) == []


class TestFromTraefikRule:
    def test_single_host(self) -> None:
        hosts = from_traefik_rule("Host(`shop.example.com`)")
        assert [h.host for h in hosts] == ["shop.example.com"]

    def test_multiple_hosts_in_one_rule(self) -> None:
        hosts = from_traefik_rule("Host(`a.example.com`, `b.example.com`)")
        assert {h.host for h in hosts} == {"a.example.com", "b.example.com"}

    def test_host_with_path_prefix(self) -> None:
        hosts = from_traefik_rule("Host(`a.example.com`) && PathPrefix(`/api`)")
        assert [h.host for h in hosts] == ["a.example.com"]

    def test_or_joined_rules(self) -> None:
        hosts = from_traefik_rule("Host(`a.example.com`) || Host(`b.example.com`)")
        assert {h.host for h in hosts} == {"a.example.com", "b.example.com"}

    def test_hostsni(self) -> None:
        assert [h.host for h in from_traefik_rule("HostSNI(`db.example.com`)")] == [
            "db.example.com"
        ]

    def test_no_host_matcher(self) -> None:
        assert from_traefik_rule("PathPrefix(`/api`)") == []


class TestFromLabels:
    def test_traefik_router_rule_label(self) -> None:
        hosts = from_labels({"traefik.http.routers.web.rule": "Host(`a.example.com`)"})
        assert [h.host for h in hosts] == ["a.example.com"]
        assert hosts[0].origin is HostnameOrigin.TRAEFIK_LABEL

    def test_caddy_label(self) -> None:
        # Coolify supports both proxies.
        hosts = from_labels({"caddy": "https://a.example.com"})
        assert [h.host for h in hosts] == ["a.example.com"]

    def test_ignores_other_labels(self) -> None:
        assert from_labels({"coolify.managed": "true"}) == []


class TestCollect:
    def test_merges_all_sources(self) -> None:
        hosts = collect(
            fqdn="https://a.example.com",
            compose_domains=json.dumps({"web": {"domain": "https://b.example.com"}}),
            envs=[{"key": "SERVICE_FQDN_X", "value": "https://c.example.com"}],
            labels={"traefik.http.routers.r.rule": "Host(`d.example.com`)"},
        )
        assert {h.host for h in hosts} == {
            "a.example.com",
            "b.example.com",
            "c.example.com",
            "d.example.com",
        }

    def test_deduplicates_across_sources(self) -> None:
        hosts = collect(
            fqdn="https://a.example.com",
            labels={"traefik.http.routers.r.rule": "Host(`a.example.com`)"},
        )
        assert len(hosts) == 1

    def test_sorted_for_stable_reports(self) -> None:
        hosts = collect(fqdn="https://z.example.com,https://a.example.com")
        assert [h.host for h in hosts] == ["a.example.com", "z.example.com"]

    def test_empty(self) -> None:
        assert collect() == []


class TestRealHostnames:
    def test_filters_generated(self) -> None:
        hosts = collect(fqdn="https://shop.example.com,https://1-2-3-4.sslip.io")
        assert [h.host for h in real_hostnames(hosts)] == ["shop.example.com"]


class TestClassify:
    def test_points_at_source_blocks(self) -> None:
        verdict = classify(
            Resolution(_h("a.example.com"), ("10.0.0.1",), ttl=300),
            source_ips=SOURCE,
            target_ips=TARGET,
        )
        assert verdict.verdict is Verdict.CUTOVER_NEEDED
        assert verdict.blocks is True

    def test_points_at_target_is_ready(self) -> None:
        verdict = classify(
            Resolution(_h("a.example.com"), ("10.0.0.2",)), source_ips=SOURCE, target_ips=TARGET
        )
        assert verdict.verdict is Verdict.READY
        assert verdict.blocks is False

    def test_points_at_both_is_ready_mid_cutover(self) -> None:
        verdict = classify(
            Resolution(_h("a.example.com"), ("10.0.0.1", "10.0.0.2")),
            source_ips=SOURCE,
            target_ips=TARGET,
        )
        assert verdict.verdict is Verdict.READY
        assert "BOTH" in verdict.detail

    def test_points_elsewhere_blocks_for_every_provider(self) -> None:
        # A CDN, proxy or load balancer in front means DNS cannot tell us where
        # the origin points - it may still be the source (invariant 7). Blocks
        # until the operator accepts that one host.
        verdict = classify(
            Resolution(_h("a.example.com"), ("104.21.1.1",)), source_ips=SOURCE, target_ips=TARGET
        )
        assert verdict.verdict is Verdict.ELSEWHERE
        assert verdict.blocks is True
        assert "CDN" in verdict.detail

    def test_a_host_that_resolved_at_plan_time_and_no_longer_does_vanished(self) -> None:
        verdict = classify(
            Resolution(_h("a.example.com"), (), error="NXDOMAIN"),
            source_ips=SOURCE,
            target_ips=TARGET,
            baseline=("10.0.0.1",),
        )
        assert verdict.verdict is Verdict.VANISHED
        assert verdict.blocks is True
        assert verdict.baseline == ("10.0.0.1",)
        assert "10.0.0.1 at plan time" in verdict.detail

    @pytest.mark.parametrize("error", ["LifetimeTimeout", "NoNameservers", None])
    def test_a_failed_lookup_is_no_deletion_but_still_blocks(self, error: str | None) -> None:
        # A timeout says nothing about the record: not VANISHED ("recreate it"),
        # yet it blocks — the source is stopped and nothing shows where it points.
        verdict = classify(
            Resolution(_h("a.example.com"), (), error=error),
            source_ips=SOURCE,
            target_ips=TARGET,
            baseline=("10.0.0.1",),
        )
        assert verdict.verdict is Verdict.UNANSWERED
        assert verdict.blocks is True
        assert verdict.baseline == ("10.0.0.1",)
        assert "could not be asked" in verdict.detail

    def test_a_failed_lookup_without_a_baseline_stays_unresolved(self) -> None:
        verdict = classify(
            Resolution(_h("a.example.invalid"), (), error="LifetimeTimeout"),
            source_ips=SOURCE,
            target_ips=TARGET,
        )
        assert verdict.verdict is Verdict.UNRESOLVED
        assert verdict.blocks is False

    def test_unresolved_then_and_now_stays_non_blocking(self) -> None:
        verdict = classify(
            Resolution(_h("a.example.invalid"), (), error="NXDOMAIN"),
            source_ips=SOURCE,
            target_ips=TARGET,
            baseline=(),
        )
        assert verdict.verdict is Verdict.UNRESOLVED
        assert verdict.blocks is False

    def test_generated_never_blocks(self) -> None:
        # Even though it resolves to the source IP, it encodes that IP and will
        # follow the server.
        verdict = classify(
            Resolution(_h("1-2-3-4.sslip.io", generated=True), ("10.0.0.1",)),
            source_ips=SOURCE,
            target_ips=TARGET,
        )
        assert verdict.verdict is Verdict.GENERATED
        assert verdict.blocks is False

    def test_unresolved_does_not_block(self) -> None:
        # A hostname that does not resolve is not serving anyone.
        verdict = classify(
            Resolution(_h("a.example.com"), (), error="NXDOMAIN"),
            source_ips=SOURCE,
            target_ips=TARGET,
        )
        assert verdict.verdict is Verdict.UNRESOLVED
        assert verdict.blocks is False

    def test_source_wildcard_url_is_server_bound_and_never_blocks(self) -> None:
        # A URL under the source server's wildcard resolves to the source, but it
        # is rewritten onto the target's wildcard at create — it must not gate.
        verdict = classify(
            Resolution(_h("pdf-tool.app.0046-20.cloud.bauer-group.com"), ("10.0.0.1",)),
            source_ips=SOURCE,
            target_ips=TARGET,
            source_wildcard="app.0046-20.cloud.bauer-group.com",
        )
        assert verdict.verdict is Verdict.SERVER_BOUND
        assert verdict.blocks is False

    def test_server_bound_wins_over_cutover_even_without_resolution(self) -> None:
        # The step does not resolve server-bound hosts (empty addresses); the
        # wildcard check short-circuits before the "no addresses" branch.
        verdict = classify(
            Resolution(_h("pdf-tool.app.0046-20.cloud.bauer-group.com"), ()),
            source_ips=SOURCE,
            target_ips=TARGET,
            source_wildcard="app.0046-20.cloud.bauer-group.com",
        )
        assert verdict.verdict is Verdict.SERVER_BOUND

    def test_custom_domain_still_blocks_with_a_source_wildcard_set(self) -> None:
        # Having a source wildcard must not make custom domains stop gating.
        verdict = classify(
            Resolution(_h("shop.example.com"), ("10.0.0.1",)),
            source_ips=SOURCE,
            target_ips=TARGET,
            source_wildcard="app.0046-20.cloud.bauer-group.com",
        )
        assert verdict.verdict is Verdict.CUTOVER_NEEDED
        assert verdict.blocks is True


class TestSharedAddresses:
    """An address both servers answer as proves neither (a shared wildcard, one CDN)."""

    # Both wildcards resolve to the source's public address, or to one CDN.
    SRC = frozenset({"203.0.113.5"})
    TGT = frozenset({"198.51.100.6", "203.0.113.5"})

    def _classify(self, *addresses: str, source: frozenset[str] = SRC) -> HostVerdict:
        return classify(
            Resolution(_h("shop.example.com"), addresses), source_ips=source, target_ips=self.TGT
        )

    def test_a_host_on_a_shared_address_is_not_ready(self) -> None:
        verdict = self._classify("203.0.113.5")
        assert verdict.verdict is Verdict.ELSEWHERE
        assert verdict.blocks is True
        assert "BOTH servers answer as" in verdict.detail

    def test_a_proxied_host_behind_one_cdn_for_both_blocks(self) -> None:
        cdn = frozenset({"104.21.1.1", "172.67.2.2"})
        verdict = classify(
            Resolution(_h("shop.example.com"), ("104.21.1.1", "172.67.2.2")),
            source_ips=frozenset({"10.0.0.5"}) | cdn,
            target_ips=frozenset({"10.0.0.6"}) | cdn,
        )
        assert verdict.verdict is Verdict.ELSEWHERE
        report = build_report(
            [Resolution(_h("shop.example.com"), ("104.21.1.1",))],
            source_ips=frozenset({"10.0.0.5"}) | cdn,
            target_ips=frozenset({"10.0.0.6"}) | cdn,
        )
        assert report.unaccepted(accept_cutover=True)

    def test_an_address_of_the_target_alone_still_proves_it(self) -> None:
        assert self._classify("198.51.100.6", "203.0.113.5").verdict is Verdict.READY

    def test_an_address_of_the_source_alone_still_proves_it(self) -> None:
        source = self.SRC | {"192.0.2.9"}
        verdict = self._classify("192.0.2.9", "203.0.113.5", source=source)
        assert verdict.verdict is Verdict.CUTOVER_NEEDED

    def test_the_checklist_never_points_a_record_at_a_shared_address(self) -> None:
        report = build_report(
            [Resolution(_h("shop.example.com"), ("203.0.113.5",))],
            source_ips=self.SRC,
            target_ips=frozenset({"203.0.113.5", "250.0.0.6"}),
        )
        (line,) = report.cutover_checklist()
        assert "an address both servers answer as" in line
        assert "is 250.0.0.6" in line


class TestReport:
    def test_blocked_when_any_hostname_points_at_source(self) -> None:
        report = build_report(
            [
                Resolution(_h("ready.example.com"), ("10.0.0.2",)),
                Resolution(_h("stale.example.com"), ("10.0.0.1",), ttl=3600),
            ],
            source_ips=SOURCE,
            target_ips=TARGET,
        )
        assert report.is_blocked
        assert len(report.blocked) == 1
        assert len(report.ready) == 1

    def test_not_blocked_when_all_ready(self) -> None:
        report = build_report(
            [Resolution(_h("a.example.com"), ("10.0.0.2",))], source_ips=SOURCE, target_ips=TARGET
        )
        assert report.is_blocked is False

    def test_checklist_is_actionable(self) -> None:
        report = build_report(
            [Resolution(_h("shop.example.com"), ("10.0.0.1",), ttl=3600)],
            source_ips=SOURCE,
            target_ips=TARGET,
        )
        (line,) = report.cutover_checklist()
        assert "shop.example.com" in line
        assert "10.0.0.1" in line
        assert "10.0.0.2" in line
        assert "3600" in line

    def test_max_ttl_tells_the_operator_how_long_to_wait(self) -> None:
        report = build_report(
            [
                Resolution(_h("a.example.com"), ("10.0.0.1",), ttl=300),
                Resolution(_h("b.example.com"), ("10.0.0.1",), ttl=3600),
            ],
            source_ips=SOURCE,
            target_ips=TARGET,
        )
        assert report.max_ttl == 3600

    def test_ambiguous_surfaced_separately_and_blocking(self) -> None:
        report = build_report(
            [Resolution(_h("cdn.example.com"), ("104.21.1.1",))],
            source_ips=SOURCE,
            target_ips=TARGET,
        )
        assert len(report.ambiguous) == 1
        assert report.is_blocked is True

    def _mixed(self) -> object:
        return build_report(
            [
                Resolution(_h("cut.example.com"), ("10.0.0.1",), ttl=300),
                Resolution(_h("cdn.example.com"), ("104.21.1.1",)),
                Resolution(_h("gone.example.com"), (), error="NXDOMAIN"),
                Resolution(_h("ok.example.com"), ("10.0.0.2",)),
            ],
            source_ips=SOURCE,
            target_ips=TARGET,
            baselines={"gone.example.com": ("10.0.0.1",)},
        )

    def test_accept_dns_answers_a_cutover_only(self) -> None:
        report = self._mixed()
        open_ = report.unaccepted(accept_cutover=True)  # type: ignore[attr-defined]
        assert [v.hostname.host for v in open_] == ["cdn.example.com", "gone.example.com"]

    def test_a_host_is_accepted_one_by_one(self) -> None:
        report = self._mixed()
        open_ = report.unaccepted(  # type: ignore[attr-defined]
            accept_cutover=False, accepted_hosts={"cdn.example.com", "gone.example.com"}
        )
        assert [v.hostname.host for v in open_] == ["cut.example.com"]
        assert report.vanished[0].hostname.host == "gone.example.com"  # type: ignore[attr-defined]

    def test_the_checklist_says_what_to_do_per_verdict(self) -> None:
        report = self._mixed()
        lines = report.cutover_checklist(cloudflare_hosts={"cut.example.com"})  # type: ignore[attr-defined]
        assert lines[0].startswith("cut.example.com: 10.0.0.1 -> 10.0.0.2 (TTL 300s)")
        assert "Cloudflare zone" in lines[0] and "Replace record" in lines[0]
        assert "--accept-dns-host cdn.example.com" in lines[1]
        assert "resolved to 10.0.0.1 at plan time" in lines[2]
        assert "recreate the record -> 10.0.0.2" in lines[2]

    def test_an_unanswered_host_is_accepted_by_name_only(self) -> None:
        report = build_report(
            [Resolution(_h("slow.example.com"), (), error="LifetimeTimeout")],
            source_ips=SOURCE,
            target_ips=TARGET,
            baselines={"slow.example.com": ("10.0.0.1",)},
        )
        assert [v.hostname.host for v in report.unanswered] == ["slow.example.com"]
        assert report.vanished == ()
        assert report.unaccepted(accept_cutover=True)
        assert not report.unaccepted(accept_cutover=False, accepted_hosts={"slow.example.com"})
        (line,) = report.cutover_checklist()
        assert "could not be asked" in line and "resume" in line
        assert "recreate" not in line
        assert "--accept-dns-host slow.example.com" in line

    def test_definitive_answers(self) -> None:
        from bg_coolify_migrate.dns.gate import is_definitive

        assert is_definitive(Resolution(_h("a.b.c"), ("1.2.3.4",)))
        assert is_definitive(Resolution(_h("a.b.c"), (), error="NXDOMAIN"))
        assert is_definitive(Resolution(_h("a.b.c"), (), error="no A/AAAA records"))
        assert not is_definitive(Resolution(_h("a.b.c"), (), error="LifetimeTimeout"))
        assert not is_definitive(Resolution(_h("a.b.c"), ()))

    def test_empty_report_is_not_blocked(self) -> None:
        assert build_report([], source_ips=SOURCE, target_ips=TARGET).is_blocked is False


def test_explanation_covers_the_acme_mechanism() -> None:
    # An operator who understands WHY will not go looking for a --force flag.
    text = explain_why_blocking_matters()
    assert "ACME" in text
    assert "rate-limit" in text
    assert "resume" in text
