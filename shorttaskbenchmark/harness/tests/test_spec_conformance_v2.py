import importlib.util
import pathlib
import sys
import types
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
EVALUATOR = ROOT / "evaluator"
sys.path.insert(0, str(EVALUATOR))

import checks


def load_reference(name):
    path = ROOT / "validation" / "reference" / "solutions" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"spec_v2_reference_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SpecConformanceV2Tests(unittest.TestCase):
    def test_plain_dict_rejection_does_not_bind_exception_subclass(self):
        reference = load_reference("recursive_patch")

        def apply_patch(base, patch, delete=reference.DELETE):
            if type(base) is not dict or type(patch) is not dict:
                raise ValueError("plain dict required")
            return reference.apply_patch(base, patch, delete)

        module = types.SimpleNamespace(DELETE=reference.DELETE, apply_patch=apply_patch)
        checks.rp_plain_dict(module)

    def test_clock_validation_allows_eager_constructor_and_value_error(self):
        reference = load_reference("ttl_set")

        class BoundedTTLSet(reference.BoundedTTLSet):
            def __init__(self, capacity, ttl, clock):
                if not callable(clock):
                    raise ValueError("clock must be callable")
                clock()
                super().__init__(capacity, ttl, clock)

        checks.ttl_strict_types(types.SimpleNamespace(BoundedTTLSet=BoundedTTLSet))

    def test_duration_order_accepts_field_or_unit_start(self):
        reference = load_reference("duration")

        def parse_duration(text):
            try:
                return reference.parse_duration(text)
            except reference.DurationParseError as error:
                if error.code == "order":
                    raise reference.DurationParseError("order", 2) from None
                raise

        module = types.SimpleNamespace(
            DurationParseError=reference.DurationParseError,
            parse_duration=parse_duration,
        )
        checks.du_structured_errors(module)

    def test_duration_position_must_be_stable_and_on_error_span(self):
        reference = load_reference("duration")
        calls = [0]

        def parse_duration(text):
            try:
                return reference.parse_duration(text)
            except reference.DurationParseError as error:
                if error.code == "order":
                    calls[0] += 1
                    raise reference.DurationParseError("order", 2 + calls[0] % 2) from None
                raise

        module = types.SimpleNamespace(
            DurationParseError=reference.DurationParseError,
            parse_duration=parse_duration,
        )
        with self.assertRaises(AssertionError):
            checks.du_structured_errors(module)


if __name__ == "__main__":
    unittest.main()
