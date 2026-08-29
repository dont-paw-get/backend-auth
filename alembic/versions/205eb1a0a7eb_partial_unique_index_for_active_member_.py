"""replace global member.email UNIQUE with a partial unique index for active members

Revision ID: 205eb1a0a7eb
Revises: aa3c28032296
Create Date: 2026-08-29 00:00:00.000000

CLIAR-177: 탈퇴 완료된 회원이 같은 이메일로 재가입할 수 없는 문제를
해결한다.

원인: 53a0fb49dca7(원래 users 테이블 생성 migration, 이후 aa8dcb64d638
에서 member로 rename되었지만 제약조건 이름은 바뀌지 않았다)에서
member.email에 테이블 전체 대상 UNIQUE 제약(uq_users_email)을 걸었다.
회원탈퇴는 member row를 물리 삭제하지 않고 status=WITHDRAWN +
deleted_at 기록으로 이력을 보존하는 정책(app/repositories/
user_repository.py의 mark_withdrawn/mark_deleted_now,
app/services/member_service.py의 start_withdrawal/complete_withdrawal)
이므로, 탈퇴 후에도 그 이메일을 가진 row가 DB에 계속 남아 있고,
uq_users_email이 같은 이메일의 재사용을 막는다.

정책: 이메일의 논리적 uniqueness는 "현재 유효한(탈퇴 완료되지 않은)
회원"에 대해서만 적용한다.
  - ACTIVE, PENDING -> 동일 이메일 신규 가입 금지(기존과 동일)
  - WITHDRAWN + deleted_at 설정됨(탈퇴 완료) -> 동일 이메일 재가입 허용

predicate로 "deleted_at IS NULL"을 선택한 이유: 회원탈퇴는 두 단계로
나뉘어 각각 별도로 commit된다(app/services/member_service.py).
  1) status: ACTIVE -> WITHDRAWN (start_withdrawal, commit)
  2) Cognito DeleteUser 호출
  3) 성공 후 deleted_at 기록(complete_withdrawal, commit)
따라서 status=WITHDRAWN이지만 deleted_at은 아직 NULL인 상태(Cognito
DeleteUser가 실패했거나 아직 재시도 전)가 실제로 존재할 수 있다
(tests/test_users_withdraw.py 참고 — 이 상태에서는 재시도가 Cognito
호출부터 다시 이루어진다). 이 중간 상태에서는 Cognito 쪽 계정이 아직
남아있을 수 있으므로 재가입을 허용하면 안 된다. "status IN
('ACTIVE','PENDING')"만으로는 이 중간 상태를 보호하지 못하지만,
"deleted_at IS NULL"은 ACTIVE/PENDING과 이 중간 상태를 모두
포함하면서 탈퇴가 실제로 "완료"된 경우만 제외한다 — deleted_at은
Cognito 삭제가 성공적으로 끝난 뒤에만 설정되므로(그 반대 경우, 즉
ACTIVE/PENDING인데 deleted_at이 설정되는 경우는 없다) 이 predicate가
"현재 회원"을 가장 정확히 표현한다. application 쪽 중복 검사
(app/repositories/user_repository.py의 get_by_email/exists_by_email)
도 동일하게 "deleted_at IS NULL"을 사용하도록 맞췄다.

기존 migration 파일(53a0fb49dca7 등)은 수정하지 않는다. uq_users_email
을 제거하고 partial unique index(uq_member_email_active)를 새로
생성한다. 물리적으로 여러 WITHDRAWN historical row가 같은 이메일을
가질 수 있게 되지만(그 자체는 index 대상이 아니므로 허용됨),
deleted_at IS NULL인 행은 이메일당 최대 1개만 존재할 수 있다.

member_id/Cognito sub 정책은 건드리지 않는다. 기존 WITHDRAWN row를
재활성화하거나 member_id를 변경하지 않는다(재가입은 항상 새
member_id로 새 row를 INSERT한다 — app/services/signup_service.py 참고).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '205eb1a0a7eb'
down_revision: Union[str, None] = 'aa3c28032296'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("uq_users_email", "member", type_="unique")
    op.create_index(
        "uq_member_email_active",
        "member",
        ["email"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    """
    global UNIQUE(email)로 되돌린다.

    이 migration이 적용된 이후 실제로 탈퇴 완료 회원이 같은 이메일로
    재가입하면, member 테이블에 같은 email을 가진 row가 2개 이상(과거
    WITHDRAWN + 새 ACTIVE/PENDING, 혹은 여러 개의 과거 WITHDRAWN)
    존재하게 된다. 이 상태에서 global UNIQUE(email)를 그대로 다시
    만들면 제약 생성 자체가 실패한다.

    이 경우 무리하게 과거 행을 삭제하거나 email을 변조해서 downgrade를
    강행하지 않는다(요구사항: 개인정보 임의 변조 금지, 이력 보존
    우선) — 대신 중복 이메일을 명시적으로 감지해 RuntimeError로
    실패시키고, 운영자가 정책적으로 판단하게 한다(9b41c7d2e5f3의
    PENDING enum downgrade, aa3c28032296의 member_agreement 참조 보호와
    동일한 원칙).
    """
    connection = op.get_bind()

    duplicate_emails = connection.execute(
        sa.text(
            "SELECT email, COUNT(*) AS cnt FROM member GROUP BY email HAVING COUNT(*) > 1"
        )
    ).fetchall()

    if duplicate_emails:
        raise RuntimeError(
            f"Cannot downgrade: {len(duplicate_emails)} email(s) are shared by "
            "more than one member row (expected once withdrawn-member "
            "re-signup, enabled by this migration, has actually happened). "
            "Restoring a table-wide UNIQUE(email) constraint would fail. "
            "This migration does not delete or alter any member row to force "
            "the downgrade through — resolve/merge the duplicate email rows "
            "manually first."
        )

    op.drop_index("uq_member_email_active", table_name="member")
    op.create_unique_constraint("uq_users_email", "member", ["email"])
