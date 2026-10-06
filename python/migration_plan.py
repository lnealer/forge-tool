# used for storing the global migration plan variable
from langchain_core.tools import tool
from utils import get_logger

_MIGRATION_PLAN = ""

logger = get_logger()

def configure_migration_plan(migration_plan):
    global _MIGRATION_PLAN
    _MIGRATION_PLAN = migration_plan or ""

@tool
def migration_plan():
    """Retrieve the migration plan from memory."""
    logger.info("Retrieving the migration plan")
    if not _MIGRATION_PLAN:
        return ""
    return _MIGRATION_PLAN