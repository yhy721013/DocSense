import os
import asyncio
from pathlib import Path
from typing import Optional, List
import tempfile
import httpx

from mineru.cli import api_client as _api_client
from mineru.cli.common import image_suffixes, office_suffixes, pdf_suffixes
from mineru.utils.guess_suffix_or_lang import guess_suffix_by_path

SUPPORTED_INPUT_SUFFIXES = set(pdf_suffixes + image_suffixes + office_suffixes)


class MinerUConverter:
    """
    MinerU 文档转 Markdown 工具类
    支持：PDF, DOCX, PPTX, PNG/JPG, 扫描件
    输出：markdown + 图片
    """

    def __init__(self, output_dir: str = "./mineru_output"):
        """
        初始化 MinerU 转换器
        :param output_dir: 输出目录
        """
        self.output_dir = Path(output_dir).expanduser().resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # 【新增】设置使用本地模型，避免自动下载
        os.environ['MINERU_MODEL_SOURCE'] = "local"
        print(f"[MinerU] 已设置模型来源为本地模式 (MINERU_MODEL_SOURCE=local)")

    def convert_to_markdown(
            self,
            input_path: str,
            use_ocr: bool = False,
            lang: str = "ch",
            extract_images: bool = True,
            formula_enable: bool = True,
            table_enable: bool = True,
            backend: str = "pipeline",
            api_url: Optional[str] = None,
            server_url: Optional[str] = None,
            output_subdir: Optional[str] = None
    ) -> str:
        """
        单个文件 / 文件夹 转 Markdown
        :param input_path: 单个文件路径 或 文件夹路径
        :param use_ocr: 是否开启 OCR（扫描 PDF、图片必开）
        :param lang: 语言 zh / en
        :param extract_images: 是否提取图片
        :param formula_enable: 是否启用公式识别
        :param table_enable: 是否启用表格识别
        :param backend: 后端类型
        :param api_url: 远程 API URL（None 则启动本地服务）
        :param server_url: 服务器 URL（仅 http-client 模式需要）
        :param output_subdir: 输出子目录名（用于区分不同文件的输出）
        :return: 输出的 markdown 文件路径
        """
        input_path = Path(input_path).expanduser().resolve()

        if not input_path.exists():
            raise FileNotFoundError(f"Input path does not exist: {input_path}")

        # 【关键修改】如果有指定输出子目录，使用独立的输出目录
        if output_subdir:
            current_output_dir = self.output_dir / output_subdir
        else:
            current_output_dir = self.output_dir

        current_output_dir.mkdir(parents=True, exist_ok=True)

        # 收集输入文件
        input_files = self._collect_input_files(input_path)
        print(f"📄 待转换文件：{len(input_files)} 个")

        # 构建表单数据
        form_data = self._build_form_data(
            language=lang,
            backend=backend,
            parse_method="ocr" if use_ocr else "auto",
            formula_enable=formula_enable,
            table_enable=table_enable,
            server_url=server_url,
            start_page_id=0,
            end_page_id=None,
            return_md=True,
            return_images=extract_images,
        )

        upload_assets = [
            _api_client.UploadAsset(path=file_path, upload_name=file_path.name)
            for file_path in input_files
        ]

        # 执行转换
        result_md_path = self._run_conversion(
            upload_assets=upload_assets,
            form_data=form_data,
            api_url=api_url,
            server_url=server_url,
            output_dir=current_output_dir
        )
        return result_md_path

    def _collect_input_files(self, input_path: Path) -> List[Path]:
        """收集输入文件"""
        if input_path.is_file():
            file_suffix = guess_suffix_by_path(input_path)
            if file_suffix not in SUPPORTED_INPUT_SUFFIXES:
                raise ValueError(f"Unsupported input file type: {input_path.name}")
            return [input_path]

        if not input_path.is_dir():
            raise ValueError(f"Input path must be a file or directory: {input_path}")

        input_files = sorted(
            (
                candidate.resolve()
                for candidate in input_path.iterdir()
                if candidate.is_file()
                   and guess_suffix_by_path(candidate) in SUPPORTED_INPUT_SUFFIXES
            ),
            key=lambda item: item.name,
        )
        if not input_files:
            raise ValueError(f"No supported files found in directory: {input_path}")
        return input_files

    def _build_form_data(
            self,
            language: str,
            backend: str,
            parse_method: str,
            formula_enable: bool,
            table_enable: bool,
            server_url: Optional[str],
            start_page_id: int,
            end_page_id: Optional[int],
            return_md: bool = True,
            return_images: bool = True,
    ) -> dict:
        """构建请求表单数据"""
        return _api_client.build_parse_request_form_data(
            lang_list=[language],
            backend=backend,
            parse_method=parse_method,
            formula_enable=formula_enable,
            table_enable=table_enable,
            server_url=server_url,
            start_page_id=start_page_id,
            end_page_id=end_page_id,
            return_md=return_md,
            return_middle_json=False,
            return_model_output=False,
            return_content_list=False,
            return_images=return_images,
            response_format_zip=True,
            return_original_file=False,
        )

    def _run_conversion(
            self,
            upload_assets: List[_api_client.UploadAsset],
            form_data: dict,
            api_url: Optional[str],
            server_url: Optional[str],
            output_dir: Optional[Path] = None
    ) -> str:
        """执行实际的文件转换"""
        local_server: Optional[_api_client.LocalAPIServer] = None
        result_zip_path: Optional[Path] = None
        task_label = f"{len(upload_assets)} file(s)"

        # 使用指定的输出目录或默认输出目录
        if output_dir is None:
            output_dir = self.output_dir

        async def run_async():
            nonlocal local_server, result_zip_path

            async with httpx.AsyncClient(
                    timeout=_api_client.build_http_timeout(),
                    follow_redirects=True,
            ) as http_client:
                try:
                    if api_url is None:
                        self._prepare_local_api_temp_dir()
                        local_server = _api_client.LocalAPIServer()
                        base_url = local_server.start()
                        print(f"Started local mineru-api: {base_url}")

                        server_health = await _api_client.wait_for_local_api_ready(
                            http_client,
                            local_server,
                        )
                    else:
                        server_health = await _api_client.fetch_server_health(
                            http_client,
                            _api_client.normalize_base_url(api_url),
                        )

                    print(f"Using API: {server_health.base_url}")
                    print(f"Submitting {len(upload_assets)} file(s)")

                    submit_response = await _api_client.submit_parse_task(
                        base_url=server_health.base_url,
                        upload_assets=upload_assets,
                        form_data=form_data,
                    )
                    print(f"task_id: {submit_response.task_id}")

                    if submit_response.queued_ahead is not None:
                        print(f"status: pending (queued_ahead={submit_response.queued_ahead})")

                    await _api_client.wait_for_task_result(
                        client=http_client,
                        submit_response=submit_response,
                        task_label=task_label,
                        status_snapshot_callback=self._on_status_update,
                    )
                    print("status: completed")

                    result_zip_path = await _api_client.download_result_zip(
                        client=http_client,
                        submit_response=submit_response,
                        task_label=task_label,
                    )
                finally:
                    if local_server is not None:
                        local_server.stop()

            assert result_zip_path is not None
            try:
                # 【关键修复】参考 demo_minerU 的实现，不依赖返回值
                _api_client.safe_extract_zip(result_zip_path, output_dir)
                print(f"Extracted result to: {output_dir}")
            finally:
                result_zip_path.unlink(missing_ok=True)

            # 【关键修复】直接返回 output_dir，而不是 extracted_path
            return output_dir

        # 运行异步代码
        result_path = asyncio.run(run_async())

        # 【关键修复】检查结果路径是否有效
        if result_path is None:
            raise RuntimeError("MinerU conversion failed: result path is None")

        # 找到 markdown 文件
        md_files = list(Path(result_path).rglob("*.md"))
        if md_files:
            return str(md_files[0])
        return str(result_path)

    def _on_status_update(self, status_snapshot):
        """状态更新回调"""
        if status_snapshot.queued_ahead is None:
            message = status_snapshot.status
        else:
            message = f"{status_snapshot.status} (queued_ahead={status_snapshot.queued_ahead})"
        print(f"status: {message}")

    def _prepare_local_api_temp_dir(self):
        """准备本地 API 临时目录"""
        current_temp_dir = Path(tempfile.gettempdir())
        if os.name == "nt" or not Path("/tmp").exists():
            return
        if not str(current_temp_dir).startswith("/mnt/"):
            return

        # vLLM/ZeroMQ IPC sockets fail on drvfs-backed temp directories under WSL.
        os.environ["TMPDIR"] = "/tmp"
        tempfile.tempdir = None
