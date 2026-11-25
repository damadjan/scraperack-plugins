import importlib.util
import unittest
from pathlib import Path

GUEST = Path(__file__).resolve().parents[1] / "plugin" / "guest"


def load_executor():
    spec = importlib.util.spec_from_file_location("executor", GUEST / "executor.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)     # serving starts only under __main__
    return module


class SourceTest(unittest.TestCase):
    def setUp(self):
        self.executor = load_executor()

    def run_payload(self, **payload):
        return self.executor.handle({"encoding": "python-source", **payload})

    def test_main_gets_params_and_kwargs(self):
        response = self.run_payload(source="def main(a, b=1):\n    return a + b\n",
                                    params=[2], kwargs={"b": 5})
        self.assertIsNone(response["error"])
        self.assertEqual(response["result"]["value"], {"encoding": "json", "data": 7})

    def test_script_without_main_returns_result_and_output(self):
        response = self.run_payload(source="print('hi')\nresult = {'n': 3}\n")
        self.assertIsNone(response["error"])
        self.assertEqual(response["result"]["value"]["data"], {"n": 3})
        self.assertEqual(response["result"]["stdout"], "hi\n")

    def test_script_without_result_returns_none(self):
        response = self.run_payload(source="x = 1\n")
        self.assertEqual(response["result"]["value"], {"encoding": "json", "data": None})

    def test_params_without_main_fail(self):
        response = self.run_payload(source="result = 1\n", params=[1])
        self.assertIn("need the source to define main", response["error"])

    def test_compiled_source_is_reused(self):
        source = "def main():\n    return 1\n"
        self.run_payload(source=source)
        self.run_payload(source=source)
        self.assertEqual(len(self.executor.SOURCES), 1)

    def test_errors_carry_the_traceback(self):
        response = self.run_payload(source="def main():\n    raise RuntimeError('boom')\n")
        self.assertIn("RuntimeError: boom", response["error"])


if __name__ == "__main__":
    unittest.main()
