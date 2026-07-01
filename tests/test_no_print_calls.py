import ast
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class NoPrintCallsTests(unittest.TestCase):
    def test_runtime_and_script_code_uses_logging_instead_of_print(self):
        paths = [ROOT / "run.py", ROOT / "clean.py"]
        paths.extend((ROOT / "app").rglob("*.py"))
        paths.extend((ROOT / "scripts").rglob("*.py"))

        violations: list[str] = []
        for path in paths:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if isinstance(node.func, ast.Name) and node.func.id == "print":
                    violations.append(f"{path.relative_to(ROOT)}:{node.lineno}")

        self.assertEqual(violations, [], f"发现未替换的 print 调用: {violations}")


if __name__ == "__main__":
    unittest.main()
