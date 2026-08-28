"""add PENDING to member_status enum

Revision ID: 9b41c7d2e5f3
Revises: 7ad3ff118602
Create Date: 2026-08-28 00:00:00.000000

BE 주도 인증 전환(PLAN.md §7.1): 회원가입이 Cognito SignUp 시점에
member row를 먼저 만들고 이메일 인증 완료 시 ACTIVE로 전이하는 2단계
구조가 되므로, member_status ENUM에 PENDING을 추가한다.

기존 row는 전혀 건드리지 않는다(ACTIVE/WITHDRAWN 그대로 유지).
PENDING은 이 migration 이후 새로 가입하는 회원에만 사용된다.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9b41c7d2e5f3'
down_revision: Union[str, None] = '7ad3ff118602'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # PostgreSQL에서 ALTER TYPE ... ADD VALUE는 트랜잭션 블록 안에서
    # 실행할 수 없다(PG 12+는 실행 자체는 허용하지만, 추가한 값을 같은
    # 트랜잭션에서 사용할 수 없다). alembic은 기본적으로 migration을
    # 트랜잭션으로 감싸므로, 여기서 명시적으로 COMMIT해서 트랜잭션을
    # 끊는다. 이 migration은 이 문장 하나만 수행하므로 COMMIT으로 인해
    # 부분 적용이 남는 위험은 없다.
    op.execute("COMMIT")

    # IF NOT EXISTS: dev DB에 수동으로 값이 추가되어 있거나 migration이
    # 재실행되는 경우에도 실패하지 않도록 한다(PG 9.6+).
    op.execute("ALTER TYPE member_status ADD VALUE IF NOT EXISTS 'PENDING'")


def downgrade() -> None:
    # PostgreSQL은 ENUM 값 삭제(ALTER TYPE ... DROP VALUE)를 지원하지
    # 않는다. 따라서 새 타입을 만들고 컬럼을 캐스팅한 뒤 구 타입을
    # 버리는 방식으로 되돌린다.
    connection = op.get_bind()

    # PENDING인 member가 남아 있으면 캐스팅이 불가능하다. 임의로
    # ACTIVE/WITHDRAWN으로 바꾸면 "이메일 인증을 하지 않은 계정"이
    # 정상 계정으로 승격되어 버리므로, 조용히 처리하지 않고 명시적으로
    # 실패시킨다. 운영자가 해당 row를 먼저 정리해야 한다.
    pending_count = connection.execute(
        sa.text("SELECT COUNT(*) FROM member WHERE status = 'PENDING'")
    ).scalar_one()

    if pending_count:
        raise RuntimeError(
            f"Cannot downgrade: {pending_count} member row(s) still have "
            "status='PENDING'. Resolve them before removing the enum value."
        )

    op.execute("ALTER TYPE member_status RENAME TO member_status_old")
    op.execute("CREATE TYPE member_status AS ENUM ('ACTIVE', 'WITHDRAWN')")
    op.execute(
        "ALTER TABLE member ALTER COLUMN status TYPE member_status "
        "USING status::text::member_status"
    )
    op.execute("DROP TYPE member_status_old")
