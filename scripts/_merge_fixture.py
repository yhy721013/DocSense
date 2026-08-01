"""Temporary script: merge architectureList into analysis_request fixture."""
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

root = Path(r"D:\2026\DocSense")
tree_data = json.loads((root / "文件解析领域树.json").read_text(encoding="utf-8"))
arch_list = tree_data["params"][0]["architectureList"]
logger.info("architectureList node count: %d", len(arch_list))

fixture_path = root / "tests" / "fixtures" / "llm" / "analysis_request.json"
fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
logger.info("fixture params count: %d", len(fixture["params"]))

for item in fixture["params"]:
    item["architectureList"] = arch_list

fixture_path.write_text(
    json.dumps(fixture, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
logger.info("Updated fixture written: %s", fixture_path)
logger.info("New fixture size: %d bytes", fixture_path.stat().st_size)
