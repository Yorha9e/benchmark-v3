import importlib.util
import pathlib
import unittest

ROOT = pathlib.Path(__file__).parent / "solutions"


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PublicSmokeTests(unittest.TestCase):
    def test_recursive_patch(self):
        module = load("recursive_patch")
        self.assertEqual(module.apply_patch({"a": 1}, {"b": 2}), {"a": 1, "b": 2})

    def test_dependency_layers(self):
        module = load("dependency_layers")
        self.assertEqual(module.dependency_layers([("app", "lib")]), [["lib"], ["app"]])

    def test_ttl_set(self):
        module = load("ttl_set")
        now = [0.0]
        values = module.BoundedTTLSet(2, 5.0, lambda: now[0])
        values.add("x")
        self.assertIn("x", values)

    def test_duration(self):
        module = load("duration")
        self.assertEqual(module.normalize_duration("1h30m"), "1h30m")


if __name__ == "__main__":
    unittest.main()
