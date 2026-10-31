from alembic import command
from alembic.config import Config

from leadgen.config import PROJECT_ROOT


def upgrade(url: str) -> None:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.attributes["url"] = url
    command.upgrade(cfg, "head")
