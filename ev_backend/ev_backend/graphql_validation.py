"""GraphQL query-complexity protection (Sprint 5, Part 5).

A validation rule that rejects overly deep queries before they execute, guarding
against nested-query abuse (e.g. station → reviews → user → … cycles). The limit
is read from settings at validation time so it can be tuned/overridden.
"""

from django.conf import settings
from graphql import GraphQLError
from graphql.language import (
    FieldNode,
    FragmentDefinitionNode,
    FragmentSpreadNode,
    InlineFragmentNode,
)
from graphql.validation import ValidationRule


def depth_limit_validator(max_depth=None):
    """Build a ValidationRule class enforcing a maximum selection depth."""

    class DepthLimitValidationRule(ValidationRule):
        def __init__(self, context):
            super().__init__(context)
            self._fragments = {}

        def enter_document(self, node, *_args):
            self._fragments = {
                d.name.value: d
                for d in node.definitions
                if isinstance(d, FragmentDefinitionNode)
            }

        def enter_operation_definition(self, node, *_args):
            limit = max_depth if max_depth is not None else settings.GRAPHQL_MAX_DEPTH
            depth = self._depth(node)
            if depth > limit:
                self.report_error(
                    GraphQLError(
                        f"Query is too deep: depth {depth} exceeds the maximum of {limit}.",
                        node,
                    )
                )

        def _depth(self, node, current=0, seen=None):
            seen = seen or set()
            selection_set = getattr(node, "selection_set", None)
            if selection_set is None:
                return current
            deepest = current
            for selection in selection_set.selections:
                if isinstance(selection, FieldNode):
                    deepest = max(deepest, self._depth(selection, current + 1, seen))
                elif isinstance(selection, InlineFragmentNode):
                    deepest = max(deepest, self._depth(selection, current, seen))
                elif isinstance(selection, FragmentSpreadNode):
                    name = selection.name.value
                    if name in seen:  # guard against fragment cycles
                        continue
                    fragment = self._fragments.get(name)
                    if fragment is not None:
                        deepest = max(deepest, self._depth(fragment, current, seen | {name}))
            return deepest

    return DepthLimitValidationRule
