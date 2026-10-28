"""life stage reference groups

Revision ID: 25fa5079b17a
Revises: 67c0348e111d
Create Date: 2026-10-28 06:44:45.709536

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "25fa5079b17a"
down_revision: str | Sequence[str] | None = "67c0348e111d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Life stages from the US DRI tables (healthy-eating-backend#34). The two earlier groups used the
# values for ages 31 to 50.
GROUPS = (
    "women_19_30",
    "women_31_50",
    "women_51_70",
    "women_71_plus",
    "men_19_30",
    "men_31_50",
    "men_51_70",
    "men_71_plus",
    "pregnancy_19_30",
    "pregnancy_31_50",
    "breastfeeding_19_30",
    "breastfeeding_31_50",
)
CONSTRAINT = op.f("ck_users_reference_group")


def _check(groups: Sequence[str]) -> str:
    return "reference_group IN (" + ", ".join(f"'{group}'" for group in groups) + ")"


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_constraint(CONSTRAINT, "users", type_="check")
    op.alter_column(
        "users",
        "reference_group",
        type_=sa.String(length=24),
        existing_type=sa.String(length=16),
        existing_nullable=True,
    )
    op.execute(
        "UPDATE users SET reference_group = CASE reference_group "
        "WHEN 'adult_women' THEN 'women_31_50' WHEN 'adult_men' THEN 'men_31_50' END "
        "WHERE reference_group IS NOT NULL"
    )
    op.create_check_constraint(CONSTRAINT, "users", _check(GROUPS))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(CONSTRAINT, "users", type_="check")
    op.execute(
        "UPDATE users SET reference_group = CASE WHEN reference_group LIKE 'men_%' "
        "THEN 'adult_men' ELSE 'adult_women' END WHERE reference_group IS NOT NULL"
    )
    op.alter_column(
        "users",
        "reference_group",
        type_=sa.String(length=16),
        existing_type=sa.String(length=24),
        existing_nullable=True,
    )
    op.create_check_constraint(CONSTRAINT, "users", _check(("adult_women", "adult_men")))
