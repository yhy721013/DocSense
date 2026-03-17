from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import requests

from config import AnythingLLMConfig


logger = logging.getLogger(__name__)


@dataclass
class AnythingLLMClient:
    config: AnythingLLMConfig

    def __post_init__(self) -> None:
        # 复用 Session 降低连接开销
        self.session = requests.Session()

    def _build_headers(self, user_id: Optional[int] = None) -> Dict[str, str]:
        headers = {
            "accept": "application/json",
            "Authorization": f"Bearer {self.config.api_key}",
        }
        if user_id is not None:
            headers["X-AnythingLLM-User-Id"] = str(user_id)
        return headers

    def _json_headers(self, user_id: Optional[int] = None) -> Dict[str, str]:
        headers = self._build_headers(user_id)
        headers["Content-Type"] = "application/json"
        return headers

    def list_workspaces(self, user_id: Optional[int] = None) -> List[Dict[str, Any]]:
        url = f"{self.config.base_url}/workspaces"
        try:
            resp = self.session.get(url, headers=self._build_headers(user_id), timeout=self.config.timeout)
            if not resp.ok:
                return []
            body = resp.json()
            return body.get("workspaces", []) if isinstance(body, dict) else []
        except Exception:
            return []

    def find_workspace_by_name(self, name: str, user_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        for workspace in self.list_workspaces(user_id):
            if workspace.get("name") == name:
                return workspace
        return None

    def create_workspace(self, name: str, user_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        url = f"{self.config.base_url}/workspace/new"
        payload = {
            "name": name,
            "similarityThreshold": 0.75,
            "openAiTemp": 0.1,
            "openAiHistory": 1,
            "openAiPrompt": (
                "你是一个文档信息抽取与判断系统。\n"
                "【重要规则】\n"
                "1. 你只能基于已提供的文档内容回答，不得使用常识或猜测。\n"
                "2. 如果文档中不存在相关信息，必须返回 null。\n"
                "3. 你必须只输出合法的 JSON，不得包含任何解释、注释、Markdown 或多余文本。\n"
                "4. JSON 的字段名、层级和类型必须严格保持一致。\n"
                "5. 不允许补充文档中未明确出现的信息。\n"
            ),
            # 返回可解析 JSON，避免“无检索结果”时前端因纯文本报错
            "queryRefusalResponse": (
                '{"outline":[],"security_level":"公开","category_confidence":0.1,'
                '"category":null,"sub_category":null,"category_candidates":[],'
                '"extract":{},"summary":"未能从文档中检索到足够信息"}'
            ),
            "chatMode": "query",
            "topN": 6,
        }
        try:
            resp = self.session.post(
                url,
                headers=self._json_headers(user_id),
                json=payload,
                timeout=self.config.timeout,
            )
            if not resp.ok:
                return None
            body = resp.json()
            return body.get("workspace") or body
        except Exception:
            return None

    def ensure_workspace(self, name: str, user_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        existing = self.find_workspace_by_name(name, user_id=user_id)
        if existing:
            return existing
        return self.create_workspace(name, user_id=user_id)

    def create_thread(
        self,
        workspace_slug: str,
        thread_name: str,
        user_id: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        url = f"{self.config.base_url}/workspace/{workspace_slug}/thread/new"
        payload: Dict[str, Any] = {"name": thread_name or f"thread-{int(time.time())}"}
        if user_id is not None:
            payload["userId"] = user_id

        try:
            resp = self.session.post(
                url,
                headers=self._json_headers(user_id),
                json=payload,
                timeout=self.config.timeout,
            )
            if not resp.ok:
                return None
            body = resp.json()
            return body.get("thread") or body
        except Exception:
            return None

    @staticmethod
    def extract_thread_slug(info: Dict[str, Any]) -> Optional[str]:
        for key in ("slug", "threadSlug", "thread_slug"):
            value = info.get(key)
            if value:
                return str(value)
        return None

    def send_prompt_to_thread(
        self,
        workspace_slug: str,
        thread_slug: str,
        prompt: str,
        user_id: Optional[int] = None,
        document_ids: Optional[List[str]] = None,
        mode: str = "chat",
    ) -> Optional[str]:
        url = f"{self.config.base_url}/workspace/{workspace_slug}/thread/{thread_slug}/chat"
        payload: Dict[str, Any] = {
            "message": prompt,
            "mode": mode,
            "files": document_ids or [],
        }
        if user_id is not None:
            payload["userId"] = user_id

        try:
            resp = self.session.post(
                url,
                headers=self._json_headers(user_id),
                json=payload,
                timeout=self.config.timeout,
                stream=True,
            )
            if not resp.ok:
                return None
            # 兼容 SSE 流式响应，拼接 textResponseChunk
            final_event: Optional[Dict[str, Any]] = None
            chunk_buffer: List[str] = []
            start_time = time.time()
            timeout_seconds = 300
            max_lines = 1000
            line_count = 0

            try:
                for raw_line in resp.iter_lines(decode_unicode=True):
                    if time.time() - start_time > timeout_seconds:
                        break
                    line_count += 1
                    if line_count > max_lines:
                        break
                    if not raw_line:
                        continue
                    line = raw_line.strip()
                    if not line:
                        continue
                    if line.startswith("data:"):
                        line = line[5:].strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    event_type = event.get("type")
                    text_chunk = event.get("textResponse")

                    if event_type == "textResponseChunk" and isinstance(text_chunk, str):
                        chunk_buffer.append(text_chunk)

                    if event.get("close") or event_type == "textResponse":
                        final_event = event
                        break

                if final_event is None and chunk_buffer:
                    final_event = {"textResponse": "".join(chunk_buffer)}
            finally:
                resp.close()

            if not final_event:
                return None

            model_answer = final_event.get("textResponse", final_event)
            if isinstance(model_answer, str):
                # 清理思维标记与代码块，尽量得到纯 JSON 字符串
                cleaned = model_answer.split("</think>")[-1] if "</think>" in model_answer else model_answer
                cleaned = cleaned.replace("<think>", "")
                match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned, flags=re.IGNORECASE)
                if match:
                    cleaned = match.group(1)
                result = cleaned.strip()
                return result if result else None

            result = json.dumps(model_answer, ensure_ascii=False)
            if not result or result in {"{}", "null"}:
                return None
            return result
        except Exception:
            return None

    def upload_document(self, file_path: str, user_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        url = f"{self.config.base_url}/document/upload"
        try:
            with open(file_path, "rb") as f:
                files = {"file": (os.path.basename(file_path), f)}
                resp = self.session.post(
                    url,
                    headers=self._build_headers(user_id),
                    files=files,
                    timeout=self.config.timeout,
                )
            if not resp.ok:
                return None
            body = resp.json()
            documents = body.get("documents")
            if isinstance(documents, list) and documents:
                return documents[0]
            return None
        except Exception:
            return None

    def fetch_workspace_document(
        self,
        workspace_slug: str,
        doc_path: str,
        user_id: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        if not doc_path:
            return None

        target_docpath = doc_path.replace("\\", "/")
        if "custom-documents/" in target_docpath:
            target_docpath = "custom-documents/" + target_docpath.split("custom-documents/")[1]

        url = f"{self.config.base_url}/workspace/{workspace_slug}"
        try:
            resp = self.session.get(url, headers=self._json_headers(user_id), timeout=self.config.timeout)
            if not resp.ok:
                return None
            workspace = resp.json().get("workspace")
            if isinstance(workspace, list):
                workspace = workspace[0] if workspace else None
            if not isinstance(workspace, dict):
                return None

            for item in workspace.get("documents", []):
                item_docpath = item.get("docpath", "").replace("\\", "/")
                if item_docpath == target_docpath:
                    return item
            return None
        except Exception:
            return None

    def wait_for_processing(self, doc_relative_path: str, retries: int = 300, delay: float = 2.0) -> bool:
        # 根据配置/平台推导的 storage 根路径轮询文档文件是否生成。
        # 若 storage 根路径不可用（如服务端部署无本地 documents 目录），保守降级为“跳过等待”。
        storage_root = self._resolve_storage_root()
        if not storage_root:
            logger.warning("未配置可用的 AnythingLLM storage 根路径，跳过处理等待")
            return True

        documents_root = os.path.normpath(os.path.join(storage_root, "documents"))
        if not os.path.isdir(documents_root):
            logger.warning("AnythingLLM documents 目录不存在: %s，跳过处理等待", documents_root)
            return True

        safe_relative_path = doc_relative_path.replace("\\", "/").strip("/")
        target_path = os.path.normpath(os.path.join(documents_root, safe_relative_path))
        documents_root_abs = os.path.abspath(documents_root)
        target_abs = os.path.abspath(target_path)

        # 在 Windows 上，若盘符不同，os.path.commonpath 会抛 ValueError，这里先显式检查盘符。
        if os.name == "nt":
            root_drive, _ = os.path.splitdrive(documents_root_abs)
            target_drive, _ = os.path.splitdrive(target_abs)
            if root_drive.lower() != target_drive.lower():
                logger.warning("检测到不同盘符的 doc 路径，拒绝等待: %s", doc_relative_path)
                return False
        try:
            if os.path.commonpath([documents_root_abs, target_abs]) != documents_root_abs:
                logger.warning("检测到异常 doc 路径，拒绝等待: %s", doc_relative_path)
                return False
        except ValueError:
            logger.warning("检测到不可比较的 doc 路径，拒绝等待: %s", doc_relative_path)
            return False

        for attempt in range(retries):
            if os.path.exists(target_path):
                return True
            time.sleep(delay)
        return False

    def _resolve_storage_root(self) -> Optional[str]:
        configured_root = (self.config.storage_root or "").strip()
        if configured_root:
            return configured_root

        if os.name == "nt":
            appdata = os.getenv("APPDATA", "").strip()
            if not appdata:
                return None
            return os.path.join(appdata, "anythingllm-desktop", "storage")

        return os.path.expanduser("~/.anythingllm/storage")

    def update_embeddings(
        self,
        doc_path: str,
        workspace_slug: str,
        user_id: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        if not doc_path:
            return False

        # 统一 docpath 格式，避免 Windows 分隔符影响匹配
        cleaned_path = doc_path.replace("\\", "/")
        if "custom-documents/" in cleaned_path:
            cleaned_path = cleaned_path.split("custom-documents/")[-1]
            cleaned_path = f"custom-documents/{cleaned_path}"
        elif cleaned_path.startswith("/"):
            cleaned_path = cleaned_path.lstrip("/")

        url = f"{self.config.base_url}/workspace/{workspace_slug}/update-embeddings"
        payload = {"adds": [cleaned_path]}

        try:
            resp = self.session.post(
                url,
                headers=self._json_headers(user_id),
                json=payload,
                timeout=self.config.timeout,
            )
            if not resp.ok:
                return False

            pin_url = f"{self.config.base_url}/workspace/{workspace_slug}/update-pin"
            pin_payload = {"docPath": cleaned_path, "pinStatus": True}
            self.session.post(
                pin_url,
                headers=self._json_headers(user_id),
                json=pin_payload,
                timeout=self.config.timeout,
            )

            # 更新文档元数据
            if metadata:
                meta_url = f"{self.config.base_url}/document/meta"
                meta_payload = {"location": cleaned_path, "metadata": metadata}
                self.session.post(
                    meta_url,
                    headers=self._json_headers(user_id),
                    json=meta_payload,
                    timeout=self.config.timeout,
                )

            return True
        except Exception:
            return False
