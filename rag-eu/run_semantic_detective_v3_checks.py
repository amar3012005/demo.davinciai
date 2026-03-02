"""Standalone verifier for Semantic Detective V3 behavior.

Runs a subset of checks without pytest collection/conftest dependencies.
"""
import importlib.util
import pathlib
import traceback


def main() -> int:
    test_file = pathlib.Path(__file__).parent / "tests" / "test_semantic_detective_v3.py"
    spec = importlib.util.spec_from_file_location("sd_v3_tests", test_file)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    test_names = [
        "test_query_profile_extracts_focus_and_zone",
        "test_docs_nav_beats_visit_website_on_click_query",
        "test_literal_type_prefers_input_over_button",
        "test_router_canary_domain_gate",
        "test_incident_replay_docs_over_visit_website",
    ]

    failed = 0
    for name in test_names:
        fn = getattr(module, name)
        try:
            fn()
            print(f"PASS: {name}")
        except Exception:
            failed += 1
            print(f"FAIL: {name}")
            traceback.print_exc()

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
