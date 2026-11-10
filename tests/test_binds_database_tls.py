"""A database's own TLS certificate files are skipped, never refused.

Found by the e2e run (test_tls_database_warns_and_migrates_plaintext): with
TLS on, every start of a standalone database writes its certificate and key to
``/data/coolify/databases/{uuid}/ssl/`` and keeps them as FILE STORAGE rows with
the content (SslHelper::generateSslCertificate v4.7.5:176-223). The bind-mount
rules refused every content file of a database ("CREATE carries no storage row
of a database"), so every TLS database was refused at PLAN. Those files are not
the database's data, and not the target's: it starts without TLS and gets a
certificate of its own server's CA once TLS is switched on there.
"""

from __future__ import annotations

import pytest

from bg_coolify_migrate.domain import binds
from bg_coolify_migrate.domain.binds import BindAction, StorageCarry, StorageRow
from bg_coolify_migrate.domain.kinds import ResourceKind
from bg_coolify_migrate.domain.manifest import ApiStorage, Decision, DockerMount, reconcile

DB = "/data/coolify/databases/u1"


def _classify(
    source: str, destination: str, *, carry: StorageCarry = StorageCarry.NONE
) -> tuple[BindAction, str]:
    return binds.classify(
        source=source,
        destination=destination,
        read_only=False,
        uuid="u1",
        rows=[StorageRow(destination, source, writes_content=True)],
        carry=carry,
    )


class TestTheRule:
    @pytest.mark.parametrize(
        ("cert_dir", "name"),
        [
            ("/var/lib/postgresql/certs", "server.crt"),
            ("/var/lib/postgresql/certs", "server.key"),
            ("/etc/mysql/certs", "server.crt"),
            ("/etc/mongo/certs", "server.pem"),
            ("/etc/redis/certs", "server.key"),
            ("/etc/keydb/certs", "server.crt"),
            ("/etc/dragonfly/certs", "server.key"),
        ],
    )
    def test_every_engine_s_certificate_is_skipped(self, cert_dir: str, name: str) -> None:
        action, reason = _classify(f"{DB}/ssl/{name}", f"{cert_dir}/{name}")
        assert action is BindAction.SKIP
        assert "TLS certificate" in reason and "own server's CA" in reason

    def test_any_other_content_file_of_a_database_is_still_refused(self) -> None:
        action, reason = _classify(f"{DB}/pg_hba.conf", "/etc/postgresql/pg_hba.conf")
        assert action is BindAction.REFUSE
        assert "carries no storage row of a database" in reason

    @pytest.mark.parametrize(
        ("source", "destination"),
        [
            (f"{DB}/ssl/server.crt", "/etc/ssl/server.crt"),  # not an engine's cert dir
            (f"{DB}/ssl/server.crt", "/var/lib/postgresql/certs/server.key"),  # another file
            (f"{DB}/server.crt", "/var/lib/postgresql/certs/server.crt"),  # not in ssl/
            (f"{DB}/ssl/ca.crt", "/var/lib/postgresql/certs/ca.crt"),  # not Coolify's name
            ("/data/coolify/databases/u2/ssl/server.crt", "/var/lib/postgresql/certs/server.crt"),
        ],
    )
    def test_only_coolify_s_exact_files_count(self, source: str, destination: str) -> None:
        assert not binds.is_database_tls_certificate(source, destination, "u1")
        assert _classify(source, destination)[0] is BindAction.REFUSE

    def test_an_application_keeps_its_own_rules(self) -> None:
        # Only a database's rows are never carried; an application's content
        # file is recreated by CREATE and skipped for that reason.
        app = "/data/coolify/applications/u1/ssl/server.crt"
        action, reason = _classify(
            app, "/var/lib/postgresql/certs/server.crt", carry=StorageCarry.API
        )
        assert action is BindAction.SKIP
        assert "CREATE recreates its storage row" in reason


class TestThePlan:
    def test_a_tls_postgres_plans_with_its_certificates_skipped(self) -> None:
        mounts = [
            DockerMount(
                container="c1", type="volume", name="postgres-data-u1", destination="/data"
            ),
            *(
                DockerMount(
                    container="c1",
                    type="bind",
                    source=f"{DB}/ssl/{name}",
                    destination=f"/var/lib/postgresql/certs/{name}",
                )
                for name in ("server.crt", "server.key")
            ),
        ]
        storages = [
            ApiStorage(kind="persistent", name="postgres-data-u1", mount_path="/data"),
            *(
                ApiStorage(
                    kind="file",
                    mount_path=f"/var/lib/postgresql/certs/{name}",
                    fs_path=f"{DB}/ssl/{name}",
                    has_content=True,
                )
                for name in ("server.crt", "server.key")
            ),
        ]

        manifest = reconcile(
            docker_mounts=mounts,
            api_storages=storages,
            resource_uuid="u1",
            kind=ResourceKind.DATABASE,
        )

        assert not manifest.refused
        certs = [i for i in manifest.items if "/ssl/" in i.source_path]
        assert len(certs) == 2
        assert all(i.decision is Decision.SKIP for i in certs)
        # The target has no such row to read back at the end of CREATE.
        assert not any(i.content_file for i in certs)
        assert [i.source_name for i in manifest.to_migrate] == ["postgres-data-u1"]
