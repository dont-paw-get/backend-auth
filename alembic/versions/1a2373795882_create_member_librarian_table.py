"""create member_librarian table

Revision ID: 1a2373795882
Revises: cf318d66faa3
Create Date: 2026-08-24 15:20:03.678026

CLIAR-87: 회원이 보유한 사서(librarian) 인스턴스를 저장하는
member_librarian 테이블을 신규 생성한다. 현재 코드베이스에는 이
테이블이 존재하지 않으므로 신규 생성이다.

- 같은 librarian_id를 여러 인스턴스로 보유할 수 있으므로
  (member_id, librarian_id) UNIQUE는 두지 않는다.
- librarian_id는 Librarian 서비스가 소유하는 식별자이므로 FK를 걸지
  않는다.
- member_id는 member.member_id를 참조하는 FK이다.
- 회원당 대표 사서(is_representative=TRUE)는 최대 1개만 허용하는
  partial unique index를 둔다.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1a2373795882'
down_revision: Union[str, None] = 'cf318d66faa3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "member_librarian",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("member_id", sa.Uuid(), nullable=False),
        sa.Column("librarian_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "evolution_stage", sa.Integer(), nullable=False, server_default="1"
        ),
        sa.Column(
            "is_representative",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "acquired_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["member_id"],
            ["member.member_id"],
            name="fk_member_librarian_member_id",
        ),
    )

    op.create_index(
        "ix_member_librarian_member",
        "member_librarian",
        ["member_id"],
        unique=False,
    )

    op.create_index(
        "uk_member_librarian_representative",
        "member_librarian",
        ["member_id"],
        unique=True,
        postgresql_where=sa.text("is_representative = TRUE AND deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uk_member_librarian_representative", table_name="member_librarian"
    )
    op.drop_index("ix_member_librarian_member", table_name="member_librarian")
    op.drop_table("member_librarian")
