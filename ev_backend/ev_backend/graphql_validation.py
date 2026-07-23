"""GraphQL query-complexity protection (Sprint 5, Part 5).

Validation rules that reject abusive queries before they execute:

* :func:`depth_limit_validator` — guards against nested-query abuse (e.g. station
  → reviews → user → … cycles) by bounding selection depth.
* :func:`complexity_limit_validator` — guards against shallow-but-wide and
  heavily-aliased queries by bounding the total selected-field count and the
  alias count (depth alone doesn't stop a flat query naming thousands of fields).
* :func:`introspection_validator` — rejects ``__schema``/``__type`` probes in
  production so the schema isn't handed to anonymous callers.

All limits are read from settings at validation time so they can be tuned or
overridden per-test. The breadth/introspection rules reuse the depth rule's
fragment handling (named + inline, cycle-safe).
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
                    # Introspection meta-fields (__schema, __type, __typename) are
                    # exempt. The standard introspection query GraphiQL and
                    # graphql-codegen send is depth 13 — deeper than any real data
                    # query we permit — but it is a fixed, bounded shape, not the
                    # nested-data / cyclic abuse this rule guards against. Counting
                    # it would break schema fetching (and codegen) for a limit that
                    # is meant for data traversal. A leaf __typename is depth-neutral
                    # anyway; this also exempts a deep __schema subtree.
                    if selection.name.value.startswith("__"):
                        continue
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


def complexity_limit_validator(max_fields=None, max_aliases=None):
    """Build a ValidationRule class bounding query breadth/cost.

    Two independent caps, either of which rejects the operation:

    * total selected-field (node) count — stops a shallow-but-wide query that
      names thousands of fields in one flat selection;
    * alias count — stops the same field being re-selected under hundreds of
      aliases to amplify a single expensive resolver.

    Fragment handling mirrors :func:`depth_limit_validator`: named spreads are
    expanded, inline fragments are transparent, and a ``seen`` set guards against
    fragment cycles. Introspection meta-fields (``__*``) are exempt for the same
    reason the depth rule exempts them — the fixed introspection query is large
    but bounded, and counting it would break schema fetching / codegen.
    """

    class ComplexityLimitValidationRule(ValidationRule):
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
            field_limit = (
                max_fields if max_fields is not None else settings.GRAPHQL_MAX_FIELDS
            )
            alias_limit = (
                max_aliases if max_aliases is not None else settings.GRAPHQL_MAX_ALIASES
            )
            fields, aliases = self._count(node)
            if fields > field_limit:
                self.report_error(
                    GraphQLError(
                        f"Query is too complex: {fields} fields exceed the maximum "
                        f"of {field_limit}.",
                        node,
                    )
                )
            if aliases > alias_limit:
                self.report_error(
                    GraphQLError(
                        f"Query has too many aliases: {aliases} exceed the maximum "
                        f"of {alias_limit}.",
                        node,
                    )
                )

        def _count(self, node, seen=None):
            """Return ``(field_count, alias_count)`` for ``node``'s subtree."""
            seen = seen or set()
            selection_set = getattr(node, "selection_set", None)
            if selection_set is None:
                return 0, 0
            fields = aliases = 0
            for selection in selection_set.selections:
                if isinstance(selection, FieldNode):
                    if selection.name.value.startswith("__"):
                        continue  # introspection subtree exempt, as in the depth rule
                    fields += 1
                    if selection.alias is not None:
                        aliases += 1
                    sub_fields, sub_aliases = self._count(selection, seen)
                    fields += sub_fields
                    aliases += sub_aliases
                elif isinstance(selection, InlineFragmentNode):
                    sub_fields, sub_aliases = self._count(selection, seen)
                    fields += sub_fields
                    aliases += sub_aliases
                elif isinstance(selection, FragmentSpreadNode):
                    name = selection.name.value
                    if name in seen:  # guard against fragment cycles
                        continue
                    fragment = self._fragments.get(name)
                    if fragment is not None:
                        sub_fields, sub_aliases = self._count(fragment, seen | {name})
                        fields += sub_fields
                        aliases += sub_aliases
            return fields, aliases

    return ComplexityLimitValidationRule


def introspection_validator(allow=None):
    """Build a ValidationRule class that rejects introspection when disallowed.

    ``__schema`` and ``__type`` hand the full schema to any caller, so they are
    refused unless ``allow`` (defaulting to ``GRAPHQL_ALLOW_INTROSPECTION or
    DEBUG``, read at validation time) is truthy. GraphiQL and codegen need
    introspection, so DEBUG keeps it on; production turns it off by default.

    ``__typename`` is always permitted — it leaks nothing and appears in real
    client queries.
    """

    class IntrospectionValidationRule(ValidationRule):
        def enter_field(self, node, *_args):
            allowed = (
                allow
                if allow is not None
                else (settings.GRAPHQL_ALLOW_INTROSPECTION or settings.DEBUG)
            )
            if allowed:
                return
            name = node.name.value
            if name in ("__schema", "__type"):
                self.report_error(
                    GraphQLError(
                        "Introspection is disabled.",
                        node,
                    )
                )

    return IntrospectionValidationRule
