import inspect
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, List, Optional

from neomodel.exceptions import MultipleNodesReturned
from neomodel.match_q import Q, QBase
from neomodel.properties import AliasProperty, ArrayProperty
from neomodel.sync_.core import StructuredNode, db
from neomodel.sync_.relationship import StructuredRel
from neomodel.util import INCOMING, OUTGOING


def _rel_helper(
    lhs,
    rhs,
    ident=None,
    relation_type=None,
    direction=None,
    relation_properties=None,
    **kwargs,  # NOSONAR
):
    """
    Generate a relationship matching string, with specified parameters.
    Examples:
    relation_direction = OUTGOING: (lhs)-[relation_ident:relation_type]->(rhs)
    relation_direction = INCOMING: (lhs)<-[relation_ident:relation_type]-(rhs)
    relation_direction = EITHER: (lhs)-[relation_ident:relation_type]-(rhs)

    :param lhs: The left hand statement.
    :type lhs: str
    :param rhs: The right hand statement.
    :type rhs: str
    :param ident: A specific identity to name the relationship, or None.
    :type ident: str
    :param relation_type: None for all direct rels, * for all of any length, or a name of an explicit rel.
    :type relation_type: str
    :param direction: None or EITHER for all OUTGOING,INCOMING,EITHER. Otherwise OUTGOING or INCOMING.
    :param relation_properties: dictionary of relationship properties to match
    :returns: string
    """
    rel_props = ""

    if relation_properties:
        rel_props_str = ", ".join(
            (f"{key}: {value}" for key, value in relation_properties.items())
        )
        rel_props = f" {{{rel_props_str}}}"

    rel_def = ""
    # relation_type is unspecified
    if relation_type is None:
        rel_def = ""
    # all("*" wildcard) relation_type
    elif relation_type == "*":
        rel_def = "[*]"
    else:
        # explicit relation_type
        rel_def = f"[{ident if ident else ''}:`{relation_type}`{rel_props}]"

    stmt = ""
    if direction == OUTGOING:
        stmt = f"-{rel_def}->"
    elif direction == INCOMING:
        stmt = f"<-{rel_def}-"
    else:
        stmt = f"-{rel_def}-"

    # Make sure not to add parenthesis when they are already present
    if lhs[-1] != ")":
        lhs = f"({lhs})"
    if rhs[-1] != ")":
        rhs = f"({rhs})"

    return f"{lhs}{stmt}{rhs}"


def _rel_merge_helper(
    lhs,
    rhs,
    ident="neomodelident",
    relation_type=None,
    direction=None,
    relation_properties=None,
    **kwargs,  # NOSONAR
):
    """
    Generate a relationship merging string, with specified parameters.
    Examples:
    relation_direction = OUTGOING: (lhs)-[relation_ident:relation_type]->(rhs)
    relation_direction = INCOMING: (lhs)<-[relation_ident:relation_type]-(rhs)
    relation_direction = EITHER: (lhs)-[relation_ident:relation_type]-(rhs)

    :param lhs: The left hand statement.
    :type lhs: str
    :param rhs: The right hand statement.
    :type rhs: str
    :param ident: A specific identity to name the relationship, or None.
    :type ident: str
    :param relation_type: None for all direct rels, * for all of any length, or a name of an explicit rel.
    :type relation_type: str
    :param direction: None or EITHER for all OUTGOING,INCOMING,EITHER. Otherwise OUTGOING or INCOMING.
    :param relation_properties: dictionary of relationship properties to merge
    :returns: string
    """

    if direction == OUTGOING:
        stmt = "-{0}->"
    elif direction == INCOMING:
        stmt = "<-{0}-"
    else:
        stmt = "-{0}-"

    rel_props = ""
    rel_none_props = ""

    if relation_properties:
        rel_props_str = ", ".join(
            (
                f"{key}: {value}"
                for key, value in relation_properties.items()
                if value is not None
            )
        )
        rel_props = f" {{{rel_props_str}}}"
        if None in relation_properties.values():
            rel_prop_val_str = ", ".join(
                (
                    f"{ident}.{key}=${key!s}"
                    for key, value in relation_properties.items()
                    if value is None
                )
            )
            rel_none_props = (
                f" ON CREATE SET {rel_prop_val_str} ON MATCH SET {rel_prop_val_str}"
            )
    # relation_type is unspecified
    if relation_type is None:
        stmt = stmt.format("")
    # all("*" wildcard) relation_type
    elif relation_type == "*":
        stmt = stmt.format("[*]")
    else:
        # explicit relation_type
        stmt = stmt.format(f"[{ident}:`{relation_type}`{rel_props}]")

    return f"({lhs}){stmt}({rhs}){rel_none_props}"


# special operators
_SPECIAL_OPERATOR_IN = "IN"
_SPECIAL_OPERATOR_ARRAY_IN = "any(x IN {ident}.{prop} WHERE x IN {val})"
_SPECIAL_OPERATOR_INSENSITIVE = "(?i)"
_SPECIAL_OPERATOR_ISNULL = "IS NULL"
_SPECIAL_OPERATOR_ISNOTNULL = "IS NOT NULL"
_SPECIAL_OPERATOR_REGEX = "=~"

_UNARY_OPERATORS = (_SPECIAL_OPERATOR_ISNULL, _SPECIAL_OPERATOR_ISNOTNULL)

_REGEX_INSESITIVE = _SPECIAL_OPERATOR_INSENSITIVE + "{}"
_REGEX_CONTAINS = ".*{}.*"
_REGEX_STARTSWITH = "{}.*"
_REGEX_ENDSWITH = ".*{}"

# regex operations that require escaping
_STRING_REGEX_OPERATOR_TABLE = {
    "iexact": _REGEX_INSESITIVE,
    "contains": _REGEX_CONTAINS,
    "icontains": _SPECIAL_OPERATOR_INSENSITIVE + _REGEX_CONTAINS,
    "startswith": _REGEX_STARTSWITH,
    "istartswith": _SPECIAL_OPERATOR_INSENSITIVE + _REGEX_STARTSWITH,
    "endswith": _REGEX_ENDSWITH,
    "iendswith": _SPECIAL_OPERATOR_INSENSITIVE + _REGEX_ENDSWITH,
}
# regex operations that do not require escaping
_REGEX_OPERATOR_TABLE = {
    "iregex": _REGEX_INSESITIVE,
}
# list all regex operations, these will require formatting of the value
_REGEX_OPERATOR_TABLE.update(_STRING_REGEX_OPERATOR_TABLE)

# list all supported operators
OPERATOR_TABLE = {
    "lt": "<",
    "gt": ">",
    "lte": "<=",
    "gte": ">=",
    "ne": "<>",
    "in": _SPECIAL_OPERATOR_IN,
    "isnull": _SPECIAL_OPERATOR_ISNULL,
    "regex": _SPECIAL_OPERATOR_REGEX,
    "exact": "=",
}
# add all regex operators
OPERATOR_TABLE.update(_REGEX_OPERATOR_TABLE)


def install_traversals(cls, node_set):
    """
    For a StructuredNode class install Traversal objects for each
    relationship definition on a NodeSet instance
    """
    rels = cls.defined_properties(rels=True, aliases=False, properties=False)

    for key in rels.keys():
        if hasattr(node_set, key):
            raise ValueError(f"Cannot install traversal '{key}' exists on NodeSet")

        rel = getattr(cls, key)
        rel.lookup_node_class()

        traversal = Traversal(source=node_set, name=key, definition=rel.definition)
        setattr(node_set, key, traversal)


def process_filter_args(cls, kwargs):
    """
    loop through properties in filter parameters check they match class definition
    deflate them and convert into something easy to generate cypher from
    """

    output = {}

    for key, value in kwargs.items():
        if "__" in key:
            prop, operator = key.rsplit("__")
            operator = OPERATOR_TABLE[operator]
        else:
            prop = key
            operator = "="

        if prop not in cls.defined_properties(rels=False):
            raise ValueError(
                f"No such property {prop} on {cls.__name__}. Note that Neo4j internals like id or element_id are not allowed for use in this operation."
            )

        property_obj = getattr(cls, prop)
        if isinstance(property_obj, AliasProperty):
            prop = property_obj.aliased_to()
            deflated_value = getattr(cls, prop).deflate(value)
        else:
            operator, deflated_value = transform_operator_to_filter(
                operator=operator,
                filter_key=key,
                filter_value=value,
                property_obj=property_obj,
            )

        # map property to correct property name in the database
        db_property = cls.defined_properties(rels=False)[prop].get_db_property_name(
            prop
        )

        output[db_property] = (operator, deflated_value)

    return output


def transform_in_operator_to_filter(operator, filter_key, filter_value, property_obj):
    """
    Transform in operator to a cypher filter
    Args:
        operator (str): operator to transform
        filter_key (str): filter key
        filter_value (str): filter value
        property_obj (object): property object
    Returns:
        tuple: operator, deflated_value
    """
    if not isinstance(filter_value, tuple) and not isinstance(filter_value, list):
        raise ValueError(
            f"Value must be a tuple or list for IN operation {filter_key}={filter_value}"
        )
    if isinstance(property_obj, ArrayProperty):
        deflated_value = property_obj.deflate(filter_value)
        operator = _SPECIAL_OPERATOR_ARRAY_IN
    else:
        deflated_value = [property_obj.deflate(v) for v in filter_value]

    return operator, deflated_value


def transform_null_operator_to_filter(filter_key, filter_value):
    """
    Transform null operator to a cypher filter
    Args:
        filter_key (str): filter key
        filter_value (str): filter value
    Returns:
        tuple: operator, deflated_value
    """
    if not isinstance(filter_value, bool):
        raise ValueError(f"Value must be a bool for isnull operation on {filter_key}")
    operator = "IS NULL" if filter_value else "IS NOT NULL"
    deflated_value = None
    return operator, deflated_value


def transform_regex_operator_to_filter(
    operator, filter_key, filter_value, property_obj
):
    """
    Transform regex operator to a cypher filter
    Args:
        operator (str): operator to transform
        filter_key (str): filter key
        filter_value (str): filter value
        property_obj (object): property object
    Returns:
        tuple: operator, deflated_value
    """

    deflated_value = property_obj.deflate(filter_value)
    if not isinstance(deflated_value, str):
        raise ValueError(f"Must be a string value for {filter_key}")
    if operator in _STRING_REGEX_OPERATOR_TABLE.values():
        deflated_value = re.escape(deflated_value)
    deflated_value = operator.format(deflated_value)
    operator = _SPECIAL_OPERATOR_REGEX
    return operator, deflated_value


def transform_operator_to_filter(operator, filter_key, filter_value, property_obj):
    if operator == _SPECIAL_OPERATOR_IN:
        operator, deflated_value = transform_in_operator_to_filter(
            operator=operator,
            filter_key=filter_key,
            filter_value=filter_value,
            property_obj=property_obj,
        )
    elif operator == _SPECIAL_OPERATOR_ISNULL:
        operator, deflated_value = transform_null_operator_to_filter(
            filter_key=filter_key, filter_value=filter_value
        )
    elif operator in _REGEX_OPERATOR_TABLE.values():
        operator, deflated_value = transform_regex_operator_to_filter(
            operator=operator,
            filter_key=filter_key,
            filter_value=filter_value,
            property_obj=property_obj,
        )
    else:
        deflated_value = property_obj.deflate(filter_value)

    return operator, deflated_value


def process_has_args(cls, kwargs):
    """
    loop through has parameters check they correspond to class rels defined
    """
    rel_definitions = cls.defined_properties(properties=False, rels=True, aliases=False)

    match, dont_match = {}, {}

    for key, value in kwargs.items():
        if key not in rel_definitions:
            raise ValueError(f"No such relation {key} defined on a {cls.__name__}")

        rhs_ident = key

        rel_definitions[key].lookup_node_class()

        if value is True:
            match[rhs_ident] = rel_definitions[key].definition
        elif value is False:
            dont_match[rhs_ident] = rel_definitions[key].definition
        elif isinstance(value, NodeSet):
            raise NotImplementedError("Not implemented yet")
        else:
            raise ValueError("Expecting True / False / NodeSet got: " + repr(value))

    return match, dont_match


class QueryAST:
    match: Optional[list]
    optional_match: Optional[list]
    where: Optional[list]
    with_clause: Optional[str]
    return_clause: Optional[str]
    order_by: Optional[str]
    skip: Optional[int]
    limit: Optional[int]
    result_class: Optional[type]
    lookup: Optional[str]
    additional_return: Optional[list]
    is_count: Optional[bool]

    def __init__(
        self,
        match: Optional[list] = None,
        optional_match: Optional[list] = None,
        where: Optional[list] = None,
        with_clause: Optional[str] = None,
        return_clause: Optional[str] = None,
        order_by: Optional[str] = None,
        skip: Optional[int] = None,
        limit: Optional[int] = None,
        result_class: Optional[type] = None,
        lookup: Optional[str] = None,
        additional_return: Optional[list] = None,
        is_count: Optional[bool] = False,
    ):
        self.match = match if match else []
        self.optional_match = optional_match if optional_match else []
        self.where = where if where else []
        self.with_clause = with_clause
        self.return_clause = return_clause
        self.order_by = order_by
        self.skip = skip
        self.limit = limit
        self.result_class = result_class
        self.lookup = lookup
        self.additional_return = additional_return if additional_return else []
        self.is_count = is_count
        self.subgraph: dict = {}


class QueryBuilder:
    def __init__(
        self, node_set, with_subgraph: bool = False, subquery_context: bool = False
    ):
        self.node_set = node_set
        self._ast = QueryAST()
        self._query_params = {}
        self._place_holder_registry = {}
        self._ident_count = 0
        self._node_counters = defaultdict(int)
        self._with_subgraph: bool = with_subgraph
        self._subquery_context: bool = subquery_context

    def build_ast(self):
        if hasattr(self.node_set, "relations_to_fetch"):
            for relation in self.node_set.relations_to_fetch:
                self.build_traversal_from_path(relation, self.node_set.source)

        self.build_source(self.node_set)

        if hasattr(self.node_set, "skip"):
            self._ast.skip = self.node_set.skip
        if hasattr(self.node_set, "limit"):
            self._ast.limit = self.node_set.limit

        return self

    def build_source(self, source) -> str:
        if isinstance(source, Traversal):
            return self.build_traversal(source)
        if isinstance(source, NodeSet):
            if inspect.isclass(source.source) and issubclass(
                source.source, StructuredNode
            ):
                ident = self.build_label(source.source.__label__.lower(), source.source)
            else:
                ident = self.build_source(source.source)

            self.build_additional_match(ident, source)

            if hasattr(source, "order_by_elements"):
                self.build_order_by(ident, source)

            if source.filters or source.q_filters:
                self.build_where_stmt(
                    ident,
                    source.filters,
                    source.q_filters,
                    source_class=source.source_class,
                )

            return ident
        if isinstance(source, StructuredNode):
            return self.build_node(source)
        raise ValueError("Unknown source type " + repr(source))

    def create_ident(self):
        self._ident_count += 1
        return "r" + str(self._ident_count)

    def build_order_by(self, ident, source):
        if "?" in source.order_by_elements:
            self._ast.with_clause = f"{ident}, rand() as r"
            self._ast.order_by = "r"
        else:
            self._ast.order_by = [f"{ident}.{p}" for p in source.order_by_elements]

    def build_traversal(self, traversal):
        """
        traverse a relationship from a node to a set of nodes
        """
        # build source
        rhs_label = ":" + traversal.target_class.__label__

        # build source
        rel_ident = self.create_ident()
        lhs_ident = self.build_source(traversal.source)
        traversal_ident = f"{traversal.name}_{rel_ident}"
        rhs_ident = traversal_ident + rhs_label
        self._ast.return_clause = traversal_ident
        self._ast.result_class = traversal.target_class

        stmt = _rel_helper(
            lhs=lhs_ident,
            rhs=rhs_ident,
            ident=rel_ident,
            **traversal.definition,
        )
        self._ast.match.append(stmt)

        if traversal.filters:
            self.build_where_stmt(rel_ident, traversal.filters)

        return traversal_ident

    def _additional_return(self, name: str):
        if name not in self._ast.additional_return and name != self._ast.return_clause:
            self._ast.additional_return.append(name)

    def build_traversal_from_path(self, relation: dict, source_class) -> str:
        path: str = relation["path"]
        stmt: str = ""
        source_class_iterator = source_class
        parts = path.split("__")
        if self._with_subgraph:
            subgraph = self._ast.subgraph
        for index, part in enumerate(parts):
            relationship = getattr(source_class_iterator, part)
            # build source
            if "node_class" not in relationship.definition:
                relationship.lookup_node_class()
            rhs_label = relationship.definition["node_class"].__label__
            rel_reference = f'{relationship.definition["node_class"]}_{part}'
            self._node_counters[rel_reference] += 1
            if index + 1 == len(parts) and "alias" in relation:
                # If an alias is defined, use it to store the last hop in the path
                rhs_name = relation["alias"]
            else:
                rhs_name = (
                    f"{rhs_label.lower()}_{part}_{self._node_counters[rel_reference]}"
                )
            rhs_ident = f"{rhs_name}:{rhs_label}"
            if relation["include_in_return"]:
                self._additional_return(rhs_name)
            if not stmt:
                lhs_label = source_class_iterator.__label__
                lhs_name = lhs_label.lower()
                lhs_ident = f"{lhs_name}:{lhs_label}"
                if not index:
                    # This is the first one, we make sure that 'return'
                    # contains the primary node so _contains() works
                    # as usual
                    self._ast.return_clause = lhs_name
                    if self._subquery_context:
                        # Don't include label in identifier if we are in a subquery
                        lhs_ident = lhs_name
                elif relation["include_in_return"]:
                    self._additional_return(lhs_name)
            else:
                lhs_ident = stmt

            rel_ident = self.create_ident()
            if self._with_subgraph and part not in self._ast.subgraph:
                subgraph[part] = {
                    "target": relationship.definition["node_class"],
                    "children": {},
                    "variable_name": rhs_name,
                    "rel_variable_name": rel_ident,
                }
            if relation["include_in_return"]:
                self._additional_return(rel_ident)
            stmt = _rel_helper(
                lhs=lhs_ident,
                rhs=rhs_ident,
                ident=rel_ident,
                direction=relationship.definition["direction"],
                relation_type=relationship.definition["relation_type"],
            )
            source_class_iterator = relationship.definition["node_class"]
            if self._with_subgraph:
                subgraph = subgraph[part]["children"]

        if relation.get("optional"):
            self._ast.optional_match.append(stmt)
        else:
            self._ast.match.append(stmt)
        return rhs_name

    def build_node(self, node):
        ident = node.__class__.__name__.lower()
        place_holder = self._register_place_holder(ident)

        # Hack to emulate START to lookup a node by id
        _node_lookup = f"MATCH ({ident}) WHERE {db.get_id_method()}({ident})=${place_holder} WITH {ident}"
        self._ast.lookup = _node_lookup

        self._query_params[place_holder] = db.parse_element_id(node.element_id)

        self._ast.return_clause = ident
        self._ast.result_class = node.__class__
        return ident

    def build_label(self, ident, cls) -> str:
        """
        match nodes by a label
        """
        ident_w_label = ident + ":" + cls.__label__

        if not self._ast.return_clause and (
            not self._ast.additional_return or ident not in self._ast.additional_return
        ):
            self._ast.match.append(f"({ident_w_label})")
            self._ast.return_clause = ident
            self._ast.result_class = cls
        return ident

    def build_additional_match(self, ident, node_set):
        """
        handle additional matches supplied by 'has()' calls
        """
        source_ident = ident

        for _, value in node_set.must_match.items():
            if isinstance(value, dict):
                label = ":" + value["node_class"].__label__
                stmt = _rel_helper(lhs=source_ident, rhs=label, ident="", **value)
                self._ast.where.append(stmt)
            else:
                raise ValueError("Expecting dict got: " + repr(value))

        for _, val in node_set.dont_match.items():
            if isinstance(val, dict):
                label = ":" + val["node_class"].__label__
                stmt = _rel_helper(lhs=source_ident, rhs=label, ident="", **val)
                self._ast.where.append("NOT " + stmt)
            else:
                raise ValueError("Expecting dict got: " + repr(val))

    def _register_place_holder(self, key):
        if key in self._place_holder_registry:
            self._place_holder_registry[key] += 1
        else:
            self._place_holder_registry[key] = 1
        return key + "_" + str(self._place_holder_registry[key])

    def _parse_q_filters(self, ident, q, source_class):
        target = []
        for child in q.children:
            if isinstance(child, QBase):
                q_childs = self._parse_q_filters(ident, child, source_class)
                if child.connector == Q.OR:
                    q_childs = "(" + q_childs + ")"
                target.append(q_childs)
            else:
                kwargs = {child[0]: child[1]}
                filters = process_filter_args(source_class, kwargs)
                for prop, op_and_val in filters.items():
                    operator, val = op_and_val
                    if operator in _UNARY_OPERATORS:
                        # unary operators do not have a parameter
                        statement = f"{ident}.{prop} {operator}"
                    else:
                        place_holder = self._register_place_holder(ident + "_" + prop)
                        if operator == _SPECIAL_OPERATOR_ARRAY_IN:
                            statement = operator.format(
                                ident=ident,
                                prop=prop,
                                val=f"${place_holder}",
                            )
                        else:
                            statement = f"{ident}.{prop} {operator} ${place_holder}"
                        self._query_params[place_holder] = val
                    target.append(statement)
        ret = f" {q.connector} ".join(target)
        if q.negated:
            ret = f"NOT ({ret})"
        return ret

    def build_where_stmt(self, ident, filters, q_filters=None, source_class=None):
        """
        construct a where statement from some filters
        """
        if q_filters is not None:
            stmts = self._parse_q_filters(ident, q_filters, source_class)
            if stmts:
                self._ast.where.append(stmts)
        else:
            stmts = []
            for row in filters:
                negate = False

                # pre-process NOT cases as they are nested dicts
                if "__NOT__" in row and len(row) == 1:
                    negate = True
                    row = row["__NOT__"]

                for prop, operator_and_val in row.items():
                    operator, val = operator_and_val
                    if operator in _UNARY_OPERATORS:
                        # unary operators do not have a parameter
                        statement = (
                            f"{'NOT' if negate else ''} {ident}.{prop} {operator}"
                        )
                    else:
                        place_holder = self._register_place_holder(ident + "_" + prop)
                        statement = f"{'NOT' if negate else ''} {ident}.{prop} {operator} ${place_holder}"
                        self._query_params[place_holder] = val
                    stmts.append(statement)

            self._ast.where.append(" AND ".join(stmts))

    def build_query(self) -> str:
        query: str = ""

        if self._ast.lookup:
            query += self._ast.lookup

        # Instead of using only one MATCH statement for every relation
        # to follow, we use one MATCH per relation (to avoid cartesian
        # product issues...).
        # There might be optimizations to be done, using projections,
        # or pusing patterns instead of a chain of OPTIONAL MATCH.
        if self._ast.match:
            query += " MATCH "
            query += " MATCH ".join(i for i in self._ast.match)

        if self._ast.optional_match:
            query += " OPTIONAL MATCH "
            query += " OPTIONAL MATCH ".join(i for i in self._ast.optional_match)

        if self._ast.where:
            query += " WHERE "
            query += " AND ".join(self._ast.where)

        if self._ast.with_clause:
            query += " WITH "
            query += self._ast.with_clause

        returned_items: list[str] = []
        if hasattr(self.node_set, "_subqueries"):
            for subquery, return_set in self.node_set._subqueries:
                outer_primary_var: str = self._ast.return_clause
                query += f" CALL {{ WITH {outer_primary_var} {subquery} }} "
                returned_items += return_set

        query += " RETURN "
        if self._ast.return_clause and not self._subquery_context:
            returned_items.append(self._ast.return_clause)
        if self._ast.additional_return:
            returned_items += self._ast.additional_return
        if hasattr(self.node_set, "_extra_results"):
            for varname, vardef in self.node_set._extra_results.items():
                if varname in returned_items:
                    # We're about to override an existing variable, delete it first to
                    # avoid duplicate error
                    returned_items.remove(varname)
                returned_items.append(f"{str(vardef)} AS {varname}")

        query += ", ".join(returned_items)

        if self._ast.order_by:
            query += " ORDER BY "
            query += ", ".join(self._ast.order_by)

        # If we return a count with pagination, pagination has to happen before RETURN
        # It will then be included in the WITH clause already
        if self._ast.skip and not self._ast.is_count:
            query += f" SKIP {self._ast.skip}"

        if self._ast.limit and not self._ast.is_count:
            query += f" LIMIT {self._ast.limit}"

        return query

    def _count(self):
        self._ast.is_count = True
        # If we return a count with pagination, pagination has to happen before RETURN
        # Like : WITH my_var SKIP 10 LIMIT 10 RETURN count(my_var)
        self._ast.with_clause = f"{self._ast.return_clause}"
        if self._ast.skip:
            self._ast.with_clause += f" SKIP {self._ast.skip}"

        if self._ast.limit:
            self._ast.with_clause += f" LIMIT {self._ast.limit}"

        self._ast.return_clause = f"count({self._ast.return_clause})"
        # drop order_by, results in an invalid query
        self._ast.order_by = None
        # drop additional_return to avoid unexpected result
        self._ast.additional_return = None
        query = self.build_query()
        results, _ = db.cypher_query(query, self._query_params)
        return int(results[0][0])

    def _contains(self, node_element_id):
        # inject id = into ast
        if not self._ast.return_clause:
            self._ast.return_clause = self._ast.additional_return[0]
        ident = self._ast.return_clause
        place_holder = self._register_place_holder(ident + "_contains")
        self._ast.where.append(f"{db.get_id_method()}({ident}) = ${place_holder}")
        self._query_params[place_holder] = node_element_id
        return self._count() >= 1

    def _execute(self, lazy: bool = False, dict_output: bool = False):
        if lazy:
            # inject id() into return or return_set
            if self._ast.return_clause:
                self._ast.return_clause = (
                    f"{db.get_id_method()}({self._ast.return_clause})"
                )
            else:
                self._ast.additional_return = [
                    f"{db.get_id_method()}({item})"
                    for item in self._ast.additional_return
                ]
        query = self.build_query()
        results, prop_names = db.cypher_query(
            query, self._query_params, resolve_objects=True
        )
        if dict_output:
            for item in results:
                yield dict(zip(prop_names, item))
            return
        # The following is not as elegant as it could be but had to be copied from the
        # version prior to cypher_query with the resolve_objects capability.
        # It seems that certain calls are only supposed to be focusing to the first
        # result item returned (?)
        if results and len(results[0]) == 1:
            for n in results:
                yield n[0]
        else:
            for result in results:
                yield result


class BaseSet:
    """
    Base class for all node sets.

    Contains common python magic methods, __len__, __contains__ etc
    """

    query_cls = QueryBuilder

    def all(self, lazy=False):
        """
        Return all nodes belonging to the set
        :param lazy: False by default, specify True to get nodes with id only without the parameters.
        :return: list of nodes
        :rtype: list
        """
        ast = self.query_cls(self).build_ast()
        results = [
            node for node in ast._execute(lazy)
        ]  # Collect all nodes asynchronously
        return results

    def __iter__(self):
        ast = self.query_cls(self).build_ast()
        for item in ast._execute():
            yield item

    def __len__(self):
        ast = self.query_cls(self).build_ast()
        return ast._count()

    def __bool__(self):
        """
        Override for __bool__ dunder method.
        :return: True if the set contains any nodes, False otherwise
        :rtype: bool
        """
        ast = self.query_cls(self).build_ast()
        _count = ast._count()
        return _count > 0

    def __nonzero__(self):
        """
        Override for __bool__ dunder method.
        :return: True if the set contains any node, False otherwise
        :rtype: bool
        """
        return self.__bool__()

    def __contains__(self, obj):
        if isinstance(obj, StructuredNode):
            if hasattr(obj, "element_id") and obj.element_id is not None:
                ast = self.query_cls(self).build_ast()
                obj_element_id = db.parse_element_id(obj.element_id)
                return ast._contains(obj_element_id)
            raise ValueError("Unsaved node: " + repr(obj))

        raise ValueError("Expecting StructuredNode instance")

    def __getitem__(self, key):
        if isinstance(key, slice):
            if key.stop and key.start:
                self.limit = key.stop - key.start
                self.skip = key.start
            elif key.stop:
                self.limit = key.stop
            elif key.start:
                self.skip = key.start

            return self

        if isinstance(key, int):
            self.skip = key
            self.limit = 1

            ast = self.query_cls(self).build_ast()
            _first_item = [node for node in ast._execute()][0]
            return _first_item

        return None


@dataclass
class Optional:
    """Simple relation qualifier."""

    relation: str


@dataclass
class AggregatingFunction:
    """Base aggregating function class."""

    input_name: str


@dataclass
class Collect(AggregatingFunction):
    """collect() function."""

    distinct: bool = False

    def __str__(self):
        if self.distinct:
            return f"collect(DISTINCT {self.input_name})"
        return f"collect({self.input_name})"


class NodeSet(BaseSet):
    """
    A class representing as set of nodes matching common query parameters
    """

    def __init__(self, source):
        self.source = source  # could be a Traverse object or a node class
        if isinstance(source, Traversal):
            self.source_class = source.target_class
        elif inspect.isclass(source) and issubclass(source, StructuredNode):
            self.source_class = source
        elif isinstance(source, StructuredNode):
            self.source_class = source.__class__
        else:
            raise ValueError("Bad source for nodeset " + repr(source))

        # setup Traversal objects using relationship definitions
        install_traversals(self.source_class, self)

        self.filters = []
        self.q_filters = Q()

        # used by has()
        self.must_match = {}
        self.dont_match = {}

        self.relations_to_fetch: list = []
        self._extra_results: dict[str] = {}
        self._subqueries: list[tuple(str, list[str])] = []

    def __await__(self):
        return self.all().__await__()

    def _get(self, limit=None, lazy=False, **kwargs):
        self.filter(**kwargs)
        if limit:
            self.limit = limit
        ast = self.query_cls(self).build_ast()
        results = [node for node in ast._execute(lazy)]
        return results

    def get(self, lazy=False, **kwargs):
        """
        Retrieve one node from the set matching supplied parameters
        :param lazy: False by default, specify True to get nodes with id only without the parameters.
        :param kwargs: same syntax as `filter()`
        :return: node
        """
        result = self._get(limit=2, lazy=lazy, **kwargs)
        if len(result) > 1:
            raise MultipleNodesReturned(repr(kwargs))
        if not result:
            raise self.source_class.DoesNotExist(repr(kwargs))
        return result[0]

    def get_or_none(self, **kwargs):
        """
        Retrieve a node from the set matching supplied parameters or return none

        :param kwargs: same syntax as `filter()`
        :return: node or none
        """
        try:
            return self.get(**kwargs)
        except self.source_class.DoesNotExist:
            return None

    def first(self, **kwargs):
        """
        Retrieve the first node from the set matching supplied parameters

        :param kwargs: same syntax as `filter()`
        :return: node
        """
        result = self._get(limit=1, **kwargs)
        if result:
            return result[0]
        else:
            raise self.source_class.DoesNotExist(repr(kwargs))

    def first_or_none(self, **kwargs):
        """
        Retrieve the first node from the set matching supplied parameters or return none

        :param kwargs: same syntax as `filter()`
        :return: node or none
        """
        try:
            return self.first(**kwargs)
        except self.source_class.DoesNotExist:
            pass
        return None

    def filter(self, *args, **kwargs):
        """
        Apply filters to the existing nodes in the set.

        :param args: a Q object

            e.g `.filter(Q(salary__lt=10000) | Q(salary__gt=20000))`.

        :param kwargs: filter parameters

            Filters mimic Django's syntax with the double '__' to separate field and operators.

            e.g `.filter(salary__gt=20000)` results in `salary > 20000`.

            The following operators are available:

             * 'lt': less than
             * 'gt': greater than
             * 'lte': less than or equal to
             * 'gte': greater than or equal to
             * 'ne': not equal to
             * 'in': matches one of list (or tuple)
             * 'isnull': is null
             * 'regex': matches supplied regex (neo4j regex format)
             * 'exact': exactly match string (just '=')
             * 'iexact': case insensitive match string
             * 'contains': contains string
             * 'icontains': case insensitive contains
             * 'startswith': string starts with
             * 'istartswith': case insensitive string starts with
             * 'endswith': string ends with
             * 'iendswith': case insensitive string ends with

        :return: self
        """
        if args or kwargs:
            self.q_filters = Q(self.q_filters & Q(*args, **kwargs))
        return self

    def exclude(self, *args, **kwargs):
        """
        Exclude nodes from the NodeSet via filters.

        :param kwargs: filter parameters see syntax for the filter method
        :return: self
        """
        if args or kwargs:
            self.q_filters = Q(self.q_filters & ~Q(*args, **kwargs))
        return self

    def has(self, **kwargs):
        must_match, dont_match = process_has_args(self.source_class, kwargs)
        self.must_match.update(must_match)
        self.dont_match.update(dont_match)
        return self

    def order_by(self, *props):
        """
        Order by properties. Prepend with minus to do descending. Pass None to
        remove ordering.
        """
        should_remove = len(props) == 1 and props[0] is None
        if not hasattr(self, "order_by_elements") or should_remove:
            self.order_by_elements = []
            if should_remove:
                return self
        if "?" in props:
            self.order_by_elements.append("?")
        else:
            for prop in props:
                prop = prop.strip()
                if prop.startswith("-"):
                    prop = prop[1:]
                    desc = True
                else:
                    desc = False

                if prop not in self.source_class.defined_properties(rels=False):
                    raise ValueError(
                        f"No such property {prop} on {self.source_class.__name__}. Note that Neo4j internals like id or element_id are not allowed for use in this operation."
                    )

                property_obj = getattr(self.source_class, prop)
                if isinstance(property_obj, AliasProperty):
                    prop = property_obj.aliased_to()

                self.order_by_elements.append(prop + (" DESC" if desc else ""))

        return self

    def _register_relation_to_fetch(
        self, relation_def: Any, alias: str = None, include_in_return: bool = True
    ):
        if isinstance(relation_def, Optional):
            item = {"path": relation_def.relation, "optional": True}
        else:
            item = {"path": relation_def}
        item["include_in_return"] = include_in_return
        if alias:
            item["alias"] = alias
        return item

    def fetch_relations(self, *relation_names):
        """Specify a set of relations to traverse and return."""
        relations = []
        for relation_name in relation_names:
            relations.append(self._register_relation_to_fetch(relation_name))
        self.relations_to_fetch = relations
        return self

    def traverse_relations(self, *relation_names, **aliased_relation_names):
        """Specify a set of relations to traverse only."""
        relations = []
        for relation_name in relation_names:
            relations.append(
                self._register_relation_to_fetch(relation_name, include_in_return=False)
            )
        for alias, relation_def in aliased_relation_names.items():
            relations.append(
                self._register_relation_to_fetch(
                    relation_def, alias, include_in_return=False
                )
            )

        self.relations_to_fetch = relations
        return self

    def annotate(self, *vars, **aliased_vars):
        """Annotate node set results with extra variables."""

        def register_extra_var(vardef, varname: str = None):
            if isinstance(vardef, AggregatingFunction):
                self._extra_results[varname if varname else vardef.input_name] = vardef
            else:
                raise NotImplementedError

        for vardef in vars:
            register_extra_var(vardef)
        for varname, vardef in aliased_vars.items():
            register_extra_var(vardef, varname)

        return self

    def _to_subgraph(self, root_node, other_nodes, subgraph):
        """Recursive method to build root_node's relation graph from subgraph."""
        root_node._relations = {}
        for name, relation_def in subgraph.items():
            for var_name, node in other_nodes.items():
                if (
                    var_name
                    not in [
                        relation_def["variable_name"],
                        relation_def["rel_variable_name"],
                    ]
                    or node is None
                ):
                    continue
                if isinstance(node, list):
                    if len(node) > 0 and isinstance(node[0], StructuredRel):
                        name += "_relationship"
                    root_node._relations[name] = []
                    for item in node:
                        root_node._relations[name].append(
                            self._to_subgraph(
                                item, other_nodes, relation_def["children"]
                            )
                        )
                else:
                    if isinstance(node, StructuredRel):
                        name += "_relationship"
                    root_node._relations[name] = self._to_subgraph(
                        node, other_nodes, relation_def["children"]
                    )

        return root_node

    def resolve_subgraph(self) -> list:
        """
        Convert every result contained in this node set to a subgraph.

        By default, we receive results from neomodel as a list of
        nodes without the hierarchy. This method tries to rebuild this
        hierarchy without overriding anything in the node, that's why
        we use a dedicated property to store node's relations.

        """
        if not self.relations_to_fetch:
            raise RuntimeError(
                "Nothing to resolve. Make sure to include relations in the result using fetch_relations() or filter()."
            )
        if not self.relations_to_fetch[0]["include_in_return"]:
            raise NotImplementedError(
                "You cannot use traverse_relations() with resolve_subgraph(), use fetch_relations() instead."
            )
        results: list = []
        qbuilder = self.query_cls(self, with_subgraph=True)
        qbuilder.build_ast()
        all_nodes = qbuilder._execute(dict_output=True)
        other_nodes = {}
        root_node = None
        for row in all_nodes:
            for name, node in row.items():
                if node.__class__ is self.source and "_" not in name:
                    root_node = node
                else:
                    if isinstance(node, list) and isinstance(node[0], list):
                        other_nodes[name] = node[0]
                    else:
                        other_nodes[name] = node
            results.append(
                self._to_subgraph(root_node, other_nodes, qbuilder._ast.subgraph)
            )
        return results

    def subquery(self, nodeset: "NodeSet", return_set: List[str]) -> "NodeSet":
        """Add a subquery to this node set.

        A subquery is a regular cypher query but executed within the context of a CALL
        statement. Such query will generally fetch additional variables which must be
        declared inside return_set variable in order to be included in the final RETURN
        statement.
        """
        qbuilder = nodeset.query_cls(nodeset, subquery_context=True).build_ast()
        for var in return_set:
            if (
                var != qbuilder._ast.return_clause
                and var not in qbuilder._ast.additional_return
                and var not in nodeset._extra_results
            ):
                raise RuntimeError(f"Variable '{var}' is not returned by subquery.")
        self._subqueries.append((qbuilder.build_query(), return_set))
        return self


class Traversal(BaseSet):
    """
    Models a traversal from a node to another.

    :param source: Starting of the traversal.
    :type source: A :class:`~neomodel.core.StructuredNode` subclass, an
                  instance of such, a :class:`~neomodel.match.NodeSet` instance
                  or a :class:`~neomodel.match.Traversal` instance.
    :param name: A name for the traversal.
    :type name: :class:`str`
    :param definition: A relationship definition that most certainly deserves
                       a documentation here.
    :type definition: :class:`dict`
    """

    def __await__(self):
        return self.all().__await__()

    def __init__(self, source, name, definition):
        """
        Create a traversal

        """
        self.source = source

        if isinstance(source, Traversal):
            self.source_class = source.target_class
        elif inspect.isclass(source) and issubclass(source, StructuredNode):
            self.source_class = source
        elif isinstance(source, StructuredNode):
            self.source_class = source.__class__
        elif isinstance(source, NodeSet):
            self.source_class = source.source_class
        else:
            raise TypeError(f"Bad source for traversal: {type(source)}")

        invalid_keys = set(definition) - {
            "direction",
            "model",
            "node_class",
            "relation_type",
        }
        if invalid_keys:
            raise ValueError(f"Prohibited keys in Traversal definition: {invalid_keys}")

        self.definition = definition
        self.target_class = definition["node_class"]
        self.name = name
        self.filters = []

    def match(self, **kwargs):
        """
        Traverse relationships with properties matching the given parameters.

            e.g: `.match(price__lt=10)`

        :param kwargs: see `NodeSet.filter()` for syntax
        :return: self
        """
        if kwargs:
            if self.definition.get("model") is None:
                raise ValueError(
                    "match() with filter only available on relationships with a model"
                )
            output = process_filter_args(self.definition["model"], kwargs)
            if output:
                self.filters.append(output)
        return self
