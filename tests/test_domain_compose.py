"""Tests for compose analysis.

`build_services` is the function that answers the user's constraint: "compose
stacks can also build when they run from src with a Dockerfile and not from an
image". `topology_fingerprint` is what turns a silently-wrong volume mapping into
a blocked migration.
"""

from __future__ import annotations

import textwrap

import pytest

from bg_coolify_migrate.domain.compose import (
    ComposeError,
    MountClass,
    build_services,
    builds_from_source,
    data_mounts,
    declared_volume_names,
    has_anonymous_volumes,
    mounts,
    parse,
    services,
    topology_fingerprint,
)


def _c(text: str) -> dict:
    return parse(textwrap.dedent(text))


class TestParse:
    def test_rejects_invalid_yaml(self) -> None:
        with pytest.raises(ComposeError, match="not valid YAML"):
            parse("services:\n  a: [unclosed")

    def test_rejects_empty(self) -> None:
        with pytest.raises(ComposeError, match="empty"):
            parse("")

    def test_rejects_non_mapping(self) -> None:
        with pytest.raises(ComposeError, match="must be a mapping"):
            parse("- just\n- a\n- list")

    def test_tolerates_null_service_body(self) -> None:
        doc = _c("""
            services:
              placeholder:
        """)
        assert services(doc) == {"placeholder": {}}


class TestBuildServices:
    def test_image_only_stack_does_not_build(self) -> None:
        doc = _c("""
            services:
              web:
                image: nginx:1.25
              db:
                image: postgres:16
        """)
        assert build_services(doc) == []
        assert builds_from_source(doc) is False

    def test_short_form_build_detected(self) -> None:
        doc = _c("""
            services:
              web:
                build: ./app
        """)
        assert build_services(doc) == ["web"]
        assert builds_from_source(doc) is True

    def test_long_form_build_detected(self) -> None:
        doc = _c("""
            services:
              web:
                build:
                  context: .
                  dockerfile: Dockerfile
        """)
        assert build_services(doc) == ["web"]

    def test_build_wins_over_image_when_both_present(self) -> None:
        # Legal compose: `image` names the build OUTPUT. It still builds.
        doc = _c("""
            services:
              web:
                build: .
                image: myorg/web:latest
        """)
        assert build_services(doc) == ["web"]
        assert builds_from_source(doc) is True

    def test_mixed_stack_reports_only_builders(self) -> None:
        doc = _c("""
            services:
              web:
                build: ./web
              worker:
                build: ./worker
              cache:
                image: redis:7
        """)
        assert build_services(doc) == ["worker", "web"] or build_services(doc) == ["web", "worker"]
        assert set(build_services(doc)) == {"web", "worker"}


class TestDeclaredVolumeNames:
    def test_uses_explicit_name_override(self) -> None:
        # Coolify emits explicit `name:` keys so there is no compose-project
        # prefix; the key and the real docker name differ.
        doc = _c("""
            services:
              db:
                image: postgres:16
            volumes:
              data:
                name: abc123_data
        """)
        assert declared_volume_names(doc) == ["abc123_data"]

    def test_falls_back_to_key(self) -> None:
        doc = _c("""
            volumes:
              data:
              cache: {}
        """)
        assert declared_volume_names(doc) == ["cache", "data"]

    def test_absent_volumes_is_empty(self) -> None:
        assert declared_volume_names(_c("services:\n  a:\n    image: x")) == []


class TestMounts:
    def test_named_volume_short_syntax(self) -> None:
        doc = _c("""
            services:
              db:
                image: postgres:16
                volumes:
                  - pgdata:/var/lib/postgresql/data
        """)
        (m,) = mounts(doc)
        assert m.mount_class is MountClass.NAMED
        assert m.source == "pgdata"
        assert m.target == "/var/lib/postgresql/data"
        assert m.service == "db"
        assert m.read_only is False

    def test_bind_mount_absolute(self) -> None:
        doc = _c("""
            services:
              app:
                volumes:
                  - /srv/data:/data
        """)
        (m,) = mounts(doc)
        assert m.mount_class is MountClass.BIND
        assert m.source == "/srv/data"

    @pytest.mark.parametrize("prefix", ["./rel", "../up", "~/home"])
    def test_relative_and_home_sources_are_binds(self, prefix: str) -> None:
        doc = parse(f"services:\n  app:\n    volumes:\n      - {prefix}:/data")
        (m,) = mounts(doc)
        assert m.mount_class is MountClass.BIND

    def test_read_only_flag(self) -> None:
        doc = _c("""
            services:
              app:
                volumes:
                  - config:/etc/app:ro
        """)
        (m,) = mounts(doc)
        assert m.read_only is True
        assert m.target == "/etc/app"

    def test_rw_and_selinux_modes_are_stripped(self) -> None:
        doc = _c("""
            services:
              app:
                volumes:
                  - data:/data:rw
                  - other:/other:z
        """)
        targets = {m.target for m in mounts(doc)}
        assert targets == {"/data", "/other"}

    def test_bare_path_is_anonymous_volume(self) -> None:
        # `- /data` with no source: docker invents a 64-hex id.
        doc = _c("""
            services:
              app:
                volumes:
                  - /data
        """)
        (m,) = mounts(doc)
        assert m.mount_class is MountClass.ANONYMOUS
        assert m.source is None
        assert m.target == "/data"

    def test_docker_sock_is_passthrough(self) -> None:
        doc = _c("""
            services:
              agent:
                volumes:
                  - /var/run/docker.sock:/var/run/docker.sock
        """)
        (m,) = mounts(doc)
        assert m.mount_class is MountClass.PASSTHROUGH

    def test_tmp_is_passthrough(self) -> None:
        doc = _c("""
            services:
              app:
                volumes:
                  - /tmp:/tmp
        """)
        (m,) = mounts(doc)
        assert m.mount_class is MountClass.PASSTHROUGH

    def test_cifs_volume_detected_via_driver_opts(self) -> None:
        # Coolify skips these; the share is already shared, copying duplicates.
        doc = _c("""
            services:
              app:
                volumes:
                  - nas:/mnt/nas
            volumes:
              nas:
                driver_opts:
                  type: cifs
                  device: //server/share
        """)
        (m,) = mounts(doc)
        assert m.mount_class is MountClass.CIFS

    def test_long_syntax_volume(self) -> None:
        doc = _c("""
            services:
              db:
                volumes:
                  - type: volume
                    source: pgdata
                    target: /var/lib/postgresql/data
                    read_only: false
        """)
        (m,) = mounts(doc)
        assert m.mount_class is MountClass.NAMED
        assert m.source == "pgdata"

    def test_long_syntax_bind(self) -> None:
        doc = _c("""
            services:
              app:
                volumes:
                  - type: bind
                    source: /srv/x
                    target: /x
        """)
        (m,) = mounts(doc)
        assert m.mount_class is MountClass.BIND

    def test_long_syntax_tmpfs(self) -> None:
        doc = _c("""
            services:
              app:
                volumes:
                  - type: tmpfs
                    target: /scratch
        """)
        (m,) = mounts(doc)
        assert m.mount_class is MountClass.TMPFS

    def test_long_syntax_without_target_raises(self) -> None:
        doc = _c("""
            services:
              app:
                volumes:
                  - type: volume
                    source: x
        """)
        with pytest.raises(ComposeError, match="without `target`"):
            mounts(doc)

    def test_tmpfs_key_is_surfaced(self) -> None:
        doc = _c("""
            services:
              app:
                tmpfs:
                  - /run
        """)
        (m,) = mounts(doc)
        assert m.mount_class is MountClass.TMPFS
        assert m.target == "/run"

    def test_unparseable_spec_raises(self) -> None:
        doc = _c("""
            services:
              app:
                volumes:
                  - a:b:c:d:e
        """)
        with pytest.raises(ComposeError, match="cannot parse volume spec"):
            mounts(doc)

    def test_unsupported_entry_type_raises(self) -> None:
        doc = _c("""
            services:
              app:
                volumes:
                  - 42
        """)
        with pytest.raises(ComposeError, match="unsupported volume entry"):
            mounts(doc)


class TestDataMounts:
    def test_only_returns_bytes_that_must_move(self) -> None:
        doc = _c("""
            services:
              app:
                volumes:
                  - data:/data
                  - /srv/host:/host
                  - /var/run/docker.sock:/var/run/docker.sock
                tmpfs:
                  - /scratch
        """)
        classes = {m.mount_class for m in data_mounts(doc)}
        assert classes == {MountClass.NAMED, MountClass.BIND}


class TestAnonymousVolumes:
    def test_detected(self) -> None:
        doc = _c("""
            services:
              app:
                volumes:
                  - /data
                  - named:/named
        """)
        anon = has_anonymous_volumes(doc)
        assert len(anon) == 1
        assert anon[0].target == "/data"

    def test_none_when_all_named(self) -> None:
        doc = _c("services:\n  app:\n    volumes:\n      - n:/n")
        assert has_anonymous_volumes(doc) == []


class TestTopologyFingerprint:
    def test_stable_across_reparse(self) -> None:
        text = """
            services:
              db:
                image: postgres:16
                volumes:
                  - pgdata:/var/lib/postgresql/data
            volumes:
              pgdata:
        """
        assert topology_fingerprint(_c(text)) == topology_fingerprint(_c(text))

    def test_ignores_image_tag_changes(self) -> None:
        # An image bump does not move any bytes, so it must not trip the gate.
        a = _c("""
            services:
              db:
                image: postgres:16
                volumes:
                  - pgdata:/var/lib/postgresql/data
            volumes:
              pgdata:
        """)
        b = _c("""
            services:
              db:
                image: postgres:17
                volumes:
                  - pgdata:/var/lib/postgresql/data
            volumes:
              pgdata:
        """)
        assert topology_fingerprint(a) == topology_fingerprint(b)

    def test_ignores_ports_env_and_labels(self) -> None:
        a = _c("""
            services:
              web:
                image: nginx
                volumes:
                  - w:/w
            volumes:
              w:
        """)
        b = _c("""
            services:
              web:
                image: nginx
                ports:
                  - "8080:80"
                environment:
                  FOO: bar
                labels:
                  traefik.enable: "true"
                volumes:
                  - w:/w
            volumes:
              w:
        """)
        assert topology_fingerprint(a) == topology_fingerprint(b)

    def test_detects_renamed_volume(self) -> None:
        # THE case this exists for: a renamed volume silently invalidates the
        # old->new mapping computed from the source.
        a = _c("""
            services:
              db:
                volumes:
                  - pgdata:/var/lib/postgresql/data
            volumes:
              pgdata:
        """)
        b = _c("""
            services:
              db:
                volumes:
                  - postgres_data:/var/lib/postgresql/data
            volumes:
              postgres_data:
        """)
        assert topology_fingerprint(a) != topology_fingerprint(b)

    def test_detects_added_volume(self) -> None:
        a = _c("services:\n  db:\n    volumes:\n      - a:/a\nvolumes:\n  a:")
        b = _c("services:\n  db:\n    volumes:\n      - a:/a\n      - b:/b\nvolumes:\n  a:\n  b:")
        assert topology_fingerprint(a) != topology_fingerprint(b)

    def test_detects_renamed_service(self) -> None:
        # Coolify derives volume names from service names, so this matters.
        a = _c("services:\n  db:\n    volumes:\n      - a:/a\nvolumes:\n  a:")
        b = _c("services:\n  database:\n    volumes:\n      - a:/a\nvolumes:\n  a:")
        assert topology_fingerprint(a) != topology_fingerprint(b)

    def test_detects_build_added(self) -> None:
        a = _c("services:\n  web:\n    image: nginx")
        b = _c("services:\n  web:\n    build: .")
        assert topology_fingerprint(a) != topology_fingerprint(b)

    def test_detects_changed_mount_path(self) -> None:
        a = _c("services:\n  db:\n    volumes:\n      - a:/old\nvolumes:\n  a:")
        b = _c("services:\n  db:\n    volumes:\n      - a:/new\nvolumes:\n  a:")
        assert topology_fingerprint(a) != topology_fingerprint(b)
