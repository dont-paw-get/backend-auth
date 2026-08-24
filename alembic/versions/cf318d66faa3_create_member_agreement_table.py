"""create member_agreement table

Revision ID: cf318d66faa3
Revises: 6acf46a83978
Create Date: 2026-08-24 15:19:58.832066

CLIAR-87: 약관 동의/철회 이력을 저장하는 member_agreement 테이블과
member_agreement_action ENUM('AGREE', 'WITHDRAW')을 생성한다.

- 현재 동의 상태를 UPDATE로 덮어쓰지 않고, 매 동의/철회마다 새 행을
  INSERT하는 이력형 테이블이다.
- member.member_id, terms.id에 대한 FK를 가지므로 이 두 테이블이
  먼저 존재해야 한다(member는 53a0fb49dca7에서, terms는 6acf46a83978
  에서 이미 생성됨).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'cf318d66faa3'
down_revision: Union[str, None] = '6acf46a83978'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


member_agreement_action_enum = sa.Enum(
    "AGREE", "WITHDRAW", name="member_agreement_action"
)


def upgrade() -> None:
    # NOTE: op.create_table가 아래 Enum 컬럼을 보고 CREATE TYPE을 자동으로
    # 실행하므로, 여기서 별도로 member_agreement_action_enum.create()를
    # 호출하지 않는다(중복 생성으로 인한 "type already exists" 오류 방지).
    op.create_table(
        "member_agreement",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("member_id", sa.Uuid(), nullable=False),
        sa.Column("terms_id", sa.BigInteger(), nullable=False),
        sa.Column("action", member_agreement_action_enum, nullable=False),
        sa.Column(
            "occurred_at",
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
            name="fk_member_agreement_member_id",
        ),
        sa.ForeignKeyConstraint(
            ["terms_id"],
            ["terms.id"],
            name="fk_member_agreement_terms_id",
        ),
    )

    op.create_index(
        "ix_member_agreement_member_terms",
        "member_agreement",
        ["member_id", "terms_id", sa.text("occurred_at DESC")],
        unique=False,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_member_agreement_member_terms", table_name="member_agreement")
    op.drop_table("member_agreement")
    member_agreement_action_enum.drop(op.get_bind(), checkfirst=False)
