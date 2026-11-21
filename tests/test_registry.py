import unittest
from unittest.mock import Mock

from campusspot.registry import Registry, UnknownOperation
from campusspot.tool_contract import Tool


class RegistryTests(unittest.TestCase):
    def test_returns_registered_instances_by_name(self):
        limit = Mock(spec=Tool)
        conflict = Mock(spec=Tool)
        tools = {"limit_check": limit, "conflict_check": conflict}
        registry = Registry(tools)
        tools.clear()
        self.assertIs(registry.resolve("limit_check"), limit)
        self.assertIs(registry.resolve("conflict_check"), conflict)

    def test_missing_name_identifies_unknown_operation(self):
        with self.assertRaises(UnknownOperation) as caught:
            Registry({}).resolve("missing")
        self.assertEqual(caught.exception.code, "UNKNOWN_OPERATION")
        self.assertEqual(caught.exception.name, "missing")
