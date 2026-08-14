"""检查武器谱真实供应商能力证明，供发布门禁和离线运维使用。"""

from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import sys

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env", override=False)

from app.modules.weaponry.adapters import (  # noqa: E402
    build_weaponry_runtime_policies,
    evaluate_weaponry_production_gate,
    load_weaponry_runtime_config,
    WeaponryRuntimeConfigurationError,
)


def main() -> int:
    """输出稳定 JSON；就绪返回 0，缺失或不匹配返回 1。"""

    try:
        config = load_weaponry_runtime_config()
        policies = build_weaponry_runtime_policies(config)
    except (TypeError, ValueError, WeaponryRuntimeConfigurationError):
        # stdout 是发布系统消费的稳定协议。具体配置异常由部署日志和同一环境中的应用
        # 启动校验输出，避免把 API Key、路径或供应商细节意外写入流水线制品。
        sys.stdout.write(
            json.dumps(
                {
                    "attestation_digest": "",
                    "expires_at": "",
                    "profile_id": "",
                    "ready": False,
                    "reason": "production_gate_configuration_invalid",
                    "verified_at": "",
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        )
        return 1
    result = evaluate_weaponry_production_gate(
        attestation_path=config.production_attestation_path,
        profile_id=policies.evidence_selection.profile_id,
        fingerprints={
            "provider": config.provider_fingerprint,
            "embedding": config.embedding_fingerprint,
            "documentProcessing": config.document_processing_fingerprint,
            "extractionModel": config.extraction_model_fingerprint,
        },
    )
    # stdout 是部署脚本消费的机器协议，因此直接写一行稳定 JSON；运行诊断仍由应用
    # 日志承担，避免在生产/脚本源码重新引入难以分级和采集的 print。
    sys.stdout.write(
        json.dumps(asdict(result), ensure_ascii=False, sort_keys=True) + "\n"
    )
    return 0 if result.ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
