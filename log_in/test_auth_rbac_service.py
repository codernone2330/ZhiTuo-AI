"""运行：python -m unittest log_in/test_auth_rbac_service.py"""
import tempfile
import unittest
from pathlib import Path

from auth_rbac_service import AuthError, AuthRbacService


class AuthRbacServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.service = AuthRbacService(Path(self.temp.name) / "auth.db", "test-secret-must-have-at-least-thirty-two-characters")
        self.tenant = self.service.create_tenant("智拓", "admin", "Admin-password-2026", "胡一骏")
        self.admin = self.service.login(self.tenant["tenantId"], "admin", "Admin-password-2026")["accessToken"]

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_login_returns_signed_token_and_roles(self) -> None:
        result = self.service.login(self.tenant["tenantId"], "admin", "Admin-password-2026")
        self.assertEqual(result["tokenType"], "Bearer")
        self.assertIn("tenant_admin", result["user"]["roles"])
        self.assertEqual(self.service.authenticate(result["accessToken"])["tenantId"], self.tenant["tenantId"])

    def test_organization_tree_and_user_are_tenant_scoped(self) -> None:
        root = self.service.add_organization(self.admin, "销售部", "SALES")
        branch = self.service.add_organization(self.admin, "华东区", "EAST", root["id"])
        user = self.service.create_user(self.admin, "sales.east", "Sales-password-2026", "李华", "sales", branch["id"])
        self.assertEqual(user["role"], "sales")
        tree = self.service.organization_tree(self.admin)
        self.assertEqual(tree[0]["children"][0]["code"], "EAST")

    def test_login_locks_after_five_invalid_passwords(self) -> None:
        for _ in range(5):
            with self.assertRaises(AuthError) as failure:
                self.service.login(self.tenant["tenantId"], "admin", "not-the-password")
            self.assertEqual(failure.exception.code, "LOGIN_FAILED")
        with self.assertRaises(AuthError):
            self.service.login(self.tenant["tenantId"], "admin", "Admin-password-2026")

    def test_disabled_user_token_is_rejected(self) -> None:
        user = self.service.create_user(self.admin, "auditor", "Auditor-password-2026", "审计员", "auditor")
        token = self.service.login(self.tenant["tenantId"], "auditor", "Auditor-password-2026")["accessToken"]
        self.service.set_user_status(self.admin, user["id"], "disabled")
        with self.assertRaises(AuthError) as failure:
            self.service.authenticate(token)
        self.assertEqual(failure.exception.code, "ACCOUNT_UNAVAILABLE")

    def test_custom_role_can_be_assigned_without_cross_tenant_leakage(self) -> None:
        role = self.service.create_custom_role(self.admin, "ops_viewer", "运营查看", ["company:read"])
        user = self.service.create_user(self.admin, "ops", "Ops-password-2026", "运营", role["code"])
        self.service.assign_role(self.admin, user["id"], "auditor")
        token = self.service.login(self.tenant["tenantId"], "ops", "Ops-password-2026")["accessToken"]
        self.assertIn("auditor", self.service.authenticate(token)["roles"])


if __name__ == "__main__":
    unittest.main()
