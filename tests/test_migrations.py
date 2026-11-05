from alembic import command

from tests.conftest import alembic_config


def test_models_match_migrations(database_url: str) -> None:
    command.check(alembic_config(database_url))


def test_downgrade_and_upgrade_roundtrip(database_url: str) -> None:
    config = alembic_config(database_url)

    command.downgrade(config, "base")
    command.upgrade(config, "head")
