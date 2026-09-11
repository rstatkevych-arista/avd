# Copyright (c) 2023-2026 Arista Networks, Inc.
# Use of this source code is governed by the Apache License 2.0
# that can be found in the LICENSE file.
from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, TypedDict, cast, Callable
import dataclasses
import functools
from pyavd._utils.get import get_v2
from .avd_indexed_list import AvdIndexedList
from .avd_list import AvdList
from .avd_model import AvdModel
from .type_vars import T_AvdModel
from .avd_profile_ref import AvdProfileRef
from collections import namedtuple
if TYPE_CHECKING:
    from collections.abc import Generator, Mapping, Sequence



def to_root_model(data: ProfileData, root_model: type[AvdModel], target: str) -> T_AvdModel:
    partial_model = _dict_from_path(data.raw_data, target)
    return root_model._from_dict(partial_model)


ProfileSpec = namedtuple("ProfileSpec", ("catalog", "target", "field_path"))

class ProfileSelector(TypedDict):
    """Profile selector metadata from an ``AvdProfileRef`` field."""

    catalog: str
    target: str


class ProfileData(AvdModel):
    """Profile catalog item with a required profile name."""
    _fields = {
        "profile": {"type": str},
        "parent_profile": {"type": str},
    }
    profile: str
    parent_profile: str | None = None
    raw_data: dict
    _allow_other_keys = True

    @classmethod
    def _from_dict(cls: type[T_AvdModel], data: Mapping) -> T_AvdModel:
        raw_data = dict(data) # shallow copy
        raw_data.pop("profile", None)
        raw_data.pop("parent_profile", None)

        model = super()._from_dict(data)
        model.raw_data = raw_data
        return model


class ProfileList(AvdIndexedList[str, ProfileData]):
    _item_type: ClassVar[type[AvdModel]] = ProfileData
    _primary_key: ClassVar[str] = "profile"


@dataclasses.dataclass
class ProfileGraphNode:
    data: AvdModel | None = None
    profile: ProfileData | None = None
    parent: ProfileGraphNode | None = None
    children: list[ProfileGraphNode] = dataclasses.field(default_factory=list)

    @property
    def is_root_node(self):
        return self.id == None
    @property
    def id(self) -> str | None:
        return None if not self.profile else self.profile.profile 


class ProfileGraph:
    def __init__(self) -> None:
        self.nodes: dict[str | None, ProfileGraphNode] = {}
        self._get_profile_cached: Callable[[str], AvdModel] = \
            functools.cache(lambda profile_id: self._get_profile(profile_id))

    @classmethod
    def _from_profile_list(cls, catalog_list: ProfileList, target_model: type[AvdModel], target: str):
        graph = ProfileGraph()
        graph.nodes[None] = ProfileGraphNode(None)
        for profile_id, profile_data in catalog_list.items():
            node = graph.nodes.setdefault(profile_id, ProfileGraphNode())
            node.profile = profile_data
            node.data = to_root_model(profile_data, target_model, target)
            if profile_data.parent_profile not in graph.nodes:
                graph.nodes[profile_data.parent_profile] = ProfileGraphNode()
            pnode, cnode = graph.nodes[profile_data.parent_profile], graph.nodes[profile_id]
            pnode.children.append(cnode)
            cnode.parent = pnode

        graph._check_cycles()
        return graph

    def _check_cycles(self) -> None:
        def _check_node(node: ProfileGraphNode, path: list[str]) -> None:
            if node.id in path:
                cycle_path = path[path.index(node.id) :] + [cast(str, node.id)]
                msg = "Cycle detected: " + " -> ".join(cycle_path)
                raise ValueError(msg)

            if node.id is not None:
                path = [*path, node.id]

            for child in node.children:
                _check_node(child, path)

        for node in self.nodes.values():
            _check_node(node, [])

    def _get_profile(self, profile_id: str) -> AvdModel:
        node = self.nodes.get(profile_id)
        if node is None:
            raise KeyError(f"Profile '{profile_id}' is missing")
        if node.parent and node.parent.id:
            return node.data._deepmerge(self.get_profile(node.parent.id))
        return node.data

    def get_profile(self, profile_id):
        return self._get_profile_cached(profile_id)


class AvdProfileResolver:
    """
    Resolve ``AvdProfileRef`` fields against reusable profile catalogs.

    Profile reference fields are represented as ``AvdProfileRef`` in generated
    ``_fields`` metadata. The same metadata also carries the profile catalog path
    and target path:

    .. code-block:: python

        _fields = {
            "interface_profile": {
                "type": AvdProfileRef,
                "catalog": "interface_profiles",
                "target": "interface",
            },
        }

    ``EosDesignsRootModel`` owns the resolver. Before normal model loading, the
    resolver walks the schema tree and loads every referenced catalog from the
    root input data. Each catalog item must be a mapping with a unique
    ``profile`` key. After normal model loading, the resolver walks the instance
    tree and combines each selected profile model into the model instance that
    owns the corresponding reference field.

    Example:

    .. code-block:: yaml

        interface_profiles:
          - profile: uplink
            description: Uplink interface
            shutdown: false

        interface_profile: uplink
        interface:
          description: Host-facing override

    With a generated model field defined as:

    .. code-block:: python

        _fields = {
            "interface_profile": {
                "type": AvdProfileRef,
                "catalog": "interface_profiles",
                "target": "interface",
            },
            "interface": {"type": Interface},
        }

    If ``interface_profile`` is set to ``uplink``, loading the root model applies
    the ``uplink`` profile to the ``interface`` model. Values already set on the
    instance take precedence over values from the profile when the profile model
    is combined into the instance.
    """
    def __init__(self, raw_data: Mapping, target_model: type[AvdModel]) -> None:
        self.raw_data = raw_data
        self.target_model = target_model
        self._get_cached_profile_graphs: Callable[[ProfileSpec], ProfileGraph] = \
            functools.cache(lambda profile_spec: self._resolve_profiles(profile_spec))

    def _resolve_profiles(self, profile_spec: ProfileSpec) -> ProfileGraph:
        catalog_list = get_v2(self.raw_data, profile_spec.catalog)
        if catalog_list is None:
            raise KeyError(f"Profile catalog '{profile_spec.catalog}' does not exist")
        catalog_list = ProfileList._from_list(catalog_list)
        profile_graph = ProfileGraph._from_profile_list(catalog_list, self.target_model, profile_spec.target)
        return profile_graph

    def _get_profile(self, profile_spec: ProfileSpec, profile_name: AvdProfileRef) -> AvdModel:

        profiles = self._get_cached_profile_graphs(profile_spec)
        return profiles._get_profile_cached(profile_name)

    def _apply_profiles(self, instance: AvdModel) -> AvdModel:
        """Apply selected profile models for all ``AvdProfileRef`` values below ``instance``."""
        root_instance = instance

        def _apply_matching_profiles(instance: AvdModel, prefix: str = "") -> None:
            for field_name, field_spec in instance._fields.items():
                field_type = field_spec["type"]
                field_value = instance._get(field_name)
                if field_type is AvdProfileRef:

                    if field_value is None:
                        continue
                    profile_selector = cast("ProfileSelector", field_spec)
                    field_spec = ProfileSpec(profile_selector["catalog"], profile_selector["target"], prefix + "." + field_name)
                    
                    profile = self._get_profile(field_spec, field_value)

                    root_instance._deepmerge(profile)
                elif isinstance(field_value, (AvdList, AvdIndexedList)):
                    for next_instance in field_value:
                        if isinstance(next_instance, AvdModel):
                            _apply_matching_profiles(next_instance, prefix + "." + field_name)
                elif isinstance(field_value, AvdModel):
                    _apply_matching_profiles(field_value, prefix + "." + field_name)

        _apply_matching_profiles(instance)
        return instance


def _dict_from_path(data: dict, path: str) -> dict:
    """Return ``data`` nested below ``path``."""
    if path == ".":
        return data

    root_dict = target_dict = {}
    path_ls = path.split(".")
    for p in path_ls:
        target_dict = target_dict.setdefault(p, {})
    target_dict.update(data)
    return root_dict
