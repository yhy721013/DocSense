import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional


class MHTMLToPDFConverter:
    """MHTML 转 PDF 转换器 - 使用系统 Chrome/Edge 无头模式"""

    def __init__(self):
        """初始化转换器"""
        self.chrome_path = self._find_chrome()
        if not self.chrome_path:
            raise RuntimeError(
                "未找到 Chrome 或 Edge 浏览器。\n"
                "请确保系统已安装 Google Chrome 或 Microsoft Edge。\n"
                "Windows 通常自带 Edge，Chrome 可从 https://www.google.com/chrome/ 下载。"
            )
        print(f"[MHTML→PDF] 使用浏览器: {self.chrome_path}")

    def _find_chrome(self) -> Optional[str]:
        """查找系统中可用的 Chrome/Edge 浏览器"""
        candidates = []

        if os.name == 'nt':
            chrome_env = os.environ.get('CHROME_PATH')
            if chrome_env:
                candidates.append(chrome_env)

            chrome_names = ['chrome.exe', 'google-chrome.exe']
            for name in chrome_names:
                p = shutil.which(name)
                if p:
                    candidates.append(p)

            chrome_dirs = [
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
            ]
            candidates.extend(chrome_dirs)

            edge_names = ['msedge.exe']
            for name in edge_names:
                p = shutil.which(name)
                if p:
                    candidates.append(p)

            edge_dirs = [
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            ]
            candidates.extend(edge_dirs)

        elif os.name == 'posix':
            import platform
            if platform.system() == 'Darwin':
                candidates.extend([
                    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
                    '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
                    '/Applications/Chromium.app/Contents/MacOS/Chromium',
                ])
            else:
                for name in ['google-chrome', 'google-chrome-stable', 'chromium-browser', 'chromium', 'microsoft-edge']:
                    p = shutil.which(name)
                    if p:
                        candidates.append(p)

        for path in candidates:
            if os.path.isfile(path):
                return path

        return None

    def convert(
        self,
        mhtml_path: str,
        output_path: Optional[str] = None,
    ) -> str:
        """
        将 MHTML 文件转换为 PDF
        直接将 MHTML 交给 Chrome 渲染，与浏览器中 Ctrl+P 效果一致

        :param mhtml_path: MHTML 文件路径
        :param output_path: 输出 PDF 路径（可选，默认与输入文件同目录同名）
        :return: 输出 PDF 文件路径
        """
        if not os.path.exists(mhtml_path):
            raise FileNotFoundError(f"MHTML 文件不存在: {mhtml_path}")

        if output_path is None:
            base_name = Path(mhtml_path).stem
            output_dir = Path(mhtml_path).parent
            output_path = str(output_dir / f"{base_name}.pdf")

        print(f"\n{'=' * 60}")
        print(f"MHTML 转 PDF")
        print(f"输入: {mhtml_path}")
        print(f"输出: {output_path}")
        print(f"{'=' * 60}")

        return self._convert_mhtml_to_pdf(mhtml_path, output_path)

    def _convert_mhtml_to_pdf(self, mhtml_path: str, output_path: str) -> str:
        """直接将 MHTML 文件交给 Chrome 无头模式生成 PDF"""
        print(f"[转换] 使用 {Path(self.chrome_path).name} 无头模式生成 PDF...")

        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

        mhtml_url = Path(mhtml_path).resolve().as_uri()

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_pdf_path = os.path.join(tmp_dir, "output.pdf")

            cmd = [
                self.chrome_path,
                '--headless=new',
                '--disable-gpu',
                '--no-sandbox',
                '--disable-software-rasterizer',
                '--allow-file-access-from-files',
                f'--print-to-pdf={tmp_pdf_path}',
                '--print-to-pdf-no-header',
                mhtml_url,
            ]

            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=60,
                    encoding='utf-8',
                    errors='replace',
                )

                if result.returncode != 0 and result.stderr:
                    stderr_lines = [l for l in result.stderr.strip().split('\n') if l.strip()]
                    error_summary = '\n'.join(stderr_lines[:5])
                    print(f"[警告] 浏览器输出:\n{error_summary}")

                if os.path.exists(tmp_pdf_path) and os.path.getsize(tmp_pdf_path) > 0:
                    self._safe_copy(tmp_pdf_path, output_path)
                    print(f"[完成] PDF 已保存至: {output_path}")
                    return output_path

                raise RuntimeError(
                    f"PDF 文件未生成或为空。浏览器返回码: {result.returncode}\n"
                    f"stderr: {result.stderr[:500] if result.stderr else '无'}"
                )

            except subprocess.TimeoutExpired:
                raise RuntimeError("PDF 生成超时（60秒）")

    @staticmethod
    def _safe_copy(src: str, dst: str, max_retries: int = 5, delay: float = 0.5):
        """带重试的文件拷贝，应对 Windows 文件锁"""
        import time
        for attempt in range(max_retries):
            try:
                shutil.copy2(src, dst)
                return
            except PermissionError:
                if attempt < max_retries - 1:
                    time.sleep(delay)
                else:
                    raise

def convert_mhtml_to_pdf(
    mhtml_path: str,
    output_path: Optional[str] = None,
    **kwargs
) -> str:
    """
    便捷函数：将 MHTML 转换为 PDF

    :param mhtml_path: MHTML 文件路径
    :param output_path: 输出 PDF 路径（可选）
    :param kwargs: 其他参数（保留兼容性）
    :return: 输出 PDF 文件路径
    """
    converter = MHTMLToPDFConverter()
    return converter.convert(mhtml_path, output_path)
