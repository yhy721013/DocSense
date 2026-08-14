"""武器谱基础设施适配器的稳定导出面。"""

from .anythingllm_clients import (
    AnythingLLMWeaponryClientFactory,
    WeaponryAnythingLLMClientFactoryProtocol,
    WeaponryAnythingLLMClients,
)
from .anythingllm_retrieval import (
    AnythingLLMTargetEvidenceRetrievalAdapter,
    normalize_anythingllm_source_url_ref,
    resolve_anythingllm_source_document_key,
)
from .anythingllm_resource_cleanup import (
    AnythingLLMWeaponryResourceCleanupAdapter,
)
from .callback_guard import SQLiteWeaponryCallbackAdapter
from .callback_recovery import SQLiteWeaponryCallbackRecoverySource
from .creation_intent_recovery import (
    AnythingLLMWeaponryCreationIntentRecoveryAdapter,
    WeaponryCreationIntentRecoveryResult,
)
from .knowledge_documents import (
    DatabaseServiceWeaponryDocumentScopeAdapter,
    SQLiteWeaponryDocumentScopeAdapter,
)
from .task_codec import WeaponryTaskCommandCodec
from .v2_callback import TaskControlWeaponryCallbackAdapter
from .v2_callback_recovery import SQLiteWeaponryV2CallbackRecoverySource
from .v2_runtime import (
    WeaponryV2Maintenance,
    WeaponryV2ResultMetrics,
    WeaponryV2TaskDispatcher,
)
from .no_auxiliary_guidance import NoAuxiliaryGuidanceAdapter
from .production_profile import (
    WeaponryProductionSelectionProfileConfig,
    build_weaponry_production_selection_policy,
)
from .production_gate import (
    WEAPONRY_PRODUCTION_ATTESTATION_SCHEMA,
    WeaponryProductionGateSnapshot,
    build_weaponry_production_attestation,
    evaluate_weaponry_production_gate,
)
from .provided_evidence_extraction import (
    AnythingLLMProvidedEvidenceExtractionAdapter,
)
from .resource_registration import (
    StoreBackedWeaponryResourceRegistrar,
    WeaponryCreatedResourceRegistrarProtocol,
)
from .sqlite import (
    SQLiteWeaponryCreationIntentStoreAdapter,
    SQLiteWeaponryInteractionAuditAdapter,
    SQLiteWeaponryResourceStoreAdapter,
)
from .terms_rule_guidance import (
    AnythingLLMReadOnlyTermsRuleProvider,
    CatalogRoutingTermsRuleProviderProtocol,
    TermsRuleChunk,
    TermsRuleGuidanceAdapter,
    TermsRuleProviderProtocol,
)
from .terms_catalog import (
    AnythingLLMTermsCatalogCoordinator,
    SQLiteTermsCatalogStateStore,
    TERMS_CATALOG_FINGERPRINT_SCHEMA,
    TermsCatalogDescriptor,
    TermsCatalogManifest,
    TermsCatalogSynchronizationError,
    TermsCatalogSyncPlan,
    TermsCatalogValidationError,
    TermsCatalogWorkspaceResolver,
    build_terms_catalog_manifest,
    workspace_name_for_fingerprint,
)
from .translation import (
    TextTranslatorWeaponryAdapter,
    TranslationEngineWeaponryAdapter,
    WeaponryTextTranslatorProtocol,
)
from .runtime_config import (
    WEAPONRY_RUNTIME_MODE_SINGLE_INSTANCE,
    WeaponryRuntimeConfig,
    WeaponryRuntimeConfigurationError,
    WeaponryRuntimeCapabilities,
    WeaponryRuntimePolicies,
    build_weaponry_runtime_policies,
    load_weaponry_runtime_config,
    validate_weaponry_runtime_capabilities,
)
from .local_dispatcher import (
    LocalWeaponryDispatcherSnapshot,
    LocalWeaponryTaskDispatcher,
)

__all__ = [
    "SQLiteWeaponryCreationIntentStoreAdapter",
    "AnythingLLMWeaponryCreationIntentRecoveryAdapter",
    "WeaponryCreationIntentRecoveryResult",
    "AnythingLLMProvidedEvidenceExtractionAdapter",
    "AnythingLLMReadOnlyTermsRuleProvider",
    "AnythingLLMTermsCatalogCoordinator",
    "AnythingLLMTargetEvidenceRetrievalAdapter",
    "normalize_anythingllm_source_url_ref",
    "resolve_anythingllm_source_document_key",
    "AnythingLLMWeaponryResourceCleanupAdapter",
    "AnythingLLMWeaponryClientFactory",
    "WEAPONRY_RUNTIME_MODE_SINGLE_INSTANCE",
    "DatabaseServiceWeaponryDocumentScopeAdapter",
    "TextTranslatorWeaponryAdapter",
    "TranslationEngineWeaponryAdapter",
    "LocalWeaponryDispatcherSnapshot",
    "LocalWeaponryTaskDispatcher",
    "NoAuxiliaryGuidanceAdapter",
    "CatalogRoutingTermsRuleProviderProtocol",
    "SQLiteTermsCatalogStateStore",
    "SQLiteWeaponryInteractionAuditAdapter",
    "SQLiteWeaponryCallbackAdapter",
    "SQLiteWeaponryCallbackRecoverySource",
    "SQLiteWeaponryResourceStoreAdapter",
    "SQLiteWeaponryDocumentScopeAdapter",
    "StoreBackedWeaponryResourceRegistrar",
    "TermsRuleChunk",
    "TermsRuleGuidanceAdapter",
    "TermsRuleProviderProtocol",
    "TERMS_CATALOG_FINGERPRINT_SCHEMA",
    "TermsCatalogDescriptor",
    "TermsCatalogManifest",
    "TermsCatalogSynchronizationError",
    "TermsCatalogSyncPlan",
    "TermsCatalogValidationError",
    "TermsCatalogWorkspaceResolver",
    "WeaponryAnythingLLMClientFactoryProtocol",
    "WeaponryAnythingLLMClients",
    "WeaponryCreatedResourceRegistrarProtocol",
    "WeaponryProductionSelectionProfileConfig",
    "WEAPONRY_PRODUCTION_ATTESTATION_SCHEMA",
    "WeaponryProductionGateSnapshot",
    "build_weaponry_production_attestation",
    "evaluate_weaponry_production_gate",
    "WeaponryRuntimeConfig",
    "WeaponryRuntimeConfigurationError",
    "WeaponryRuntimeCapabilities",
    "WeaponryRuntimePolicies",
    "WeaponryTaskCommandCodec",
    "TaskControlWeaponryCallbackAdapter",
    "SQLiteWeaponryV2CallbackRecoverySource",
    "WeaponryV2Maintenance",
    "WeaponryV2ResultMetrics",
    "WeaponryV2TaskDispatcher",
    "WeaponryTextTranslatorProtocol",
    "build_weaponry_production_selection_policy",
    "build_weaponry_runtime_policies",
    "build_terms_catalog_manifest",
    "load_weaponry_runtime_config",
    "validate_weaponry_runtime_capabilities",
    "workspace_name_for_fingerprint",
]
