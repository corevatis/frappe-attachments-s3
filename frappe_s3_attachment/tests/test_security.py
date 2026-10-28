"""Sicherheitstests ohne Site/DB (Mocks): Download-Header und Rechteprüfung.

    env/bin/python -m unittest frappe_s3_attachment.tests.test_security -v
"""

import unittest
from unittest import mock

import frappe

from frappe_s3_attachment import controller as c


class Doc(frappe._dict):
    def has_permission(self, ptype="read"):
        return self.readable


class TestDownloadHeaders(unittest.TestCase):
    def _resp(self, name):
        return c.build_download_response(name, b"<svg onload=alert(1)>")

    def test_html_and_svg_are_attachments(self):
        for name in ("x.html", "x.htm", "x.svg", "x.xhtml", "x.js", "noext"):
            r = self._resp(name)
            self.assertTrue(r.headers["Content-Disposition"].startswith("attachment"), name)
            self.assertEqual(r.mimetype, "application/octet-stream", name)
            self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
            self.assertIn("sandbox", r.headers["Content-Security-Policy"])

    def test_safe_images_inline_with_server_side_type(self):
        for name, t in (("a.JPG", "image/jpeg"), ("b.png", "image/png"), ("c.webp", "image/webp"), ("d.pdf", "application/pdf")):
            r = self._resp(name)
            self.assertTrue(r.headers["Content-Disposition"].startswith("inline"), name)
            self.assertEqual(r.mimetype, t)
            self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
            self.assertIn("sandbox", r.headers["Content-Security-Policy"])


class TestAccessCheck(unittest.TestCase):
    def _run(self, docs):
        rows = [frappe._dict(name=d.name, is_private=d.is_private, file_name="f.png") for d in docs]
        by_name = {d.name: d for d in docs}
        with mock.patch.object(c, "_file_rows_for_key", return_value=rows), mock.patch.object(
            frappe, "get_doc", side_effect=lambda dt, n: by_name[n]
        ):
            return c.check_read_access("k")

    def test_private_foreign_user_denied(self):
        with self.assertRaises(frappe.PermissionError):
            self._run([Doc(name="F1", is_private=1, readable=False)])

    def test_private_own_user_allowed(self):
        self.assertEqual(self._run([Doc(name="F1", is_private=1, readable=True)]).name, "F1")

    def test_public_allowed_without_permission(self):
        self.assertEqual(self._run([Doc(name="F1", is_private=0, readable=False)]).name, "F1")

    def test_forged_public_row_does_not_open_private_object(self):
        # Angreifer legt eigenen öffentlichen Datensatz mit fremdem Schlüssel an
        with self.assertRaises(frappe.PermissionError):
            self._run([Doc(name="EVIL", is_private=0, readable=True), Doc(name="F1", is_private=1, readable=False)])

    def test_unknown_key_denied(self):
        with mock.patch.object(c, "_file_rows_for_key", return_value=[]):
            with self.assertRaises(frappe.PermissionError):
                c.check_read_access("unknown")

    def test_generate_file_denies_before_touching_s3(self):
        with mock.patch.object(c, "_file_rows_for_key", return_value=[]), mock.patch.object(c, "S3Operations") as s3:
            with self.assertRaises(frappe.PermissionError):
                c.generate_file(key="k")
            s3.assert_not_called()


if __name__ == "__main__":
    unittest.main()
