"""A marker for "not given", where None is a real value (for example "clear this setting")."""

from enum import Enum


class Unset(Enum):
    UNSET = "unset"


UNSET = Unset.UNSET
