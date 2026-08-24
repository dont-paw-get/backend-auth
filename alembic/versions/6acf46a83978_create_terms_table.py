"""create terms table

Revision ID: 6acf46a83978
Revises: 58da6f61f1f6
Create Date: 2026-08-24 15:19:57.677803

CLIAR-87: 약관 원문을 저장하는 terms 테이블을 신규 생성한다.

- 별도의 version 컬럼은 두지 않는다. 약관 내용이 바뀌면 새 행을
  INSERT하여 과거 내용을 보존하는 방식이다.
- code별로 현재 유효한(만료/삭제되지 않은) 행은 최대 1개만 존재해야
  하므로 partial unique index(uk_terms_active_code)를 둔다.
- expired_at은 NULL이거나 effective_at보다 뒤여야 한다는 CHECK을 둔다.
- 이번 migration에서는 실제 약관 문구(content)가 아직 확정되지 않았으므로
  seed 데이터를 INSERT하지 않는다.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6acf46a83978'
down_revision: Union[str, None] = '58da6f61f1f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "terms",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("code", sa.String(length=50), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "is_required", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expired_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint(
            "expired_at IS NULL OR expired_at > effective_at",
            name="ck_terms_effective_period",
        ),
    )

    op.create_index(
        "uk_terms_active_code",
        "terms",
        ["code"],
        unique=True,
        postgresql_where=sa.text("expired_at IS NULL AND deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uk_terms_active_code", table_name="terms")
    op.drop_table("terms")
