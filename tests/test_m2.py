"""
M2 verification tests: Java single-file extraction.

Tests extract_from_source on fixture Java files to verify nodes, edges,
and unresolved_references are correctly produced.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codewiki.extraction.tree_sitter import extract_from_source

FIXTURES = Path(__file__).parent / "fixtures" / "java_sample"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _extract(name: str):
    source = _read(name)
    return extract_from_source(f"com/example/demo/{name}", source, "java")


class TestExtractionBasics:
    """Verify basic extraction structure."""

    def test_file_node_created(self):
        result = _extract("DiscountService.java")
        file_nodes = [n for n in result.nodes if n.kind == "file"]
        assert len(file_nodes) == 1
        assert file_nodes[0].name == "DiscountService.java"
        assert file_nodes[0].language == "java"

    def test_no_errors(self):
        result = _extract("DiscountService.java")
        assert len(result.errors) == 0, f"Extraction errors: {result.errors}"

    def test_duration_recorded(self):
        result = _extract("DiscountService.java")
        assert result.duration_ms >= 0


class TestNodes:
    """Verify node extraction."""

    def test_class_node(self):
        result = _extract("DiscountService.java")
        classes = [n for n in result.nodes if n.kind == "class"]
        assert len(classes) == 1
        assert classes[0].name == "DiscountService"
        assert "DiscountService" in classes[0].qualified_name
        assert classes[0].visibility == "public"

    def test_method_nodes(self):
        result = _extract("DiscountService.java")
        methods = [n for n in result.nodes if n.kind == "method"]
        method_names = {m.name for m in methods}
        assert "calculateDiscount" in method_names
        assert "batchCalculate" in method_names
        # Constructor
        assert "DiscountService" in method_names  # constructor name = class name

    def test_field_nodes(self):
        result = _extract("DiscountService.java")
        fields = [n for n in result.nodes if n.kind in ("field", "constant")]
        field_names = {f.name for f in fields}
        assert "calculator" in field_names
        # static final → constant
        constants = [n for n in result.nodes if n.kind == "constant"]
        assert any(c.name == "MAX_DISCOUNT" for c in constants)

    def test_parameter_nodes(self):
        result = _extract("DiscountService.java")
        params = [n for n in result.nodes if n.kind == "parameter"]
        param_names = {p.name for p in params}
        assert "orderId" in param_names
        assert "amount" in param_names

    def test_import_nodes(self):
        result = _extract("DiscountService.java")
        imports = [n for n in result.nodes if n.kind == "import"]
        import_names = {i.name for i in imports}
        assert "java.util.List" in import_names
        assert "java.util.ArrayList" in import_names

    def test_namespace_node(self):
        result = _extract("DiscountService.java")
        namespaces = [n for n in result.nodes if n.kind == "namespace"]
        assert len(namespaces) == 1
        assert namespaces[0].name == "com.example.demo"

    def test_qualified_name(self):
        result = _extract("DiscountService.java")
        methods = [n for n in result.nodes if n.kind == "method" and n.name == "calculateDiscount"]
        assert len(methods) == 1
        # qualified_name should contain class::method pattern
        assert "DiscountService" in methods[0].qualified_name
        assert "calculateDiscount" in methods[0].qualified_name

    def test_signature(self):
        result = _extract("DiscountService.java")
        methods = {n.name: n for n in result.nodes if n.kind == "method"}
        sig = methods.get("calculateDiscount")
        assert sig is not None
        assert sig.signature is not None
        assert "double" in sig.signature  # return type
        assert "int" in sig.signature or "orderId" in sig.signature  # params

    def test_return_type(self):
        result = _extract("DiscountService.java")
        methods = {n.name: n for n in result.nodes if n.kind == "method"}
        calc = methods.get("calculateDiscount")
        assert calc is not None
        assert calc.return_type is None  # double is primitive → None

    def test_docstring(self):
        result = _extract("DiscountService.java")
        classes = [n for n in result.nodes if n.kind == "class"]
        assert classes[0].docstring is not None
        assert "discount" in classes[0].docstring.lower()


class TestInterface:
    """Verify interface extraction."""

    def test_interface_node(self):
        result = _extract("DiscountCalculator.java")
        interfaces = [n for n in result.nodes if n.kind == "interface"]
        assert len(interfaces) == 1
        assert interfaces[0].name == "DiscountCalculator"

    def test_interface_methods(self):
        result = _extract("DiscountCalculator.java")
        methods = [n for n in result.nodes if n.kind == "method"]
        method_names = {m.name for m in methods}
        assert "getRate" in method_names
        assert "applyDiscount" in method_names


class TestEnum:
    """Verify enum extraction."""

    def test_enum_node(self):
        result = _extract("OrderStatus.java")
        enums = [n for n in result.nodes if n.kind == "enum"]
        assert len(enums) == 1
        assert enums[0].name == "OrderStatus"

    def test_enum_members(self):
        result = _extract("OrderStatus.java")
        members = [n for n in result.nodes if n.kind == "enum_member"]
        member_names = {m.name for m in members}
        assert "PENDING" in member_names
        assert "SHIPPED" in member_names
        assert "CANCELLED" in member_names
        assert len(members) == 5


class TestContainsEdges:
    """Verify contains edge hierarchy."""

    def test_file_contains_class(self):
        result = _extract("DiscountService.java")
        file_node = next(n for n in result.nodes if n.kind == "file")
        class_node = next(n for n in result.nodes if n.kind == "class")
        # With a package declaration, hierarchy is file → namespace → class
        namespace = next((n for n in result.nodes if n.kind == "namespace"), None)
        if namespace:
            # file → namespace
            ns_contains = [e for e in result.edges if e.kind == "contains"
                           and e.source == file_node.id and e.target == namespace.id]
            assert len(ns_contains) == 1
            # namespace → class
            cls_contains = [e for e in result.edges if e.kind == "contains"
                            and e.source == namespace.id and e.target == class_node.id]
            assert len(cls_contains) == 1
        else:
            # No package: file → class directly
            contains = [e for e in result.edges if e.kind == "contains"
                        and e.source == file_node.id and e.target == class_node.id]
            assert len(contains) == 1

    def test_class_contains_methods(self):
        result = _extract("DiscountService.java")
        class_node = next(n for n in result.nodes if n.kind == "class")
        methods = [n for n in result.nodes if n.kind == "method"]
        for method in methods:
            contains = [e for e in result.edges if e.kind == "contains"
                        and e.source == class_node.id and e.target == method.id]
            assert len(contains) == 1, f"Missing contains edge: class → {method.name}"

    def test_class_contains_fields(self):
        result = _extract("DiscountService.java")
        class_node = next(n for n in result.nodes if n.kind == "class")
        fields = [n for n in result.nodes if n.kind in ("field", "constant")]
        for field in fields:
            contains = [e for e in result.edges if e.kind == "contains"
                        and e.source == class_node.id and e.target == field.id]
            assert len(contains) == 1, f"Missing contains edge: class → {field.name}"

    def test_method_contains_parameters(self):
        result = _extract("DiscountService.java")
        methods_with_params = [n for n in result.nodes if n.kind == "method"]
        params = [n for n in result.nodes if n.kind == "parameter"]
        if params:
            # At least one method should contain a parameter
            found = False
            for param in params:
                for method in methods_with_params:
                    contains = [e for e in result.edges if e.kind == "contains"
                                and e.source == method.id and e.target == param.id]
                    if contains:
                        found = True
                        break
            assert found, "No method→parameter contains edges found"


class TestUnresolvedReferences:
    """Verify unresolved reference generation."""

    def test_method_call_refs(self):
        result = _extract("DiscountService.java")
        call_refs = [r for r in result.unresolved_references if r.reference_kind == "calls"]
        ref_names = {r.reference_name for r in call_refs}
        # calculator.getRate is called in calculateDiscount
        assert any("getRate" in name for name in ref_names), f"getRate not in {ref_names}"
        # calculateDiscount is called in batchCalculate
        assert any("calculateDiscount" in name for name in ref_names), f"calculateDiscount not in {ref_names}"

    def test_instantiation_refs(self):
        result = _extract("DiscountService.java")
        inst_refs = [r for r in result.unresolved_references if r.reference_kind == "instantiates"]
        inst_names = {r.reference_name for r in inst_refs}
        assert "Order" in inst_names  # new Order(...)
        assert "ArrayList" in inst_names  # new ArrayList<>()
        assert "Discount" in inst_names  # new Discount(...)

    def test_import_refs(self):
        result = _extract("DiscountService.java")
        import_refs = [r for r in result.unresolved_references if r.reference_kind == "imports"]
        import_names = {r.reference_name for r in import_refs}
        assert "java.util.List" in import_names
        assert "java.util.ArrayList" in import_names

    def test_refs_have_location(self):
        result = _extract("DiscountService.java")
        for ref in result.unresolved_references:
            assert ref.line > 0
            assert ref.column >= 0
            assert ref.file_path == "com/example/demo/DiscountService.java"
            assert ref.language == "java"


class TestLombok:
    """Verify Lombok member synthesis."""

    def test_data_generates_getters_setters(self):
        result = _extract("Order.java")
        methods = {n.name: n for n in result.nodes if n.kind == "method"}
        # @Data on class → getters for all non-static fields
        assert "getId" in methods, f"getId not found in {list(methods.keys())}"
        assert "getAmount" in methods
        # @Data → setters for non-final fields
        assert "setAmount" in methods
        # @Data → equals, hashCode, toString
        assert "equals" in methods
        assert "hashCode" in methods
        assert "toString" in methods

    def test_field_level_getter(self):
        result = _extract("Order.java")
        methods = {n.name: n for n in result.nodes if n.kind == "method"}
        # @Getter on id field → getId
        assert "getId" in methods

    def test_field_level_setter(self):
        result = _extract("Order.java")
        methods = {n.name: n for n in result.nodes if n.kind == "method"}
        # @Setter on amount field → setAmount
        assert "setAmount" in methods

    def test_lombok_decorator(self):
        result = _extract("Order.java")
        lombok_methods = [n for n in result.nodes if n.kind == "method" and n.decorators and "lombok" in n.decorators]
        assert len(lombok_methods) > 0
        for m in lombok_methods:
            assert m.docstring is not None
            assert "Lombok-generated" in m.docstring

    def test_lombok_no_override(self):
        """Lombok should not override an explicitly declared method."""
        result = _extract("Order.java")
        methods = [n for n in result.nodes if n.kind == "method" and n.name == "process"]
        # process() is explicitly declared, should not be duplicated by Lombok
        assert len(methods) == 1


class TestEdgeCases:
    """Verify edge cases."""

    def test_empty_file(self):
        result = extract_from_source("Empty.java", "", "java")
        # Should have at least a file node
        assert len(result.nodes) >= 1
        assert result.nodes[0].kind == "file"

    def test_simple_class(self):
        source = "public class Foo { }"
        result = extract_from_source("Foo.java", source, "java")
        classes = [n for n in result.nodes if n.kind == "class"]
        assert len(classes) == 1
        assert classes[0].name == "Foo"
