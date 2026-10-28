"""Tests for what an API re-creation cannot carry to the target.

Each rule is pinned from both sides: it fires on the shape Coolify 4.3.15-4.5.0
cannot reproduce, it is SILENT on a stock resource (a refusal or warning on a
correct setup teaches operators to skip them), and it is silent below the
release where the behaviour starts.
"""

from __future__ import annotations

import base64
from typing import Any

import pytest

from bg_coolify_migrate.domain import fidelity
from bg_coolify_migrate.domain.kinds import DatabaseEngine
from bg_coolify_migrate.domain.versions import at_least, parse_version

V451 = (4, 5, 1)
V45 = (4, 5, 0)
V44 = (4, 4, 0)
V43 = (4, 3, 10)
#: A source server's wildcard base: hosts under it are bound to that server.
WILD = "app.0046-20.example.net"


class TestVersions:
    def test_parses_plain_prefixed_and_suffixed(self) -> None:
        assert parse_version("4.5.0") == (4, 5, 0)
        assert parse_version("v4.3.10") == (4, 3, 10)
        assert parse_version("4.4.0-beta.1") == (4, 4, 0)

    def test_refuses_to_guess(self) -> None:
        assert parse_version("nightly") is None
        assert parse_version(None) is None

    def test_compares_numerically(self) -> None:
        assert at_least((4, 3, 10), (4, 3, 3))

    def test_an_unknown_version_applies_the_rule(self) -> None:
        """Failing toward checking: "we could not tell" is not "it is fine"."""
        assert at_least(None, (9, 9, 9))


# ── applications ─────────────────────────────────────────────────────────────


class TestWebhookSecrets:
    def test_a_short_secret_refuses_with_its_length_never_its_value(self) -> None:
        out = fidelity.application_refusals(
            {"manual_webhook_secret_github": "hunter2"}, version=V45
        )
        assert len(out) == 1
        assert "manual_webhook_secret_github (7 characters)" in out[0]
        assert "hunter2" not in out[0]  # invariant 5: never a secret's value

    def test_coolifys_own_40_character_secrets_pass(self) -> None:
        assert fidelity.application_refusals(
            {"manual_webhook_secret_github": "x" * 40}, version=V45
        ) == []

    def test_an_empty_secret_is_allowed(self) -> None:
        # ManualWebhookSecret: "An empty value disables the webhook and stays allowed."
        assert fidelity.application_refusals(
            {"manual_webhook_secret_gitlab": ""}, version=V45
        ) == []

    def test_silent_before_4_4(self) -> None:
        assert fidelity.application_refusals(
            {"manual_webhook_secret_github": "short"}, version=V43
        ) == []


class TestBasicAuthPassword:
    def test_over_72_characters_refuses(self) -> None:
        out = fidelity.application_refusals({"http_basic_auth_password": "p" * 73}, version=V45)
        assert len(out) == 1
        assert "73 characters" in out[0]
        assert "p" * 73 not in out[0]

    def test_exactly_72_passes(self) -> None:
        assert fidelity.application_refusals({"http_basic_auth_password": "p" * 72}, version=V45) == []

    def test_counts_characters_like_mb_strlen_not_bytes(self) -> None:
        # 72 'ä' is 144 bytes but 72 characters: Laravel's max counts the latter.
        assert fidelity.application_refusals({"http_basic_auth_password": "ä" * 72}, version=V45) == []

    def test_silent_on_4_4(self) -> None:
        assert fidelity.application_refusals(
            {"http_basic_auth_password": "p" * 100}, version=(4, 4, 7)
        ) == []


def _app(**kw: Any) -> dict[str, Any]:
    base = {"build_pack": "nixpacks", "ports_exposes": "3000,9000", "fqdn": "https://app.example.com"}
    return {**base, **kw}


_TRAEFIK_LABELS = (
    "traefik.enable=true\n"
    "traefik.http.routers.http-0-abc.rule=Host(`app.example.com`)\n"
    "traefik.http.services.http-0-abc.loadbalancer.server.port={port}\n"
)


class TestHiddenPortOverrides:
    def test_a_label_port_the_fqdn_does_not_explain_refuses(self) -> None:
        source = _app(custom_labels=_TRAEFIK_LABELS.format(port=9000))
        assert fidelity.hidden_port_overrides(source) == [9000]
        out = fidelity.application_refusals(source, version=V45)
        assert len(out) == 1
        assert "[9000]" in out[0]

    def test_routing_to_the_first_exposed_port_is_the_default_and_silent(self) -> None:
        assert fidelity.hidden_port_overrides(_app(custom_labels=_TRAEFIK_LABELS.format(port=3000))) == []

    def test_a_port_written_in_the_fqdn_is_carried_and_silent(self) -> None:
        # A row not re-saved since the upgrade still has the port in its fqdn.
        source = _app(
            fqdn="https://app.example.com:9000", custom_labels=_TRAEFIK_LABELS.format(port=9000)
        )
        assert fidelity.hidden_port_overrides(source) == []

    def test_reads_ui_stored_base64_labels(self) -> None:
        encoded = base64.b64encode(_TRAEFIK_LABELS.format(port=9000).encode()).decode()
        assert fidelity.hidden_port_overrides(_app(custom_labels=encoded)) == [9000]

    def test_reads_caddy_labels(self) -> None:
        labels = "caddy_0=https://app.example.com\ncaddy_0.reverse_proxy={{upstreams 9000}}\n"
        assert fidelity.hidden_port_overrides(_app(custom_labels=labels)) == [9000]

    def test_compose_applications_are_not_checked(self) -> None:
        source = _app(build_pack="dockercompose", custom_labels=_TRAEFIK_LABELS.format(port=9000))
        assert fidelity.hidden_port_overrides(source) == []

    def test_silent_before_4_3_15(self) -> None:
        source = _app(custom_labels=_TRAEFIK_LABELS.format(port=9000))
        assert fidelity.application_refusals(source, version=(4, 3, 14)) == []

    def test_a_static_site_routes_to_80_whatever_ports_exposes_says(self) -> None:
        """REVIEW: generateLabelsApplication uses [80] for is_static (docker.php:1229).

        Switching an app to static never rewrites ports_exposes, so reading 3000 as
        the fallback refused a perfectly ordinary static site with a wrong diagnosis.
        """
        source = _app(settings={"is_static": True}, custom_labels=_TRAEFIK_LABELS.format(port=80))
        assert fidelity.hidden_port_overrides(source) == []
        assert fidelity.application_refusals(source, version=V45) == []


class TestHealthCheckCommand:
    def test_a_command_the_api_would_reject_refuses_without_echoing_it(self) -> None:
        cmd = "curl -f http://localhost:3000/health || exit 1"
        out = fidelity.application_refusals({"health_check_command": cmd}, version=V43)
        assert len(out) == 1
        assert f"({len(cmd)} characters)" in out[0]
        assert "||" not in out[0]

    def test_a_plain_command_passes_on_every_version(self) -> None:
        cmd = "wget -qO- http://127.0.0.1:8080/healthz"
        assert fidelity.application_refusals({"health_check_command": cmd}, version=(4, 1, 2)) == []

    def test_over_1000_characters_refuses(self) -> None:
        assert len(fidelity.application_refusals({"health_check_command": "a" * 1001}, version=V45)) == 1

    def test_no_command_is_silent(self) -> None:
        assert fidelity.application_refusals({"health_check_command": None}, version=V45) == []


class TestApplicationWarnings:
    def test_silent_on_a_stock_application(self) -> None:
        stock = _app(
            settings={
                "traefik_certificate_resolver": None,
                "is_permanent_redirect_enabled": False,
                "include_coolify_default_variables": True,
                "is_container_label_readonly_enabled": True,
            },
            http_basic_auth_hash_algorithm="bcrypt",
            is_maintenance_enabled=False,
            custom_docker_run_options="--cap-add SYS_ADMIN",
        )
        assert fidelity.application_warnings(stock, version=V45) == []

    def test_argon2id_basic_auth(self) -> None:
        out = fidelity.application_warnings(
            {"is_http_basic_auth_enabled": True, "http_basic_auth_hash_algorithm": "argon2id"},
            version=V45,
        )
        assert len(out) == 1
        assert "argon2id" in out[0]
        assert "Caddy >= 2.11" in out[0]

    def test_argon2id_is_irrelevant_while_basic_auth_is_off(self) -> None:
        assert fidelity.application_warnings(
            {"is_http_basic_auth_enabled": False, "http_basic_auth_hash_algorithm": "argon2id"},
            version=V45,
        ) == []

    @pytest.mark.parametrize(
        ("settings", "needle"),
        [
            ({"traefik_certificate_resolver": "dnsresolver"}, "'dnsresolver'"),
            ({"is_permanent_redirect_enabled": True}, "permanent redirects"),
            ({"include_coolify_default_variables": False}, "COOLIFY_SERVER_IP"),
        ],
    )
    def test_settings_the_target_cannot_share(self, settings: dict[str, Any], needle: str) -> None:
        out = fidelity.application_warnings({"settings": settings}, version=V45)
        assert len(out) == 1
        assert needle in out[0]

    def test_hand_edited_labels(self) -> None:
        out = fidelity.application_warnings(
            {"settings": {"is_container_label_readonly_enabled": False}, "custom_labels": "x=y"},
            version=V45,
        )
        assert len(out) == 1
        assert "regenerates its labels" in out[0]

    @pytest.mark.parametrize("version", [V45, None])
    def test_default_variables_off_warns_until_4_5_1(self, version: tuple[int, ...] | None) -> None:
        """An unknown release gets no field a release in range would refuse — so
        the setting is not carried there either, and the warning stands."""
        settings = {"include_coolify_default_variables": False}
        assert len(fidelity.application_warnings({"settings": settings}, version=version)) == 1

    def test_default_variables_off_is_carried_from_4_5_1(self) -> None:
        settings = {"include_coolify_default_variables": False}
        assert fidelity.application_warnings({"settings": settings}, version=V451) == []

    def test_readonly_labels_off_is_carried_from_4_5_1(self) -> None:
        """The target keeps the source's labels verbatim: they route only the app's
        own custom domain, which the DNS gate guards."""
        source = {
            "settings": {"is_container_label_readonly_enabled": False},
            "fqdn": "https://shop.example.com",
            "custom_labels": "traefik.http.routers.r0.rule=Host(`shop.example.com`)\n"
            "traefik.http.middlewares.gz.compress=true",
        }
        assert fidelity.application_warnings(source, version=V451, source_wildcard=WILD) == []

    def test_readonly_labels_routing_a_source_bound_host_are_not_kept(self) -> None:
        """Kept verbatim they would route — and request a certificate for — a host
        that still resolves to the source (invariant 7), so the target regenerates
        them instead, and the plan says which hosts and why."""
        source = {
            "settings": {"is_container_label_readonly_enabled": False},
            "fqdn": f"https://shop.example.com,https://shop.{WILD}",
            "custom_labels": "traefik.http.routers.r0.rule=Host(`shop.example.com`) || "
            f"Host(`shop.{WILD}`)\ncaddy_0=https://extra.example.org",
        }
        [line] = fidelity.application_warnings(source, version=V451, source_wildcard=WILD)
        assert f"shop.{WILD}" in line and "extra.example.org" in line
        assert "readonly labels ON instead" in line
        assert "shop.example.com'" not in line  # its own custom domain is fine


class TestLabelsRouteBeyondDomains:
    def test_own_custom_domains_only(self) -> None:
        source = {
            "fqdn": "https://a.example.com,https://b.example.com:8443",
            "custom_labels": "traefik.http.routers.x.rule=Host(`a.example.com`,`b.example.com`)",
        }
        assert fidelity.labels_route_beyond_domains(source, source_wildcard=WILD) == []

    def test_base64_labels_are_read(self) -> None:
        labels = "traefik.http.routers.x.rule=Host(`elsewhere.example.com`)"
        source = {"fqdn": "", "custom_labels": base64.b64encode(labels.encode()).decode()}
        assert fidelity.labels_route_beyond_domains(source, source_wildcard=None) == [
            "elsewhere.example.com"
        ]

    def test_a_generated_host_is_never_own(self) -> None:
        host = "app-abc.10.0.0.1.sslip.io"
        source = {"fqdn": f"http://{host}", "custom_labels": f"caddy_0=http://{host}"}
        assert fidelity.labels_route_beyond_domains(source, source_wildcard=None) == [host]

    def test_a_host_regexp_cannot_be_enumerated(self) -> None:
        source = {"fqdn": "", "custom_labels": "traefik.http.routers.x.rule=HostRegexp(`{s:.+}`)"}
        [entry] = fidelity.labels_route_beyond_domains(source, source_wildcard=None)
        assert entry.startswith("HostRegexp(")

    def test_no_labels(self) -> None:
        assert fidelity.labels_route_beyond_domains({"fqdn": "https://a.b"}, source_wildcard=None) == []

    def test_a_static_site(self) -> None:
        out = fidelity.application_warnings({"settings": {"is_static": True}}, version=V45)
        assert len(out) == 1
        assert "static" in out[0]

    def test_maintenance_mode(self) -> None:
        out = fidelity.application_warnings({"is_maintenance_enabled": True}, version=V45)
        assert len(out) == 1
        assert "maintenance" in out[0]

    def test_newly_mapped_run_options(self) -> None:
        out = fidelity.application_warnings(
            {"custom_docker_run_options": "--read-only --tmpfs /run --cap-add NET_ADMIN"},
            version=V45,
        )
        assert len(out) == 1
        assert "['--read-only', '--tmpfs']" in out[0]

    def test_run_options_are_silent_before_4_5(self) -> None:
        assert fidelity.application_warnings(
            {"custom_docker_run_options": "--read-only"}, version=(4, 4, 7)
        ) == []


class TestNewlyMappedRunOptions:
    def test_short_tty_flags(self) -> None:
        assert fidelity.newly_mapped_run_options("-it --init") == ["-it"]

    @pytest.mark.parametrize("token", ["-dit", "-itd", "-td", "-i"])
    def test_combined_short_flags_upstream_maps(self, token: str) -> None:
        # docker.php v4.5.0:1797: any ^-[dit]+$ token carrying i or t.
        assert fidelity.newly_mapped_run_options(token) == [token]

    def test_detach_alone_was_never_mapped(self) -> None:
        assert fidelity.newly_mapped_run_options("-d") == []

    def test_an_explicit_false_used_to_read_as_true(self) -> None:
        assert fidelity.newly_mapped_run_options("--privileged=false") == ["--privileged=false"]

    def test_false_on_a_flag_neither_parser_maps_is_silent(self) -> None:
        assert fidelity.newly_mapped_run_options("--rm=false") == []

    def test_value_flags_with_equals(self) -> None:
        assert fidelity.newly_mapped_run_options("--pids-limit=100") == ["--pids-limit"]

    def test_unbalanced_quotes_do_not_crash(self) -> None:
        assert fidelity.newly_mapped_run_options("--label 'oops --tty") == ["--tty"]

    def test_empty(self) -> None:
        assert fidelity.newly_mapped_run_options(None) == []


# ── services and databases ───────────────────────────────────────────────────


class TestServiceAndDatabaseWarnings:
    def test_service_silent_when_stock(self) -> None:
        assert fidelity.service_warnings(
            {"include_coolify_default_variables": True, "is_maintenance_enabled": False, "applications": []}
        ) == []

    def test_service_sub_application_tls(self) -> None:
        out = fidelity.service_warnings(
            {
                "applications": [
                    {"name": "web", "traefik_certificate_resolver": "dns"},
                    {"name": "api", "is_permanent_redirect_enabled": False},
                ]
            }
        )
        assert len(out) == 1
        assert "['web']" in out[0]

    def test_service_maintenance_and_default_variables(self) -> None:
        out = fidelity.service_warnings(
            {"include_coolify_default_variables": False, "is_maintenance_enabled": True}
        )
        assert len(out) == 2

    def test_database_default_variables(self) -> None:
        assert len(fidelity.database_warnings({"include_coolify_default_variables": False})) == 1
        assert fidelity.database_warnings({"include_coolify_default_variables": True}) == []

    def test_unsupported_engine_names_it(self) -> None:
        refusal = fidelity.unsupported_engine_refusal({"type": "standalone-sqlite"})
        assert "'standalone-sqlite'" in refusal
        assert "SQLite" in refusal


# ── compose ──────────────────────────────────────────────────────────────────

_PLAIN = """
services:
  db:
    image: postgres:16
    volumes: [db-data:/var/lib/postgresql/data]
volumes:
  db-data: {}
"""


class TestComposeRefusals:
    def test_a_plain_compose_is_silent(self) -> None:
        assert fidelity.compose_refusals(_PLAIN, version=V45) == []

    @pytest.mark.parametrize(
        "decl",
        ["external: true", "external: 'true'", "external: {name: shared}", "name: shared\n    external: true"],
    )
    def test_external_volumes_refuse(self, decl: str) -> None:
        raw = f"services:\n  a:\n    image: x\nvolumes:\n  data:\n    {decl}\n"
        out = fidelity.compose_refusals(raw, version=V45)
        assert len(out) == 1
        assert "'data' is external" in out[0]

    def test_external_false_is_an_ordinary_volume(self) -> None:
        raw = "services:\n  a:\n    image: x\nvolumes:\n  data:\n    external: false\n"
        assert fidelity.compose_refusals(raw, version=V45) == []

    def test_the_external_name_is_the_one_docker_will_use(self) -> None:
        raw = "services:\n  a:\n    image: x\nvolumes:\n  data:\n    external:\n      name: legacy_vol\n"
        assert "'legacy_vol'" in fidelity.compose_refusals(raw, version=V45)[0]

    @pytest.mark.parametrize(
        "decl",
        [
            "driver_opts: {type: none, o: bind, device: /srv/data}",
            "driver_opts: {type: nfs, o: 'addr=10.0.0.5', device: ':/export'}",
            "driver: rexray/ebs",
        ],
    )
    def test_driver_backed_volumes_refuse(self, decl: str) -> None:
        raw = f"services:\n  a:\n    image: x\nvolumes:\n  data:\n    {decl}\n"
        out = fidelity.compose_refusals(raw, version=V45)
        assert len(out) == 1
        assert "driver" in out[0]

    def test_an_explicit_local_driver_is_ordinary(self) -> None:
        raw = "services:\n  a:\n    image: x\nvolumes:\n  data:\n    driver: local\n"
        assert fidelity.compose_refusals(raw, version=V45) == []

    def test_volume_shapes_are_silent_before_4_4(self) -> None:
        raw = "services:\n  a:\n    image: x\nvolumes:\n  data:\n    external: true\n"
        assert fidelity.compose_refusals(raw, version=V43) == []

    @pytest.mark.parametrize("spec", ["scale: 3", "deploy:\n      replicas: 2"])
    def test_more_than_one_replica_refuses_on_4_5(self, spec: str) -> None:
        raw = f"services:\n  worker:\n    image: x\n    {spec}\n"
        out = fidelity.compose_refusals(raw, version=V45)
        assert len(out) == 1
        assert "'worker'" in out[0]
        assert fidelity.compose_refusals(raw, version=(4, 4, 7)) == []

    def test_one_replica_is_fine(self) -> None:
        raw = "services:\n  worker:\n    image: x\n    deploy:\n      replicas: 1\n"
        assert fidelity.compose_refusals(raw, version=V45) == []

    def test_unparseable_or_absent_compose_is_not_this_modules_diagnosis(self) -> None:
        assert fidelity.compose_refusals("services: [", version=V45) == []
        assert fidelity.compose_refusals(None, version=V45) == []

    def test_redump_warning_only_before_4_4(self) -> None:
        assert fidelity.compose_redump_warning(version=V45) == []
        assert len(fidelity.compose_redump_warning(version=V43)) == 1


# ── storages ─────────────────────────────────────────────────────────────────


class TestStorageRefusals:
    def _storages(self, host_path: str | None) -> dict[str, Any]:
        return {"persistent_storages": [{"name": "a1-data", "mount_path": "/data", "host_path": host_path}]}

    def test_a_host_path_storage_refuses_from_4_3_22(self) -> None:
        out = fidelity.storage_refusals(self._storages("/srv/app/data"), version=(4, 3, 22))
        assert len(out) == 1
        assert "'/srv/app/data'" in out[0]

    def test_older_instances_still_accept_it(self) -> None:
        assert fidelity.storage_refusals(self._storages("/srv/app/data"), version=(4, 3, 21)) == []

    def test_a_plain_volume_is_silent(self) -> None:
        assert fidelity.storage_refusals(self._storages(None), version=V45) == []
        assert fidelity.storage_refusals(self._storages(""), version=V45) == []

    def test_unreadable_storages_are_silent(self) -> None:
        assert fidelity.storage_refusals(None, version=V45) == []


# ── environment variables ────────────────────────────────────────────────────


def _env(key: str, value: object = "v", **kw: Any) -> dict[str, Any]:
    env: dict[str, Any] = {"key": key, "is_literal": False, "is_multiline": False, **kw}
    if value is not _ABSENT:
        env["value"] = value
    return env


_ABSENT = object()


class TestSecretManagerReferences:
    def test_a_non_literal_reference_refuses(self) -> None:
        out = fidelity.env_refusals(
            [_env("DB_URL", "postgres://u:{{ vault.DB_PASS }}@db/x")],
            collection="applications",
            engine=None,
            version=V45,
        )
        assert len(out) == 1
        assert "['DB_URL']" in out[0]

    def test_a_literal_reference_only_warns(self) -> None:
        envs = [_env("TEMPLATE", "{{vault.X}}", is_literal=True)]
        assert fidelity.env_refusals(envs, collection="applications", engine=None, version=V45) == []
        warnings = fidelity.env_warnings(
            envs, collection="applications", engine=None, compose=None, version=V45
        )
        assert any("['TEMPLATE']" in w for w in warnings)

    def test_shared_variable_syntax_is_not_a_secret_manager(self) -> None:
        envs = [_env("X", "{{project.API_URL}}")]
        assert fidelity.env_refusals(envs, collection="applications", engine=None, version=V45) == []

    def test_silent_before_4_4(self) -> None:
        envs = [_env("X", "{{vault.Y}}")]
        assert fidelity.env_refusals(envs, collection="applications", engine=None, version=V43) == []


class TestLockedVariables:
    def test_a_locked_generated_service_secret_refuses(self) -> None:
        envs = [_env("SERVICE_PASSWORD_POSTGRES", _ABSENT, is_shown_once=True)]
        out = fidelity.env_refusals(envs, collection="services", engine=None, version=V45)
        assert len(out) == 1
        assert "SERVICE_PASSWORD_POSTGRES" in out[0]

    def test_a_locked_redis_password_refuses(self) -> None:
        envs = [_env("REDIS_PASSWORD", _ABSENT, is_shown_once=True)]
        out = fidelity.env_refusals(
            envs, collection="databases", engine=DatabaseEngine.REDIS, version=V45
        )
        assert len(out) == 1

    def test_a_null_value_is_a_null_row_not_a_hidden_one(self) -> None:
        """The hide is makeHidden: the key is ABSENT. A present null is just null.

        On 4.1.x every locked value is still returned, and a genuinely null row
        must not send the planner reading files for a value that does not exist.
        """
        envs = [_env("REDIS_PASSWORD", None, is_shown_once=True)]
        assert fidelity.env_refusals(
            envs, collection="databases", engine=DatabaseEngine.REDIS, version=V45
        ) == []

    def test_a_locked_redis_password_on_keydb_only_warns(self) -> None:
        """REVIEW: a KeyDB target falls back to the carried keydb_password.

        Its create makes no REDIS_PASSWORD row, so the server password is the same
        on both sides; refusing would block a migration that cannot fail this way.
        """
        envs = [_env("REDIS_PASSWORD", _ABSENT, is_shown_once=True)]
        assert fidelity.env_refusals(
            envs, collection="databases", engine=DatabaseEngine.KEYDB, version=V45
        ) == []
        warnings = fidelity.env_warnings(
            envs, collection="databases", engine=DatabaseEngine.KEYDB, compose=None, version=V45
        )
        assert any("['REDIS_PASSWORD']" in w and "locked" in w for w in warnings)

    def test_other_locked_keys_warn_and_flag_credentials(self) -> None:
        envs = [
            _env("STRIPE_SECRET", _ABSENT, is_shown_once=True),
            _env("FEATURE_FLAG", _ABSENT, is_shown_once=True),
        ]
        assert fidelity.env_refusals(envs, collection="applications", engine=None, version=V45) == []
        out = fidelity.env_warnings(envs, collection="applications", engine=None, compose=None, version=V45)
        assert len(out) == 1
        assert "['FEATURE_FLAG', 'STRIPE_SECRET']" in out[0]
        assert "['STRIPE_SECRET'] look like credentials" in out[0]

    def test_a_readable_locked_variable_is_copied_and_silent(self) -> None:
        envs = [_env("TOKEN", "abc", is_shown_once=True, version="4.5.0")]
        assert fidelity.env_warnings(envs, collection="applications", engine=None, compose=None, version=V45) == []


class TestLegacyEscaping:
    @pytest.mark.parametrize(
        ("value", "flags", "changes"),
        [
            ('pa"ss', {}, True),
            ("C:\\tmp", {}, True),
            ("it's", {}, True),
            ("abc #comment", {}, True),
            ("tab\there", {}, True),
            ("plain-value_1.2", {}, False),
            ("$HOME/x", {}, False),  # both forms interpolate
            ("it's", {"is_literal": True}, True),
            ("no quote", {"is_literal": True}, False),
            ("line1\nline2", {"is_multiline": True}, False),
            ('{"a": "$B"}', {}, True),
            ('{"a": "b"}', {}, False),
            ("", {}, False),
            ("{{vault.X}}", {}, False),
        ],
    )
    def test_application_predicate(self, value: str, flags: dict[str, bool], changes: bool) -> None:
        env = _env("K", value, **flags)
        assert (
            fidelity.legacy_escaping_changes_value(env, collection="applications", engine=None)
            is changes
        )

    def test_databases_keep_the_quotes_around_literal_values(self) -> None:
        env = _env("K", "no quote", is_literal=True)
        assert fidelity.legacy_escaping_changes_value(env, collection="databases", engine=None)

    def test_json_newlines_matter_outside_applications(self) -> None:
        env = _env("K", '{"a":\n"b"}')
        assert not fidelity.legacy_escaping_changes_value(env, collection="applications", engine=None)
        assert fidelity.legacy_escaping_changes_value(env, collection="services", engine=None)

    def test_a_redis_password_with_a_dollar(self) -> None:
        env = _env("REDIS_PASSWORD", "pa$s")
        assert fidelity.legacy_escaping_changes_value(
            env, collection="databases", engine=DatabaseEngine.REDIS
        )

    def test_warns_only_for_rows_saved_before_4_5(self) -> None:
        envs = [
            _env("OLD", 'a"b', version="4.4.7"),
            _env("UNSTAMPED", 'a"b'),
            _env("NEW", 'a"b', version="4.5.0"),
            _env("DB_PASSWORD", "x\\y", version="4.3.10"),
        ]
        out = fidelity.env_warnings(envs, collection="applications", engine=None, compose=None, version=V45)
        assert len(out) == 1
        assert "['DB_PASSWORD', 'OLD', 'UNSTAMPED']" in out[0]
        assert "['DB_PASSWORD'] look like credentials" in out[0]
        assert 'a"b' not in out[0] and "x\\y" not in out[0]

    def test_silent_before_4_5(self) -> None:
        envs = [_env("OLD", 'a"b', version="4.4.7")]
        assert fidelity.env_warnings(envs, collection="applications", engine=None, compose=None, version=(4, 4, 7)) == []


class TestRedisPasswordQuoting:
    def test_redis_with_shell_characters(self) -> None:
        envs = [_env("REDIS_PASSWORD", "a;b", version="4.5.0")]
        out = fidelity.env_warnings(
            envs, collection="databases", engine=DatabaseEngine.REDIS, compose=None, version=V45
        )
        assert any("--requirepass unquoted" in w for w in out)

    def test_redis_with_a_generated_password_is_silent(self) -> None:
        envs = [_env("REDIS_PASSWORD", "aB3dE6gH9jK2", version="4.5.0")]
        assert fidelity.env_warnings(
            envs, collection="databases", engine=DatabaseEngine.REDIS, compose=None, version=V45
        ) == []

    def test_keydb_with_a_redis_password_variable(self) -> None:
        envs = [_env("REDIS_PASSWORD", "x", version="4.5.0")]
        out = fidelity.env_warnings(
            envs, collection="databases", engine=DatabaseEngine.KEYDB, compose=None, version=V45
        )
        assert any("AS the server password" in w for w in out)

    def test_silent_before_4_4(self) -> None:
        envs = [_env("REDIS_PASSWORD", "a;b", version="4.3.10")]
        assert fidelity.env_warnings(
            envs, collection="databases", engine=DatabaseEngine.REDIS, compose=None, version=V43
        ) == []


class TestEmptyComposeVariables:
    @pytest.mark.parametrize(
        "compose",
        [
            "services:\n  a:\n    environment:\n      - OPT\n",
            "services:\n  a:\n    environment:\n      OPT:\n",
            "services:\n  a:\n    command: run ${OPT-fallback}\n",
            "services:\n  a:\n    command: run ${OPT?required}\n",
        ],
    )
    def test_reads_that_tell_unset_from_empty(self, compose: str) -> None:
        envs = [_env("OPT", "", version="4.5.0")]
        out = fidelity.env_warnings(envs, collection="services", engine=None, compose=compose, version=V45)
        assert len(out) == 1
        assert "['OPT']" in out[0]

    def test_colon_forms_treat_both_alike(self) -> None:
        envs = [_env("OPT", "", version="4.5.0")]
        compose = "services:\n  a:\n    command: run ${OPT:-fallback} ${OPT}\n"
        assert fidelity.env_warnings(envs, collection="services", engine=None, compose=compose, version=V45) == []

    def test_a_preserved_null(self) -> None:
        envs = [_env("OPT", None, preserve_compose_null=True, version="4.4.0")]
        compose = "services:\n  a:\n    environment:\n      - OPT\n"
        out = fidelity.env_warnings(envs, collection="services", engine=None, compose=compose, version=V45)
        assert any("['OPT']" in w for w in out)

    def test_not_for_non_compose_resources(self) -> None:
        envs = [_env("OPT", "", version="4.5.0")]
        assert fidelity.env_warnings(envs, collection="applications", engine=None, compose=None, version=V45) == []


# ── servers ──────────────────────────────────────────────────────────────────


class TestServers:
    def test_a_build_server_target_refuses_every_resource(self) -> None:
        out = fidelity.server_refusals(
            {"name": "builder", "settings": {"server_role": "build"}}, collection="databases", source={}
        )
        assert len(out) == 1
        assert "build server" in out[0]

    def test_a_deployment_only_target_refuses_a_building_app_without_an_image(self) -> None:
        target = {"name": "edge", "settings": {"server_role": "deployment"}}
        out = fidelity.server_refusals(target, collection="applications", source={"build_pack": "nixpacks"})
        assert len(out) == 1
        assert "deployments only" in out[0]

    @pytest.mark.parametrize(
        "source",
        [
            {"build_pack": "dockerimage"},
            {"build_pack": "dockercompose"},
            {"build_pack": "nixpacks", "docker_registry_image_name": "ghcr.io/acme/app"},
        ],
    )
    def test_a_deployment_only_target_takes_what_it_can_run(self, source: dict[str, Any]) -> None:
        target = {"settings": {"server_role": "deployment"}}
        assert fidelity.server_refusals(target, collection="applications", source=source) == []

    def test_roles_both_and_absent_are_silent(self) -> None:
        for settings in ({"server_role": "both"}, {}):
            assert fidelity.server_refusals(
                {"settings": settings}, collection="applications", source={"build_pack": "nixpacks"}
            ) == []

    def test_differing_certificate_resolvers(self) -> None:
        out = fidelity.server_warnings(
            {"settings": {"traefik_certificate_resolver": "letsencrypt"}},
            {"settings": {"traefik_certificate_resolver": None}},
        )
        assert len(out) == 1

    def test_same_or_unknown_resolvers_are_silent(self) -> None:
        same = {"settings": {"traefik_certificate_resolver": "letsencrypt"}}
        assert fidelity.server_warnings(same, same) == []
        # Below 4.5 neither server has the key.
        assert fidelity.server_warnings({"settings": {}}, {"settings": {}}) == []
