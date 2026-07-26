import unittest
from unittest.mock import patch

from services.outlook_registration.producer import OutlookProduceResult, save_outlook_account


class OutlookPersistTest(unittest.TestCase):
    def test_save_upserts(self):
        # Use in-memory path if db available; otherwise skip on import errors
        try:
            from core.db import init_db, engine, OutlookAccountModel
            from sqlmodel import Session, select

            init_db()
        except Exception as exc:  # pragma: no cover
            self.skipTest(f"db unavailable: {exc}")

        email = "producer_test_user@outlook.com"
        row = save_outlook_account(
            email=email,
            password="Passw0rd!",
            client_id="cid",
            refresh_token="",
            enabled=True,
        )
        self.assertEqual(row.email, email)
        row2 = save_outlook_account(
            email=email,
            password="Passw0rd2!",
            client_id="cid2",
            refresh_token="",
            enabled=True,
        )
        self.assertEqual(row2.password, "Passw0rd2!")
        with Session(engine) as s:
            found = s.exec(select(OutlookAccountModel).where(OutlookAccountModel.email == email)).all()
            self.assertEqual(len(found), 1)


if __name__ == "__main__":
    unittest.main()
