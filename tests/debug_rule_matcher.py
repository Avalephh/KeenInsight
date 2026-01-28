#!/usr/bin/env python3
"""
Debug script to identify the issue with SQLRuleMatcher
"""

import sys

# Add the src directory to the path
sys.path.append("/root/DREAM/src")


def test_import():
    """Test importing SQLRuleMatcher"""
    try:
        print("Testing import...")
        print("✅ Import successful")
        return True
    except Exception as e:
        print(f"❌ Import failed: {e}")
        import traceback

        traceback.print_exc()
        return False


def test_initialization():
    """Test SQLRuleMatcher initialization"""
    try:
        print("\nTesting initialization...")
        from agent.action.diagnose_tools import SQLRuleMatcher

        matcher = SQLRuleMatcher()
        print("✅ Initialization successful")
        print(f"Loaded {len(matcher.rules)} rules")
        return True
    except Exception as e:
        print(f"❌ Initialization failed: {e}")
        import traceback

        traceback.print_exc()
        return False


def test_rule_matching():
    """Test rule matching functionality"""
    try:
        print("\nTesting rule matching...")
        from agent.action.diagnose_tools import SQLRuleMatcher

        matcher = SQLRuleMatcher()

        test_sql = "SELECT * FROM table1 UNION ALL SELECT * FROM table2 ORDER BY col1"
        rules = matcher.match_rules(test_sql)
        print(f"✅ Rule matching successful, found {len(rules)} rules")

        descriptions = matcher.get_rule_descriptions(test_sql)
        print(f"Rule descriptions:\n{descriptions}")
        return True
    except Exception as e:
        print(f"❌ Rule matching failed: {e}")
        import traceback

        traceback.print_exc()
        return False


def test_prompt_integration():
    """Test prompt integration"""
    try:
        print("\nTesting prompt integration...")
        from agent.prompt import build_action_space_prompt

        # Mock query info
        class MockQueryInfo:
            def __init__(self, query):
                self.query = query

        query_info = MockQueryInfo("SELECT * FROM table1 UNION ALL SELECT * FROM table2 ORDER BY col1")
        root_causes = ["poorly written queries"]

        action_space_prompt = build_action_space_prompt(
            root_causes=root_causes,
            db=None,
            knob_config={},
            current_knob_values={},
            query_info=query_info,
        )

        print("✅ Prompt generation successful")
        print(f"Generated prompt length: {len(action_space_prompt)}")
        return True
    except Exception as e:
        print(f"❌ Prompt integration failed: {e}")
        import traceback

        traceback.print_exc()
        return False


if __name__ == "__main__":
    print("Debugging SQLRuleMatcher...")

    # Test 1: Import
    test1 = test_import()

    # Test 2: Initialization
    test2 = test_initialization()

    # Test 3: Rule matching
    test3 = test_rule_matching()

    # Test 4: Prompt integration
    test4 = test_prompt_integration()

    if all([test1, test2, test3, test4]):
        print("\n🎉 All tests passed!")
    else:
        print("\n❌ Some tests failed. Check the output above for details.")
