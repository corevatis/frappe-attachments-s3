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
        with mock.patch.object(c, "_file_rows_for_key", return_value=[]), mock.patch.object(
            c, "_session_user", return_value="user@example.com"
        ), mock.patch.object(c, "S3Operations") as s3:
            with self.assertRaises(frappe.PermissionError):
                c.generate_file(key="k")
            s3.assert_not_called()



class FakeBody:
    """botocore StreamingBody: read() darf nicht benutzt werden (Vollpufferung)."""

    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False
        self.chunk_sizes = []

    def read(self, *args):
        raise AssertionError("Body.read() puffert das ganze Objekt")

    def iter_chunks(self, chunk_size=1024):
        self.chunk_sizes.append(chunk_size)
        yield from self.chunks

    def close(self):
        self.closed = True


class FakeS3:
    def __init__(self, body, length, stream=1):
        self.body = body
        self.length = length
        self.s3_settings_doc = frappe._dict(stream_downloads=stream)

    def read_file_from_s3(self, key):
        return {"Body": self.body, "ContentLength": self.length}


class TestStreaming(unittest.TestCase):
    """Review Epic 0, F-2: S3-Objekt streamen statt im Worker puffern."""

    def _download(self, name="foto.png", chunks=(b"a" * 10, b"b" * 5), conf=None, user="user@example.com"):
        body = FakeBody(list(chunks))
        length = sum(len(x) for x in chunks)
        file_doc = frappe._dict(name="F1", file_name=name, is_private=1)
        with mock.patch.object(c, "_session_user", return_value=user), mock.patch.object(
            c, "_conf", return_value=frappe._dict(conf or {})
        ), mock.patch.object(c, "check_read_access", return_value=file_doc), mock.patch.object(
            c, "S3Operations", return_value=FakeS3(body, length)
        ):
            response = c.generate_file(key="site/2026/x.png")
        return response, body, length

    def test_streamed_with_content_length(self):
        response, body, length = self._download()
        self.assertTrue(response.is_streamed)
        self.assertTrue(response.direct_passthrough)
        self.assertEqual(response.headers["Content-Length"], str(length))
        self.assertFalse(body.closed)  # noch nichts gelesen
        self.assertEqual(b"".join(response.response), b"a" * 10 + b"b" * 5)
        self.assertTrue(body.closed)
        self.assertEqual(body.chunk_sizes, [c.STREAM_CHUNK_SIZE])

    def test_body_closed_when_client_aborts(self):
        response, body, _ = self._download()
        iterator = iter(response.response)
        next(iterator)
        iterator.close()  # werkzeug/gunicorn schließen den Iterator bei Abbruch
        self.assertTrue(body.closed)

    def test_large_files_never_inline(self):
        response, _, _ = self._download(conf={"s3_inline_max_bytes": 10})
        self.assertTrue(response.headers["Content-Disposition"].startswith("attachment"))
        self.assertEqual(response.mimetype, "application/octet-stream")
        response, _, _ = self._download()
        self.assertTrue(response.headers["Content-Disposition"].startswith("inline"))

    def test_boto_timeouts_set(self):
        settings = frappe._dict(region_name="us-east-1", endpoint_url=None, aws_key=None, aws_secret=None,
                                bucket_name="b", folder_name="f")
        with mock.patch.object(c, "_settings", return_value=settings), mock.patch.object(
            c, "_conf", return_value=frappe._dict()
        ), mock.patch.object(c.boto3, "client") as client:
            c.S3Operations()
        config = client.call_args.kwargs["config"]
        self.assertEqual((config.connect_timeout, config.read_timeout), (5, 30))


class TestGuestAccess(unittest.TestCase):
    """Review Epic 0, F-2: Gäste nur mit s3_allow_guest_downloads, dann nur öffentliche Dateien."""

    def _call(self, conf, rows):
        docs = {r.name: Doc(name=r.name, is_private=r.is_private, readable=False) for r in rows}
        s3 = FakeS3(FakeBody([b"x"]), 1)
        with mock.patch.object(c, "_session_user", return_value="Guest"), mock.patch.object(
            c, "_conf", return_value=frappe._dict(conf)
        ), mock.patch.object(c, "_count_guest_download") as counter, mock.patch.object(
            c, "_file_rows_for_key", return_value=rows
        ), mock.patch.object(frappe, "get_doc", side_effect=lambda dt, n: docs[n]), mock.patch.object(
            c, "S3Operations", return_value=s3
        ) as s3_cls:
            try:
                return c.generate_file(key="k"), counter, s3_cls
            except frappe.PermissionError:
                return None, counter, s3_cls

    def test_guest_denied_by_default_even_for_public_file(self):
        public = [frappe._dict(name="F1", is_private=0, file_name="logo.png")]
        response, counter, s3_cls = self._call({}, public)
        self.assertIsNone(response)
        s3_cls.assert_not_called()
        counter.assert_not_called()

    def test_guest_allowed_public_file_with_switch(self):
        public = [frappe._dict(name="F1", is_private=0, file_name="logo.png")]
        response, counter, _ = self._call({"s3_allow_guest_downloads": 1}, public)
        self.assertIsNotNone(response)
        self.assertEqual(b"".join(response.response), b"x")
        counter.assert_called_once()  # Rate-Limit je IP

    def test_guest_never_gets_private_file(self):
        private = [frappe._dict(name="F1", is_private=1, file_name="foto.png")]
        response, _, s3_cls = self._call({"s3_allow_guest_downloads": 1}, private)
        self.assertIsNone(response)
        s3_cls.assert_not_called()


if __name__ == "__main__":
    unittest.main()
