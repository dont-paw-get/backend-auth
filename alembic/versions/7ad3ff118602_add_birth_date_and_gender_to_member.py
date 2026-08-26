"""add birth_date and gender to member

Revision ID: 7ad3ff118602
Revises: 1a2373795882
Create Date: 2026-08-26 14:57:31.868734

CLIAR-120: member에 birth_date(생년월일), gender(성별)를 추가한다.

- 기존 member row가 이미 dev DB에 존재하므로, 두 컬럼은 모두
  nullable로 추가한다. 이 migration은 기존 row에 어떤 값도
  backfill하지 않는다(가짜 생년월일, MALE/FEMALE 기본값, UNKNOWN 등
  임의 값을 넣지 않는다). 신규 회원 bootstrap에서의 필수 여부는
  API schema 레벨(app/schemas/user.py)에서만 강제하며, DB 제약으로는
  강제하지 않는다.
- gender는 기존 member_status와 동일한 방식(PostgreSQL native ENUM)을
  사용한다. 현재 허용 값은 MALE/FEMALE 두 가지뿐이다.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7ad3ff118602'
down_revision: Union[str, None] = '1a2373795882'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


member_gender_enum = sa.Enum("MALE", "FEMALE", name="member_gender")


def upgrade() -> None:
    # member 테이블이 이 컬럼을 갖기 전에 먼저 ENUM 타입을 생성한다
    # (member_status를 만들 때의 58da6f61f1f6 패턴과 동일).
    member_gender_enum.create(op.get_bind(), checkfirst=False)

    op.add_column("member", sa.Column("birth_date", sa.Date(), nullable=True))
    op.add_column(
        "member", sa.Column("gender", member_gender_enum, nullable=True)
    )


def downgrade() -> None:
    op.drop_column("member", "gender")
    op.drop_column("member", "birth_date")

    member_gender_enum.drop(op.get_bind(), checkfirst=False)
