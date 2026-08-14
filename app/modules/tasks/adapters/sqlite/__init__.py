"""Task Control SQLite 基础设施。

阶段 2-2 按顺序开放 Bootstrap/Schema、Connection/UoW 和 Store 能力。包级仅导出已经完成
验收的能力对象；原始 Connection、Schema DDL helper 和单笔 Transaction 不向业务层导出。
"""

from .bootstrap import (
    TaskControlBootstrapError,
    TaskControlBootstrapResult,
    bootstrap_task_control_database,
    validate_existing_task_control_database,
)
from .schema import (
    ROOT_MANIFEST_FINGERPRINT,
    TaskControlDatabaseIdentity,
    TaskControlSchemaError,
    component_schema_ddl,
    install_component_schema,
)
from .connection import SQLiteConnectionFactory, SQLiteConnectionFactoryError
from .transaction import (
    SQLiteBusyError,
    SQLiteTransactionError,
    SQLiteTransactionManager,
)
from .unit_of_work import (
    SQLiteCallbackDeliveryUnitOfWorkFactory,
    SQLiteTaskAdmissionUnitOfWorkFactory,
    SQLiteTaskExecutionUnitOfWorkFactory,
    SQLiteTaskControlQueryUnitOfWorkFactory,
    SQLiteTaskRecoveryUnitOfWorkFactory,
)
from .control_store import SQLiteTaskControlStore
from .callback_control_store import SQLiteCallbackControlStore
from .composition import (
    SQLiteTaskControlUnitOfWorkFactories,
    build_sqlite_task_control_uow_factories,
)


__all__ = [
    "ROOT_MANIFEST_FINGERPRINT",
    "SQLiteBusyError",
    "SQLiteConnectionFactory",
    "SQLiteConnectionFactoryError",
    "SQLiteCallbackControlStore",
    "SQLiteCallbackDeliveryUnitOfWorkFactory",
    "SQLiteTaskAdmissionUnitOfWorkFactory",
    "SQLiteTaskControlStore",
    "SQLiteTaskControlUnitOfWorkFactories",
    "SQLiteTaskExecutionUnitOfWorkFactory",
    "SQLiteTaskControlQueryUnitOfWorkFactory",
    "SQLiteTaskRecoveryUnitOfWorkFactory",
    "SQLiteTransactionError",
    "SQLiteTransactionManager",
    "TaskControlBootstrapError",
    "TaskControlBootstrapResult",
    "TaskControlDatabaseIdentity",
    "TaskControlSchemaError",
    "component_schema_ddl",
    "install_component_schema",
    "bootstrap_task_control_database",
    "validate_existing_task_control_database",
    "build_sqlite_task_control_uow_factories",
]
