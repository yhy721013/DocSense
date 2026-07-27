from __future__ import annotations

import hashlib
import importlib.util
import ast
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = PROJECT_ROOT / "scripts" / "legacy_office"


def _load_module(name: str, filename: str):
    script_dir = str(SCRIPT_DIR)
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载 {filename}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


fetch_assets = _load_module("legacy_office_fetch_assets", "fetch_assets.py")
package_offline = _load_module("legacy_office_package_offline", "package_offline.py")
smoke_test_macos = _load_module(
    "legacy_office_smoke_test_macos", "smoke_test_macos.py"
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class LegacyOfficeDeliveryTests(unittest.TestCase):
    def test_lock_pins_official_26_2_5_artifacts_and_checksums(self):
        lock = json.loads(
            (SCRIPT_DIR / "artifacts.lock.json").read_text(encoding="utf-8")
        )
        self.assertEqual(lock["libreOfficeVersion"], "26.2.5")
        self.assertEqual(lock["allowedVersionSeries"], "26.2")

        windows = lock["installers"]["windows-x64"]
        self.assertEqual(windows["architecture"], "x86_64")
        self.assertEqual(
            windows["filename"], "LibreOffice_26.2.5_Win_x86-64.msi"
        )
        self.assertEqual(
            windows["sha256"],
            "f15ba07bfcb0186986cf3171063506f5d207c11f8cc051ba0d135209e9e915f9",
        )
        self.assertTrue(windows["url"].startswith("https://download.documentfoundation.org/"))
        self.assertEqual(windows["certification"], "static-and-mock-only")

        macos = lock["installers"]["macos-arm64"]
        self.assertEqual(macos["architecture"], "aarch64")
        self.assertEqual(
            macos["filename"], "LibreOffice_26.2.5_MacOS_aarch64.dmg"
        )
        self.assertEqual(
            macos["sha256"],
            "c99fb4fe574437fc4cb820a4ca15271bca325920861f7139858b36d7f9df78ad",
        )
        self.assertTrue(macos["checksumUrl"].endswith(".dmg.sha256"))

    def test_apache_poi_sample_is_pinned_to_original_commit_bytes(self):
        lock = fetch_assets.load_lock()
        sample = lock["smokeSamples"]["powerpoint-2002-apache-poi.ppt"]
        self.assertEqual(
            sample["commit"], "63eda92cc63b39d4e8956e7775ccddb1f0d925cf"
        )
        self.assertEqual(
            sample["sha256"],
            "7d485f5d3fbfc18191854b3a6c370e7195df736442e29799d38786c62259173f",
        )
        self.assertEqual(sample["license"], "Apache-2.0")
        self.assertIn("raw.githubusercontent.com/apache/poi/", sample["url"])

    def test_package_verifier_rejects_non_ole_and_wrong_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            non_ole = root / "sample.doc"
            non_ole.write_bytes(b"not an OLE2 file")
            with self.assertRaisesRegex(RuntimeError, "不是 Office OLE2"):
                package_offline._verify_sample(non_ole, _sha256(non_ole.read_bytes()))

            wrong_hash = root / "sample.xls"
            wrong_hash.write_bytes(package_offline.OLE2_MAGIC + b"payload")
            with self.assertRaisesRegex(RuntimeError, "SHA-256 不匹配"):
                package_offline._verify_sample(wrong_hash, "0" * 64)

    def test_package_builder_emits_manifest_checksums_scripts_and_samples(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            downloads = root / "downloads"
            samples = root / "samples"
            output = root / "output"
            downloads.mkdir()
            samples.mkdir()

            installer_bytes = b"fake installer"
            license_bytes = b"fake license"
            (downloads / "fake.dmg").write_bytes(installer_bytes)
            (downloads / "MPL-2.0.txt").write_bytes(license_bytes)

            sample_records = {}
            for name, format_name in (
                ("word-sample.doc", "doc"),
                ("powerpoint-2002-apache-poi.ppt", "ppt"),
                ("excel-sample.xls", "xls"),
            ):
                data = package_offline.OLE2_MAGIC + name.encode("ascii")
                (samples / name).write_bytes(data)
                sample_records[name] = {
                    "format": format_name,
                    "origin": "test",
                    "sha256": _sha256(data),
                }

            fake_lock = {
                "schemaVersion": 1,
                "libreOfficeVersion": "26.2.5",
                "allowedVersionSeries": "26.2",
                "installers": {
                    "macos-arm64": {
                        "architecture": "aarch64",
                        "filename": "fake.dmg",
                        "url": "https://example.invalid/fake.dmg",
                        "checksumUrl": "https://example.invalid/fake.dmg.sha256",
                        "sha256": _sha256(installer_bytes),
                        "certification": "requires-real-smoke",
                    }
                },
                "licenses": [
                    {
                        "filename": "MPL-2.0.txt",
                        "url": "https://example.invalid/MPL-2.0.txt",
                        "sha256": _sha256(license_bytes),
                    }
                ],
                "smokeSamples": sample_records,
            }

            with mock.patch.object(
                package_offline, "load_lock", return_value=fake_lock
            ):
                archive_path = package_offline.build_bundle(
                    platform="macos-arm64",
                    downloads_dir=downloads,
                    samples_dir=samples,
                    output_dir=output,
                    overwrite=False,
                )

            self.assertTrue(archive_path.is_file())
            with zipfile.ZipFile(archive_path) as archive:
                names = archive.namelist()
                prefix = f"{archive_path.stem}/"
                self.assertIn(f"{prefix}MANIFEST.json", names)
                self.assertIn(f"{prefix}SHA256SUMS", names)
                self.assertIn(f"{prefix}install.sh", names)
                self.assertIn(f"{prefix}preflight.sh", names)
                self.assertIn(f"{prefix}samples/SOURCES.md", names)
                manifest = json.loads(
                    archive.read(f"{prefix}MANIFEST.json").decode("utf-8")
                )
                self.assertFalse(manifest["docsense"]["automaticInstallation"])
                self.assertEqual(manifest["platform"], "macos-arm64")
                checksums = archive.read(f"{prefix}SHA256SUMS").decode("utf-8")
                self.assertIn("installer/fake.dmg", checksums)
                self.assertIn("samples/word-sample.doc", checksums)

    def test_delivery_scope_is_documented_and_binary_outputs_are_ignored(self):
        env_text = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
        readme_text = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        interface_text = (
            PROJECT_ROOT / "docs" / "接口文档" / "文件处理和报告生成.md"
        ).read_text(encoding="utf-8")
        gitignore_text = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")

        self.assertIn("DOCSENSE_LEGACY_OFFICE_ENABLED=false", env_text)
        self.assertIn("DOCSENSE_LIBREOFFICE_ALLOWED_VERSION_SERIES=26.2", env_text)
        self.assertIn(".doc → .docx", readme_text)
        self.assertIn("Windows 尚未实机认证", readme_text)
        self.assertIn("templateOutline` | 不支持", interface_text)
        self.assertIn("dist/legacy-office/", gitignore_text)

        forbidden_suffixes = {".doc", ".ppt", ".xls", ".msi", ".dmg", ".zip"}
        committed_assets = [
            path
            for path in SCRIPT_DIR.rglob("*")
            if path.is_file() and path.suffix.lower() in forbidden_suffixes
        ]
        self.assertEqual(committed_assets, [])

    def test_platform_preflight_scripts_pin_filters_and_process_tree_cleanup(self):
        windows = (SCRIPT_DIR / "Preflight-Windows.ps1").read_text(encoding="utf-8")
        macos = (SCRIPT_DIR / "smoke_test_macos.py").read_text(encoding="utf-8")
        for filter_name in (
            "docx:Office Open XML Text",
            "pptx:Impress MS PowerPoint 2007 XML",
            "xlsx:Calc Office Open XML",
        ):
            self.assertIn(filter_name, windows)
            self.assertIn(filter_name, macos)
        self.assertIn("taskkill.exe", windows)
        self.assertIn("/T", windows)
        self.assertIn("/F", windows)
        self.assertIn("start_new_session=True", macos)
        self.assertIn("os.killpg", macos)
        self.assertIn("LibreOfficeDev", windows)
        self.assertIn("LibreOfficeDev", macos)
        self.assertLess(
            windows.index("$Process.StandardOutput.ReadToEndAsync()"),
            windows.index("$StandardOutputTask.IsCompleted"),
        )
        self.assertLess(
            windows.index("$Process.StandardError.ReadToEndAsync()"),
            windows.index("$StandardErrorTask.IsCompleted"),
        )
        self.assertNotIn("$Process.StandardOutput.ReadToEnd()", windows)
        self.assertNotIn("$Process.StandardError.ReadToEnd()", windows)

    def test_windows_process_and_version_share_bounded_pipe_deadline(self):
        windows = (SCRIPT_DIR / "Preflight-Windows.ps1").read_text(encoding="utf-8")
        bounded_start = windows.index("function Invoke-BoundedProcess")
        bounded_end = windows.index("function Test-Ole2", bounded_start)
        bounded = windows[bounded_start:bounded_end]
        for marker in (
            "$Clock.ElapsedMilliseconds",
            "$Process.HasExited",
            "$StandardOutputTask.IsCompleted",
            "$StandardErrorTask.IsCompleted",
            "Stop-ProcessTree -ProcessId $Process.Id",
            "Stop-ProfileProcesses -ProfileUri $ProfileUri",
            "父进程已退出，但输出管道未在总时限内 EOF",
        ):
            self.assertIn(marker, bounded)

        version_start = windows.index("$VersionTemporaryRoot")
        version_end = windows.index('Write-Host "版本门禁通过', version_start)
        version_gate = windows[version_start:version_end]
        self.assertIn("Invoke-BoundedProcess", version_gate)
        self.assertIn('-Operation "LibreOffice --version"', version_gate)
        self.assertIn("[Math]::Min($TimeoutSeconds, 20)", version_gate)
        self.assertIn("Stop-ProfileProcesses", version_gate)
        self.assertNotIn("& $ExecutablePath --version", windows)

    def test_preflight_requires_nonempty_checksum_manifest(self):
        windows = (SCRIPT_DIR / "Preflight-Windows.ps1").read_text(encoding="utf-8")
        macos = (SCRIPT_DIR / "preflight_macos.sh").read_text(encoding="utf-8")
        checksum_function = windows[
            windows.index("function Test-BundleChecksums"):
            windows.index("function Quote-ProcessArgument")
        ]
        self.assertIn("缺少 SHA256SUMS", checksum_function)
        self.assertIn("$ChecksumLines.Count -eq 0", checksum_function)
        self.assertNotIn("return\n", checksum_function)
        self.assertIn('[[ ! -s "$SCRIPT_DIR/SHA256SUMS" ]]', macos)
        self.assertLess(
            macos.index('[[ ! -s "$SCRIPT_DIR/SHA256SUMS" ]]'),
            macos.index('python3 "$SCRIPT_DIR/smoke_test.py"'),
        )

    def test_macos_installer_never_moves_unconfirmed_existing_target(self):
        installer = (SCRIPT_DIR / "install_macos.sh").read_text(encoding="utf-8")
        self.assertIn('[[ -e "$1" || -L "$1" ]]', installer)
        self.assertIn('[[ ! -L "$TARGET_APP"', installer)
        existing_gate = installer.index('if path_exists "$TARGET_APP"; then')
        replace_gate = installer.index(
            'if [[ "$REPLACE" != true ]]; then',
            existing_gate,
        )
        mount = installer.index("hdiutil attach", existing_gate)
        move = installer.index('sudo mv "$TARGET_APP" "$BACKUP_APP"', mount)
        second_existing_gate = installer.rfind(
            'if path_exists "$TARGET_APP"; then',
            mount,
            move,
        )
        second_replace_gate = installer.index(
            'if [[ "$REPLACE" != true ]]; then',
            second_existing_gate,
        )
        self.assertLess(replace_gate, mount)
        self.assertLess(second_existing_gate, second_replace_gate)
        self.assertLess(second_replace_gate, move)
        self.assertIn("拒绝移动既有", installer[second_replace_gate:move])

    def test_all_delivery_version_gates_reject_prerelease_markers(self):
        stable = "LibreOffice 26.2.5.2 cd7284b4cbbfeb507e630c1aac019f4157393acb"
        self.assertEqual(
            smoke_test_macos._validate_version_output(stable, "26.2.5"),
            stable,
        )
        prerelease_outputs = (
            "LibreOfficeDev 26.2.5.2",
            "LibreOffice 26.2.5.2 alpha1",
            "LibreOffice 26.2.5.2.beta2",
            "LibreOffice 26.2.5.2-rc1",
            "LibreOffice 26.2.5.2 nightly",
            "LibreOffice 26.2.5.2 development",
        )
        for output in prerelease_outputs:
            with self.subTest(output=output):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "开发版|预发布",
                ):
                    smoke_test_macos._validate_version_output(output, "26.2.5")

        windows = (SCRIPT_DIR / "Preflight-Windows.ps1").read_text(encoding="utf-8")
        windows_installer = (SCRIPT_DIR / "Install-Windows.ps1").read_text(
            encoding="utf-8"
        )
        macos_installer = (SCRIPT_DIR / "install_macos.sh").read_text(
            encoding="utf-8"
        )
        for marker in ("alpha", "beta", "rc", "nightly", "development"):
            self.assertIn(marker, windows.lower())
            self.assertIn(marker, windows_installer.lower())
            self.assertIn(marker, macos_installer.lower())

    def test_windows_installer_stable_gate_rejects_prerelease_versions(self):
        installer = (SCRIPT_DIR / "Install-Windows.ps1").read_text(
            encoding="utf-8"
        )
        gate_start = installer.index("$VersionOutput =")
        gate_end = installer.index('Write-Host "安装完成', gate_start)
        gate = installer[gate_start:gate_end]

        self.assertIn('(& $Executable --version 2>&1 | Out-String).Trim()', gate)
        self.assertIn('(?i)LibreOfficeDev', gate)
        self.assertIn(
            '(?i)(?:^|[^A-Za-z])(?:alpha|beta|rc|nightly|development)',
            gate,
        )
        self.assertIn(
            '-notmatch "\\bLibreOffice\\s+26\\.2\\.5(?:\\.\\d+)*\\b"',
            gate,
        )
        self.assertIn("安装后的版本门禁失败", gate)

    def test_macos_timeout_kills_group_when_parent_exits_before_descendants(self):
        process = mock.Mock()
        process.pid = 12345
        process.wait.return_value = 0
        with (
            mock.patch.object(
                smoke_test_macos,
                "_process_group_exists",
                side_effect=(True, False, False),
            ),
            mock.patch.object(smoke_test_macos.os, "killpg") as killpg,
        ):
            smoke_test_macos._terminate_process_group(process, grace_seconds=0)

        self.assertEqual(
            killpg.call_args_list,
            [
                mock.call(12345, smoke_test_macos.signal.SIGTERM),
                mock.call(12345, smoke_test_macos.signal.SIGKILL),
            ],
        )
        process.wait.assert_called_once_with(timeout=5)

    @unittest.skipUnless(sys.platform == "darwin", "仅在 macOS 验证真实 PGID")
    def test_macos_timeout_really_kills_child_after_group_leader_exits(self):
        leader_code = r"""
import signal
import subprocess
import sys
import time
subprocess.Popen([
    sys.executable,
    "-c",
    "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)",
])
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
print("ready", flush=True)
time.sleep(60)
"""
        process = subprocess.Popen(
            [sys.executable, "-c", leader_code],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            self.assertIsNotNone(process.stdout)
            assert process.stdout is not None
            self.assertEqual(process.stdout.readline().strip(), "ready")
            time.sleep(0.1)
            smoke_test_macos._terminate_process_group(
                process,
                grace_seconds=0.2,
            )
            self.assertIsNotNone(process.poll())
            self.assertFalse(
                smoke_test_macos._process_group_exists(process.pid)
            )
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.fail("受控测试进程组未能清理")
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()

    def test_delivery_docs_fail_closed_for_multi_sheet_workbooks(self):
        for filename in ("README.md", "BUNDLE_README.md"):
            content = (SCRIPT_DIR / filename).read_text(encoding="utf-8")
            self.assertIn("仅支持恰好一个可解析 Sheet", content, filename)
            self.assertIn("fail-closed", content, filename)

    def test_delivery_python_scripts_use_logging_without_direct_print_calls(self):
        python_scripts = sorted(SCRIPT_DIR.glob("*.py"))
        self.assertEqual(len(python_scripts), 4)
        for script in python_scripts:
            source = script.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(script))
            direct_prints = [
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "print"
            ]
            self.assertEqual(direct_prints, [], script.name)
            self.assertIn("logging", source, script.name)


if __name__ == "__main__":
    unittest.main()
