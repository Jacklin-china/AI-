"""Single source of truth for the comic creative-context field domain.

Every place that whitelists, maps or derives creative-context fields must read
from this module instead of re-declaring the literals. Before this module the
same domain was spelled out in at least four places with drifting contents
(for example ``action`` was accepted in one list and rejected in another), which
made inheritance checks reject valid requests. See
``docs/architecture/memory-system-redesign.md`` (Module B / P11-P13).

Three vocabularies exist and are NOT interchangeable:

* **group key** – the coarse inheritable domain used by edit scopes and the
  inheritance whitelist (``character_context`` ...).
* **lock slot** – the public lock attribute / Bible slot used by
  :class:`CreativeContextLock` and prompt contexts (``character`` ...).
* **director leaf** – the fine-grained ``director_plan.*`` / ``cinematography.*``
  fields that a scope can also address.

Keep this module data-only and import-free from the rest of the domain so it can
be consumed by projects, coordinator and the shell without cycles.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ContextFieldGroup:
    """One coarse creative-context domain and how it maps to other vocabularies."""

    key: str
    """Coarse group key, e.g. ``character_context``."""

    lock_slot: str
    """Public lock attribute / Bible slot, e.g. ``character``."""

    director_plan_leaves: tuple[str, ...] = ()
    """``director_plan`` leaves this group can also address."""

    cinematography_leaves: tuple[str, ...] = ()
    """``cinematography`` leaves this group can also address."""

    bible: bool = False
    """Whether this group is backed by a Bible slot (only the three core ones are)."""

    @property
    def label(self) -> str:
        """Public label shown to users; the lock slot is already human-readable."""
        return self.lock_slot


CONTEXT_FIELD_GROUPS: tuple[ContextFieldGroup, ...] = (
    ContextFieldGroup(
        key="character_context",
        lock_slot="character",
        director_plan_leaves=("character_presence",),
        bible=True,
    ),
    ContextFieldGroup(
        key="world_context",
        lock_slot="world",
        director_plan_leaves=("subject_environment_relation",),
        bible=True,
    ),
    ContextFieldGroup(
        key="style_context",
        lock_slot="visual_style",
        director_plan_leaves=("style_boundary",),
        bible=True,
    ),
    ContextFieldGroup(
        key="camera_style",
        lock_slot="camera_style",
        cinematography_leaves=(
            "shot_size",
            "camera_angle",
            "camera_distance",
            "spatial_feel",
            "camera_language",
            "depth_strategy",
            "lens_or_spatial_feel",
            "material_language",
        ),
    ),
    ContextFieldGroup(
        key="color",
        lock_slot="color",
        cinematography_leaves=("color_relationship",),
    ),
    ContextFieldGroup(
        key="lighting",
        lock_slot="lighting",
        cinematography_leaves=("lighting", "light_source", "light_direction"),
    ),
    ContextFieldGroup(
        key="environment",
        lock_slot="environment",
        director_plan_leaves=("subject_environment_relation",),
    ),
)

# ``action`` is a writable edit scope but never an inheritable Bible slot: it
# expresses "the user is changing the subject motion" and carries no durable
# creative context to inherit. Keeping it explicit here documents the intent so
# the two lists no longer drift.
ACTION_SCOPE = "action"

#: Coarse group keys that may be written as an edit scope (includes ``action``).
EDIT_SCOPE_KEYS: frozenset[str] = frozenset(
    {group.key for group in CONTEXT_FIELD_GROUPS} | {ACTION_SCOPE}
)

#: Coarse group keys that may be inherited across turns (excludes ``action``).
INHERIT_KEYS: frozenset[str] = frozenset(group.key for group in CONTEXT_FIELD_GROUPS)

#: Groups backed by a Bible / lock slot, keyed by coarse group key.
BIBLE_GROUP_TO_SLOT: dict[str, str] = {
    group.key: group.lock_slot for group in CONTEXT_FIELD_GROUPS if group.bible
}

#: Reverse map, lock slot -> coarse group key.
SLOT_TO_BIBLE_GROUP: dict[str, str] = {
    slot: key for key, slot in BIBLE_GROUP_TO_SLOT.items()
}

#: Coarse group keys allowed inside ``bible_sources`` (declaration order, for
#: deterministic iteration when seeding prompts).
BIBLE_SOURCE_KEYS: tuple[str, ...] = tuple(BIBLE_GROUP_TO_SLOT)

#: Cinematography leaf names owned by each coarse group.
CINEMATOGRAPHY_LEAVES: dict[str, frozenset[str]] = {
    group.key: frozenset(group.cinematography_leaves)
    for group in CONTEXT_FIELD_GROUPS
    if group.cinematography_leaves
}

#: director_plan leaf names owned by each coarse group.
DIRECTOR_PLAN_LEAVES: dict[str, frozenset[str]] = {
    group.key: frozenset(group.director_plan_leaves)
    for group in CONTEXT_FIELD_GROUPS
    if group.director_plan_leaves
}


def lock_slots() -> frozenset[str]:
    """All lock attribute names (``character`` / ``world`` / ``visual_style`` ...)."""
    return frozenset(group.lock_slot for group in CONTEXT_FIELD_GROUPS)


__all__ = [
    "ACTION_SCOPE",
    "BIBLE_GROUP_TO_SLOT",
    "BIBLE_SOURCE_KEYS",
    "CINEMATOGRAPHY_LEAVES",
    "CONTEXT_FIELD_GROUPS",
    "ContextFieldGroup",
    "DIRECTOR_PLAN_LEAVES",
    "EDIT_SCOPE_KEYS",
    "INHERIT_KEYS",
    "SLOT_TO_BIBLE_GROUP",
    "lock_slots",
]
