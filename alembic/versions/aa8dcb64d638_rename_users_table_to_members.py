"""rename users table to member

Revision ID: aa8dcb64d638
Revises: 53a0fb49dca7
Create Date: 2026-08-18 00:00:00.000000

CLIAR-65: 기존에 생성된 `users` 테이블명을 `member`로 변경한다.
컬럼 구조(PK인 user_id 포함)와 제약조건은 그대로 유지하며
테이블명만 변경한다. 기존 CLIAR-30 migration(53a0fb49dca7)은
이미 공유된 히스토리이므로 수정하지 않고, 새 migration으로
rename만 수행한다.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "aa8dcb64d638"
down_revision: Union[str, None] = "53a0fb49dca7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.rename_table("users", "member")


def downgrade() -> None:
    op.rename_table("member", "users")
